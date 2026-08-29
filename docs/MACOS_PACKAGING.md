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

It creates a venv, installs the GUI/audio/ASR/translate extras plus PyInstaller,
**runs the test suite**, builds `dist/BabelFishR.app`, and verifies the
`Info.plist` carries the microphone usage string.

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

To notarize (needs an Apple Developer account and a stored keychain profile):

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
bundle and survives. Override the location with `BABELFISHR_HOME` if you want
assets on an external volume.

## Fresh-machine installation

1. Copy `BabelFishR.app` to `/Applications`.
2. Right-click ▸ Open (until the app is signed).
3. Approve the microphone prompt. If none appears:
   System Settings ▸ Privacy & Security ▸ Microphone ▸ enable BabelFishR,
   then quit and relaunch.
4. The setup assistant explains the one-time online preparation.
5. Prepare (see `docs/FIELD_OPERATION.md`), then validate offline.

The CLI is available inside the bundle if you want it:

```bash
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR --help
```

## Entitlements

Requested: audio input; unsigned executable memory and library validation
disabled (native extensions such as ctranslate2 need these under the hardened
runtime); network client, used **only** during preparation; user-selected
file read/write for exports.

Not requested: camera, location, contacts. A test asserts these stay absent.
