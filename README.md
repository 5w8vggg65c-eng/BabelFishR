# BabelFishR

Receive-only desktop application for macOS that turns radio traffic into a
searchable, translated, chat-style log.

Audio arrives from a radio's accessory output through a computer audio input.
BabelFishR detects each transmission, records the original audio, transcribes
it, detects the language, translates it into your language, and shows it as a
timeline you can play back, correct, tag and export.

**It never transmits.** There is no PTT actuation, no transmit path, and no
code that keys a radio.

---

## Download BabelFishR for macOS

Releases: **https://github.com/5w8vggg65c-eng/BabelFishR/releases**

Download **`BabelFishR-macOS-arm64.dmg`**. It is a complete application for
Apple Silicon Macs. You do not need Python, Terminal, this repository, or any
developer tools to install or use it.

1. **Download** `BabelFishR-macOS-arm64.dmg` from the Releases page.
2. **Open the DMG** (double-click it in Downloads).
3. **Drag BabelFishR onto the Applications folder** in the window that opens.
4. **Open BabelFishR** from Applications or Launchpad.
   *Alpha builds are not notarized by Apple yet — see below.*
5. **Approve the audio-input permission** when macOS asks.
   BabelFishR needs macOS audio-input permission. You can then choose the
   MacBook microphone, a USB audio interface, or another connected input.
6. **Choose your input** in the *Audio input* panel at the top of the window.
   Nothing is chosen for you, and nothing is recorded until you choose.
7. **Prepare for offline use** in the setup window that appears on first run:
   pick a speech model and the languages you expect to hear, and press
   *Prepare now*. This is the one step that needs the internet.
8. **Run Field Check**, then disconnect from the network and confirm it still
   passes. From then on BabelFishR works entirely offline.

### If macOS says the app cannot be opened

Alpha builds are **ad-hoc signed and not notarized by Apple**, because
notarization requires a paid Apple Developer ID. macOS will refuse the first
launch. To open it: **right-click (or Control-click) BabelFishR in Applications,
choose Open, then click Open in the dialog.** You only have to do this once.

Each release states plainly whether it was notarized. If a release does not say
it was notarized by Apple, it was not.

### Checking your download

Every release ships a `.sha256` file beside the DMG:

```
shasum -a 256 BabelFishR-macOS-arm64.dmg
```

The result should match the release notes.

### Upgrading

Drag the new BabelFishR over the old one. Your models, language packs,
recordings, database, corrections, tags, bookmarks and settings live in
`~/Library/Application Support/BabelFishR/` and are not touched by an upgrade.

### Removing BabelFishR

Open **BabelFishR-macOS-arm64.dmg** again and double-click **Uninstall
BabelFishR**. It runs from the disk image — there is nothing to install for it,
and it needs neither Python nor Terminal. It shows you the exact list of paths
first and deletes nothing until you tick the acknowledgement box *and* type
`DELETE`. Cancel changes nothing.

Complete removal permanently deletes:

- BabelFishR.app
- Whisper speech-recognition models
- Argos language packs
- **every recording — these cannot be recovered; nothing goes to the Trash**
- the transmission database, with all transcripts and translations
- your settings
- diagnostic reports and logs
- BabelFishR's caches, preferences and saved application state
- the Argos index, download cache and configuration under `argos/`

It also asks macOS to forget BabelFishR's microphone permission. Copy anything
you want to keep somewhere else first. If something cannot be removed, the
uninstaller names it rather than claiming it finished.

There is deliberately no uninstall command inside BabelFishR itself.

---

## Choosing your audio input

This is the part of BabelFishR that matters most in the field, so it is worth
reading once.

**BabelFishR records whatever the input you selected is carrying. It does not
detect radios.** Connecting a cable to your Mac does not tell the application
anything about what is on the other end of it. Nothing in the audio identifies
a radio, a frequency, a channel, or a mode. If you select the MacBook
microphone, BabelFishR records the room — accurately, and with no idea that it
is not a radio.

Everything below is a different thing you might be selecting.

