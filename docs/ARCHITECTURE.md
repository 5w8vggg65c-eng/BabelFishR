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

## Capture first, classify second

The Priority-0 invariant. A transmission is written to disk **and** inserted
into the database before anything classifies, transcribes, translates or
analyses it.

Classification (`ContentClass`: speech / noise / tone / digital-suspected /
unknown) is *advice about processing*, never a gate on persistence. The
settings reflect that split explicitly:

- `min_duration` and the open threshold decide whether something is an **event
  at all**, and therefore whether it is recorded;
- `auto_process_*` decide only whether an **ASR call happens automatically**.

The operator can always overrule the classifier per transmission (*Transcribe
anyway*, *Analyze as digital*). An earlier build discarded events classified as
broadband noise before the recorder ever saw them, which meant a misclassified
digital burst was simply gone.

Digital-vs-static uses amplitude-distribution statistics: static is Gaussian
(kurtosis ~3.0, crest ~3.8) while discrete symbols are sub-Gaussian (kurtosis
1.4-2.0, crest 1.2-1.7). This is a routing hint validated on synthetic
fixtures only, and it is deliberately incapable of affecting persistence.

## Durability

Consequences of writing before processing:

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
| `modes` | operating modes, offline guards, Application Support paths |
| `readiness` | Field Check: real smoke tests, no downloads |
| `preparation` | the only code permitted to download anything |
| `analysis/` | DSD-neo as an optional local post-processing provider |
| `sources` | optional SDR `SignalSource` extension point |
| `ui/theme` | appearance-adaptive semantic styling |
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
