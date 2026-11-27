"""Network-restricted modes against network transfers.

Two faults from an independent review:

(a) the desktop could show Offline Field Use while a model download was still
    running in its worker thread;
(b) ``babelfishr languages install`` and the remote language catalogue ignored
    both network-restricted modes - the install even upgraded itself to
    Online / Setup - so a network call was made in Offline Field Use.

Every transfer now registers with babelfishr.transfers until its worker has
really ended; a switch to a restricted mode closes the gate at once, cancels
what runs, and is shown and saved only after everything settled - or is
declined, accurately, when something will not stop. The CLI applies the policy
before any network access.

The network itself is replaced by spies that record a call and fail the test.
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time
import types

import pytest

from babelfishr import cli
from babelfishr.app import BabelFishRApp, ModeChangeRefused, ModeTransition
from babelfishr.modes import OfflineViolation, OperatingMode
from babelfishr.transfers import TransferRegistry, registry

RESTRICTED = [OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY]
TURBO = "large-v3-turbo"


# ---- shared helpers -----------------------------------------------------
@pytest.fixture(autouse=True)
def _registry_is_left_clean():
    owner = registry()
    assert not owner.active() and not owner.restricted, "a previous test leaked"
    yield
    leaked_active = owner.active()
    leaked_gate = owner.restricted
    # Do not poison later tests, but do fail this one.
    with owner._cond:
        owner._active.clear()
        owner._restrictions.clear()
    assert not leaked_active, f"transfers left registered: {leaked_active}"
    assert not leaked_gate, "the restriction gate was left closed"


class NetworkSpies:
    """Every route to the network, replaced by a recorder. ``forbid`` makes
    each call fail the test as well (it is still recorded)."""

    def __init__(self, monkeypatch, forbid=True):
        import babelfishr.providers.argos as argos
        import babelfishr.providers.whisper_local as whisper_local

        self.calls = []
        self.forbid = forbid
        installed = []
        self.installed_languages = installed

        package = types.ModuleType("argostranslate.package")
        package.update_package_index = self._spy("argos.update_package_index")
        package.get_available_packages = self._spy("argos.get_available_packages",
                                                   result=[])
        package.install_from_path = self._spy("argos.install_from_path")
        package.get_installed_packages = lambda: []
        translate = types.ModuleType("argostranslate.translate")
        translate.get_installed_languages = lambda: list(installed)
        root = types.ModuleType("argostranslate")
        root.package = package
        root.translate = translate
        monkeypatch.setitem(sys.modules, "argostranslate", root)
        monkeypatch.setitem(sys.modules, "argostranslate.package", package)
        monkeypatch.setitem(sys.modules, "argostranslate.translate", translate)

        def fetch(*_a, **_k):
            self._record("argos.fetch_package_index")
            raise OSError("network unavailable in this test")

        monkeypatch.setattr(argos, "fetch_package_index", fetch)
        argos.reset_package_index_state()

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen",
                            self._spy("urllib.urlopen", error=OSError("no network")))
        import huggingface_hub

        monkeypatch.setattr(huggingface_hub, "snapshot_download",
                            self._spy("hf.snapshot_download", error=OSError("no network")))
        monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                            self._spy("hf.hf_hub_download", error=OSError("no network")))
        monkeypatch.setattr(whisper_local, "prepare_model",
                            self._spy("whisper.prepare_model", error=OSError("no network")))

    def _record(self, name):
        self.calls.append(name)
        if self.forbid:
            pytest.fail(f"network call in a restricted mode: {name}")

    def _spy(self, name, result=None, error=None):
        def spy(*_a, **_k):
            self._record(name)
            if error is not None:
                raise error
            return result
        return spy

    def add_installed(self, source, target):
        to_lang = types.SimpleNamespace(code=target)
        translation = types.SimpleNamespace(to_lang=to_lang)
        self.installed_languages.append(
            types.SimpleNamespace(code=source, translations_from=[translation]))


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """An isolated BabelFishR profile: its own home, no config in the cwd."""
    home = tmp_path / "profile"
    monkeypatch.setenv("BABELFISHR_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARGOS_PACKAGES_DIR", raising=False)
    return home


def set_saved_mode(mode: OperatingMode):
    assert cli.main(["mode", "--set", mode.value]) == 0


class HeldTransfer:
    """A fake transfer, registered with the owner, held on an Event.

    ``honour_cancel``: the cancel request releases it (it then raises, as a
    cancelled download does). ``fail``: it raises when released.
    """

    def __init__(self, honour_cancel=True, fail=False, owner=None):
        self.release = threading.Event()
        self.registered = threading.Event()
        self.honour_cancel = honour_cancel
        self.fail = fail
        self.cancel_calls = 0
        self.outcome = None
        self.owner = owner or registry()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.registered.wait(5), "the fake transfer never registered"

    def _cancel(self):
        self.cancel_calls += 1
        if self.honour_cancel:
            self.release.set()

    def _run(self):
        try:
            with self.owner.begin("model", "a held test download",
                                  cancel=self._cancel):
                self.registered.set()
                self.release.wait(30)
                if self.fail or self.cancel_calls:
                    raise RuntimeError("transfer stopped")
            self.outcome = "finished"
        except RuntimeError as exc:
            self.outcome = f"raised: {exc}"

    def finish(self):
        self.release.set()
        self.thread.join(5)
        assert not self.thread.is_alive()


def saved_mode(app) -> str:
    path = pathlib.Path(app.config.settings_path())
    if not path.exists():
        return ""
    from babelfishr.config import Config

    return Config.load(str(path)).mode


@pytest.fixture
def app(config, store, tmp_path):
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    application = BabelFishRApp(config=config, store=store)
    application.config.save()
    yield application
    transition = application.mode_transition
    if transition is not None:
        transition.abandon("test teardown")


# ---- CLI: policy before any network call ------------------------------------
@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_language_install_refused_in_restricted_mode_without_network(
        profile, monkeypatch, capsys, mode):
    spies = NetworkSpies(monkeypatch)
    set_saved_mode(mode)
    rc = cli.main(["languages", "install", "es", "en"])
    err = capsys.readouterr().err
    assert rc != 0
    assert mode.title in err and "Nothing was downloaded" in err
    assert spies.calls == []


@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_mode_flag_restricts_language_install(profile, monkeypatch, capsys, mode):
    spies = NetworkSpies(monkeypatch)
    set_saved_mode(OperatingMode.ONLINE_SETUP)
    rc = cli.main(["languages", "--mode", mode.value, "install", "es", "en"])
    assert rc != 0 and mode.title in capsys.readouterr().err
    assert spies.calls == []


@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_remote_catalogue_refused_in_restricted_mode(profile, monkeypatch,
                                                         capsys, mode):
    spies = NetworkSpies(monkeypatch)
    spies.add_installed("es", "en")
    set_saved_mode(mode)
    rc = cli.main(["languages", "list", "--available"])
    assert rc != 0 and mode.title in capsys.readouterr().err
    assert spies.calls == []


@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_installed_languages_listed_offline(profile, monkeypatch, capsys, mode):
    spies = NetworkSpies(monkeypatch)
    spies.add_installed("es", "en")
    set_saved_mode(mode)
    rc = cli.main(["languages", "list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "es -> en" in out
    assert spies.calls == []


@pytest.mark.parametrize("mode", RESTRICTED)
def test_cli_offline_readiness_check_needs_no_network(profile, monkeypatch, capsys,
                                                       mode):
    spies = NetworkSpies(monkeypatch)
    set_saved_mode(mode)
    cli.main(["field-check", "--no-smoke-tests", "--mode", mode.value])
    assert "Cloud processing" in capsys.readouterr().out
    assert spies.calls == []


def test_cli_online_install_does_reach_the_spies(profile, monkeypatch, capsys):
    """Control: the spies sit on the real network routes."""
    spies = NetworkSpies(monkeypatch, forbid=False)
    set_saved_mode(OperatingMode.ONLINE_SETUP)
    rc = cli.main(["languages", "install", "es", "en"])
    assert rc != 0                     # the stand-in network is down
    assert "argos.fetch_package_index" in spies.calls
    assert not registry().active()


# ---- the shared owner -------------------------------------------------------
def test_registry_gate_blocks_and_cancels_and_settles():
    owner = TransferRegistry()
    held = HeldTransfer(owner=owner)
    assert [t.description for t in owner.active()] == ["a held test download"]
    gate = owner.restrict("Offline Field Use was requested")
    with pytest.raises(OfflineViolation):
        owner.begin("language", "another pack")
    assert owner.wait_settled(5)
    assert held.cancel_calls == 1 and held.outcome.startswith("raised")
    gate.release()
    with owner.begin("language", "allowed again"):
        pass
    held.thread.join(5)


def test_nested_registration_is_one_transfer():
    owner = TransferRegistry()
    with owner.begin("setup", "outer"):
        with owner.begin("model", "inner"):
            assert len(owner.active()) == 1
    assert not owner.active()


# ---- App.set_mode / request_mode -------------------------------------------
@pytest.mark.parametrize("mode", RESTRICTED)
def test_mode_is_not_shown_or_saved_until_the_transfer_settles(app, mode):
    held = HeldTransfer(honour_cancel=False)
    transition = app.request_mode(mode.value, timeout=10)
    assert transition.pending
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    assert held.cancel_calls == 1, "the running transfer was not asked to stop"
    # New transfers are refused from the moment of the request.
    with pytest.raises(OfflineViolation):
        registry().begin("language", "a new pack")
    held.finish()
    assert transition.wait() == ModeTransition.DONE
    assert app.mode is mode and saved_mode(app) == mode.value
    assert not registry().restricted


def test_new_installs_are_blocked_immediately_after_the_request(app, monkeypatch):
    from babelfishr.preparation import install_asr_model, install_language

    spies = NetworkSpies(monkeypatch)
    held = HeldTransfer(honour_cancel=False)
    app.request_mode(OperatingMode.FIELD_OFFLINE.value, timeout=10)
    # The config still says Online / Setup, so only the gate stops these.
    with pytest.raises(OfflineViolation):
        install_asr_model(app.config, TURBO)
    with pytest.raises(OfflineViolation):
        install_language("es", "en", OperatingMode.ONLINE_SETUP)
    assert spies.calls == []
    held.finish()
    assert app.mode_transition is None or app.mode_transition.wait() == "done"
    assert app.mode is OperatingMode.FIELD_OFFLINE


def test_set_mode_waits_for_a_cancel_that_is_honoured(app):
    held = HeldTransfer(honour_cancel=True)
    app.set_mode(OperatingMode.FIELD_OFFLINE.value, timeout=5)
    assert held.outcome and held.outcome.startswith("raised")
    assert app.mode is OperatingMode.FIELD_OFFLINE
    assert saved_mode(app) == OperatingMode.FIELD_OFFLINE.value
    held.thread.join(5)


def test_set_mode_declines_when_a_transfer_ignores_cancel(app):
    held = HeldTransfer(honour_cancel=False)
    with pytest.raises(ModeChangeRefused) as refused:
        app.set_mode(OperatingMode.FIELD_OFFLINE.value, timeout=0.3)
    message = str(refused.value)
    assert "A download is still finishing" in message
    assert "Offline Field Use was not turned on" in message
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    assert not registry().restricted, "the gate was not released"
    assert app.mode_transition is None
    held.finish()
    with registry().begin("model", "allowed again"):
        pass


def test_a_failing_transfer_still_settles(app):
    held = HeldTransfer(honour_cancel=False, fail=True)
    transition = app.request_mode(OperatingMode.RECORD_ONLY.value, timeout=10)
    assert transition.pending
    held.release.set()
    assert transition.wait() == ModeTransition.DONE
    assert held.outcome == "raised: transfer stopped"
    assert app.mode is OperatingMode.RECORD_ONLY


def test_quick_toggles_end_consistent(app):
    held = HeldTransfer(honour_cancel=False)
    first = app.request_mode(OperatingMode.FIELD_OFFLINE.value, timeout=10)
    back = app.request_mode(OperatingMode.ONLINE_SETUP.value)
    assert first.state == ModeTransition.ABANDONED and back.state == "done"
    assert not registry().restricted and app.mode is OperatingMode.ONLINE_SETUP
    second = app.request_mode(OperatingMode.FIELD_OFFLINE.value, timeout=10)
    third = app.request_mode(OperatingMode.RECORD_ONLY.value, timeout=10)
    assert second.state == ModeTransition.ABANDONED and third.pending
    assert registry().restricted, "the gate opened between two requests"
    assert first.poll() == ModeTransition.ABANDONED      # stale: never commits
    held.finish()
    assert third.wait() == ModeTransition.DONE
    assert app.mode is OperatingMode.RECORD_ONLY
    assert saved_mode(app) == OperatingMode.RECORD_ONLY.value
    assert not registry().restricted


def test_without_transfers_the_switch_is_immediate(app):
    transition = app.request_mode(OperatingMode.FIELD_OFFLINE.value)
    assert transition.state == ModeTransition.DONE
    assert app.mode is OperatingMode.FIELD_OFFLINE and not registry().restricted


def test_closing_the_app_releases_a_pending_request(app):
    held = HeldTransfer(honour_cancel=False)
    transition = app.request_mode(OperatingMode.FIELD_OFFLINE.value, timeout=10)
    app.close()
    assert transition.state == ModeTransition.ABANDONED
    assert not registry().restricted
    held.finish()


# ---- GUI ---------------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=10):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.005)


def pump_until(qt_app, condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        pump(qt_app, 2)
    return condition()


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow
    from test_offline_indicator import Jobs

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    config.asr.model = "small"
    Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.app.config.save()
    win.shown_messages = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: win.shown_messages.append(a[2]))
    monkeypatch.setattr(win, "_choose", lambda *a, **k: "Download")
    win.show()
    pump(qt_app)
    yield win
    transition = win.app.mode_transition
    if transition is not None:
        transition.abandon("test teardown")
    win.close()


@pytest.fixture
def held_download(monkeypatch):
    """The real install path (worker, job, preparation) with the model
    download stood in for by one held on an Event."""
    import babelfishr.providers.whisper_local as whisper_local
    from babelfishr.download_progress import DownloadCancelled

    state = types.SimpleNamespace(release=threading.Event(), started=threading.Event(),
                                  honour_cancel=True, saw_cancel=False)

    def prepare_model(name, models_root, cancelled=None, **_kw):
        state.started.set()
        while not state.release.wait(0.01):
            if cancelled is not None and cancelled():
                state.saw_cancel = True
                if state.honour_cancel:
                    raise DownloadCancelled("cancelled")
        raise OSError("stand-in network finished without a model")

    monkeypatch.setattr(whisper_local, "prepare_model", prepare_model)
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "faster_whisper",
                            types.SimpleNamespace(WhisperModel=object))
    yield state
    state.release.set()


def _start_held_install(qt_app, window, held_download):
    window._install_model(TURBO)
    assert held_download.started.wait(5), "the download never started"
    assert pump_until(qt_app, lambda: registry().busy)


def test_gui_does_not_show_offline_until_the_download_settles(qt_app, window,
                                                              held_download):
    _start_held_install(qt_app, window, held_download)
    changed = window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    assert changed is False
    assert window.app.mode is OperatingMode.ONLINE_SETUP
    assert "OFFLINE" not in window.mode_button.text()
    assert saved_mode(window.app) == OperatingMode.ONLINE_SETUP.value
    assert window.mode_actions[OperatingMode.ONLINE_SETUP.value].isChecked()
    assert not window.install_model_button.isEnabled()
    with pytest.raises(OfflineViolation):
        registry().begin("language", "a new pack")

    assert pump_until(qt_app, lambda: window.app.mode is OperatingMode.FIELD_OFFLINE)
    assert held_download.saw_cancel
    assert not registry().busy and not registry().restricted
    assert "OFFLINE FIELD USE" in window.mode_button.text()
    assert saved_mode(window.app) == OperatingMode.FIELD_OFFLINE.value
    assert pump_until(qt_app, lambda: window._install_worker is None)
    assert "cancelled" in window.install_panel.status.text()


def test_gui_declines_when_the_download_will_not_stop(qt_app, window, held_download):
    held_download.honour_cancel = False
    window.app.mode_settle_timeout = 0.4
    _start_held_install(qt_app, window, held_download)
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    assert pump_until(qt_app, lambda: window.shown_messages)
    message = window.shown_messages[-1]
    assert "A download is still finishing" in message
    assert "Offline Field Use was not turned on" in message
    assert window.app.mode is OperatingMode.ONLINE_SETUP
    assert "ONLINE" in window.mode_button.text()
    assert window.mode_actions[OperatingMode.ONLINE_SETUP.value].isChecked()
    assert saved_mode(window.app) == OperatingMode.ONLINE_SETUP.value
    assert not registry().restricted
    assert registry().busy, "the stalled download really is still running"
    held_download.release.set()
    assert pump_until(qt_app, lambda: window._install_worker is None)
    assert not registry().busy


def test_gui_quick_toggles_end_consistent(qt_app, window, held_download):
    held_download.honour_cancel = False
    _start_held_install(qt_app, window, held_download)
    assert window._apply_mode(OperatingMode.FIELD_OFFLINE.value) is False
    assert window._apply_mode(OperatingMode.ONLINE_SETUP.value) is True
    assert not registry().restricted
    assert window.app.mode is OperatingMode.ONLINE_SETUP
    assert window._apply_mode(OperatingMode.FIELD_OFFLINE.value) is False
    pump(qt_app)
    assert window.app.mode is OperatingMode.ONLINE_SETUP
    held_download.release.set()
    assert pump_until(qt_app, lambda: window.app.mode is OperatingMode.FIELD_OFFLINE)
    assert pump_until(qt_app, lambda: window._install_worker is None)
    assert not registry().restricted and not registry().busy
    assert "OFFLINE FIELD USE" in window.mode_button.text()
    assert saved_mode(window.app) == OperatingMode.FIELD_OFFLINE.value
    assert window.shown_messages == []


@pytest.mark.parametrize("mode", RESTRICTED)
def test_gui_transfers_make_no_network_call_in_restricted_modes(
        qt_app, window, monkeypatch, mode):
    from babelfishr.ui.workers import install_model_job, prepare_field_job

    window._apply_mode(mode.value)
    assert window.app.mode is mode
    spies = NetworkSpies(monkeypatch)
    window._install_model(TURBO)
    pump(qt_app)
    assert window._install_worker is None
    # The jobs behind the install panel and the Setup Assistant, called as
    # their worker would.
    with pytest.raises(OfflineViolation):
        install_model_job(window.app.config, TURBO)
    with pytest.raises(OfflineViolation):
        prepare_field_job(window.app.config, TURBO, [("es", "en")])
    assert spies.calls == []
    assert not registry().active()


# ---- engine-level downloads (a Whisper engine fetching on first load) --------
@pytest.fixture
def held_engine_download(monkeypatch):
    """The real engine factory and load path; only prepare_model (the
    provider's download internals) is held on an Event."""
    import babelfishr.providers.whisper_local as whisper_local
    from babelfishr.download_progress import DownloadCancelled

    state = types.SimpleNamespace(release=threading.Event(),
                                  started=threading.Event(),
                                  honour_cancel=True, saw_cancel=False,
                                  outcome=None)

    def prepare_model(name, models_root, local_files_only=False,
                      cancelled=None, **_kw):
        assert not local_files_only
        state.started.set()
        while not state.release.wait(0.01):
            if cancelled is not None and cancelled():
                state.saw_cancel = True
                if state.honour_cancel:
                    raise DownloadCancelled("cancelled")
        raise OSError("stand-in network finished without a model")

    monkeypatch.setattr(whisper_local, "prepare_model", prepare_model)
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=object))
    yield state
    state.release.set()


