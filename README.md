# BabelFishR

Receive-only desktop application that turns radio traffic into a searchable,
translated, chat-style log.

Audio arrives from a radio's accessory output through a computer audio input.
BabelFishR detects each transmission, records the original audio, transcribes
it, detects the language, translates it into your language, and shows it as a
timeline of bubbles you can play back, correct, tag and export.

**It never transmits.** There is no PTT actuation, no transmit path, and no
code that keys a radio.

---

## What has actually been tested, and what has not

This distinction matters more than any feature list, so it comes first.

### Verified in automated tests (simulated audio, no hardware)

- **The capture-first invariant**: speech, static, tones and digital-shaped
  events are all recorded and logged before anything classifies them.
- Detection, segmentation, pre-roll, hang time and squelch-tail trimming
  against synthetic radio fixtures with known ground truth.
- Recording, storage, search, review queue, retention, provenance and export.
- The full pipeline end to end, using deterministic mock engines.
- **Field Offline enforcement**: no cloud provider is constructed, no mock
  output is produced, and a missing local engine fails honestly.
- The Qt UI, headless, in both light and dark appearance.
- DSD-neo integration driven against a stub binary reproducing its interface.

### Verified against the real libraries (not mocks)

- **faster-whisper 1.2.1**: `local_files_only` loading with a missing model
  fails in 0.22 s with no download attempt — the field guarantee. Model
  preparation uses `faster_whisper.utils.download_model(..., output_dir=)`,
  whose signature and asset list were read from the installed package.
- **argostranslate**: availability correctly reports *unavailable* when the
  library is installed but no language pack is; routes come from Argos's own
  resolved translation graph, including composite (pivot) routes.
- The Claude translation engine's request shape against the real Anthropic SDK
  (rejected only at authentication, with no API key present).
- **The Field Offline pipeline with outbound sockets refused** — capture,
  processing, search and export all complete with `socket.connect`,
  `create_connection`, `getaddrinfo` and `http.client` raising.

### NOT tested — no hardware was available

- **Any physical radio, FalconClaw PTT, cable or USB audio interface.**
- **macOS**: CoreAudio, the microphone-permission prompt, device hot-plug,
  app packaging. Development and testing ran on Linux.
- **Live audio capture through PortAudio.** The capture path is written and
  unit-tested through a fake source, but no real input device was opened.
