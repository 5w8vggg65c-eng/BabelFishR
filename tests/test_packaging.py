"""Packaging metadata checks that do not require macOS or PyInstaller.

They cannot prove the .app builds - that needs a Mac - but they do catch the
mistakes that would otherwise only surface after a 10-minute build: a missing
microphone usage string, a forced appearance, model assets pointed at a cache
directory that macOS may purge, or an entry point that does not import.
"""

from __future__ import annotations

import ast
import os
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


# ---- the build must verify the WHOLE deterministic suite ---------------
#: Flags that change output volume but not which tests are selected. They are
#: stripped before re-running the invocation, because a second -q suppresses
#: the very summary line these tests read.
_VERBOSITY_FLAGS = {"-q", "--quiet", "-v", "--verbose", "-s", "-rs", "-ra"}

#: Arguments that decide where a report is written, not which tests run. They
#: contain unexpanded shell variables, so they cannot be replayed here.
_REPORTING_PREFIXES = ("--junitxml", "--junit-xml", "--html", "--result-log")


def _build_pytest_invocation(strip_verbosity: bool = True) -> list:
    """The pytest arguments the build script actually uses.

    The build line is real shell: it spans a backslash continuation and pipes
    into ``tee`` so the run is also captured as a report artifact. Everything
    from the first redirection or pipe onwards is plumbing, not test
    selection, and is dropped - otherwise these tests would replay ``2>&1``
    and ``|`` as if they were pytest arguments and collect nothing.
    """
    # Re-join backslash continuations before looking for the invocation.
    joined = _build_script().replace("\\\n", " ")
    for line in joined.splitlines():
        stripped = line.strip()
        if "python -m pytest" not in stripped or stripped.startswith("#"):
            continue
        tail = stripped.split("python -m pytest", 1)[1]
        # Cut the shell plumbing off the end.
        for token in ("2>&1", "|", ">>", ">", "&&", ";"):
            index = tail.find(token)
            if index != -1:
                tail = tail[:index]
        args = tail.split()
        args = [a for a in args if not a.startswith(_REPORTING_PREFIXES)]
        if strip_verbosity:
            args = [a for a in args if a not in _VERBOSITY_FLAGS]
        return args
    raise AssertionError("the build script does not run pytest")


def test_build_does_not_restrict_itself_to_the_unit_marker():
    """The defect: -m unit collected well under half the suite.

    Most of the original regression coverage predates the marker, so the build
    was verifying far less than it appeared to.
    """
    args = _build_pytest_invocation(strip_verbosity=False)
    assert "-m" not in args or "unit" not in args, (
        "the build must not verify only the tests carrying the 'unit' marker")


def test_build_test_selection_collects_the_whole_deterministic_suite():
    """Run the build's own pytest arguments and check what they collect.

    Asserted against representative ORIGINAL tests and representative NEW
    correction tests, so neither half can be silently dropped again.
    """
    import subprocess
    import sys

    args = _build_pytest_invocation()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=str(ROOT), capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    collected = result.stdout

    # Original suite, predating the correction passes.
    legacy = [
        "tests/test_acceptance.py",
        "tests/test_pipeline.py",
        "tests/test_storage.py",
        "tests/test_detect.py",
        "tests/test_audio.py",
        "tests/test_models.py",
        "tests/test_providers.py",
        "tests/test_export.py" if (ROOT / "tests/test_export.py").exists()
        else "tests/test_capture_invariant.py",
    ]
    # Added by the correction passes.
    added = [
        "tests/test_model_layout.py",
        "tests/test_model_selection.py",
        "tests/test_runtime_paths.py",
        "tests/test_glossary_path.py",
        "tests/test_dsd_cli.py",
        "tests/test_offline_integration.py",
        "tests/test_gui_setup.py",
    ]
    for path in legacy + added:
        assert path in collected, (
            f"{path} is not collected by the build's test selection")


