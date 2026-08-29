"""Checks on the release pipeline and the documentation a downloader reads.

None of this can build a .app - that needs a Mac. What it can do is catch the
mistakes that would otherwise only surface after a ten-minute build on a
runner, and stop the documentation drifting away from what the pipeline
actually does.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
WORKFLOW = ROOT / ".github" / "workflows" / "macos-release.yml"

pytestmark = pytest.mark.unit


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _readme_flat() -> str:
    """The README with wrapping collapsed, for checking whole sentences."""
    return " ".join(_readme().split())


# ---- the workflow ------------------------------------------------------
def test_the_release_workflow_exists_and_parses():
    assert WORKFLOW.exists(), "there is no macOS release workflow"
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    job = data["jobs"]["build"]
    assert job["steps"], "the build job has no steps"


def test_the_workflow_can_be_run_by_hand_and_by_tag():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    # PyYAML reads the bare key `on:` as the boolean True.
    triggers = data.get("on") or data.get(True)
    assert "workflow_dispatch" in triggers
    assert any(pattern.startswith("v") for pattern in triggers["push"]["tags"])


def test_the_first_step_refuses_a_non_apple_silicon_runner():
    """An Intel build labelled arm64 would be worse than no build at all."""
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    steps = data["jobs"]["build"]["steps"]
    first = steps[0]
    assert "arm64" in first["name"] or "Apple Silicon" in first["name"]
    body = first["run"]
    assert "uname -m" in body
    assert 'ARCH" != "arm64"' in body and "exit 1" in body, (
        "the architecture gate has to be fatal, not a warning")

    # And it has to come before anything is built.
    build = next(i for i, step in enumerate(steps)
                 if "build_macos.sh" in str(step.get("run", "")))
    assert build > 0


def test_the_runner_is_an_apple_silicon_label():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    runs_on = str(data["jobs"]["build"]["runs-on"])
    assert "macos" in runs_on


def test_the_workflow_pins_python_312():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    versions = [str(step.get("with", {}).get("python-version"))
                for step in data["jobs"]["build"]["steps"]
                if "setup-python" in str(step.get("uses", ""))]
    assert versions == ["3.12"]


def test_the_workflow_builds_verifies_and_packages():
    body = _workflow()
    for required in ("packaging/build_macos.sh", "packaging/verify_bundle.sh",
                     "BabelFishR-macOS-arm64.dmg",
                     "BabelFishR-macOS-arm64.dmg.sha256"):
        assert required in body, f"the workflow never mentions {required}"


def test_the_workflow_uploads_the_image_and_the_reports():
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_workflow())
    uploads = [step for step in data["jobs"]["build"]["steps"]
               if "upload-artifact" in str(step.get("uses", ""))]
    assert len(uploads) >= 2
    paths = " ".join(str(step["with"]["path"]) for step in uploads)
    assert "BabelFishR-macOS-arm64.dmg" in paths
    assert "build-reports" in paths
    # A missing DMG must fail the run rather than uploading nothing quietly.
    dmg_upload = next(step for step in uploads
                      if "dmg" in str(step["with"]["path"]))
    assert dmg_upload["with"]["if-no-files-found"] == "error"


def test_a_tag_publishes_a_prerelease_not_a_release():
    body = _workflow()
    assert "--prerelease" in body
    assert "gh release create" in body


def test_the_release_notes_never_claim_notarization_that_did_not_happen():
    body = _workflow()
    assert "UNNOTARIZED ALPHA" in body
    assert "$NOTARIZED" in body, (
        "the notes must be written from the signing report, not assumed")
    assert "notarized by Apple. It opens normally" in body


# ---- the scripts -------------------------------------------------------
@pytest.mark.parametrize("script", [
    "build_macos.sh", "verify_bundle.sh", "verify_independence.sh",
    "sign_macos.sh", "make_dmg.sh",
])
def test_every_packaging_script_is_valid_shell(script):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    path = PACKAGING / script
    assert path.exists(), f"packaging/{script} is missing"
    result = subprocess.run([bash, "-n", str(path)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_workflow_uses_maintained_action_versions():
    """Deprecated Node runtimes produce a warning annotation on every run.

    Pinned by major version deliberately: a floating ref would let a third
    party change what runs in the release pipeline.
    """
    import re

    body = _workflow()
    used = dict(re.findall(r"uses:\s*(actions/[a-z-]+)@v(\d+)", body))
    minimums = {"actions/checkout": 5, "actions/setup-python": 6,
                "actions/upload-artifact": 5}
    for action, minimum in minimums.items():
        assert action in used, f"{action} is not used"
        assert int(used[action]) >= minimum, (
            f"{action}@v{used[action]} is below the maintained v{minimum}")


def test_the_workflow_exercises_coreaudio_in_the_built_bundle():
    body = _workflow()
    assert "CoreAudio" in body
    independence = (PACKAGING / "verify_independence.sh").read_text()
    assert "--selftest-coreaudio" in independence
    assert "does NOT prove audio capture works" in independence


def test_the_independence_check_scrubs_the_environment():
    """Otherwise it proves nothing: the build machine would satisfy imports."""
    body = (PACKAGING / "verify_independence.sh").read_text(encoding="utf-8")
    assert "env -i" in body
    for variable in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        assert variable in body
    assert "cd /" in body, "it must run from outside the repository"
    for flag in ("--version", "--help", "--selftest-import",
                 "--selftest-independence", "--selftest-gui",
                 "--selftest-coreaudio"):
        assert flag in body


def test_the_independence_check_looks_for_build_machine_library_paths():
    body = (PACKAGING / "verify_independence.sh").read_text(encoding="utf-8")
    assert "otool -L" in body
    assert "/usr/lib/" in body and "/System/" in body, (
        "OS libraries are fine; anything else absolute is not")


def test_the_dmg_contains_an_applications_shortcut_and_a_checksum():
    body = (PACKAGING / "make_dmg.sh").read_text(encoding="utf-8")
    assert "ln -s /Applications" in body
    assert "hdiutil create" in body
    assert "shasum -a 256" in body
    # Mounted and inspected before anyone downloads it.
    assert "hdiutil attach" in body and "hdiutil detach" in body


def test_the_entry_point_supports_the_checks_the_scripts_run():
    import ast

    source = (PACKAGING / "app_entry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {node.name for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef)}
    assert {"_selftest_independence", "_selftest_gui",
            "_selftest_coreaudio"} <= names
    for flag in ("--selftest-import", "--selftest-independence",
                 "--selftest-gui", "--selftest-coreaudio"):
        assert flag in source


def test_the_independence_selftest_requires_the_engines_and_the_gui():
    """A bundle without PySide6 or CTranslate2 is not the shipped application."""
    import ast
    import sys

    sys.path.insert(0, str(PACKAGING))
    try:
        import app_entry
    finally:
        sys.path.pop(0)

    required = set(app_entry.REQUIRED_MODULES)
    for module in ("PySide6.QtWidgets", "numpy", "sounddevice",
                   "faster_whisper", "ctranslate2", "argostranslate",
                   "babelfishr.ui.main_window"):
        assert module in required, f"{module} is not checked for"
    assert ast.parse((PACKAGING / "app_entry.py").read_text())


# ---- the documentation a downloader reads ------------------------------
def test_the_readme_starts_with_the_download_section():
    """A non-developer must not have to scroll past pip to find the app."""
    text = _readme()
    download = text.index("## Download BabelFishR for macOS")
    developer = text.index("# Developer setup")
    assert download < developer

    for early_marker in ("pip install", "python3 -m venv", "git clone"):
        first = text.find(early_marker)
        assert first == -1 or first > developer, (
            f"'{early_marker}' appears before the developer section")


def test_the_readme_gives_the_eight_installation_steps():
    text = _readme()
    section = text[text.index("## Download BabelFishR for macOS"):
                   text.index("## Choosing your audio input")]
    for step in ("BabelFishR-macOS-arm64.dmg", "Applications", "audio-input",
                 "Choose your input", "Prepare", "Field Check", "offline"):
        assert step in section, f"the install steps never mention {step}"
    assert "Releases" in section


def test_the_readme_explains_the_audio_input_situations():
    text = _readme()
    section = text[text.index("## Choosing your audio input"):]
    for topic in ("MacBook microphone", "USB audio interface",
                  "Analogue radio audio through a USB interface",
                  "Native USB audio from a radio", "SDR input"):
        assert topic in section, f"the input guide never covers {topic}"


def test_the_readme_says_the_app_does_not_identify_a_radio_by_itself():
    text = _readme_flat()
    assert "It does not detect radios." in text
    assert ("Connecting a cable to your Mac does not tell the application "
            "anything about what is on the other end of it.") in text


def test_the_readme_is_honest_about_notarization():
    text = _readme()
    assert "not notarized" in text
    assert "right-click" in text.lower() or "control-click" in text.lower()
    assert "If a release does not say" in text


def test_the_readme_states_the_hardware_that_was_never_tested():
    text = _readme()
    section = text[text.index("### NOT tested"):]
    for item in ("FalconClaw", "USB audio interface", "DSD-neo", "SDR",
                 "M5 MacBook Air"):
        assert item in section, f"the untested list omits {item}"


def test_the_readme_does_not_promise_translation_of_any_language():
    text = _readme().lower()
    assert "does not claim \"any language\"" in text or \
           'does not claim "any language"' in text
    assert "only the pairs you install" in text


def test_the_readme_does_not_require_a_terminal_for_normal_use():
    text = _readme_flat()
    section = text[:text.index("# Developer setup")]
    assert "You do not need Python, Terminal, this repository" in section
    assert "You never need a terminal for any of it." in section