def _start_engine_load(app, state):
    from babelfishr.providers import build_transcription_engine

    engine = build_transcription_engine(app.config, requested="faster-whisper",
                                        mode=OperatingMode.ONLINE_SETUP)
    assert engine.local_files_only is False

    def load():
        try:
            engine.preload()
            state.outcome = "loaded"
        except BaseException as exc:  # noqa: BLE001
            state.outcome = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    assert state.started.wait(5), "the engine never started its download"
    assert registry().busy, "the engine's download is not accounted for"
    return thread


@pytest.mark.parametrize("mode", RESTRICTED)
def test_engine_download_holds_the_switch_until_cancelled(app, held_engine_download,
                                                         mode):
    thread = _start_engine_load(app, held_engine_download)
    held_engine_download.honour_cancel = False         # not yet
    transition = app.request_mode(mode.value, timeout=10)
    assert transition.pending
    time.sleep(0.05)
    assert held_engine_download.saw_cancel, "the owner's cancel did not reach it"
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    held_engine_download.honour_cancel = True
    assert transition.wait() == ModeTransition.DONE
    thread.join(5)
    assert "Cancelled" in held_engine_download.outcome
    assert app.mode is mode and saved_mode(app) == mode.value


def test_engine_download_that_will_not_stop_declines_the_switch(
        app, held_engine_download):
    thread = _start_engine_load(app, held_engine_download)
    held_engine_download.honour_cancel = False
    with pytest.raises(ModeChangeRefused) as refused:
        app.set_mode(OperatingMode.FIELD_OFFLINE.value, timeout=0.3)
    assert "A download is still finishing" in str(refused.value)
    assert "the Whisper model" in str(refused.value)
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    assert not registry().restricted
    held_engine_download.release.set()
    thread.join(5)
    assert not registry().busy