| What you select | What it is | When to use it |
|---|---|---|
| **MacBook microphone** | The microphone in your Mac. | Bench testing. Talk into it and watch a transmission appear. Never for a real watch. |
| **A USB audio interface** | A separate box with a line input. | The normal way to bring analogue receiver audio in. |
| **Analogue radio audio through a USB interface** | Radio accessory/speaker output → cable → interface line input → Mac. | The standard FalconClaw path. What the interface hears is what you get. |
| **Native USB audio from a radio** | A radio that presents itself to the Mac as a USB sound device. | Simpler wiring, same result. It appears in the list like any other input. |
| **SDR input (optional)** | A software-defined radio feeding the signal path. | Off by default, no device drivers are bundled, and it is unvalidated. |

### The rules BabelFishR follows about your input

These exist because *an operator must never believe BabelFishR is receiving
radio traffic while it is actually recording the surrounding room.*

- **Nothing is selected for you.** The list opens on "Choose an audio input".
  The macOS system default is *labelled*, but never selected.
- **Your choice is remembered by device identity, not by position in a list.**
  Unplug the interface, reboot, plug it back into a different port: BabelFishR
  still knows which device it is.
- **A missing device is never replaced with a different one.** Not the MacBook
  microphone, not the system default, and not another interface that happens
  to be connected. If the device you chose is not there, BabelFishR refuses to
  start and tells you which device is missing.
- **The active input is on screen the whole time**, reading
  `INPUT: USB Audio CODEC — CONNECTED`, with a live level meter beside it.
- **If it disconnects mid-watch**, the line turns red and reads
  `RADIO INPUT DISCONNECTED`. Recording of transmissions already captured is
  unaffected. BabelFishR waits for *that same device* and resumes when it
  returns. Both times are logged.
- **If two connected inputs cannot be told apart**, BabelFishR refuses to
  start and says so. Two of the same USB interface, with no unique identifier
  between them, look identical in every property macOS exposes — but only one
  of them may have your radio on it. The line reads `CANNOT IDENTIFY`, both
  candidates are named, and you unplug the one you do not want. It does not
  pick one.
- **Every input you choose is pinned to that device.** There is no lock
  setting to forget to switch on.
- **A radio profile can remember its own input**, so selecting the profile
  selects the right interface — or says which one is missing.
- **"Use the macOS system default input"** exists as its own clearly labelled
  choice, for when that is genuinely what you want. It is never a fallback.

---

## What has actually been tested, and what has not

This distinction matters more than any feature list.

### Verified on a real Apple Silicon Mac (GitHub `macos-26` arm64 runner)

- The application bundle **builds, launches and imports its own code** as a
  frozen app.
- It is **standalone**: run from `/` with `PYTHONPATH`, `PYTHONHOME` and
  `VIRTUAL_ENV` removed and a minimal `PATH`, every required module — including
  PySide6, NumPy, sounddevice, faster-whisper, CTranslate2 and Argos Translate
  — resolves from inside the bundle, and Qt finds its own platform plugin
  there.
- The **main window constructs and opens** (offscreen).
- The **disk image mounts** and contains a launchable app and an `/Applications`
  drop target.
- The **whole deterministic test suite passes on macOS arm64**, not only on
  the Linux development machine.

### Verified in automated tests (simulated audio, no hardware)

- **The capture-first invariant**: speech, static, tones and digital-shaped
  events are all recorded and logged before anything classifies them.
- Detection, segmentation, pre-roll, hang time and squelch-tail trimming
  against synthetic radio fixtures with known ground truth.
- Recording, storage, search, review queue, retention, provenance and export.
- The full pipeline end to end, using deterministic mock engines.
- **Field Offline enforcement**: no cloud provider is constructed, no mock
  output is produced, and a missing local engine fails honestly.
- **Audio input selection**: an explicit choice of the built-in microphone or
  an external interface; a choice that survives a restart; a device that comes
  back on a different index still being recognised; a *different* device that
  inherits the old index being refused; no fallback to the microphone or the
  system default when the chosen device is missing; a refusal to choose
  between two indistinguishable interfaces at start, after a restart, on
  profile restoration and on reconnect; and the input controls freezing for
  the duration of a watch. These use a simulated device list.
