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

- Detection, segmentation, pre-roll, hang time and squelch-tail trimming
  against synthetic radio fixtures with known ground truth.
- Recording, storage, search, review queue, retention and export.
- The full pipeline end to end, using deterministic mock engines.
- The Qt UI, headless: a replay session drives the real UI code path and
  populates the timeline.
- The Claude translation engine's request shape against the real Anthropic SDK
  (request rejected only at authentication, with no API key present).

### NOT tested — no hardware was available

- **Any physical radio, FalconClaw PTT, cable or USB audio interface.**
- **macOS**: CoreAudio, the microphone-permission prompt, device hot-plug,
  app packaging. Development and testing ran on Linux.
- **Live audio capture through PortAudio.** The capture path is written and
  unit-tested through a fake source, but no real input device was opened.
- **Real transcription.** `faster-whisper` was not installed or run; the engine
  wrapper is written and its failure paths tested, but no audio has been
  transcribed by it.
- **Real translation.** Neither Argos nor a live Claude API call has produced a
  translation here. Only the mock engine has run end to end.

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
babelfishr devices         # list audio inputs
babelfishr gui             # launch the desktop app
```

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

- Recordings: `recordings/` (configurable), original WAV, never modified.
- Database: `babelfishr.sqlite3`, holding metadata, transcripts, translations
  and corrections.
- Help ▸ "Where are my recordings?" shows both paths in the app.

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

- [macOS + FalconClaw validation procedure](docs/MACOS_VALIDATION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Experimental decoders](docs/EXPERIMENTAL.md)

## Licence

MIT.

## Legal note

Monitoring rules differ by country: what you may listen to, record, and share
is your responsibility. BabelFishR is receive-only and includes no capability
to decrypt protected communications.
