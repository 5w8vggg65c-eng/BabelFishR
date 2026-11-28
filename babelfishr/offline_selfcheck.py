"""Offline tokenizer self-check, runnable from the frozen app.

faster-whisper's WhisperModel fetches a tokenizer from the Hugging Face Hub
when a model folder has no ``tokenizer.json`` (only ``vocabulary.*``, as
older conversions ship). BabelFishR must refuse such a folder before the
library is constructed, on every route, with no network. This check builds
exactly that situation in a temporary profile - a vocabulary-only model and
an empty Hugging Face cache, Offline Field Use, no real weights - runs the
operator routes (Field Check with smoke tests, the engine's preload,
warm-up and first transcription, a custom model folder, and a Record Only
monitoring run that still archives its recording) with every network route
replaced by a recorder, and reports whether the refusal held.

Packaged: ``BabelFishR.app/Contents/MacOS/BabelFishR --selftest-offline-tokenizer``
(packaging/verify_independence.sh runs it). From source:
``python -c "from babelfishr.offline_selfcheck import run; raise SystemExit(run())"``.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import socket
import sys
import tempfile
import types
from typing import Callable, List

MODEL = "small"
#: A vocabulary-only legacy model: no tokenizer.json, no real weights.
VOCAB_ONLY = {"config.json": b"{}", "model.bin": b"\0" * 1024,
              "vocabulary.txt": b"a\nb\n"}


class Sentinels:
    """Every way the refused load could reach the network, recorded (and
    refused): the Hub tokenizer fetch, huggingface_hub downloads, sockets,
    and the WhisperModel constructor itself."""

    def __init__(self):
        self.calls: List[str] = []
        self._undo: List[Callable[[], None]] = []

    def _record(self, name: str):
        def spy(*_args, **_kwargs):
            self.calls.append(name)
            raise OSError(f"{name} called during the offline tokenizer check")
        return spy

    def _set(self, owner, name: str, value) -> None:
        previous = getattr(owner, name, None)
        had = hasattr(owner, name)
        setattr(owner, name, value)
        self._undo.append(lambda: setattr(owner, name, previous) if had
                          else delattr(owner, name))

    def install(self) -> "Sentinels":
        real_connect = socket.socket.connect
        calls = self.calls

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                calls.append(f"socket.connect {address!r}")
                raise OSError("network disabled during the offline tokenizer check")
            return real_connect(sock, address)

        self._set(socket.socket, "connect", connect)
        self._set(socket, "create_connection", self._record("socket.create_connection"))
        try:
            import tokenizers

            real = tokenizers.Tokenizer
            spy = self._record("tokenizers.Tokenizer.from_pretrained")
            proxy = types.SimpleNamespace(
                from_pretrained=spy, from_file=real.from_file,
                from_buffer=real.from_buffer, from_str=real.from_str)
            self._set(tokenizers, "Tokenizer", proxy)
        except ImportError:
            pass
        try:
            import huggingface_hub

            for name in ("hf_hub_download", "snapshot_download"):
                self._set(huggingface_hub, name, self._record(f"huggingface_hub.{name}"))
        except ImportError:
            pass
        try:
            import faster_whisper

            self._set(faster_whisper, "WhisperModel",
                      self._record("faster_whisper.WhisperModel"))
        except ImportError:
            # Stand in for the library, so the gate is still exercised.
            module = types.ModuleType("faster_whisper")
            module.WhisperModel = self._record("faster_whisper.WhisperModel")
            previous = sys.modules.get("faster_whisper")
            sys.modules["faster_whisper"] = module
            self._undo.append(lambda: sys.modules.__setitem__("faster_whisper", previous)
                              if previous is not None
                              else sys.modules.pop("faster_whisper", None))
        return self

    def remove(self) -> None:
        while self._undo:
            self._undo.pop()()


@contextlib.contextmanager
def _environment(values: dict):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def write_vocab_only_model(directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in VOCAB_ONLY.items():
        (directory / name).write_bytes(data)
    return directory


def run(out=print) -> int:
    """0 when every route refuses the vocabulary-only model with the clear
    message and no network call; 1 otherwise."""
    from .config import Config
    from .modes import OperatingMode
    from .providers import EngineUnavailable
    from .providers.whisper_local import (TOKENIZER_MISSING, FasterWhisperEngine,
                                          ModelState, inspect_model_directory,
                                          model_directory_for)

    problems: List[str] = []
    with tempfile.TemporaryDirectory(prefix="babelfishr-tokenizer-check-") as tmp:
        root = pathlib.Path(tmp)
        cache = root / "empty-hf-cache"
        cache.mkdir()
        env = {"HF_HOME": str(cache), "HF_HUB_CACHE": str(cache),
               "HUGGINGFACE_HUB_CACHE": str(cache), "HF_HUB_OFFLINE": "1",
               "BABELFISHR_HOME": str(root / "home")}
        with _environment(env):
            config = Config()
            config.app_home = str(root / "home")
            config.database = str(root / "check.sqlite3")
            config.recording.directory = str(root / "recordings")
            config.asr.model = MODEL
            config.mode = OperatingMode.FIELD_OFFLINE.value
            directory = write_vocab_only_model(
                model_directory_for(config.paths().models, MODEL))
            custom = write_vocab_only_model(root / "custom-model")
            sentinels = Sentinels().install()
            try:
                state, missing = inspect_model_directory(directory)
                if state is ModelState.COMPLETE:
                    problems.append("a vocabulary-only folder inspected as complete")
                out(f"inspect      : {state.value}, missing {missing}")

                from .readiness import field_check

                for mode in (OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY):
                    check = field_check(config, run_smoke_tests=True, mode=mode).get(
                        "Local ASR model present")
                    detail = getattr(check, "detail", "")
                    out(f"field check  : {mode.value}: {check.status.value} - {detail}")
                    if check.status.value != "fail" or TOKENIZER_MISSING not in detail:
                        problems.append(f"Field Check ({mode.value}) did not refuse it")

                engines = {
                    "pinned folder": FasterWhisperEngine(
                        model=MODEL, models_root=str(config.paths().models),
                        local_files_only=True),
                    "custom folder": FasterWhisperEngine(
                        model=MODEL, model_path=str(custom), local_files_only=True),
                }
                import numpy as np

                for label, engine in engines.items():
                    for route, call in (
                            ("preload", engine.preload),
                            ("warm_up", engine.warm_up),
                            ("transcribe", lambda e=engine: e.transcribe(
                                np.zeros(16000, dtype=np.float32), 16000))):
                        try:
                            call()
                        except EngineUnavailable as exc:
                            if TOKENIZER_MISSING not in str(exc):
                                problems.append(f"{label} {route}: unclear refusal: {exc}")
                        except Exception as exc:  # noqa: BLE001
                            problems.append(f"{label} {route}: {type(exc).__name__}: {exc}")
                        else:
                            problems.append(f"{label} {route}: was not refused")
                out("engine       : preload, warm-up and transcription refused "
                    "(pinned and custom folders)" if not any(
                        "folder" in p for p in problems) else "engine       : FAILED")
                problems.extend(_record_only_run(config, out))
            finally:
                sentinels.remove()
            for name in VOCAB_ONLY:
                if not (directory / name).exists():
                    problems.append(f"the legacy model's {name} was removed")
        if sentinels.calls:
            problems.append("network or library calls: " + ", ".join(sentinels.calls))
    for problem in problems:
        out(f"FAIL: {problem}")
    if problems:
        return 1
    out("ok:   a vocabulary-only model is refused offline before WhisperModel, "
        "with no network call; Record Only still archives recordings")
    return 0


def _record_only_run(config, out) -> List[str]:
    """Record Only on a synthetic message: recorded and stored, no ASR and
    no registered transfer."""
    from .app import BabelFishRApp
    from .modes import OperatingMode
    from .storage import Store
    from .testing import standard_fixture
    from .transfers import registry

    problems = []
    config.mode = OperatingMode.RECORD_ONLY.value
    wav = standard_fixture(16000).write(str(pathlib.Path(config.app_home) / "fixture.wav"))
    store = Store(config.database, recordings_dir=config.recording.directory)
    began = []
    owner = registry()
    real_begin = owner.begin
    owner.begin = lambda *a, **k: began.append(a) or real_begin(*a, **k)
    try:
        app = BabelFishRApp(config=config, store=store)
        app.select_engines()
        if app.transcription is not None:
            problems.append("Record Only selected a transcription engine")
        app.start_session(replay_path=wav, name="tokenizer check")
        app.run_replay()
        transmissions = app.transmissions()
        app.stop_session()
        kept = [t for t in transmissions
                if t.audio_path and pathlib.Path(t.audio_path).exists()]
        if not transmissions or len(kept) != len(transmissions):
            problems.append("Record Only did not archive every recording")
        out(f"record only  : {len(kept)} recording(s) archived, no ASR")
    finally:
        owner.begin = real_begin
        store.close()
        config.mode = OperatingMode.FIELD_OFFLINE.value
    if began:
        problems.append(f"Record Only registered transfers: {began}")
    return problems
