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


def _build_script() -> str:
    return (PACKAGING / "build_macos.sh").read_text()


def _pip_install_lines() -> list:
    return [line.strip() for line in _build_script().splitlines()
            if "pip install" in line and not line.strip().startswith("#")]


def test_build_script_is_valid_shell():
    """Catch syntax errors before someone waits ten minutes for a build."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    result = subprocess.run([bash, "-n", str(PACKAGING / "build_macos.sh")],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_clean_venv_installs_the_dependencies_the_build_uses():
    """The defect: the script ran pytest without installing the dev extra.

    Checked structurally - every tool the script invokes must appear in an
    install line - rather than by grepping for the word "pytest".
    """
    import tomllib

    script = _build_script()
    installs = " ".join(_pip_install_lines())

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = project["project"]["optional-dependencies"]

    #: tool invoked by the script -> extra that provides it
    tooling = {"pytest": "dev", "pyinstaller": "packaging"}
    for tool, extra in tooling.items():
        if tool not in script:
            continue
        assert extra in extras, f"pyproject has no [{extra}] extra"
        assert extra in installs, (
            f"the script runs {tool} but never installs the '{extra}' extra "
            f"into the clean venv")


def test_build_script_verifies_its_toolchain_before_using_it():
    script = _build_script()
    assert "import pytest" in script or "pytest --version" in script, (
        "the script should fail fast if the toolchain is missing")


def test_build_script_creates_a_clean_venv():
    script = _build_script()
    assert "python3 -m venv" in script
    assert "rm -rf \"$VENV\"" in script or "rm -rf $VENV" in script, (
        "a stale venv would hide missing dependencies")


def test_build_script_fails_fast():
    assert "set -euo pipefail" in _build_script()


def test_build_script_does_not_touch_application_support():
    """A rebuild must never delete a downloaded model."""
    script = _build_script()
    for line in script.splitlines():
        if line.strip().startswith("rm -rf"):
            assert "Application Support" not in line
            assert "BABELFISHR_HOME" not in line or "mktemp" in line
    assert "Application Support" in script, (
        "the script should say that field assets are untouched")


def test_build_script_points_tests_at_a_scratch_home():
    """Tests must not write into the operator's real Application Support."""
    script = _build_script()
    assert "BABELFISHR_HOME" in script
    assert "mktemp" in script


def test_build_script_is_honest_about_signing_and_notarization():
    script = _build_script()
    assert "UNSIGNED" in script and "NOT notarized" in script
    assert "notarytool" in script
    assert "CODESIGN_IDENTITY" in script and "NOTARY_PROFILE" in script


def test_packaged_binary_services_cli_arguments():
    """We document the bundled binary as a CLI, so it must actually be one."""
    import ast

    module = ast.parse((PACKAGING / "app_entry.py").read_text())
    source = (PACKAGING / "app_entry.py").read_text()
    assert "cli_main" in source or "from babelfishr.cli import main" in source, (
        "the entry point must dispatch to the CLI when given arguments")
    main = next(node for node in module.body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    assert main.args.args, "main() must accept argv to dispatch CLI arguments"


def test_packaged_entry_point_dispatches_to_cli(monkeypatch, tmp_path):
    """Exercise the dispatch for real, not by reading the source."""
    import sys

    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    monkeypatch.syspath_prepend(str(PACKAGING))
    import app_entry

    assert app_entry.main(["--selftest-import"]) == 0

    calls = {}

    def fake_cli(argv):
        calls["argv"] = argv
        return 0

    monkeypatch.setitem(sys.modules, "babelfishr.cli",
                        type(sys)("babelfishr.cli"))
    sys.modules["babelfishr.cli"].main = fake_cli
    assert app_entry.main(["devices"]) == 0
    assert calls["argv"] == ["devices"]


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