def test_restricted_engine_is_not_wrapped_and_never_downloads(app, monkeypatch):
    from babelfishr.providers import EngineUnavailable, build_transcription_engine

    spies = NetworkSpies(monkeypatch)
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(WhisperModel=object))
    engine = build_transcription_engine(app.config, requested="faster-whisper",
                                        mode=OperatingMode.ONLINE_SETUP)
    engine.local_files_only = True                     # as Field Offline builds it
    with pytest.raises(EngineUnavailable):
        engine._load()
    assert spies.calls == []


# ---- the Setup Assistant's mode changes --------------------------------------
def _ready_payload():
    from babelfishr.preparation import ASR_STEP, PreparationResult
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    result = PreparationResult()
    result.add(ASR_STEP, True, "small ok")
    result.add("Language pack es->en", True, "installed")
    ready = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local transcription smoke test", "Local translation smoke test"):
        ready.add(Check(name, CheckStatus.PASS, ""))
    return {"preparation": result, "readiness": ready, "asr_model": "small",
            "language_pairs": [("es", "en")]}


def _assistant(parent_window=None, app=None):
    from PySide6 import QtWidgets

    from babelfishr.ui.setup_assistant import SetupAssistant

    dialog = SetupAssistant(app or parent_window.app, parent_window)
    dialog.done_with = None
    dialog.finished.connect(lambda code: setattr(dialog, "done_with", code))
    assert dialog.result() != QtWidgets.QDialog.Accepted
    return dialog


