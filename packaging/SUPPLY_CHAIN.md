# Release supply chain

What the macOS build installs and runs, where it comes from, and what a
reviewer can and cannot check without a Mac.

## Jobs and tokens

`.github/workflows/macos-release.yml` has two jobs:

| job | token | does |
| --- | --- | --- |
| `build` (macos-26, Apple Silicon) | `contents: read` | checks out with `persist-credentials: false`, installs the hash-pinned lock, runs the full test suite, builds with PyInstaller, signs (Developer ID if the secrets exist, ad hoc if not), builds the DMG and its `.sha256`, and uploads them. It hashes the DMG itself and exports the result as the job output `dmg_sha256`. |
| `publish` (ubuntu-latest) | `contents: write` | runs only for a `v*` tag push, or a manual run with `publish_prerelease=true` and a `release_tag`. It needs `build`, has no checkout and runs no code from the repository. It downloads the DMG artifact and the reports artifact, then re-hashes the DMG. If that hash differs from `needs.build.outputs.dmg_sha256`, or from the `.sha256` file, it stops. Otherwise it publishes the prerelease with the same tag, asset names, title and notes as before. |

The workflow-level default is `permissions: {}`, so a new job gets nothing
unless it asks. `publish` is the only job with `contents: write`.
`.github/workflows/macos-preview.yml` (the `[build macOS preview]` build) is
`contents: read` throughout and never publishes. Its artifacts are still
`BabelFishR-macOS-arm64-dmg` and `BabelFishR-macOS-reports`, and it also
exports `dmg_sha256`.

## Pinned actions

Every `uses:` is a full 40-hex commit SHA, with the tag in a comment. Each
SHA came from the action's own repository over git, and then the commit was
fetched to confirm it is a commit object:

```
git ls-remote --tags https://github.com/actions/checkout 'refs/tags/v5*'
git init -q tmp && git -C tmp fetch --depth=1 https://github.com/actions/checkout <sha> \
  && git -C tmp cat-file -t <sha>        # -> commit
```

| action | tag | commit |
| --- | --- | --- |
| actions/checkout | v5.1.0 | fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 |
| actions/setup-python | v6.3.0 | ece7cb06caefa5fff74198d8649806c4678c61a1 |
| actions/upload-artifact | v5.0.0 | 330a01c490aca151604b8cf639adc76d48f6c5d4 |
| actions/download-artifact | v6.0.0 | 018cc2cf5baa6db3ef3c5f8a56943fffe632ef53 |

If a tag is annotated, `git ls-remote` prints two lines for it. Use the
commit from the peeled `^{}` line, not the tag object. To update an action,
repeat these steps for the new tag and change the SHA and the comment
together. Every workflow must use the same SHA for a given action.

## The Python lock

`packaging/build_macos.sh` installs dependencies in three steps:

```
python -m pip install --require-hashes --no-deps --only-binary :all: -r packaging/requirements-macos-arm64.txt
python -m pip install --no-deps --no-index --no-build-isolation -e ".[gui,audio,asr,translate,dev,packaging]"
python -m pip check
```

The first step refuses any file whose sha256 is not in the lock, resolves
nothing beyond it, and never runs an sdist's build code. The second installs
the project from the checkout. It cannot download anything, and it builds with
the locked `setuptools` and `wheel`. `pip check` then confirms the lock is
complete. pip itself is the copy bundled with the setup-python CPython 3.12
that created the venv. It is not upgraded from the network, because the
earlier `pip install --upgrade pip wheel` was unpinned.

**Target:** macOS arm64, CPython 3.12. The lock is not meant for any other
platform.

**How it was produced** (2026-11-27). From the repository root, on any OS:

```
packaging/refresh_macos_lock.sh
```

This runs `uv pip compile packaging/requirements-macos-arm64.in
--python-platform aarch64-apple-darwin --python-version 3.12 --only-binary :all:
--generate-hashes --no-emit-package babelfishr` with
`MACOSX_DEPLOYMENT_TARGET=26.0`. uv takes every version and sha256 from PyPI,
and none were typed by hand. The deployment target matches the macos-26
runner, so uv resolves what pip on that runner resolves. With uv's default
target of macOS 12, the result is PySide6 6.11.3 and torch 2.10, not
PySide6 6.12.0 and torch 2.15. Then `narrow_lock_hashes.py` keeps only the hashes of wheels a macOS
arm64 CPython 3.12 interpreter can install (cp312, abi3 or pure-Python;
`arm64` or `universal2`; macOS <= 26.0). It keeps a hash only when it
appears in both uv's output and PyPI's JSON API
(`https://pypi.org/pypi/<name>/<version>/json`) for that exact file. It fails
if a pin is left without a hash. The lock has 92 pins and 99 sha256 hashes (91 pins from
the provenance below, plus socksio - see the next paragraph).

