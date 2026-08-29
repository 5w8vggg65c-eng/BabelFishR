# macOS packaging and releases

Goal: the operator downloads a disk image, drags one icon onto another, and
never sees Python, pip or a terminal.

**Status.** The bundle is built, verified, ad-hoc signed and packaged into
`BabelFishR-macOS-arm64.dmg` by
[`.github/workflows/macos-release.yml`](../.github/workflows/macos-release.yml)
on a GitHub-hosted Apple Silicon runner. It has **not** been notarized (that
needs a paid Apple Developer ID), and it has **not** been run against real
hardware: no radio, no FalconClaw, no USB interface, no real audio device.

## The release pipeline

Triggered by hand (`workflow_dispatch`) or by pushing a `v*` tag, which also
publishes a GitHub **prerelease**.

1. **Architecture gate, first and fatal.** `uname -m` must report `arm64`. An
   Intel build published under an Apple Silicon name would be worse than no
   build: it would run under Rosetta, or not at all, and nobody would know why.
2. Python 3.12, installed inside the runner only.
3. `packaging/build_macos.sh` — clean venv, the whole deterministic suite,
   the bundle, verification, signing, the independence check, the disk image.
4. `packaging/verify_bundle.sh` again, independently of the build script.
5. Artifacts: the DMG, its `.sha256`, the test report and JUnit XML, the build
   log, the build environment (`pip freeze`), the bundle-verification output,
   the independence report and the signing report.

## Build

```bash
# on macOS, from the repository root
./packaging/build_macos.sh                        # bundle only
BABELFISHR_MAKE_DMG=1 ./packaging/build_macos.sh  # and the disk image
```

It creates a **clean** venv, installs `[gui,audio,asr,translate,dev,packaging]`
— the dev extra provides pytest and the packaging extra provides PyInstaller,
both of which the script then verifies import — runs the **full** deterministic
suite (plain `pytest`, not a marker subset; tests needing real models, a real
dsd-neo binary or hardware skip themselves) with `BABELFISHR_HOME` pointed at a
scratch directory, builds `dist/BabelFishR.app`, then verifies, signs and
proves it standalone.

`BABELFISHR_SKIP_TESTS=1` skips the test step. Never set it for a release.
`BABELFISHR_REPORT_DIR` chooses where the reports are written.

Each step can be run on its own against an existing bundle:

```bash
./packaging/verify_bundle.sh dist/BabelFishR.app
./packaging/sign_macos.sh dist/BabelFishR.app
./packaging/verify_independence.sh dist/BabelFishR.app
./packaging/make_dmg.sh dist/BabelFishR.app dist/BabelFishR-macOS-arm64.dmg
```

**Every check is fatal.** A bundle that cannot start is never signed, and a
bundle that is not standalone is never packaged.

## Proving the app is standalone

`packaging/verify_independence.sh` runs the frozen binary from `/`, under
`env -i` with `PYTHONPATH`, `PYTHONHOME` and `VIRTUAL_ENV` removed and a
minimal `PATH`. It fails if:

- `--version`, `--help` or `--selftest-import` do not work;
- any required module — `babelfishr`, NumPy, PySide6 (Core/Gui/Widgets),
  `sounddevice`, `faster_whisper`, `ctranslate2`, `argostranslate` — resolves
  to a file outside the bundle;
- Qt's plugin path is outside the bundle, or `libqcocoa.dylib` is missing;
- the main window will not construct offscreen;
- any bundled `.dylib` or `.so` still references an absolute path that is not
  an OS library under `/usr/lib` or `/System`.

It also runs `--selftest-coreaudio`, which is the only place in this project
where the real CoreAudio ABI is exercised: the frameworks are loaded, a size
query is issued for the device list, and a non-zero OSStatus - what a wrong
selector or a wrong `AudioObjectPropertyAddress` layout produces - fails the
build. Any devices the machine reports must be coherent enough to identify.