def _saved_setup(app):
    from babelfishr.config import Config

    return Config.load(str(app.config.settings_path())).setup


def test_assistant_record_only_through_the_window(qt_app, window):
    from PySide6 import QtWidgets

    dialog = _assistant(window)
    dialog._record_only()
    assert dialog.result() == QtWidgets.QDialog.Accepted
    assert window.app.mode is OperatingMode.RECORD_ONLY
    assert saved_mode(window.app) == OperatingMode.RECORD_ONLY.value
    assert _saved_setup(window.app).record_only_acknowledged
    assert "RECORD ONLY" in window.mode_button.text()


def test_assistant_record_only_waits_for_a_running_download(qt_app, window,
                                                            held_download):
    from PySide6 import QtWidgets

    held_download.honour_cancel = False
    _start_held_install(qt_app, window, held_download)
    dialog = _assistant(window)
    dialog._record_only()
    # Handed to the window, which waits: the dialog closes, nothing is shown
    # or saved as Record Only yet.
    assert dialog.result() == QtWidgets.QDialog.Accepted
    assert window.app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(window.app) == OperatingMode.ONLINE_SETUP.value
    assert not _saved_setup(window.app).record_only_acknowledged
    assert "ONLINE" in window.mode_button.text()
    held_download.release.set()
    assert pump_until(qt_app, lambda: window.app.mode is OperatingMode.RECORD_ONLY)
    assert saved_mode(window.app) == OperatingMode.RECORD_ONLY.value
    assert pump_until(qt_app, lambda: window._install_worker is None)
    assert not registry().restricted


