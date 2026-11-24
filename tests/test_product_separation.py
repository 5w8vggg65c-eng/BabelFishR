"""BabelFishR does not include, load or discover the KingFishR SDR features.

The SDR receiver, DSD-neo analysis, recorded-IQ source and experimental
decoders moved to the separate ``kingfishr`` distribution. These checks
prove the separation in the app, CLI, setup, readiness and packaging, and
that a settings file which selected the SDR receiver never records from the
microphone or the system default in its place.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

import pytest

from babelfishr import extensions
from babelfishr.app import RECEIVER_NOT_INCLUDED, BabelFishRApp
from babelfishr.audio.devices import InputNotSelected
from babelfishr.config import Config

REPO = pathlib.Path(__file__).resolve().parents[1]
CORE = REPO / "babelfishr"
SDR_MODULES = ("receiver", "analysis", "experimental")


# --- the core holds no SDR code and no way to find it ------------------------

def test_the_sdr_packages_are_not_in_babelfishr():
    for name in SDR_MODULES:
        assert not (CORE / name).exists(), f"babelfishr/{name} still exists"
    assert not (CORE / "ui" / "analysis_dialog.py").exists()
    assert (REPO / "kingfishr" / "kingfishr" / "receiver").is_dir()


def _imports(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            yield ("." * node.level) + (node.module or "")
        elif isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                yield (arg.value if isinstance(arg, ast.Constant)
                       else "<dynamic import>")


def test_no_core_module_imports_kingfishr_or_discovers_plugins():
    problems = []
    for path in CORE.rglob("*.py"):
        for name in _imports(path):
            if (name.startswith("kingfishr") or name == "<dynamic import>"
                    or name.lstrip(".").split(".")[0] in SDR_MODULES
                    or "entry_points" in name or "pkg_resources" in name
                    or name in ("importlib.metadata", "pkgutil")):
                problems.append(f"{path.relative_to(REPO)}: {name}")
        text = path.read_text(encoding="utf-8")
        for needle in ("entry_points(", "iter_entry_points", "walk_packages(",
                       "iter_modules("):
            if needle in text:
                problems.append(f"{path.relative_to(REPO)}: {needle}")
    assert not problems, problems


def test_the_registry_is_empty_and_the_product_is_babelfishr():
    assert extensions.registered() == ()
    assert extensions.product() is extensions.BABELFISHR
    assert extensions.hooks("window_built") == []


# --- a real BabelFishR start loads none of it, even with KingFishR present ---

STARTUP = r"""
import json, os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets
from babelfishr.config import Config
from babelfishr.app import BabelFishRApp
from babelfishr.cli import build_parser
from babelfishr.readiness import field_check
from babelfishr import preparation, diagnostics
from babelfishr.ui.main_window import MainWindow
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine

qt = QtWidgets.QApplication([])
config = Config()
config.database = sys.argv[1] + "/db.sqlite3"
config.recording.directory = sys.argv[1] + "/rec"
config.asr.engine = config.translate.engine = "mock"
app = BabelFishRApp(config=config)
window = MainWindow(app)
report = field_check(config, run_smoke_tests=False)
commands = sorted(build_parser()._subparsers._group_actions[0].choices)
loaded = sorted(m for m in sys.modules if m.split(".")[0] == "kingfishr"
                or any(part in ("receiver", "analysis", "experimental")
                       for part in m.split(".")[1:2]))
menus = [a.text().replace("&", "") for a in window.menuBar().actions()]
print(json.dumps({"loaded": loaded, "commands": commands, "menus": menus,
                  "checks": [c.name for c in report.checks],
                  "title": window.windowTitle()}))
