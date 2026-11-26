"""Offline Field Use readiness: what a green tick may and may not claim.

- Translation is checked for the route the settings would use: a selected
  Spanish source is not vouched for by a German -> English pack; English
  into English with "skip if same language" is the genuine pass-through
  (no Argos at all); Automatic smoke-tests every installed route into the
  target and names the ones verified.
- The microphone decision is read through Qt's permission API without a
  dialog: granted passes, an affirmative denial fails (and means recording
  is not possible), "not asked yet" keeps the tick off until the first
  watch, and a build that cannot read it does not keep a working Mac red.
- A chosen input that is missing or ambiguous means recording is not
  possible; the built-in microphone chosen for a bench test is legitimate.
- offline_field_problems lists every required check that is not a pass.

Synthetic reports and stand-in engines only; nothing is downloaded.
"""

from __future__ import annotations

import types

import pytest

from babelfishr import readiness
from babelfishr.config import Config
from babelfishr.modes import OperatingMode
from babelfishr.readiness import (OFFLINE_REQUIRED, Check, CheckStatus, ReadinessReport,
                                  offline_field_problems)

PASS, WARN, FAIL, SKIP = (CheckStatus.PASS, CheckStatus.WARN, CheckStatus.FAIL,
                          CheckStatus.SKIP)


def ready_report(mode=OperatingMode.FIELD_OFFLINE, **overrides):
    report = ReadinessReport(mode=mode)
    for name in OFFLINE_REQUIRED:
        status, detail, data = overrides.get(name, (PASS, "ok", {}))
        report.add(Check(name, status, detail, data=data))
    status, detail, data = overrides.get(
        "Microphone permission", (SKIP, "not applicable", {"acceptable": True}))
    report.add(Check("Microphone permission", status, detail, data=data))
    report.add(Check("Free storage", *overrides.get("Free storage", (PASS, "90 GB"))))
    return report


# --- the verdict ------------------------------------------------------------------

def test_every_required_pass_is_ready_and_advisory_warnings_do_not_block():
    assert offline_field_problems(ready_report()) == []
    assert ready_report().field_ready
    assert offline_field_problems(ready_report(**{"Free storage": (WARN, "3 GB")})) == []
    system_default = (WARN, "macOS system default (chosen deliberately)",
                      {"acceptable": True})
    assert offline_field_problems(ready_report(**{"Selected audio input": system_default})) == []


@pytest.mark.parametrize("name", OFFLINE_REQUIRED)
@pytest.mark.parametrize("status,word", [(FAIL, "failed"), (WARN, "partial"),
                                         (SKIP, "not verified")])
def test_any_required_check_that_is_not_a_pass_is_named(name, status, word):
    problems = offline_field_problems(ready_report(**{name: (status, "why", {})}))
    assert problems == [f"{name}: {word} (why)"]


def test_a_missing_check_or_another_mode_is_not_ready():
    report = ready_report()
    report.checks = [c for c in report.checks if c.name != "Local translation smoke test"]
    assert offline_field_problems(report) == ["Local translation smoke test: not checked"]
    assert offline_field_problems(ready_report(mode=OperatingMode.ONLINE_SETUP))[0] \
        .startswith("Checked for Online / Setup")


@pytest.mark.parametrize("authorization,status,blocks,records", [
    ("granted", PASS, False, True),
    ("denied", FAIL, True, False),
    ("undetermined", WARN, True, True),
    ("unknown", WARN, False, True),
    (None, WARN, False, True),
])
def test_the_microphone_decision_on_macos(monkeypatch, authorization, status, blocks,
                                          records):
    monkeypatch.setattr(readiness.platform, "system", lambda: "Darwin")
    check = readiness._microphone_permission_check(authorization)
    assert check.status is status
    report = ready_report(**{"Microphone permission": (check.status, check.detail,
                                                       check.data)})
    assert bool(offline_field_problems(report)) is blocks
    assert report.can_record is records


def test_off_macos_the_permission_does_not_apply():
    check = readiness._microphone_permission_check("not-applicable")
    assert check.status is SKIP
    assert offline_field_problems(ready_report(**{"Microphone permission": (
        check.status, check.detail, check.data)})) == []


class FakeQtApp:
    def __init__(self, status):
        self.status, self.requested = status, []

    def checkPermission(self, permission):            # noqa: N802 - Qt's name
        return self.status

    def requestPermission(self, *a):                  # noqa: N802 - must not be used
        self.requested.append(a)


@pytest.mark.parametrize("qt_status,frozen,deployed,expected", [
    ("Granted", False, False, "granted"),
    ("Undetermined", False, False, "undetermined"),
    ("Denied", True, True, "denied"),
    ("Denied", False, False, "unknown"),      # a development run: no usage description
    ("Denied", True, False, "unknown"),       # packaged without the permission backend
])
def test_reading_the_decision_never_asks(monkeypatch, qt_status, frozen, deployed,
                                         expected):
    from PySide6 import QtCore

    monkeypatch.setattr(readiness.platform, "system", lambda: "Darwin")
    fake = FakeQtApp(getattr(QtCore.Qt.PermissionStatus, qt_status))
    monkeypatch.setattr(QtCore.QCoreApplication, "instance", staticmethod(lambda: fake))
    monkeypatch.setattr(QtCore.QThread, "isMainThread", staticmethod(lambda: True))
    monkeypatch.setattr(readiness.sys if hasattr(readiness, "sys") else __import__("sys"),
                        "frozen", frozen, raising=False)
    monkeypatch.setattr(readiness, "_permission_backend_deployed", lambda qtcore: deployed)
    assert readiness.microphone_authorization() == expected
    assert fake.requested == [], "a permission dialog was requested"