def test_assistant_switches_to_offline_after_preparation(qt_app, window,
                                                         held_download):
    held_download.honour_cancel = False
    _start_held_install(qt_app, window, held_download)
    dialog = _assistant(window)
    dialog._finished(_ready_payload())
    assert dialog.status_label.text() == "Ready for offline field use"
    assert window.app.config.setup.completed
    assert window.app.mode is OperatingMode.ONLINE_SETUP, "offline shown early"
    assert saved_mode(window.app) == OperatingMode.ONLINE_SETUP.value
    held_download.release.set()
    assert pump_until(qt_app,
                      lambda: window.app.mode is OperatingMode.FIELD_OFFLINE)
    assert saved_mode(window.app) == OperatingMode.FIELD_OFFLINE.value
    assert "OFFLINE FIELD USE" in window.mode_button.text()
    assert pump_until(qt_app, lambda: window._install_worker is None)


def test_assistant_switches_to_offline_at_once_when_nothing_runs(qt_app, window):
    dialog = _assistant(window)
    dialog._finished(_ready_payload())
    assert window.app.mode is OperatingMode.FIELD_OFFLINE
    assert saved_mode(window.app) == OperatingMode.FIELD_OFFLINE.value


def test_assistant_without_a_window_refuses_record_only_and_stays_open(
        qt_app, app):
    from PySide6 import QtWidgets

    held = HeldTransfer(honour_cancel=False)
    dialog = _assistant(app=app)
    dialog._record_only()
    assert dialog.result() != QtWidgets.QDialog.Accepted and dialog.done_with is None
    text = dialog.log_view.toPlainText()
    assert "The mode was not changed: A download is still finishing" in text
    assert "Record Only was not turned on" in text
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    assert not _saved_setup(app).record_only_acknowledged
    assert not registry().restricted
    held.finish()
    dialog._record_only()                              # nothing running now
    assert dialog.result() == QtWidgets.QDialog.Accepted
    assert app.mode is OperatingMode.RECORD_ONLY


def test_assistant_without_a_window_reports_a_declined_offline_switch(qt_app, app):
    held = HeldTransfer(honour_cancel=False)
    dialog = _assistant(app=app)
    dialog._finished(_ready_payload())
    text = dialog.log_view.toPlainText()
    assert "The mode was not changed: A download is still finishing" in text
    assert "Offline Field Use was not turned on" in text
    assert app.mode is OperatingMode.ONLINE_SETUP
    assert saved_mode(app) == OperatingMode.ONLINE_SETUP.value
    assert not registry().restricted
    held.finish()
