"""Static checks on the release supply chain.

The macOS jobs cannot run here (they need a macos-26 Apple Silicon runner and,
for Developer ID signing, repository secrets). What can be checked without
them is the shape that makes the pipeline trustworthy:

* least privilege: every job declares its permissions, and the release
  workflow's publish job is the only one that can write;
* no token left behind: every checkout uses persist-credentials: false, and
  the publish job does not check out or build anything;
* immutable actions: every ``uses:`` is a full 40-hex commit SHA;
* reproducible inputs: the Python lock pins every distribution with a hash,
  covers everything the build installs, and the build installs only from it;
* tested identity: the publish job re-hashes the downloaded DMG and compares
  it with the sha256 the build job reported before it publishes anything.

See packaging/SUPPLY_CHAIN.md.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "macos-release.yml"
PREVIEW = WORKFLOWS / "macos-preview.yml"
PACKAGING = ROOT / "packaging"
LOCK = PACKAGING / "requirements-macos-arm64.txt"
LOCK_INPUT = PACKAGING / "requirements-macos-arm64.in"
BUILD_SCRIPT = PACKAGING / "build_macos.sh"

pytestmark = pytest.mark.unit

ALL_WORKFLOWS = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _load(path: pathlib.Path) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(job: dict) -> list:
    return job.get("steps") or []


def _canonical(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_pins() -> dict:
    """name -> version for every requirement in the lock."""
    pins = {}
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if match:
            pins[_canonical(match.group(1))] = match.group(2)
    return pins


# ---- permissions ---------------------------------------------------------
def test_there_are_workflows_to_check():
    assert RELEASE in ALL_WORKFLOWS and PREVIEW in ALL_WORKFLOWS


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_every_job_declares_its_permissions(path):
    data = _load(path)
    top = data.get("permissions", {})
    assert top in ({}, {"contents": "read"}), (
        f"{path.name}: the workflow-level default grants {top!r}; grant "
        "nothing (or read) there and let each job ask for what it needs")
    for name, job in data["jobs"].items():
        assert isinstance(job.get("permissions"), dict), (
            f"{path.name}: job {name!r} inherits its token permissions instead "
            "of declaring them")


def test_only_the_publish_job_can_write():
    data = _load(RELEASE)
    writers = {}
    for name, job in data["jobs"].items():
        for scope, level in job["permissions"].items():
            if level == "write":
                writers.setdefault(name, []).append(scope)
    assert writers == {"publish": ["contents"]}, writers
    assert data["jobs"]["build"]["permissions"] == {"contents": "read"}


def test_the_preview_never_gets_a_write_token():
    data = _load(PREVIEW)
    assert data.get("permissions") == {"contents": "read"}
    for name, job in data["jobs"].items():
        assert job["permissions"] == {"contents": "read"}, name


# ---- credentials and checkouts -------------------------------------------
@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_every_checkout_drops_its_credentials(path):
    data = _load(path)
    checkouts = 0
    for name, job in data["jobs"].items():
        for step in _steps(job):
            if "actions/checkout" in str(step.get("uses", "")):
                checkouts += 1
                assert (step.get("with") or {}).get("persist-credentials") is False, (
                    f"{path.name}: job {name!r} leaves the token in .git/config")
    assert checkouts, f"{path.name} never checks out the source"


def test_the_publish_job_does_not_check_out_or_build():
    job = _load(RELEASE)["jobs"]["publish"]
    assert job.get("needs") in ("build", ["build"])
    for step in _steps(job):
        uses = str(step.get("uses", ""))
        run = str(step.get("run", ""))
        if "actions/checkout" in uses:
            assert (step.get("with") or {}).get("persist-credentials") is False
        for forbidden in ("build_macos.sh", "pip install", "pyinstaller",
                          "pytest", "make_dmg.sh", "sign_macos.sh"):
            assert forbidden not in run, (
                f"the publish job runs {forbidden}; only the read-only build "
                "job may execute code from the checkout")
    assert not any("actions/checkout" in str(s.get("uses", "")) for s in _steps(job)), (
        "the publish job needs nothing from the source tree")


# ---- pinned actions ------------------------------------------------------
@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
             if re.match(r"^\s*(-\s+)?uses:", line)]
    assert lines, f"{path.name} uses no actions?"
    for line in lines:
        assert re.fullmatch(
            r"(-\s+)?uses:\s*[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+#\s*v\d+(\.\d+)*", line), (
            f"{path.name}: {line!r} is not pinned to a full commit SHA with "
            "its version in a comment")


def test_one_version_of_each_action_is_used():
    shas = {}
    for path in ALL_WORKFLOWS:
        for action, sha in re.findall(r"uses:\s*([\w.-]+/[\w./-]+)@([0-9a-f]{40})",
                                      path.read_text(encoding="utf-8")):
            shas.setdefault(action, set()).add(sha)
    drift = {action: found for action, found in shas.items() if len(found) > 1}
    assert not drift, f"the same action is pinned to different commits: {drift}"


# ---- tested identity across the job boundary -----------------------------
def test_the_build_job_exports_the_dmg_sha256_it_computed():
    job = _load(RELEASE)["jobs"]["build"]
    assert job["outputs"]["dmg_sha256"] == "${{ steps.dmg.outputs.sha }}"
    collect = next(step for step in _steps(job) if step.get("id") == "dmg")
    body = collect["run"]
    assert 'shasum -a 256 "$DMG"' in body, (
        "the exported sha256 must be computed from the image, not copied "
        "from the .sha256 file")
    assert 'echo "sha=$SHA"' in body
    assert "does not match the image" in body


def test_the_publish_job_reverifies_the_dmg_before_publishing():
    job = _load(RELEASE)["jobs"]["publish"]
    steps = _steps(job)
    names = [str(step.get("name", "")) for step in steps]

    downloads = [i for i, step in enumerate(steps)
                 if "actions/download-artifact" in str(step.get("uses", ""))]
    dmg_download = next(i for i in downloads
                        if steps[i]["with"]["name"] == "BabelFishR-macOS-arm64-dmg")

    verify = next(i for i, step in enumerate(steps)
                  if "needs.build.outputs.dmg_sha256" in str(step.get("env", {})))
    body = steps[verify]["run"]
    assert 'sha256sum "$DMG"' in body
    assert '"$ACTUAL" != "$EXPECTED_SHA256"' in body and "exit 1" in body
    assert "^[0-9a-f]{64}$" in body, "an empty build output must not pass"

    publish = names.index("Publish the prerelease")
    assert dmg_download < verify < publish, names
    env = steps[publish]["env"]
    assert env["SHA256"] == "${{ needs.build.outputs.dmg_sha256 }}"
    assert env["GH_REPO"] == "${{ github.repository }}"
    assert env["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_the_publish_condition_and_release_commands_are_unchanged():
    job = _load(RELEASE)["jobs"]["publish"]
    condition = " ".join(job["if"].split())
    assert condition == (
        "startsWith(github.ref, 'refs/tags/v') || "
        "(github.event_name == 'workflow_dispatch' && "
        "inputs.publish_prerelease == 'true' && inputs.release_tag != '')")
    run = next(s for s in _steps(job) if s.get("name") == "Publish the prerelease")["run"]
    for required in ('gh release upload "$TAG"', 'gh release create "$TAG"',
                     '--title "BabelFishR $TAG (macOS Apple Silicon)"',
                     "--notes-file release-notes.md", "--prerelease",
                     '--target "$GITHUB_SHA"', "--clobber",
                     '"dist/BabelFishR-macOS-arm64.dmg"',
                     '"dist/BabelFishR-macOS-arm64.dmg.sha256"',
                     "build-reports/signing-status.txt"):
        assert required in run, required
    env = next(s for s in _steps(job) if s.get("name") == "Publish the prerelease")["env"]
    assert env["TAG"] == ("${{ startsWith(github.ref, 'refs/tags/v') && "
                          "github.ref_name || inputs.release_tag }}")


@pytest.mark.parametrize("path", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_no_input_is_spliced_into_a_shell_script(path):
    """Expressions go through env:, so a crafted value is data, not code."""
    data = _load(path)
    for name, job in data["jobs"].items():
        for step in _steps(job):
            run = str(step.get("run", ""))
            assert "${{" not in run, (
                f"{path.name}: job {name!r} step {step.get('name')!r} expands an "
                "expression directly inside its script")


def test_the_preview_keeps_its_artifacts():
    job = _load(PREVIEW)["jobs"]["build"]
    uploads = {step["with"]["name"]: step for step in _steps(job)
               if "actions/upload-artifact" in str(step.get("uses", ""))}
    assert set(uploads) == {"BabelFishR-macOS-arm64-dmg", "BabelFishR-macOS-reports"}
    assert "dist/BabelFishR-macOS-arm64.dmg.sha256" in uploads[
        "BabelFishR-macOS-arm64-dmg"]["with"]["path"]
    assert job["outputs"]["dmg_sha256"] == "${{ steps.build.outputs.dmg_sha256 }}"


# ---- the lock ------------------------------------------------------------
def test_every_lock_requirement_is_pinned_and_hashed():
    text = LOCK.read_text(encoding="utf-8")
    # Join continuation lines, drop comment lines, collapse whitespace.
    logical = [" ".join(line.split())
               for line in text.replace("\\\n", " ").splitlines()
               if not line.lstrip().startswith("#")]
    entries = [line for line in logical if line]
    assert len(entries) > 50, "the lock looks truncated"
    for entry in entries:
        assert not entry.startswith(("-e", "--editable", "-r", "-c", "--index-url",
                                     "--extra-index-url", "--find-links", "-f")), entry
        assert "file:" not in entry and "git+" not in entry and "://" not in entry, entry
        requirement, _, hashes = entry.partition(" --hash=")
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[^\s=]+", requirement), (
            f"not an exact pin: {requirement!r}")
        assert hashes, f"{requirement} has no --hash"
        for digest in ("--hash=" + hashes).split():
            assert re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", digest), digest


def test_the_lock_covers_everything_the_build_installs():
    """Every declared dependency the build pulls in, plus the build backend."""
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    match = re.search(r'-e "\.\[([a-z,]+)\]"', BUILD_SCRIPT.read_text(encoding="utf-8"))
    assert match, "build_macos.sh no longer installs the project with extras"
    wanted = list(project["project"]["dependencies"])
    for extra in match.group(1).split(","):
        wanted += extras[extra]
    wanted += project["build-system"]["requires"]

    pins = _lock_pins()
    for requirement in wanted:
        name = _canonical(re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0])
        assert name in pins, f"{requirement} is installed by the build but not locked"

    # The packages the release actually depends on, named explicitly so a
    # careless refresh that drops one fails here rather than on the runner.
    for name in ("pyinstaller", "pyinstaller-hooks-contrib", "pyside6",
                 "pyside6-essentials", "pyside6-addons", "shiboken6",
                 "faster-whisper", "ctranslate2", "argostranslate",
                 "sentencepiece", "numpy", "huggingface-hub", "sounddevice",
                 "tzdata", "certifi", "setuptools", "wheel", "pytest",
                 "pytest-timeout", "pyyaml"):
        assert name in pins, f"{name} is missing from the lock"


def test_the_lock_matches_the_client_api_the_tests_expect():
    """tests/test_download_real_hub.py targets the huggingface_hub 2.x API."""
    assert _lock_pins()["huggingface-hub"].split(".")[0] == "2"
    assert _lock_pins()["pyside6"].split(".")[0] == "6"


def test_the_lock_records_how_it_was_made():
    header = LOCK.read_text(encoding="utf-8").splitlines()[:3]
    assert any("packaging/refresh_macos_lock.sh" in line for line in header)
    refresh = (PACKAGING / "refresh_macos_lock.sh").read_text(encoding="utf-8")
    for flag in ("--generate-hashes", "--python-platform aarch64-apple-darwin",
                 "--python-version 3.12", "--only-binary :all:",
                 "packaging/requirements-macos-arm64.in",
                 "-o packaging/requirements-macos-arm64.txt"):
        assert flag in refresh, flag
    assert "-e .[gui,audio,asr,translate,dev,packaging]" in LOCK_INPUT.read_text(
        encoding="utf-8")


# ---- the build script installs only from the lock ------------------------
def _install_commands() -> list:
    text = BUILD_SCRIPT.read_text(encoding="utf-8").replace("\\\n", " ")
    return [" ".join(line.split()) for line in text.splitlines()
            if "pip install" in line and not line.strip().startswith("#")]


def test_the_build_installs_only_from_the_hashed_lock():
    commands = _install_commands()
    assert len(commands) == 2, commands
    lock, project = commands
    for flag in ("--require-hashes", "--no-deps", "--only-binary :all:",
                 '-r "$LOCK"'):
        assert flag in lock, f"the lock install lacks {flag}: {lock}"
    assert 'LOCK="packaging/requirements-macos-arm64.txt"' in BUILD_SCRIPT.read_text(
        encoding="utf-8")
    for flag in ("--no-deps", "--no-index", "--no-build-isolation", "-e"):
        assert flag in project, f"the project install lacks {flag}: {project}"
    for command in commands:
        assert "--upgrade" not in command and " -U " not in command, (
            f"an unpinned upgrade from the network: {command}")
    assert "python -m pip check" in BUILD_SCRIPT.read_text(encoding="utf-8")


# ---- every packaging script is valid shell -------------------------------
@pytest.mark.parametrize("script", sorted(PACKAGING.glob("*.sh")), ids=lambda p: p.name)
def test_packaging_scripts_parse(script):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    result = subprocess.run([bash, "-n", str(script)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_supply_chain_is_documented():
    doc = (PACKAGING / "SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    for topic in ("refresh_macos_lock.sh", "--require-hashes", "git ls-remote",
                  "persist-credentials", "contents: write", "macos-26",
                  "CODESIGN_IDENTITY", "45986512040", "140463426880",
                  "narrow_lock_hashes.py"):
        assert topic in doc, f"SUPPLY_CHAIN.md does not cover {topic}"


def test_the_hash_filter_keeps_only_wheels_the_mac_runner_can_install():
    """packaging/narrow_lock_hashes.py decides which hashes the lock admits."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "narrow_lock_hashes", PACKAGING / "narrow_lock_hashes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = [
        "numpy-2.5.5-cp312-cp312-macosx_14_0_arm64.whl",
        "pyside6_essentials-6.12.0-cp310-abi3-macosx_14_0_universal2.whl",
        "wrapt-2.5.2-cp312-cp312-macosx_11_0_arm64.whl",
        "tzdata-2026.6-py2.py3-none-any.whl",
        "torch-2.15.0-cp312-none-macosx_14_0_arm64.whl",
    ]
    drop = [
        "numpy-2.5.5-cp312-cp312-macosx_14_0_x86_64.whl",
        "numpy-2.5.5-cp313-cp313-macosx_14_0_arm64.whl",
        "numpy-2.5.5-cp312-cp312-manylinux_2_28_aarch64.whl",
        "numpy-2.5.5-cp312-cp312-win_arm64.whl",
        "numpy-2.5.5.tar.gz",
        "future-1.0-cp313-abi3-macosx_14_0_arm64.whl",
        "future-1.0-cp312-cp312-macosx_27_0_arm64.whl",
    ]
    assert all(module.compatible(name) for name in keep)
    assert not any(module.compatible(name) for name in drop)
    # The lock carries only a handful of hashes per pin, not every platform's.
    text = LOCK.read_text(encoding="utf-8")
    assert text.count("--hash=") < 2 * len(_lock_pins())