def test_off_the_gui_thread_it_does_not_guess(monkeypatch):
    from PySide6 import QtCore

    monkeypatch.setattr(readiness.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(QtCore.QCoreApplication, "instance",
                        staticmethod(lambda: FakeQtApp(QtCore.Qt.PermissionStatus.Granted)))
    monkeypatch.setattr(QtCore.QThread, "isMainThread", staticmethod(lambda: False))
    assert readiness.microphone_authorization() == "unknown"


# --- the translation route ---------------------------------------------------------

class FakeArgos:
    pairs = [("de", "en")]
    broken = ()
    tested = []
    constructed = 0

    def __init__(self, target_language, package_dir):
        type(self).constructed += 1

    def library_installed(self):
        return True

    def installed_pairs(self):
        return list(self.pairs)

    def direct_pairs(self):
        return list(self.pairs)

    def readiness(self):
        return {}

    def smoke_test(self, source, target):
        type(self).tested.append((source, target))
        if (source, target) in self.broken:
            raise RuntimeError("model would not load")
        return types.SimpleNamespace(text=f"[{source}->{target}] hello")


@pytest.fixture
def argos(monkeypatch, tmp_path):
    import babelfishr.modes as modes
    import babelfishr.providers.argos as argos_module

    FakeArgos.tested, FakeArgos.constructed, FakeArgos.broken = [], 0, ()
    FakeArgos.pairs = [("de", "en")]
    monkeypatch.setattr(argos_module, "ArgosTranslateEngine", FakeArgos)
    monkeypatch.setattr(modes, "bootstrap_environment", lambda config: None)
    config = Config()
    config.app_home = str(tmp_path / "home")
    config.translate.target_language = "en"
    return config


def _translation(config, smoke=True):
    report = ReadinessReport()
    readiness._check_translation(config, report, smoke)
    return report.get("Installed translation paths"), report.get("Local translation smoke test")


def test_spanish_is_not_vouched_for_by_a_german_pack(argos):
    argos.session.source_language_mode, argos.session.source_language = "specified", "es"
    paths, smoke = _translation(argos)
    assert paths.status is FAIL and "es->en" in paths.detail and "de->en" in paths.detail
    assert smoke.status is SKIP and FakeArgos.tested == [], "de->en was tested for es"


def test_the_selected_route_is_the_one_tested(argos):
    FakeArgos.pairs = [("de", "en"), ("es", "en")]
    argos.session.source_language_mode, argos.session.source_language = "specified", "es"
    paths, smoke = _translation(argos)
    assert paths.status is PASS and smoke.status is PASS
    assert FakeArgos.tested == [("es", "en")] and "es->en" in smoke.detail


def test_english_into_english_is_the_genuine_pass_through(argos):
    argos.session.source_language_mode, argos.session.source_language = "specified", "en"
    FakeArgos.pairs = []
    paths, smoke = _translation(argos)
    assert paths.status is PASS and smoke.status is PASS
    assert "pass-through" in smoke.detail and FakeArgos.constructed == 0
    argos.translate.skip_if_same_language = False          # then it is a real route
    paths, smoke = _translation(argos)
    assert paths.status is FAIL and smoke.status is SKIP


def test_automatic_names_the_routes_it_verified(argos):
    FakeArgos.pairs = [("de", "en"), ("es", "en"), ("en", "fr")]
    paths, smoke = _translation(argos)
    assert smoke.status is PASS
    assert sorted(FakeArgos.tested) == [("de", "en"), ("es", "en")]
    assert "verified de->en, es->en" in smoke.detail
    assert "other language is transcribed but not translated" in smoke.detail
    assert smoke.data["verified"] == ["de->en", "es->en"]


def test_automatic_with_one_broken_route_is_not_a_pass(argos):
    FakeArgos.pairs = [("de", "en"), ("es", "en")]
    FakeArgos.broken = (("es", "en"),)
    _, smoke = _translation(argos)
    assert smoke.status is FAIL
    assert "es->en" in smoke.detail and "verified: de->en" in smoke.detail


def test_without_smoke_tests_nothing_is_verified(argos):
    FakeArgos.pairs = [("de", "en")]
    _, smoke = _translation(argos, smoke=False)
    assert smoke.status is SKIP and FakeArgos.tested == []
    report = ready_report(**{"Local translation smoke test": (SKIP, "smoke tests disabled", {})})
    assert offline_field_problems(report) == [
        "Local translation smoke test: not verified (smoke tests disabled)"]