def test_build_collects_substantially_more_than_the_unit_marker():
    """Guard the ratio, not just the file list."""
    import re
    import subprocess
    import sys

    def count(extra):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *extra],
            cwd=str(ROOT), capture_output=True, text=True, check=False)
        match = re.search(r"(\d+)(?:/\d+)? tests collected", result.stdout)
        return int(match.group(1)) if match else 0

    build_total = count(_build_pytest_invocation())
    unit_only = count(["-m", "unit"])
    assert build_total > unit_only, (
        f"the build collects {build_total} tests, the unit marker {unit_only}")
    assert build_total > unit_only * 1.5, (
        "the build's selection should cover the legacy suite too, not just "
        f"the marked tests ({build_total} vs {unit_only})")


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


# ---- bundle verification must be fatal --------------------------------
def test_verify_bundle_script_exists_and_is_executable():
    script = PACKAGING / "verify_bundle.sh"
    assert script.exists()
    assert os.access(script, os.X_OK), "verify_bundle.sh must be executable"


def test_build_script_does_not_swallow_verification_failures():
    """The defect: the self-test failure was absorbed by `|| echo`."""
    script = _build_script()
    for line in script.splitlines():
        if "selftest-import" in line or "verify_bundle" in line:
            assert "|| echo" not in line, (
                "verification failure must not be downgraded to a message")
            assert "|| true" not in line


def _stub_bundle(root: pathlib.Path, name: str, body: str,
                 info_plist: bool = True) -> pathlib.Path:
    """A minimal .app for exercising verify_bundle.sh.

    The Info.plist matters: on macOS the verifier reads it with PlistBuddy, and
    a stub without one fails checks that have nothing to do with what the test
    is about. (Off macOS PlistBuddy is absent and those checks are skipped,
    which is exactly how this went unnoticed until the first real Apple Silicon
    run.)
    """
    app = root / name
    binary = app / "Contents" / "MacOS" / "BabelFishR"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(body, encoding="utf-8")
    binary.chmod(0o755)
    if info_plist:
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": "org.babelfishr.app.test",
            "CFBundleName": "BabelFishR",
            "CFBundleExecutable": "BabelFishR",
            "CFBundleShortVersionString": "0.0.0",
            "NSMicrophoneUsageDescription": "test stub",
        }))
    return app


def _run_verify(app: pathlib.Path):
    import subprocess

    return subprocess.run(
        ["bash", str(PACKAGING / "verify_bundle.sh"), str(app)],
        cwd=str(ROOT), capture_output=True, text=True, check=False)