window.close()
app.close()
"""


def test_a_babelfishr_start_loads_no_sdr_module(tmp_path):
    pytest.importorskip("PySide6")
    env = dict(os.environ)
    # KingFishR importable, as when both are installed: still not loaded.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "kingfishr")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run([sys.executable, "-c", STARTUP, str(tmp_path)],
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["loaded"] == [], result["loaded"]
    assert "analyze" not in result["commands"]
    assert "Receiver" not in result["menus"]
    assert result["title"] == "BabelFishR"
    assert not any(word in name for name in result["checks"]
                   for word in ("DSD", "SDR"))


def test_a_message_menu_has_no_digital_analysis(qt_app):
    import datetime as dt

    from babelfishr.models import ProcessingState, Transmission
    from babelfishr.ui.playback import PlaybackController, SystemOpenBackend
    from babelfishr.ui.timeline import TransmissionBubble

    bubble = TransmissionBubble(
        Transmission(session_id="s", started_at=dt.datetime.now(dt.timezone.utc),
                     duration=2.0, state=ProcessingState.COMPLETE,
                     audio_path="/nonexistent.wav"),
        PlaybackController(SystemOpenBackend()))
    texts = [a.text() for a in bubble.findChildren(type(bubble.play_action))]
    assert not any("Analyze" in t or "digital" in t.lower() for t in texts), texts
    assert not hasattr(bubble, "analyze_action")


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


# --- packaging -----------------------------------------------------------------

def test_the_babelfishr_package_and_bundle_exclude_kingfishr():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'include = ["babelfishr*"]' in pyproject
    assert "kingfishr" not in pyproject
    spec = (REPO / "packaging" / "babelfishr.spec").read_text(encoding="utf-8")
    assert '"kingfishr"' in spec.split("excludes=")[1].split("]")[0]
    assert 'collect_submodules("babelfishr")' in spec
    assert "kingfishr" not in spec.split("excludes=")[0]
    kf = (REPO / "kingfishr" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "kingfishr"' in kf and 'include = ["kingfishr*"]' in kf
    assert 'kingfishr = "kingfishr.cli:main"' in kf


# --- a legacy receiver selection: no microphone, settings kept -----------------

LEGACY = """
[audio.input]
kind = "receiver"
confirmed = true

[receiver]
sdrpp_path = "/Applications/SDR++.app"
frequency_hz = 155160000.0
mode = "FM"
digital = true
digital_protocol = "dmr"

[analysis]
dsd_path = "/opt/dsd-neo/bin/dsd-neo"

[sdr]
driver = "recorded-iq"
recording_path = "/data/iq.wav"
"""


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(LEGACY, encoding="utf-8")
    config = Config.load(str(path))
    config.database = str(tmp_path / "db.sqlite3")
    config.recording.directory = str(tmp_path / "rec")
    return config, path


class _Sentinel(Exception):
    pass


def _no_microphone(monkeypatch):
    """Any attempt to open, list-and-pick or resolve an audio device fails
    the test: nothing may stand in for the receiver."""
    import babelfishr.app as app_module

    def refuse(*args, **kwargs):
        raise _Sentinel("an audio input was opened or resolved")

    monkeypatch.setattr(app_module, "LiveAudioSource", refuse, raising=False)
    monkeypatch.setattr(app_module, "resolve_input", refuse, raising=False)


def test_a_legacy_receiver_selection_never_records_from_the_microphone(
        legacy, monkeypatch):
    config, _ = legacy
    _no_microphone(monkeypatch)
    app = BabelFishRApp(config=config)
    try:
        status = app.input_status()
        assert status["state"] == "receiver-unavailable"
        assert "Choose an audio input" in status["message"]
        assert app.receiver is None
        with pytest.raises(InputNotSelected) as raised:
            app.start_session(name="legacy")
        assert str(raised.value) == RECEIVER_NOT_INCLUDED
        assert "microphone" in str(raised.value)
        assert app.session is None and app.capture is None
    finally:
        app.close()


def test_the_inert_receiver_settings_survive_a_save(legacy, tmp_path):
    config, path = legacy
    saved = tmp_path / "saved.toml"
    config.save(str(saved))
    again = Config.load(str(saved))
    assert again.audio.input.kind == "receiver" and again.audio.input.confirmed
    assert again.input_is_receiver
    assert again.receiver.frequency_hz == 155160000.0
    assert again.receiver.digital_protocol == "dmr"
    assert again.analysis.dsd_path == "/opt/dsd-neo/bin/dsd-neo"
    assert again.sdr.driver == "recorded-iq"


def test_the_input_panel_asks_for_an_input_and_cannot_start(qt_app, legacy,
                                                            monkeypatch):
    from babelfishr.ui.input_panel import InputPanel

    config, _ = legacy
    app = BabelFishRApp(config=config)
    try:
        panel = InputPanel(app)
        assert (panel.device_box.currentData() or {}).get("kind") == "none"
        assert panel.device_box.findData({"kind": "receiver"}) == -1
        ok, message = panel.ready_to_monitor()
        assert not ok and message == RECEIVER_NOT_INCLUDED
        assert "choose an audio input" in panel.status_label.text().lower()
        assert panel.selected_identity() is None
    finally:
        app.close()


def test_kingfishr_ships_the_same_mit_notice():
    """KingFishR is its own distribution: it carries the full MIT notice,
    identical to BabelFishR's, and declares it so every build includes it
    (checked in the built wheel: dist-info/licenses/LICENSE)."""
    ours = (REPO / "LICENSE").read_bytes()
    assert (REPO / "kingfishr" / "LICENSE").read_bytes() == ours
    assert b"Copyright (c) 2026 BabelFishR contributors" in ours
    kf = (REPO / "kingfishr" / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license-files = ["LICENSE"]' in kf