It deliberately does **not** require a device to exist. A hosted runner has no
audio hardware, so zero devices is the expected result there, and the report
says so rather than letting a green tick imply that audio capture was tested.

The checks that have to run inside the frozen process are the entry point's
`--selftest-independence`, `--selftest-coreaudio` and `--selftest-gui` flags.

Result from the first successful Apple Silicon run: every required module
loaded from `BabelFishR.app/Contents/Frameworks`, `libqcocoa.dylib` and the
Qt multimedia plugins were present inside the bundle, the main window opened,
and no native library referenced a build-machine path.

## Signing and notarization

`packaging/sign_macos.sh` supports exactly two honest outcomes, and never
describes one as the other.

**Developer ID.** Set `CODESIGN_IDENTITY` to the certificate's common name.
Nested frameworks and native libraries are signed first, then the bundle, with
the hardened runtime and `packaging/entitlements.plist`. If notarization
credentials are also present — `NOTARY_PROFILE`, or `APPLE_ID` +
`APPLE_TEAM_ID` + `APPLE_APP_PASSWORD` — the image is submitted, the ticket is
stapled, and the result is validated. A failed notarization fails the build.

```bash
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE=AC
BABELFISHR_MAKE_DMG=1 ./packaging/build_macos.sh
```

In the release workflow these come from repository secrets of the same names.
No credential is echoed; the notarytool output is filtered before it is logged.

**Ad-hoc.** With no certificate the bundle is still signed, with `-`. This is
not cosmetic: an *unsigned* bundle has no stable code identity, so macOS cannot
attach a microphone permission grant to it. The report and the release notes
say **UNNOTARIZED ALPHA**, and explain that the operator must right-click ▸
Open once. It is never called notarized.

The absence of a paid Apple Developer ID does not block a testable DMG. It only
changes what the DMG is honestly called.

## The disk image

`packaging/make_dmg.sh` stages the app beside a symlink to `/Applications`,
builds a compressed UDZO image, verifies it, mounts it once to confirm it
really contains a launchable app and the drop target, then writes
`BabelFishR-macOS-arm64.dmg.sha256` beside it.

## Where things live


Field assets are under **Application Support**, deliberately not a cache:

```
~/Library/Application Support/BabelFishR/
├── models/            Whisper models + manifest.json
├── language-packs/    Argos packages
├── Recordings/        original WAVs, one per transmission
├── Logs/              babelfishr.log (rotated)
├── babelfishr.sqlite3 metadata, transcripts, translations, corrections
└── settings.toml
```

macOS may purge `~/Library/Caches` under disk pressure. Losing a 1.5 GB model
in the field because the disk got tight is not acceptable, so nothing required
lives there.

**Upgrades replace the app bundle only.** Everything above is outside the
bundle and survives; the build script never removes anything under Application
Support, and runs its tests against a scratch home so a build cannot touch a
prepared model. Override the location with `BABELFISHR_HOME` if you want
assets on an external volume.

## Fresh-machine installation

1. Open `BabelFishR-macOS-arm64.dmg` and drag BabelFishR to Applications.
2. Right-click ▸ Open ▸ Open (needed once, while builds are unnotarized).
3. Approve the microphone prompt. If none appears:
   System Settings ▸ Privacy & Security ▸ Microphone ▸ enable BabelFishR,
   then quit and relaunch.
4. The setup assistant explains the one-time online preparation.
5. Prepare (see `docs/FIELD_OPERATION.md`), then validate offline.

The CLI is available inside the bundle: the packaged binary launches the GUI
when given no arguments and dispatches to the CLI when given any.

```bash
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR --help
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR field-check
```

## Entitlements

Requested: audio input; unsigned executable memory and library validation
disabled (native extensions such as ctranslate2 need these under the hardened
runtime); network client, used **only** during preparation; user-selected
file read/write for exports.

Not requested: camera, location, contacts. A test asserts these stay absent.