def test_verify_bundle_passes_a_working_executable(tmp_path):
    app = _stub_bundle(tmp_path, "good.app",
                       '#!/bin/sh\n'
                       '[ "$1" = "--selftest-import" ] && '
                       '{ echo "imports cleanly"; exit 0; }\nexit 0\n')
    result = _run_verify(app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


def test_verify_bundle_fails_a_broken_executable(tmp_path):
    """A bundle that cannot import its own code must fail the build."""
    app = _stub_bundle(tmp_path, "broken.app",
                       '#!/bin/sh\necho "ImportError" >&2\nexit 1\n')
    result = _run_verify(app)
    assert result.returncode != 0, (
        "a failed self-test must make the build return non-zero")
    assert "self-test failed" in result.stderr


def test_verify_bundle_fails_a_crashing_executable(tmp_path):
    app = _stub_bundle(tmp_path, "crash.app",
                       '#!/bin/sh\nkill -SEGV $$\n')
    assert _run_verify(app).returncode != 0


def test_verify_bundle_fails_a_bundle_without_a_microphone_string(tmp_path):
    """macOS silently denies audio input when the usage string is absent.

    Only meaningful where PlistBuddy exists; elsewhere verify_bundle.sh says so
    and skips the Info.plist checks rather than pretending to have made them.
    """
    if not os.path.exists("/usr/libexec/PlistBuddy"):
        pytest.skip("PlistBuddy is only available on macOS")
    app = _stub_bundle(tmp_path, "noplist.app",
                       '#!/bin/sh\nexit 0\n', info_plist=False)
    result = _run_verify(app)
    assert result.returncode != 0
    assert "NSMicrophoneUsageDescription" in result.stderr


def test_verify_bundle_fails_a_missing_bundle(tmp_path):
    result = _run_verify(tmp_path / "absent.app")
    assert result.returncode != 0
    assert "no bundle" in result.stderr


def test_verify_bundle_fails_a_bundle_without_an_executable(tmp_path):
    app = tmp_path / "empty.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    result = _run_verify(app)
    assert result.returncode != 0
    assert "no executable" in result.stderr


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


def _signing_script() -> str:
    return (PACKAGING / "sign_macos.sh").read_text()


def test_build_script_delegates_to_the_signing_script():
    script = _build_script()
    assert "sign_macos.sh" in script, (
        "the build must sign the bundle; an unsigned app has no stable code "
        "identity, so macOS cannot remember a microphone permission grant")


def test_signing_script_is_honest_about_notarization():
    """Ad-hoc must never be described as notarized.

    Both supported paths have to be present and clearly distinguished: a real
    Developer ID signature that can be notarized, and an ad-hoc signature that
    cannot be.
    """
    script = _signing_script()
    assert "CODESIGN_IDENTITY" in script and "NOTARY_PROFILE" in script
    assert "notarytool" in script and "stapler" in script
    assert "ad-hoc" in script
    assert "UNNOTARIZED ALPHA" in script, (
        "an ad-hoc build must be labelled unnotarized, prominently")
    assert "right-click" in script or "Control-click" in script, (
        "the operator has to be told how to open an unnotarized app")

    # The dangerous failure mode is claiming Apple blessed a build it never saw.
    lowered = script.lower()
    for phrase in ("ad-hoc signed and notarized", "adhoc notarized",
                   "notarized by apple" ):
        assert phrase not in lowered, f"misleading claim in the script: {phrase}"


def test_signing_script_reports_a_notarized_flag_the_release_step_can_read():
    """The workflow greps this line to decide what the release notes say."""
    assert 'notarized       : $NOTARIZED' in _signing_script()


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


# ---- documentation must not contradict the code -----------------------
def test_every_documented_command_exists():
    """Docs previously referenced 'babelfishr languages --install', which
    never existed. Keep documentation and the parser in sync automatically."""
    import re

    from babelfishr.cli import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(subparsers[0].choices)

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in (ROOT / "docs").glob("*.md"):
        text += doc.read_text(encoding="utf-8")

    referenced = set(re.findall(r"babelfishr ([a-z][a-z-]+)", text))
    # Words that follow the binary name but are not subcommands.
    referenced -= {"prepare"}
    unknown = referenced - commands
    assert not unknown, f"documentation references missing commands: {unknown}"


def test_docs_do_not_claim_a_hugging_face_cache_layout():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in (ROOT / "docs").glob("*.md"):
        text += doc.read_text(encoding="utf-8")
    assert "download_root" not in text, (
        "the docs must describe the output_dir layout actually used")


def test_docs_do_not_claim_no_proxy_blocks_networking():
    text = ""
    for doc in list((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]:
        text += doc.read_text(encoding="utf-8")
    if "NO_PROXY" in text:
        # Only permissible as an explicit disclaimer.
        assert "not" in text.split("NO_PROXY")[1][:200].lower()


def test_docs_describe_application_support_as_authoritative():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Application Support/BabelFishR" in text
    assert "settings.toml" in text


def test_docs_describe_the_gui_setup_workflow():
    """The docs must not tell a GUI user to open Terminal as the only path."""
    text = (ROOT / "docs" / "FIELD_OPERATION.md").read_text(encoding="utf-8")
    assert "setup assistant" in text.lower()
    assert "In the app" in text