- The Qt UI, headless, in both light and dark appearance.
- DSD-neo integration driven against a stub binary reproducing its interface.

### Verified against the real libraries (not mocks)

- **faster-whisper 1.2.1**: `local_files_only` loading with a missing model
  fails in 0.22 s with no download attempt — the field guarantee. Model
  preparation uses `faster_whisper.utils.download_model(..., output_dir=)`.
- **argostranslate**: availability correctly reports *unavailable* when the
  library is installed but no language pack is; routes come from Argos's own
  resolved translation graph, including composite (pivot) routes.
- **The Field Offline pipeline with outbound sockets refused** — capture,
  processing, search and export all complete with `socket.connect`,
  `create_connection`, `getaddrinfo` and `http.client` raising.

### NOT tested — no hardware was available

- **Any physical radio, FalconClaw PTT, cable or USB audio interface.**
- **Any real audio input device.** No CoreAudio device has ever been opened by
  this code, and the microphone-permission prompt has never been seen. Device
  hot-plug, disconnection and reconnection are covered only by simulation.
- **The M5 MacBook Air specifically.** The bundle was built and launched on a
  GitHub-hosted Apple Silicon runner, which is not your machine.
- **Real transcription output.** No model weights could be downloaded in the
  development environment (Hugging Face is blocked by its network policy), so
  **no audio has actually been transcribed**.
- **Real translation output.** No language pack could be downloaded, so **no
  text has actually been translated**.
- **DSD-neo itself.** The integration is tested against a stub. The real binary
  has never run and no real digital traffic has been decoded.
- **Any SDR.** The `SignalSource` interface and a recorded-file reference
  implementation are tested; no device driver is bundled or claimed.
- **Apple notarization**, unless a specific release says otherwise.

Everything above is written and reviewable, but "written" is not "working".
Treat the first run on your Mac as the real integration test, and use the
[macOS validation procedure](docs/MACOS_VALIDATION.md).

---

## Signal path

```
Radio
  └─ radio-specific FalconClaw downlead
      └─ FalconClaw FC-PTT
          └─ FalconClaw Nexus-to-3.5 mm AUX adapter (used in reverse)
              └─ computer audio input (USB interface recommended)
                  └─ BabelFishR
```

The FalconClaw PTT is an analogue audio routing and switching device. It is not
an SDR, tuner, scanner, modem or USB sound card.

**The radio does all the radio work**: tuning, receiving, demodulating, and —
where the radio supports it — decoding digital voice into ordinary audio at the
accessory connector. BabelFishR sees only the resulting waveform.

### What BabelFishR cannot know from that waveform

It cannot determine the RF frequency, the radio's make or model, the selected
channel, the modulation, who transmitted, where they are, whether the PTT is
pressed, or the state of the radio's squelch/COR line.

Frequency and channel labels come from a **radio profile you create**. They are
recorded as operator-supplied metadata and labelled as such throughout the app
and in every export. Automatic frequency synchronisation would require a
radio-specific CAT/serial/USB/Bluetooth control plugin; that is a deliberate
future extension point, not a current feature.

---

## Offline field operation

BabelFishR is built to work with the network switched off, after a one-time
online preparation. **In the app**, that is the first-run setup assistant:
choose a model and language pairs, press *Prepare now*, and watch it download,
verify and run a real Field Check — on a background thread, with a cancel
button and a *Copy Diagnostic Report* button if anything goes wrong. You never
need a terminal for any of it.

The assistant shows which translation routes are **installed**, by name. It
does not claim "any language": only the pairs you install can be translated
offline. Traffic in any other language is still recorded, and still transcribed
when the speech model recognises it.

Models are prepared into `~/Library/Application Support/BabelFishR/models/<name>/`
and loaded from that directory explicitly — never from a Hugging Face cache
that an upgrade or a disk cleanup could remove. An interrupted download is
reported as *incomplete*, with the missing files named, and repaired on the
next preparation.

Three modes, enforced in code rather than by convention:

