"""Local speech recognition via faster-whisper (CTranslate2).

This is the recommended path on Apple silicon: the model runs on the machine,
nothing is uploaded, and an M-series Mac with 24 GB comfortably runs the
``small`` or ``medium`` model in better than real time for short transmissions.

Model layout
------------
Models are prepared into ``<AppPaths.models>/<model-name>/`` with
huggingface_hub into the same plain ``output_dir`` layout
``faster_whisper.utils.download_model(name, output_dir=...)`` produces (but
with real byte progress, see prepare_model), and loaded by
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

import fnmatch
import logging
import threading
import time
import enum
import pathlib
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from ..download_progress import (CancelCheck, DownloadCancelled,  # noqa: F401
                                 DownloadProgress, ProgressCallback,
                                 ProgressTracker, progress_bar_class)
from ..dsp.resample import resample
from ..models import TranscriptSegment, signoff_is_noise, stock_signoff
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


#: The files faster-whisper's own download_model fetches, and nothing else.
ALLOW_PATTERNS = ("config.json", "preprocessor_config.json", "model.bin",
                  "tokenizer.json", "vocabulary.*")


def prepare_model(model_name: str, models_root, local_files_only: bool = False,
                  progress: Optional[ProgressCallback] = None,
                  cancelled: Optional[CancelCheck] = None) -> pathlib.Path:
    """Download (or verify) a model into its own directory. Returns the path.

    Fetches exactly what faster-whisper's ``download_model`` would (same
    repository, same files, same plain ``output_dir`` layout), but with a
    progress bar that reports real bytes: *progress* receives a
    :class:`DownloadProgress` snapshot ("resolving", then "downloading"),
    throttled to about ten per second plus file boundaries. It is called on
    the downloading thread. *cancelled* is polled during the transfer and
    between files; when it returns True, :class:`DownloadCancelled` is
    raised and the files not yet complete are absent, so the directory
    inspects as INCOMPLETE (repairable), never COMPLETE.

    The file list and sizes come from the Hub's metadata first; when that
    is unavailable the total is reported as unknown (``None``) and the
    download still proceeds through ``snapshot_download``. Files already
    present at their published size are counted as done, not fetched again.
    """
    target = model_directory_for(models_root, model_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    repo_id = _repository_id(download_source(model_name))
    tracker = ProgressTracker(progress, cancelled)
    import huggingface_hub

    if local_files_only:
        resolved = huggingface_hub.snapshot_download(
            repo_id, local_dir=str(target), allow_patterns=list(ALLOW_PATTERNS),
            local_files_only=True)
        return pathlib.Path(resolved)

    target.mkdir(parents=True, exist_ok=True)
    tracker.update(phase="resolving", file="")
    tracker.check_cancelled()
    files = _published_files(huggingface_hub, repo_id)
    tracker.check_cancelled()
    bar = progress_bar_class(tracker)

    if files is None:
        # No metadata: the size is unknown, but the download still runs.
        tracker.update(phase="downloading", bytes_total=None, files_total=None)
        resolved = huggingface_hub.snapshot_download(
            repo_id, local_dir=str(target), allow_patterns=list(ALLOW_PATTERNS),
            tqdm_class=bar)
        tracker.check_cancelled()
        tracker.update(file="")
        return pathlib.Path(resolved)

    pending, present = [], []
    for name, size in files:
        local = target / name
        if size is not None and local.is_file() and local.stat().st_size == size:
            present.append(name)
        else:
            pending.append((name, size))
    sizes = [size for _name, size in pending]
    total = None if any(size is None for size in sizes) else sum(sizes)
    tracker.update(phase="downloading", bytes_done=0, bytes_total=total,
                   files_done=len(present), files_total=len(files), file="")
    for name, _size in pending:
        tracker.check_cancelled()
        tracker.update(file=name)
        huggingface_hub.hf_hub_download(repo_id, filename=name,
                                        local_dir=str(target), tqdm_class=bar)
        tracker.check_cancelled()
        tracker.update(files_done=tracker.snapshot.files_done + 1)
    tracker.update(file="")
    return target


def _repository_id(source: str) -> str:
    """The Hub repository behind *source*, as faster-whisper resolves it."""
    if "/" in source:
        return source
    try:
        from faster_whisper import utils as fw_utils

        known = dict(getattr(fw_utils, "_MODELS", {}) or {})
    except Exception:  # noqa: BLE001
        known = {}
    if source in known:
        return known[source]
    if source in CANONICAL_REPOSITORIES:
        return CANONICAL_REPOSITORIES[source]
    if source in _SYSTRAN_MODELS:
        return f"Systran/faster-whisper-{source}"
    raise ValueError(f"Invalid model size {source!r}, expected one of: "
                     f"{', '.join(sorted(known) or MODEL_SIZES)}")


#: Sizes published under Systran/faster-whisper-<name>, for a faster-whisper
#: whose name table cannot be read.
_SYSTRAN_MODELS = ("tiny", "base", "small", "medium", "large-v3")


def _published_files(huggingface_hub, repo_id: str):
    """``[(filename, size or None)]`` for the files to fetch, smallest first
    and ``model.bin`` last, or ``None`` when the Hub's metadata cannot be read."""
    try:
        info = huggingface_hub.HfApi().model_info(repo_id, files_metadata=True)
        siblings = list(info.siblings or [])
    except Exception as exc:  # noqa: BLE001 - fall back to an unknown total
        log.info("no file metadata for %s (%s); size unknown", repo_id, exc)
        return None
    files = []
    for sibling in siblings:
        name = getattr(sibling, "rfilename", None)
        if not name or not any(fnmatch.fnmatch(name, pattern)
                               for pattern in ALLOW_PATTERNS):
            continue
        size = getattr(sibling, "size", None)
        if size is None and getattr(sibling, "lfs", None) is not None:
            size = getattr(sibling.lfs, "size", None)
        files.append((name, int(size) if size is not None else None))
    if not files:
        return None
    files.sort(key=lambda item: (item[0] == "model.bin", item[1] or 0, item[0]))
    return files


