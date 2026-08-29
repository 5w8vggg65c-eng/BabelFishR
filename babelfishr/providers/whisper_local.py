"""Local speech recognition via faster-whisper (CTranslate2).

This is the recommended path on Apple silicon: the model runs on the machine,
nothing is uploaded, and an M-series Mac with 24 GB comfortably runs the
``small`` or ``medium`` model in better than real time for short transmissions.

Radio audio is not podcast audio.  Transmissions are short, band-limited,
often clipped, and frequently in a language the operator does not speak, so
this wrapper leans on Whisper's own language detection, keeps the ASR-side VAD
off (our detector has already cut the clip), and feeds the operator's
vocabulary in as an initial prompt.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, List, Optional, Sequence

import numpy as np

from ..dsp.resample import resample
from ..models import TranscriptSegment
from .base import (EngineError, EngineUnavailable, PrivacyProfile,
                   TranscriptionEngine, TranscriptionResult)

log = logging.getLogger(__name__)

#: Whisper is trained on 16 kHz mono audio.
TARGET_RATE = 16_000

MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")


class FasterWhisperEngine(TranscriptionEngine):
    """faster-whisper wrapper. Loads the model lazily on first use."""

    id = "faster-whisper"
    name = "Whisper (local, faster-whisper)"
    detects_language = True
    privacy = PrivacyProfile()  # local

    def __init__(self, model: str = "small", device: str = "auto",
                 compute_type: str = "default", beam_size: int = 5,
                 download_root: Optional[str] = None,
                 condition_on_previous_text: bool = False,
                 local_files_only: bool = True,
                 model_path: Optional[str] = None):
        self.model_size = model
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.download_root = download_root
        self.condition_on_previous_text = condition_on_previous_text
        #: Default True: in the field a silent 1.5 GB download attempt is a
        #: failure, not a feature. Preparation mode sets this False on purpose.
        self.local_files_only = local_files_only
        #: An explicit directory containing a converted model, used in the
        #: field so loading cannot depend on a shared cache being intact.
        self.model_path = model_path
        self._model: Any = None
        self.version = f"faster-whisper/{model}"

    # -- availability ----------------------------------------------------
    def library_installed(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def model_directory(self) -> Optional[pathlib.Path]:
        """The local directory this engine would load, if one is configured."""
        if self.model_path:
            return pathlib.Path(self.model_path).expanduser()
        if self.download_root:
            return pathlib.Path(self.download_root).expanduser() / self.model_size
        return None

    def model_present(self) -> bool:
        """Whether a usable model exists locally, without loading it.

        A converted CTranslate2 model is a directory containing ``model.bin``
        and a tokenizer; checking for those is what lets Field Check say "the
        model is missing" instead of discovering it at the first transmission.
        """
        directory = self.model_directory()
        if directory is None:
            return False
        if not directory.is_dir():
            return False
        return (directory / "model.bin").exists()

    def available(self) -> bool:
        """Installed AND, when restricted to local files, actually present.

        Reporting "available" merely because the library imports is what let a
        missing model become a field failure instead of a setup failure.
        """
        if not self.library_installed():
            return False
        if self.local_files_only and self.model_directory() is not None:
            return self.model_present()
        return True

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        if not self.library_installed():
            return ("faster-whisper is not installed. Install the ASR extra:\n"
                    "    pip install 'babelfishr[asr]'")
        directory = self.model_directory()
        return (f"No local Whisper model at {directory}.\n"
                f"Prepare one with a network connection:\n"
                f"    babelfishr prepare-field --asr-model {self.model_size}")

    def warm_up(self) -> "TranscriptionResult":
        """Load the model and prove it can transcribe.

        Loading alone is not readiness: a model can load and still fail on the
        first real clip. This runs a short bundled fixture through it and
        returns the result, so Field Check can report a genuine smoke test
        rather than "the import worked".
        """
        self._load()
        from ..testing import speech_like

        return self.transcribe(speech_like(1.5, TARGET_RATE, level_dbfs=-14),
                               TARGET_RATE)

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(self.unavailable_reason()) from exc

        device = self.device
        compute_type = self.compute_type
        if device == "auto":
            # CTranslate2 has no Metal backend: on Apple silicon the CPU path
            # with int8 is the fast one, and it is what we default to.
            device = "cpu"
            if compute_type == "default":
                compute_type = "int8"
        elif compute_type == "default":
            compute_type = "float16" if device == "cuda" else "int8"

        # An explicit directory wins: in the field, loading must not depend on
        # a shared cache still being intact.
        target = str(self.model_path) if self.model_path else self.model_size
        try:
            self._model = WhisperModel(
                target, device=device, compute_type=compute_type,
                download_root=self.download_root,
                local_files_only=self.local_files_only)
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if self.local_files_only:
                hint = ("\nThis engine is restricted to local files, so it will "
                        "not download. Run 'babelfishr prepare-field' online.")
            raise EngineUnavailable(
                f"could not load Whisper model {target!r}: {exc}{hint}") from exc
        log.info("loaded faster-whisper %s on %s (%s, local_files_only=%s)",
                 target, device, compute_type, self.local_files_only)
        return self._model

    def close(self) -> None:
        self._model = None

    # -- transcription ---------------------------------------------------
    def transcribe(self, audio: np.ndarray, sample_rate: int, *,
                   language: Optional[str] = None,
                   vocabulary: Optional[Sequence[str]] = None,
                   ) -> TranscriptionResult:
        model = self._load()
        samples = self._prepare(audio, sample_rate)
        if samples.size == 0:
            return TranscriptionResult(engine=self.id, engine_version=self.version,
                                       no_speech=True)

        initial_prompt = None
        if vocabulary:
            # Whisper accepts a text prompt that biases decoding toward these
            # spellings - the practical way to get callsigns transcribed right.
            initial_prompt = ", ".join(list(vocabulary)[:100])

        try:
            segments, info = model.transcribe(
                samples,
                language=language,
                beam_size=self.beam_size,
                initial_prompt=initial_prompt,
                # Our detector has already isolated the transmission; Whisper's
                # own VAD would only re-trim (and sometimes drop) short clips.
                vad_filter=False,
                condition_on_previous_text=self.condition_on_previous_text,
                word_timestamps=False,
            )
            collected = list(segments)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"whisper transcription failed: {exc}") from exc

        parts: List[TranscriptSegment] = []
        logprobs: List[float] = []
        no_speech_probs: List[float] = []
        for seg in collected:
            text = (seg.text or "").strip()
            confidence = _logprob_to_confidence(getattr(seg, "avg_logprob", None))
            parts.append(TranscriptSegment(
                start=float(getattr(seg, "start", 0.0)),
                end=float(getattr(seg, "end", 0.0)),
                text=text, confidence=confidence))
            if getattr(seg, "avg_logprob", None) is not None:
                logprobs.append(float(seg.avg_logprob))
            if getattr(seg, "no_speech_prob", None) is not None:
                no_speech_probs.append(float(seg.no_speech_prob))

        full_text = " ".join(p.text for p in parts if p.text).strip()
        overall = _logprob_to_confidence(
            float(np.mean(logprobs)) if logprobs else None)
        no_speech = bool(no_speech_probs and float(np.mean(no_speech_probs)) > 0.6
                         and not full_text)

        return TranscriptionResult(
            text=full_text,
            language=getattr(info, "language", None) or language,
            language_confidence=_safe_float(
                getattr(info, "language_probability", None)),
            confidence=overall, segments=parts, engine=self.id,
            engine_version=self.version, no_speech=no_speech,
        )

    def _prepare(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """Resample to 16 kHz float32. The original clip on disk is untouched."""
        data = np.asarray(audio, dtype=np.float64).ravel()
        if data.size == 0:
            return np.zeros(0, dtype=np.float32)
        if sample_rate != TARGET_RATE:
            data = resample(data, sample_rate, TARGET_RATE)
        peak = float(np.max(np.abs(data)))
        if peak > 1.0:
            data = data / peak
        return data.astype(np.float32)


def _logprob_to_confidence(avg_logprob: Optional[float]) -> Optional[float]:
    """Map Whisper's average log probability onto a 0..1 confidence.

    Whisper reports mean token log-probability, typically about -0.1 for a
    confident decode and below -1.0 for a doubtful one. Exponentiating gives a
    number that behaves like a confidence for thresholding and review queues.
    """
    if avg_logprob is None:
        return None
    return round(float(np.clip(np.exp(avg_logprob), 0.0, 1.0)), 3)


def _safe_float(value) -> Optional[float]:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None