**Provenance of the versions.** They are the exact versions the official Mac
preview build resolved and tested. The source is GitHub Actions run
45986512040, job 140463426880 (macOS operator preview, commit b3f64e8,
success), on macos-26 arm64 with Python 3.12.10. The run's reports artifact
(id 15192313755, `build-environment.txt`) could not be downloaded from here,
because the artifact storage host is not reachable. The versions were
therefore read from the job log, through the GitHub API:

- pip's `Successfully installed ...` line (log line 615) lists 91
  third-party distributions plus the local `babelfishr`. Every name and
  version is identical to the lock.
- The only other line is the earlier `pip install --upgrade pip wheel`,
  which installed pip-26.3, packaging-26.4 and wheel-0.48.0. packaging and
  wheel are locked at those versions. pip is deliberately no longer upgraded
  (see above), and the runner's bundled pip is 25.0.1.
- The log also names each wheel file pip downloaded or took from its cache.
  Apart from pip itself, those 91 filenames are identical to the 91 files
  that check 1 below downloaded, so the hashes in the lock are the hashes of
  the exact files that run installed.

**Addition after Preview 11: socksio 1.0.0.** On a Mac whose network settings
supply a SOCKS proxy (ALL_PROXY=socks5h://...), the packaged CLI could not
install any model: huggingface_hub's client (httpx2/httpcore2) refuses to start
without socksio. `socksio>=1.0,<2` was added to the `asr` extra (and `all`),
and the lock regenerated with `refresh_macos_lock.sh`. The only change to
the lock was the new pin: every existing version and hash stayed the same.
Its single hash is that of `socksio-1.0.0-py3-none-any.whl`, matching PyPI's
JSON API, and a `--require-hashes` install of it succeeded. No Mac run has
installed it yet; the next preview run is the first to do so.

uv was not told to use these versions: an independent resolution today, for
the same target, produced the same set. The input file
`requirements-macos-arm64.in` is the project with the extras the build
installs, plus `setuptools` and `wheel`. Dependencies arrive through those
extras, for example torch and spacy through argostranslate and stanza, and
huggingface_hub through faster-whisper. The `cloud` extra is not used by the
build and is not locked.

**How it was checked** (on Linux, without a Mac):

1. Every wheel was downloaded for the exact target with
   `pip download --require-hashes --no-deps --only-binary :all: --platform macosx_26_0_arm64 --python-version 3.12 --implementation cp --abi cp312 -r packaging/requirements-macos-arm64.txt`.
   All 91 downloaded and passed the hash check. The check was repeated
   offline after the hashes were narrowed.
2. Each downloaded wheel's sha256 was compared with the digest in PyPI's JSON
   API (`https://pypi.org/pypi/<name>/<version>/json`). There were 0
   mismatches.
3. Resolving the `.in` file offline (`--no-index --find-links <those wheels>`)
   gave exactly the lock, so the 91 wheels are a complete dependency closure.
4. The install commands were exercised on Linux with CPython 3.12 and the
   bundled pip 24.0, using the pure-Python part of the lock: the hashed
   install, the `--no-index` editable install, a `pip check` failure for a
   missing dependency, and a refusal of a tampered hash.

**To refresh:** run `packaging/refresh_macos_lock.sh` (it needs uv, python3
and network access to PyPI). Extra arguments go to
uv, for example `--upgrade-package numpy`. Review the diff, run
`tests/test_release_workflow.py`, and repeat check 1 above. Then run a
preview build on the Mac runner, which is the only place the full suite runs
against these exact wheels. Never edit a hash by hand. If a refresh would
change a major version that the tests depend on (huggingface_hub 2.x,
PySide6 6.x), the lock tests fail.

## What is checked here, and what needs other environments

**Checked locally, by source:** `tests/test_release_workflow.py` parses both
workflows and asserts the following:

- each job's permissions, and that only `publish` has `contents: write`
- `persist-credentials: false` on every checkout
- every action pinned to a SHA
- that the publish job does not check out or build anything
- that it re-verifies the DMG sha256 against the build job's output before
  publishing
- that the publish condition and release commands are unchanged
- that no expressions are spliced into shell scripts
- that every lock line is an exact pin with sha256 hashes
- that the lock covers every dependency the build installs
- that `build_macos.sh` installs only with `--require-hashes`
- `bash -n` on every `packaging/*.sh`

**Not executed here.** These checks need credentials or environments this
repository's local tests do not have:

- **The macOS jobs themselves:** the macos-26 Apple Silicon runner, the
  hashed install of the real arm64 wheels, the full suite on macOS,
  PyInstaller, `codesign`, `hdiutil` and the bundle self-tests. They run only
  on GitHub's runner, so the first preview build after a lock change is the
  execution test.
- **Developer ID signing and notarization:** the repository secrets
  `CODESIGN_IDENTITY`, `NOTARY_PROFILE`, `APPLE_ID`, `APPLE_TEAM_ID` and
  `APPLE_APP_PASSWORD`. Without them the build signs ad hoc, and the release
  notes say the build is unnotarized.
- **The publish job:** a tag push or manual dispatch, with `GITHUB_TOKEN`
  holding `contents: write`. The job is covered only by the static checks
  above until it runs.