- **Real transcription output.** faster-whisper is installed and its loading
  and failure paths are exercised, but no model weights could be downloaded
  here (Hugging Face is blocked by this environment's network policy), so **no
  audio has actually been transcribed**.
- **Real translation output.** argostranslate is installed, but no language
  pack could be downloaded, so **no text has actually been translated**.
- **DSD-neo itself.** The integration is tested against a stub that reproduces
  its interface. The real binary has never run, and no real digital traffic
  has been decoded.
- **Any SDR.** The `SignalSource` interface and a recorded-file reference
  implementation are tested; no device driver is bundled or claimed.
- **The macOS .app.** Spec, entitlements and build script are written and
  metadata-tested; no bundle has been built, signed or notarized.

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

## Install

Requires Python 3.9+ (3.11+ recommended). On an Apple-silicon Mac:

```bash
git clone <this repo> && cd BabelFishR
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,audio]"          # desktop app + live audio capture
pip install -e ".[gui,audio,asr]"      # add real local transcription (Whisper)
pip install -e ".[all]"                # everything, including cloud translation
```

Extras:

| Extra | Gives you | Runs where |
|---|---|---|
| `audio` | live capture via PortAudio (`sounddevice`) | local |
| `gui` | the PySide6 desktop application | local |
| `asr` | real transcription (`faster-whisper`) | local |
| `translate` | offline translation (Argos) | local |
| `cloud` | Claude API translation (opt-in) | sends transcript text |

With no extras installed the core still runs: you can replay WAV files through
the pipeline with mock engines, which is how the test suite works.

## Run

```bash
babelfishr doctor          # check the install, devices, engines, credentials
babelfishr field-check     # prove offline readiness (downloads nothing)
babelfishr devices         # list audio inputs
babelfishr gui             # launch the desktop app
```

## Offline field operation

BabelFishR is built to work with the network switched off, after a one-time
online preparation. **In the app**, that is the first-run setup assistant:
choose a model and language pairs, press *Prepare now*, and watch it download,
verify and run a real Field Check — all on a background thread, with a cancel
button.

The same thing from a terminal:

```bash
babelfishr prepare-field --asr-model small --language es-en --language de-en
babelfishr mode --set field-offline
babelfishr field-check     # must still pass with the network unplugged
```

Models are prepared into `~/Library/Application Support/BabelFishR/models/<name>/`
and loaded from that directory explicitly — never from a Hugging Face cache
that an upgrade or a disk-cleanup could remove. An interrupted download is
reported as *incomplete*, with the missing files named, and repaired on the
next preparation.

Three modes, enforced in code rather than by convention:

| Mode | Cloud | Placeholder output | Downloads | Processing |
|---|---|---|---|---|
| Field Offline | never constructed | refused | refused | local only |
| Online / Setup | if explicitly selected | allowed | allowed | yes |
| Record Only | never constructed | refused | refused | none |

**Nothing leaves the Mac because a local engine is missing.** A missing model
produces an honest failure, never a silent cloud call, and recording continues
regardless. Field Offline also refuses preparation and language-pack installs
outright.

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

## Digital post-processing (optional)

If a local `dsd-neo` is installed, a recording can be analysed after the fact:

```bash
babelfishr analyze <transmission-id>                   # -fa, hunts all profiles
babelfishr analyze <transmission-id> --protocol dmr-dual
```

The original WAV is opened read-only; conversions go to derived files. A failure
means *no usable decode from this input*, not *the recording was lost* — and
ordinary accessory audio is often the wrong input for a digital decoder anyway,
so a negative result says very little. BabelFishR does not attempt to defeat
encryption.

### Diagnostics before you trust it with real traffic

```bash
babelfishr level --device 2          # live meter; aim for speech peaks near -12 dBFS
babelfishr calibrate --device 2      # listen to an idle channel, suggest a threshold
babelfishr test-record --device 2 --seconds 10 --output test.wav
babelfishr replay test.wav           # run that clip through the whole pipeline
babelfishr selftest                  # synthetic fixture through the whole pipeline
```

### Headless use

```bash
babelfishr listen --device 2 --target-language en
babelfishr search "roadblock"
babelfishr export --format bundle --output ./session-export
```

## Privacy

Nothing leaves the computer unless you configure a cloud engine. The UI states
which mode you are in before a session starts, and the engine status screen
names exactly what each engine sends where.

The Claude translation engine sends **transcript text only** — never audio.
It is unavailable until you supply a key:

```bash
# macOS Keychain (preferred)
security add-generic-password -U -s BabelFishR -a ANTHROPIC_API_KEY -w

# or an environment variable, or a .env file (see .env.example)
export ANTHROPIC_API_KEY=...
```

No key is ever written to the config file, and `.env` is gitignored.

## Where your data lives

Everything writable resolves under one place:

```
~/Library/Application Support/BabelFishR/
├── settings.toml       operating mode and setup choices
├── babelfishr.sqlite3  metadata, transcripts, translations, corrections
├── Recordings/         original WAVs, never modified
├── models/             prepared Whisper models
├── language-packs/     Argos packages (ARGOS_PACKAGES_DIR)
└── Logs/
```

An explicit absolute path in the config or environment still wins; an explicit
*relative* path resolves against the config file that declared it, never the
process working directory — a Finder-launched `.app` has a working directory of
`/`. Help ▸ "Where are my recordings?" shows the resolved paths in the app.

Original audio and original transcript are never overwritten. Corrections are
stored alongside them, and exports carry both.

## Digital voice

If your radio decodes DMR/P25/D-STAR/Fusion and puts intelligible voice out of
its accessory connector, BabelFishR treats it as ordinary audio and it works.

Raw digital bursts are a different problem requiring a protocol-specific plugin
and validation against recorded samples. BabelFishR does not attempt to decode
them, does not bundle a vocoder, and will not help recover encrypted traffic.

## Experimental decoders

An earlier prototype produced signalling decoders (CTCSS, DCS, DTMF, CW,
AFSK1200/APRS, MDC-1200, POCSAG, RTTY). They are preserved under
`babelfishr/experimental/`, kept out of the receive path, and disabled unless
you set `BABELFISHR_EXPERIMENTAL=1`. They were verified against synthetic
signals only — never off-air. See [docs/EXPERIMENTAL.md](docs/EXPERIMENTAL.md).

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

The suite uses deterministic mock engines: no model downloads, no API keys, no
network. `tests/test_acceptance.py` maps one test to each MVP acceptance
criterion.

## Documentation

- [Field operation and zero-connectivity validation](docs/FIELD_OPERATION.md)
- [Digital post-processing with DSD-neo](docs/DIGITAL_ANALYSIS.md)
- [macOS + FalconClaw validation procedure](docs/MACOS_VALIDATION.md)
- [macOS packaging](docs/MACOS_PACKAGING.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Experimental decoders](docs/EXPERIMENTAL.md)

## Licence

MIT.

## Legal note

Monitoring rules differ by country: what you may listen to, record, and share
is your responsibility. BabelFishR is receive-only and includes no capability
to decrypt protected communications.
