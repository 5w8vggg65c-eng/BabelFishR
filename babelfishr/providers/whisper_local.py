"""Local speech recognition via faster-whisper (CTranslate2).

This is the recommended path on Apple silicon: the model runs on the machine,
nothing is uploaded, and an M-series Mac with 24 GB comfortably runs the
``small`` or ``medium`` model in better than real time for short transmissions.

Model layout
------------
Models are prepared into ``<AppPaths.models>/<model-name>/`` with
``faster_whisper.utils.download_model(name, output_dir=...)``, and loaded by
passing that directory to ``WhisperModel``.  This matters: passing
``download_root`` instead makes faster-whisper treat the path as a *Hugging
Face cache*, which uses a ``models--org--repo/snapshots/<sha>/`` layout - so a
presence check for ``<models>/<name>/model.bin`` would never find anything a
download had actually written, and field loading would depend on the HF cache
still existing. The explicit directory removes both problems.

Radio audio is not podcast audio.  Transmissions are short, band-limited,
often clipped, and frequently in a language the operator does not speak, so
this wrapper leans on Whisper's own language detection, keeps the ASR-side VAD
off (our detector has already cut the clip), and feeds the operator's
vocabulary in as an initial prompt.
"""

from __future__ import annotations

import logging
import enum
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

#: Assets faster-whisper's own download_model fetches. ``model.bin`` and
#: ``config.json`` are mandatory; a model also needs one tokenizer asset, which
#: is ``tokenizer.json`` on newer conversions and ``vocabulary.*`` on older
#: ones, so either satisfies the check.
REQUIRED_ASSETS = ("model.bin", "config.json")
TOKENIZER_ASSETS = ("tokenizer.json", "vocabulary.json", "vocabulary.txt")


class ModelState(str, enum.Enum):
    """What a prepared model directory actually contains."""

    MISSING = "missing"
    INCOMPLETE = "incomplete"
    """The directory exists but assets are absent - an interrupted download."""

    COMPLETE = "complete"


def model_directory_for(models_root, model_name: str) -> pathlib.Path:
    """The single authoritative location for a prepared model.

    Everything - preparation, the manifest, presence checks, Field Check, the
    engine factory and the real-model tests - resolves through this one
    function, so they cannot disagree about where a model lives.
    """
    return pathlib.Path(models_root).expanduser() / str(model_name)


def inspect_model_directory(directory) -> tuple:
    """Return ``(ModelState, missing_assets)`` for a model directory."""
    path = pathlib.Path(directory)
    if not path.is_dir():
        return (ModelState.MISSING, list(REQUIRED_ASSETS))
    missing = [name for name in REQUIRED_ASSETS if not (path / name).exists()]
    if not any((path / name).exists() for name in TOKENIZER_ASSETS):
        missing.append("tokenizer.json or vocabulary.*")
    if missing:
        # An empty or half-written directory is worse than none: it looks
        # prepared. Report it as incomplete so it can be repaired.
        return (ModelState.INCOMPLETE if any(path.iterdir()) else ModelState.MISSING,
                missing)
    return (ModelState.COMPLETE, [])


def prepare_model(model_name: str, models_root, local_files_only: bool = False
                  ) -> pathlib.Path:
    """Download (or verify) a model into its own directory. Returns the path.

    Uses faster-whisper's supported preparation entry point with an explicit
    ``output_dir``, so the result is a plain directory we own rather than an
    entry in a shared cache.
    """
    from faster_whisper.utils import download_model

    target = model_directory_for(models_root, model_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved = download_model(model_name, output_dir=str(target),
                              local_files_only=local_files_only)
    return pathlib.Path(resolved)


class FasterWhisperEngine(TranscriptionEngine):
    """faster-whisper wrapper. Loads the model lazily on first use."""

    id = "faster-whisper"
    name = "Whisper (local, faster-whisper)"
    detects_language = True
    privacy = PrivacyProfile()  # local

    def __init__(self, model: str = "small", device: str = "auto",
                 compute_type: str = "default", beam_size: int = 5,
                 models_root: Optional[str] = None,
                 condition_on_previous_text: bool = False,
                 local_files_only: bool = True,
                 model_path: Optional[str] = None):
        self.model_size = model
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.models_root = models_root
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
        if self.models_root:
            return model_directory_for(self.models_root, self.model_size)
        return None

    def model_state(self) -> tuple:
        """``(ModelState, missing_assets)`` without loading anything."""
        directory = self.model_directory()
        if directory is None:
            return (ModelState.MISSING, list(REQUIRED_ASSETS))
        return inspect_model_directory(directory)

    def model_present(self) -> bool:
        """Whether a *complete* model exists locally, without loading it.

        Completeness matters: an interrupted download leaves a directory that
        looks prepared but fails at the first transmission, which in the field
        is the worst possible moment to find out.
        """
        return self.model_state()[0] is ModelState.COMPLETE

    def prepare(self, local_files_only: bool = False) -> pathlib.Path:
        """Fetch or verify this engine's model. Returns the resolved directory."""
        if self.model_path:
            return pathlib.Path(self.model_path).expanduser()
        if not self.models_root:
            raise EngineUnavailable("no model directory is configured")
        return prepare_model(self.model_size, self.models_root,
                             local_files_only=local_files_only)

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
        state, missing = self.model_state()
        directory = self.model_directory()
        if state is ModelState.INCOMPLETE:
            return (f"The Whisper model at {directory} is incomplete - an "
                    f"interrupted download. Missing: {', '.join(missing)}.\n"
                    f"Repair it with a network connection:\n"
                    f"    babelfishr prepare-field --asr-model {self.model_size}")
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

        # Always load from the resolved directory when one exists, in both
        # Online/Setup and Field Offline: loading must never depend on a shared
        # Hugging Face cache still being intact.
        directory = self.model_directory()
        if directory is not None and self.model_present():
            target = str(directory)
        elif self.local_files_only:
            raise EngineUnavailable(self.unavailable_reason())
        else:
            # Online/Setup with nothing prepared yet: fetch into our directory
            # rather than letting faster-whisper populate a cache we do not own.
            target = str(self.prepare())
        try:
            self._model = WhisperModel(
                target, device=device, compute_type=compute_type,
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
