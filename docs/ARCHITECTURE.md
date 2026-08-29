# Architecture

## Flow

```
AudioSource ──► LevelMeter ──► RadioActivityDetector ──► Recorder ──► Store
(live | replay)      │                                                  │
                     └──► SafetyRecorder (optional, continuous)         │
                                                                        ▼
                                            ProcessingPipeline (worker threads)
                                              transcribe → detect language
                                              → translate → Store → EventBus
                                                                        │
                                                          ┌─────────────┴──────┐
                                                          ▼                    ▼
                                                    Qt timeline            CLI output
```

## Threading

Three groups, deliberately separated:

| Thread | Does | Must never |
|---|---|---|
| PortAudio callback | copies samples into a queue | block, allocate heavily, or call an engine |
| Capture | meter, detect, write WAV, insert row | run transcription |
| Processing pool | transcribe, translate, update rows | touch Qt widgets |

The Qt front-end owns no worker threads: it polls `EventBus.drain()` on a
100 ms timer, so every widget update happens on the GUI thread.

If the processing pool falls behind, the queue grows and transcripts arrive
late. Audio is never dropped for that reason — capture only writes files.

## Durability

A transmission is written to disk **and** inserted into the database before any
engine sees it. Consequences:

- An engine failure, a missing model or a crash cannot lose captured audio.
- A failure sets `state=failed` with an `ErrorInfo`, leaving the audio and any
  earlier stage's output intact — a translation failure keeps the transcript.
- Unfinished work is re-queued on the next start (`resume_pending`).
- Optional safety recording writes the raw stream in chunks regardless of what
  the detector decided, so a segmentation failure is recoverable.

## Modules

| Module | Responsibility |
|---|---|
| `models` | `Transmission`, `Session`, `RadioProfile`, states, errors |
| `storage` | SQLite, FTS5 search, filters, review queue, retention |
| `export` | JSON, CSV, Markdown, self-contained session bundles |
| `audio/` | devices, live capture, replay, WAV I/O, metering, calibration, safety recording |
| `detect` | radio-aware transmission detection |
| `providers/` | engine protocols, mocks, Whisper, Argos, Claude, glossary, credentials |
| `pipeline` | capture service, processing workers, event bus, recorder |
| `app` | facade tying it together for both front-ends |
| `ui/` | PySide6 window, timeline, bubbles, widgets |
| `cli` | diagnostics, replay, headless monitoring, search, export |
| `testing` | synthetic radio fixtures with ground truth |
| `experimental/` | quarantined signalling decoders (see EXPERIMENTAL.md) |

## Detection

Energy alone is not enough for radio audio. The detector combines:

- a noise floor that falls fast and rises slowly, so it never chases a talker;
- hysteresis plus hang time, so pauses inside speech do not split a message;
- pre-roll and post-roll, so the first and last words survive;
- **spectral flatness measured inside the 200–3800 Hz speech band** — measured
  across the full spectrum, band-limited audio all looks tonal;
- **envelope modulation** (standard deviation of frame levels), the strongest
  single discriminator between speech (6–15 dB) and stationary static (1–3 dB);
- a bounded squelch-tail trimmer that can never consume a whole transmission.

Content flags (`likely_noise`, `likely_tone`) gate the ASR call, so static and
courtesy beeps never cost a transcription.

## Extension points

- **Frequency sync**: `RadioProfile` is the single source of channel metadata.
  A CAT/serial plugin would update it; nothing else needs to change.
- **Engines**: implement `TranscriptionEngine` / `TranslationEngine` and add a
  factory entry in `providers/__init__.py`.
- **Sources**: subclass `AudioSource`; replay and live already share it.
- **Detectors**: implement `TransmissionDetector`.