| Mode | Cloud | Placeholder output | Downloads | Processing |
|---|---|---|---|---|
| Field Offline | never constructed | refused | refused | local only |
| Online / Setup | if explicitly selected | allowed | allowed | yes |
| Record Only | never constructed | refused | refused | none |

BabelFishR switches to Field Offline only when preparation succeeded **and**
Field Check passed. Either one alone is not enough, and the app says which one
failed.

**Nothing leaves the Mac because a local engine is missing.** A missing model
produces an honest failure, never a silent cloud call, and recording continues
regardless.

The offline guarantee is architectural — no cloud provider is constructed, and
no download path is reachable — not an environment variable. `NO_PROXY` is
deliberately *not* used: it bypasses proxies rather than blocking anything.

See [docs/FIELD_OPERATION.md](docs/FIELD_OPERATION.md) for the
zero-connectivity validation procedure.

## Capture first, classify second

Every event crossing the activity threshold is written to disk and the database
**before** anything classifies, transcribes or analyses it. Static, tones and
suspected digital bursts are all kept; classification only decides whether an
ASR call happens automatically, and every bubble offers *Transcribe anyway* and
*Analyze as digital*.

A transmission cannot be received twice, so a misclassification must never be
the reason one is gone.

## Where your data lives

Everything writable resolves under one place:

```
~/Library/Application Support/BabelFishR/
├── settings.toml       operating mode, setup choices, chosen audio input
├── babelfishr.sqlite3  metadata, transcripts, translations, corrections
├── Recordings/         original WAVs, never modified
├── models/             prepared Whisper models
├── language-packs/     Argos packages (ARGOS_PACKAGES_DIR)
├── argos/              Argos package index, download cache and config
└── Logs/
```

`argos/` is there because Argos Translate otherwise puts its index, its
download cache and its configuration in three folders in your home directory
(`~/.local/share/argos-translate`, `~/.config/argos-translate`,
`~/.local/cache/argos-translate`). BabelFishR points them here instead, so
everything it creates is in one place and the uninstaller can find all of it.
If an earlier version left those folders behind, BabelFishR removes the files
it wrote and the folders once they are empty — and leaves anything it did not
write, naming it, in case another Argos installation owns it.

Reinstalling or upgrading the application does not touch any of it. Help ▸
"Where are my recordings?" shows the resolved paths in the app.

Original audio and original transcript are never overwritten. Corrections are
stored alongside them, and exports carry both.

## Digital voice

If your radio decodes DMR/P25/D-STAR/Fusion and puts intelligible voice out of
its accessory connector, BabelFishR treats it as ordinary audio and it works.

Raw digital bursts are a different problem requiring a protocol-specific plugin
and validation against recorded samples. BabelFishR does not attempt to decode
them, does not bundle a vocoder, and will not help recover encrypted traffic.

If a local `dsd-neo` is installed, a recording can be analysed after the fact.
A failure means *no usable decode from this input*, not *the recording was
lost* — and ordinary accessory audio is often the wrong input for a digital
decoder anyway, so a negative result says very little. See
[docs/DIGITAL_ANALYSIS.md](docs/DIGITAL_ANALYSIS.md).

## Privacy

Nothing leaves the computer unless you configure a cloud engine. The window
states which mode you are in before a session starts, and the engine status
screen names exactly what each engine sends where.

The optional Claude translation engine sends **transcript text only** — never
audio — and is unavailable until you supply a key. No key is ever written to
the settings file.

## Documentation

- [Field operation and zero-connectivity validation](docs/FIELD_OPERATION.md)
- [macOS + FalconClaw validation procedure](docs/MACOS_VALIDATION.md)
- [Digital post-processing with DSD-neo](docs/DIGITAL_ANALYSIS.md)
- [macOS packaging, signing and releases](docs/MACOS_PACKAGING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Experimental decoders](docs/EXPERIMENTAL.md)

---

# Developer setup

**Nothing in this section is needed to use BabelFishR.** It is for building it
from source, changing it, or running it on a platform with no release build.

## Install from source

Requires Python 3.9+ (3.12 is what the release is built with).

