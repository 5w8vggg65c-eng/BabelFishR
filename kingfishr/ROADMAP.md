# KingFishR roadmap

KingFishR is BabelFishR's offline capture, transcription and translation
core plus specialised SDR features. This page separates what exists from
what is planned. **Nothing under "Not built" exists in this repository**,
and no capability below it is claimed.

## Built (moved from BabelFishR, with its history)

- SDR++ receiver input: start/show SDR++, tune over rigctl, receive its
  network audio sink (`kingfishr/receiver/`). Verified against stubs and
  synthetic audio in tests; the receiver path still needs verification on
  real hardware (see BabelFishR `docs/LIVE_CAPTURE.md`, "Still to verify").
- DSD-neo digital-voice decoding of the receiver stream and of stored
  recordings (`kingfishr/analysis/`, `kingfishr/digital.py`).
- Recorded-IQ reference signal source (`kingfishr/sources.py`).
- Experimental signalling decoders, opt-in and validated against synthetic
  signals only (`kingfishr/experimental/`, `KINGFISHR_EXPERIMENTAL=1`).

## Not built (roadmap only)

- Drone RF scanning, identification and alerts.
- Direction finding / foxhunting.
- ATAK integration.
- Mesh networking.
- On-body EUD / SDR / antenna integration.

Each of these would need its own design, hardware validation and safety
review before any claim is made. Receive-only remains a constraint: nothing
in KingFishR transmits or keys a radio.
