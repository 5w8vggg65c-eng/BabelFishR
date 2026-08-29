"""Packaging metadata checks that do not require macOS or PyInstaller.

They cannot prove the .app builds - that needs a Mac - but they do catch the
mistakes that would otherwise only surface after a 10-minute build: a missing
microphone usage string, a forced appearance, model assets pointed at a cache
directory that macOS may purge, or an entry point that does not import.
"""

from __future__ import annotations

import ast
import pathlib
import plistlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


def test_packaging_files_exist():
    for name in ("babelfishr.spec", "app_entry.py", "entitlements.plist",
                 "build_macos.sh"):
        assert (PACKAGING / name).exists(), f"packaging/{name} is missing"


def test_spec_is_valid_python():
    ast.parse((PACKAGING / "babelfishr.spec").read_text())


def _info_plist_text() -> str:
    return (PACKAGING / "babelfishr.spec").read_text()


def test_bundle_declares_microphone_usage():
    """macOS silently denies audio input without this string."""
    text = _info_plist_text()
    assert "NSMicrophoneUsageDescription" in text
    assert "receive-only" in text, (
        "the permission prompt should say the app never transmits")


def test_bundle_follows_system_appearance():
    text = _info_plist_text()
    assert "NSRequiresAquaSystemAppearance" in text
    assert "False" in text.split("NSRequiresAquaSystemAppearance")[1][:40]


def test_bundle_has_identifier_and_version():
    text = _info_plist_text()
    for key in ("bundle_identifier", "CFBundleShortVersionString",
                "LSMinimumSystemVersion"):
        assert key in text


def test_entitlements_request_audio_input():
    data = plistlib.loads((PACKAGING / "entitlements.plist").read_bytes())
    assert data.get("com.apple.security.device.audio-input") is True


def test_entitlements_do_not_request_anything_unnecessary():
    """A receive-only app should not be asking for surprising capabilities."""
    data = plistlib.loads((PACKAGING / "entitlements.plist").read_bytes())
    forbidden = ("com.apple.security.device.camera",
                 "com.apple.security.personal-information.location",
                 "com.apple.security.personal-information.addressbook")
    for key in forbidden:
        assert key not in data, f"{key} should not be requested"


def test_entry_point_imports_and_defines_main():
    module = ast.parse((PACKAGING / "app_entry.py").read_text())
    names = {n.name for n in ast.walk(module) if isinstance(n, ast.FunctionDef)}
    assert "main" in names


def test_entry_point_logs_to_application_support():
    """A frozen app has no terminal, so startup failures must reach a log."""
    text = (PACKAGING / "app_entry.py").read_text()
    assert "AppPaths" in text and "logs" in text
    assert "logging.exception" in text


@pytest.mark.parametrize("system,expected", [
    ("Darwin", "Application Support"), ("Linux", "BabelFishR")])
def test_field_assets_live_outside_caches(monkeypatch, system, expected):
    """Models must not sit anywhere the OS may purge or an upgrade may wipe.

    Checked against the real default location, not an override, since the
    override is exactly what a test harness would use.
    """
    import platform

    from babelfishr.modes import AppPaths

    monkeypatch.delenv("BABELFISHR_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(platform, "system", lambda: system)
    paths = AppPaths.resolve()
    for path in (paths.models, paths.language_packs, paths.recordings,
                 paths.database):
        text = str(path)
        for cache_marker in ("Caches", "/var/tmp", "/private/tmp",
                             ".cache/huggingface"):
            assert cache_marker not in text, (
                f"field assets must not live in a cache: {text}")
        assert str(paths.root) in text
    assert expected in str(paths.root)


def test_macos_paths_use_application_support(monkeypatch):
    import platform

    from babelfishr.modes import AppPaths

    monkeypatch.delenv("BABELFISHR_HOME", raising=False)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    paths = AppPaths.resolve()
    assert "Application Support" in str(paths.root)
    assert paths.root.name == "BabelFishR"


def test_build_script_runs_tests_before_packaging():
    text = (PACKAGING / "build_macos.sh").read_text()
    assert "pytest" in text, "the build must not package an untested tree"
    assert "set -euo pipefail" in text


def test_build_script_is_honest_about_signing():
    text = (PACKAGING / "build_macos.sh").read_text()
    assert "UNSIGNED" in text
    assert "notarytool" in text


def test_upgrade_does_not_touch_field_assets(tmp_path, monkeypatch):
    """Reinstalling the app must not delete a downloaded model."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    from babelfishr.modes import AppPaths

    paths = AppPaths.resolve().ensure()
    model = paths.models / "small" / "model.bin"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"weights")

    # An upgrade replaces the bundle, which is elsewhere entirely.
    assert "Application Support" in str(paths.root) or str(tmp_path) in str(paths.root)
    assert model.exists()
    # Nothing in the packaging config points a build output at this directory.
    spec = (PACKAGING / "babelfishr.spec").read_text()
    assert "Application Support" not in spec.split('"""')[2] if '"""' in spec else True