```bash
git clone https://github.com/5w8vggg65c-eng/BabelFishR.git && cd BabelFishR
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,audio]"          # desktop app + live audio capture
pip install -e ".[gui,audio,asr]"      # add real local transcription (Whisper)
pip install -e ".[all]"                # everything, including cloud translation
```

| Extra | Gives you | Runs where |
|---|---|---|
| `audio` | live capture via PortAudio (`sounddevice`) | local |
| `gui` | the PySide6 desktop application | local |
| `asr` | real transcription (`faster-whisper`) | local |
| `translate` | offline translation (Argos) | local |
| `cloud` | Claude API translation (opt-in) | sends transcript text |
| `dev` | pytest | local |
| `packaging` | PyInstaller | local |

With no extras installed the core still runs: you can replay WAV files through
the pipeline with mock engines, which is how the test suite works.

## Command line

```bash
babelfishr doctor                    # check the install, devices, engines
babelfishr devices                   # list audio inputs
babelfishr input --select 2          # remember one, by stable identity
babelfishr input                     # is the chosen input connected?
babelfishr field-check               # prove offline readiness (downloads nothing)
babelfishr prepare-field --asr-model small --language es-en --language de-en
babelfishr mode --set field-offline
babelfishr gui                       # launch the desktop app

babelfishr level --device 2          # live meter; aim for peaks near -12 dBFS
babelfishr calibrate --device 2      # suggest a threshold from an idle channel
babelfishr test-record --device 2 --seconds 10 --output test.wav
babelfishr replay test.wav           # run that clip through the whole pipeline
babelfishr listen --device 2 --target-language en
babelfishr search "roadblock"
babelfishr export --format bundle --output ./session-export
babelfishr analyze <transmission-id>  # optional dsd-neo post-processing
```

A cloud key, if you want one, goes in the macOS Keychain or the environment —
never in the settings file:

```bash
security add-generic-password -U -s BabelFishR -a ANTHROPIC_API_KEY -w
```

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Deterministic mock engines: no model downloads, no API keys, no network. Tests
needing real models, a real `dsd-neo`, real hardware or macOS skip themselves
with a reason. `tests/test_acceptance.py` maps one test to each MVP acceptance
criterion.

## Building the macOS application

On an Apple Silicon Mac:

```bash
./packaging/build_macos.sh                  # clean venv, full suite, bundle,
                                            # verify, sign, independence check
BABELFISHR_MAKE_DMG=1 ./packaging/build_macos.sh
```

Individually:

```bash
./packaging/verify_bundle.sh dist/BabelFishR.app        # metadata + real launch
./packaging/sign_macos.sh dist/BabelFishR.app           # Developer ID or ad-hoc
./packaging/verify_independence.sh dist/BabelFishR.app  # prove it is standalone
./packaging/make_dmg.sh dist/BabelFishR.app dist/BabelFishR-macOS-arm64.dmg
```

Set `CODESIGN_IDENTITY` for a Developer ID signature, and either
`NOTARY_PROFILE` or `APPLE_ID` + `APPLE_TEAM_ID` + `APPLE_APP_PASSWORD` to
notarize. Without a certificate the bundle is ad-hoc signed and labelled
**UNNOTARIZED ALPHA**; it is never described as notarized.

Releases are built by `.github/workflows/macos-release.yml`, which refuses to
run unless `uname -m` reports `arm64`. See
[docs/MACOS_PACKAGING.md](docs/MACOS_PACKAGING.md).

## Experimental decoders

An earlier prototype produced signalling decoders (CTCSS, DCS, DTMF, CW,
AFSK1200/APRS, MDC-1200, POCSAG, RTTY). They are preserved under
`babelfishr/experimental/`, kept out of the receive path, and disabled unless
you set `BABELFISHR_EXPERIMENTAL=1`. They were verified against synthetic
signals only — never off-air. See [docs/EXPERIMENTAL.md](docs/EXPERIMENTAL.md).

---

## Licence

MIT.

## Legal note

Monitoring rules differ by country: what you may listen to, record, and share
is your responsibility. BabelFishR is receive-only and includes no capability
to decrypt protected communications.