#: The Hugging Face repositories behind names that older faster-whisper
#: releases do not know. 1.1.0 added the "large-v3-turbo" shorthand; 1.0.x
#: (still allowed by pyproject) would reject it. The local directory and
#: the configured name stay "large-v3-turbo" either way.
CANONICAL_REPOSITORIES = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def download_source(model_name: str) -> str:
    """What to hand faster-whisper's download_model for *model_name*: the
    name itself when this faster-whisper knows it, else the canonical
    repository id."""
    try:
        from faster_whisper.utils import available_models

        known = set(available_models())
    except Exception:  # noqa: BLE001 - an older or unusual release
        known = set()
    if model_name in known or model_name not in CANONICAL_REPOSITORIES:
        return model_name
    return CANONICAL_REPOSITORIES[model_name]


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
                 model_path: Optional[str] = None,
                 shared_language_encode: bool = False):
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
        #: Off by default, pending a native bench. With automatic language,
        #: identify the language from the encoder output of exactly the
        #: window that is then decoded, so the encoder runs once instead of
        #: twice per short clip. See _detect_on_decode_window for what is
        #: and is not identical to faster-whisper's own detection.
        self.shared_language_encode = bool(shared_language_encode)
        self._model: Any = None
        #: One model load at a time: a preload on a worker of one processor
        #: and a transcription on another (a retiring one, or several
        #: workers) must not both construct the model.
        self._load_lock = threading.Lock()
        self.version = f"faster-whisper/{model}"

    # -- availability ----------------------------------------------------
    def library_installed(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except Exception:  # noqa: BLE001
            return False
        return True

    def runtime_settings(self) -> Tuple[str, str]:
        """Backend and precision passed to CTranslate2 when these weights load."""
        device = "cpu" if self.device == "auto" else self.device
        precision = self.compute_type
        if precision == "default":
            precision = "float16" if device == "cuda" else "int8"
        return device, precision

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

    def prepare(self, local_files_only: bool = False,
                progress: Optional[ProgressCallback] = None,
                cancelled: Optional[CancelCheck] = None) -> pathlib.Path:
        """Fetch or verify this engine's model. Returns the resolved directory."""
        if self.model_path:
            return pathlib.Path(self.model_path).expanduser()
        if not self.models_root:
            raise EngineUnavailable("no model directory is configured")
        extra = {key: value for key, value in (("progress", progress),
                                                ("cancelled", cancelled))
                 if value is not None}
        return prepare_model(self.model_size, self.models_root,
                             local_files_only=local_files_only, **extra)

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
        with self._load_lock:
            if self._model is not None:
                return self._model
            return self._load_locked()

    def _load_locked(self):
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise EngineUnavailable(self.unavailable_reason()) from exc

        device, compute_type = self.runtime_settings()

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

    def preload(self) -> None:
        """Load the model ahead of the first message (the processing worker
        calls this when monitoring starts). Only a load: no transcription and
        no readiness claim - warm_up() remains the readiness self-test. Any
        failure propagates; the caller logs it and the first real message
        reports it in the ordinary way."""
        self._load()

    def close(self) -> None:
        self._model = None

    def _encode_reuse(self, model) -> Optional["_EncodeReuse"]:
        """The model's encode() wrapped once in an exact-reuse cache, or None
        when the model has no instance encode() to wrap (stand-ins)."""
        current = getattr(model, "encode", None)
        if isinstance(current, _EncodeReuse):
            return current
        if not callable(current):
            return None
        with self._load_lock:
            current = getattr(model, "encode", None)
            if isinstance(current, _EncodeReuse):
                return current
            try:
                # WhisperModel calls self.encode(...) for both language
                # detection and decoding, so an instance attribute is seen
                # by both.
                wrapper = _EncodeReuse(current)
                setattr(model, "encode", wrapper)
            except Exception:  # noqa: BLE001 - optional, never fatal
                return None
            return wrapper

    def _detect_on_decode_window(self, model, samples: np.ndarray
                                 ) -> Optional[Tuple[str, float]]:
        """``(language, probability)`` from the encoder output of the first
        window faster-whisper will decode, or None to leave detection to
        faster-whisper itself.

        faster-whisper 1.2.1 ``transcribe(language=None)`` runs
        ``detect_language`` on ``pad_or_trim(features[..., :3000])`` and
        then, with ``encoder_output=None``, encodes the first decode window
        again: ``pad_or_trim(features[:, :min(3000, content_frames)])`` with
        ``content_frames = features.shape[-1] - 1``. For a clip of 30 s or
        more both inputs are the same 3000 frames (the exact-reuse cache then
        saves the second pass by itself). For a shorter clip - every live
        clip - they differ in exactly one column: the final 10 ms frame,
        which detection sees and decoding does not. This method builds the
        decode window exactly as generate_segments does, encodes it once,
        and takes the top language as ``detect_language`` does with its
        default single segment. The decode that follows is then
        byte-for-byte the input faster-whisper would decode; the language
        decision is made without that one trailing frame, which is why this
        is off until a native bench shows the same decisions.
        """
        extractor = getattr(model, "feature_extractor", None)
        native = getattr(model, "model", None)
        if (extractor is None or native is None
                or not getattr(native, "is_multilingual", False)):
            return None
        try:
            features = extractor(samples)
            frames = int(getattr(extractor, "nb_max_frames", 3000))
            content_frames = features.shape[-1] - 1
            if content_frames < 1:
                return None
            window = features[:, :min(frames, content_frames)]
            if window.shape[-1] < frames:
                # As faster_whisper.audio.pad_or_trim: zeros after the end.
                window = np.pad(window, [(0, 0), (0, frames - window.shape[-1])])
            results = native.detect_language(model.encode(window))[0]
            token, probability = results[0]
            language = str(token)[2:-2]
        except Exception as exc:  # noqa: BLE001 - fall back to the stock path
            log.debug("shared-encode language detection unavailable: %s", exc)
            return None
        if not language:
            return None
        return language, float(probability)

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

        voiced = _voiced_samples(samples)
        result = self._decode(model, samples, voiced, language, vocabulary)
        if voiced != 0:
            return result
        # A long stretch of room noise can mask a short final utterance in
        # the stateful voice detector. Give a distinct rise at the end a
        # fresh detector context. Keep the full decode whenever it actually
        # placed substantive words there.
        tail_voice = _late_voice(samples)
        if not tail_voice:
            return result
        start, count = tail_voice
        offset = start / TARGET_RATE
        if result.text and not stock_signoff(result.text):
            words_at_end = any(s.text and s.end > offset and s.start < samples.size / TARGET_RATE
                               for s in result.segments)
            if (not words_at_end or result.no_speech_probability is None
                    or result.no_speech_probability >= 0.5):
                return result
        else:
            # Only a blank or stock output needs another audio decode.
            try:
                focused = self._decode(model, samples[start:], count, language, vocabulary)
            except EngineError:
                log.info("late-speech decode unavailable; retaining full recording result")
                return result
            tail_duration = (samples.size - start) / TARGET_RATE
            if (not focused.text or not focused.segments
                    or any(s.start >= tail_duration or s.end <= 0 for s in focused.segments)
                    or focused.no_speech_probability is None
                    or focused.no_speech_probability >= 0.5
                    or signoff_is_noise(stock_signoff(focused.text), focused.speech_seconds,
                                        focused.speech_ratio, focused.no_speech_probability,
                                        focused.confidence)):
                return result
            for segment in focused.segments:
                segment.start = offset + max(0.0, segment.start)
                segment.end = offset + min(tail_duration, segment.end)
            result = focused
        result.speech_seconds = round(count / TARGET_RATE, 2)
        result.speech_ratio = round(count / samples.size, 3)
        log.info("speech confirmed in final %.1f s: %.2f s voiced",
                 (samples.size - start) / TARGET_RATE, result.speech_seconds)
        return result

    def _decode(self, model, samples, voiced, language, vocabulary) -> TranscriptionResult:
        # The full-recording voice detector decides one thing here: with
        # the language left to automatic detection and *no* voiced sample
        # found at all, Whisper decodes once at temperature 0 instead of
        # climbing its temperature-fallback ladder (up to six decodes per
        # window; the suspected - not yet timed - cause of 20 s of noise
        # taking ~113 s on a Mac, pending Mac timing). The audio
        # is still transcribed in full and still goes through the speech
        # check; nothing is trimmed, capped or dropped. Unknown or non-zero
        # detector results, and a specified language (Retry as speech in...),
        # keep Whisper's normal decoding.
        single_pass = language is None and voiced == 0
        decode = {"temperature": 0.0} if single_pass else {}
        started = time.monotonic()

        # Within this call an encoder pass over byte-identical input is done
        # once (see _EncodeReuse): the same deterministic function of the
        # same bytes, so nothing about the result can change.
        reuse = self._encode_reuse(model)
        if reuse is not None:
            reuse.arm()
        detected = None
        if language is None and self.shared_language_encode:
            detected = self._detect_on_decode_window(model, samples)
        call_language = detected[0] if detected else language

        initial_prompt = None
        if vocabulary:
            # Whisper accepts a text prompt that biases decoding toward these
            # spellings - the practical way to get callsigns transcribed right.
            initial_prompt = ", ".join(list(vocabulary)[:100])

        try:
            segments, info = model.transcribe(
                samples,
                language=call_language,
                beam_size=self.beam_size,
                initial_prompt=initial_prompt,
                # Our detector has already isolated the transmission; Whisper's
                # own VAD would only re-trim (and sometimes drop) short clips.
                vad_filter=False,
                condition_on_previous_text=self.condition_on_previous_text,
                word_timestamps=False,
                **decode,
            )
            collected = list(segments)
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"whisper transcription failed: {exc}") from exc
        finally:
            passes, reused = reuse.disarm() if reuse is not None else (None, 0)

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
        # Evidence about whether this was speech at all, for the pipeline to
        # weigh - nothing is dropped here. Whisper's no-speech estimate,
        # weighted by how long each segment is...
        no_speech_probability = None
        weights = [max(0.0, p.end - p.start) for p in parts]
        if no_speech_probs and len(no_speech_probs) == len(weights) and sum(weights) > 0:
            no_speech_probability = round(float(np.average(no_speech_probs,
                                                           weights=weights)), 3)
        elif no_speech_probs:
            no_speech_probability = round(float(np.mean(no_speech_probs)), 3)
        # ...and an independent opinion: the voice-activity model bundled
        # with faster-whisper (Silero, local, no download).
        speech_ratio = speech_seconds = None
        if full_text and voiced is not None:
            speech_ratio = round(voiced / float(samples.size), 3)
            speech_seconds = round(voiced / float(TARGET_RATE), 2)
        temperatures = [float(t) for t in (getattr(seg, "temperature", None)
                                           for seg in collected) if t is not None]
        # Aggregate figures only - no words, no audio.
        log.info("whisper: %.1f s audio, language %s, VAD voiced %s, decode %s, "
                 "highest temperature used %s, encoder passes %s (%d reused), %.2f s",
                 samples.size / float(TARGET_RATE),
                 "specified" if language else (
                     "automatic, on the decode window" if detected else "automatic"),
                 "unknown" if voiced is None else f"{voiced / float(TARGET_RATE):.2f} s",
                 "single pass at temperature 0" if single_pass else "default fallback",
                 f"{max(temperatures):.1f}" if temperatures else "n/a",
                 "n/a" if passes is None else passes, reused,
                 time.monotonic() - started)

        if detected:
            # Recorded per transmission exactly as automatic detection is:
            # the language found and its probability (faster-whisper itself
            # reports 1.0 for a language it was given).
            result_language, result_confidence = detected[0], _safe_float(detected[1])
        else:
            result_language = getattr(info, "language", None) or language
            result_confidence = _safe_float(getattr(info, "language_probability", None))

        return TranscriptionResult(
            text=full_text,
            language=result_language,
            language_confidence=result_confidence,
            confidence=overall, segments=parts, engine=self.id,
            engine_version=self.version, no_speech=no_speech,
            no_speech_probability=no_speech_probability, speech_ratio=speech_ratio,
            speech_seconds=speech_seconds,
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


class _EncodeReuse:
    """Wraps ``WhisperModel.encode``: while armed on the calling thread, an
    encode of input byte-identical to the previous one returns the previous
    output instead of running the encoder again.

    Exact by construction - the encoder is a deterministic function of its
    input and the same bytes go in - and narrow: one entry, per thread, only
    between arm() and disarm() inside one FasterWhisperEngine.transcribe()
    call. Reusing an encoder output for detection and decoding is what
    faster-whisper itself does across its temperature fallbacks. Any
    difference in shape, type or a single bit runs the encoder as usual.
    """

    def __init__(self, encode):
        self._encode = encode
        self._local = threading.local()

    def arm(self) -> None:
        self._local.armed, self._local.entry = True, None
        self._local.passes = self._local.reused = 0

    def disarm(self) -> Tuple[int, int]:
        """End the call: ``(encoder passes run, passes reused)``."""
        local = self._local
        counts = (getattr(local, "passes", 0), getattr(local, "reused", 0))
        local.armed, local.entry = False, None
        return counts

    def __call__(self, features, *args, **kwargs):
        local = self._local
        if not getattr(local, "armed", False) or args or kwargs:
            return self._encode(features, *args, **kwargs)
        key = np.ascontiguousarray(features)
        entry = local.entry
        if (entry is not None and entry[0].shape == key.shape
                and entry[0].dtype == key.dtype
                and np.array_equal(entry[0].view(np.uint8), key.view(np.uint8))):
            local.reused += 1
            return entry[1]
        output = self._encode(features)
        local.passes += 1
        local.entry = (key.copy(), output)
        return output


_telemetry_disabled = False


def _disable_onnx_telemetry() -> None:
    """Switch off onnxruntime's optional telemetry events, once, before the
    VAD creates its first ONNX session. Uses onnxruntime's own public switch
    (``onnxruntime.disable_telemetry_events``); where it does not exist this
    does nothing."""
    global _telemetry_disabled
    if _telemetry_disabled:
        return
    _telemetry_disabled = True
    try:
        import onnxruntime

        switch = getattr(onnxruntime, "disable_telemetry_events", None)
        if callable(switch):
            switch()
    except Exception as exc:  # noqa: BLE001 - optional, never fatal
        log.debug("could not switch off onnxruntime telemetry: %s", exc)


def _voiced_samples(samples: np.ndarray, sample_rate: int = TARGET_RATE) -> Optional[int]:
    """How many samples of *samples* (16 kHz float) the bundled Silero VAD
    calls speech, exactly, or None when it cannot run - never a guess, and
    never raises: no opinion is a valid answer.

    Uses faster-whisper's own voice-activity detector and its packaged model
    (faster_whisper/assets/silero_vad_v6.onnx): local, nothing to download.
    The speech chunks are measured unpadded, so the figure is what the model
    heard, not what faster-whisper would keep around it for transcription.
    """
    data = np.asarray(samples, dtype=np.float32).ravel()
    if data.size == 0:
        return None
    try:
        _disable_onnx_telemetry()
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        chunks = get_speech_timestamps(data, VadOptions(speech_pad_ms=0),
                                       sampling_rate=sample_rate)
        voiced = sum(max(0, int(c["end"]) - int(c["start"])) for c in chunks)
    except Exception as exc:  # noqa: BLE001 - optional evidence, never fatal
        log.debug("local VAD unavailable: %s", exc)
        return None
    return min(voiced, data.size)


def _late_voice(samples: np.ndarray) -> Optional[Tuple[int, int]]:
    """A fresh VAD reading for a short burst above a longer stretch of ambient audio."""
    tail_size = 4 * TARGET_RATE
    if samples.size < 2 * tail_size:
        return None
    frame_size = TARGET_RATE // 50
    # Compare adjacent four-second intervals using 20 ms frame power.
    # One loud click cannot lift the recent 90th percentile.
    frames = samples[-2 * tail_size:].reshape(-1, frame_size)
    power = np.mean(frames * frames, axis=1)
    tail_frames = tail_size // frame_size
    ambient = np.percentile(power[:-tail_frames], 80)
    recent = np.percentile(power[-tail_frames:], 90)
    if recent <= max(1e-12, 2.512 * ambient):  # 4 dB in mean-square level
        return None
    start = samples.size - tail_size
    count = _voiced_samples(samples[start:])
    return (start, count) if count and count >= TARGET_RATE // 2 else None


def local_speech_activity(samples: np.ndarray, sample_rate: int = TARGET_RATE
                          ) -> Tuple[Optional[float], Optional[float]]:
    """``(share of the clip, seconds)`` of speech by the local VAD, or
    ``(None, None)`` when it cannot run (see _voiced_samples)."""
    voiced = _voiced_samples(samples, sample_rate)
    if voiced is None:
        return None, None
    size = np.asarray(samples).size
    return round(voiced / float(size), 3), round(voiced / float(sample_rate), 2)


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
