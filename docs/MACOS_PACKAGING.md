# macOS packaging

Goal: the operator double-clicks an app. No Python, no pip, no terminal during
normal field use.

**Status: unbuilt and unsigned.** The spec, entry point, entitlements and build
script are written and their metadata is unit-tested, but no `.app` has been
produced — building requires macOS, which this development environment is not.
Treat the first build as the real test.

## Build

```bash
# on macOS, from the repository root
./packaging/build_macos.sh
```

It creates a **clean** venv, installs `[gui,audio,asr,translate,dev,packaging]`
— the dev extra provides pytest and the packaging extra provides PyInstaller,
both of which the script then verifies import — runs the **full** deterministic suite (plain `pytest`, not a marker subset —
tests needing real models, a real dsd-neo binary or hardware skip themselves)
with `BABELFISHR_HOME` pointed at a scratch directory, builds
`dist/BabelFishR.app`, and runs `packaging/verify_bundle.sh`, which checks the `Info.plist` keys and
launches the frozen binary with `--selftest-import`.

**Bundle verification is fatal.** If the packaged executable cannot start, the
build exits non-zero and nothing is signed or notarized. You can run it against
an existing bundle directly:

```bash
./packaging/verify_bundle.sh dist/BabelFishR.app
```

Set `BABELFISHR_SKIP_TESTS=1` to skip the test step deliberately.

## Signing and notarization

Currently **neither signed nor notarized**. Consequences:

- Gatekeeper blocks the app on any Mac other than the one that built it.
- An unsigned app is frequently denied microphone access, which for this
  application means silence.

To sign, set an identity and rebuild:

```bash
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
./packaging/build_macos.sh
```

To notarize (needs an Apple Developer account and a stored keychain profile),
set `NOTARY_PROFILE` and the build script does it:

```bash
export CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE=AC
./packaging/build_macos.sh
```

Equivalently, by hand:

```bash
ditto -c -k --keepParent dist/BabelFishR.app BabelFishR.zip
xcrun notarytool submit BabelFishR.zip --keychain-profile AC --wait
xcrun stapler staple dist/BabelFishR.app
```

Until then, opening it locally means right-click ▸ Open ▸ Open.

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

1. Copy `BabelFishR.app` to `/Applications`.
2. Right-click ▸ Open (until the app is signed).
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
