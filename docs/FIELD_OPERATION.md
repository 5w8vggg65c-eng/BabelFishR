# Field operation: preparation, readiness, and going offline

BabelFishR is meant to work with the network switched off. That is only true
once a speech model and language packs are on disk, so preparation is a
deliberate, explicitly online step — and everything after it is enforced.

---

## The three modes

| Mode | Cloud providers | Placeholder output | Downloads | Processing |
|---|---|---|---|---|
| **Field Offline** | cannot be constructed | refused | refused | local only |
| **Online / Setup** | allowed if explicitly selected | allowed | allowed | yes |
| **Record Only** | cannot be constructed | refused | refused | none — capture only |

Field Offline is enforced in code, not by convention: a cloud engine is never
*constructed*, so there is no object that could be called by accident, and a
missing local engine produces an honest failure rather than a silent fallback.

```bash
babelfishr mode                       # show the current mode
babelfishr mode --set field-offline   # persist a mode
```

**Nothing leaves the Mac because a local engine is missing.** If Argos is not
installed, translation fails and says so; it does not quietly reach for a cloud
service.

---

## One-time preparation (needs internet)

```bash
babelfishr prepare-field --asr-model small \
  --language es-en --language de-en --language uk-en
```

This:

1. checks free space against the model's size;
2. downloads the Whisper model into the app's own folder — **not** a cache
   directory, so the OS cannot purge it and an app upgrade cannot delete it;
3. installs the Argos language packs you named;
4. **re-opens the model with downloads disabled** and transcribes a fixture,
   which is the only way to know that offline loading actually works;
5. runs a real translation through each installed pair;
6. records a manifest of what was installed;
7. finishes by running Field Check as if the network were already gone.

Language packs are managed separately too:

```bash
babelfishr languages list                # installed packs, direct and via-en routes
babelfishr languages list --available    # what could be installed (needs internet)
babelfishr languages install es en
babelfishr languages remove es en
```

### Do not expect "any language"

BabelFishR translates exactly the pairs you installed. `babelfishr languages
list` shows them, including routes that work by pivoting through English.
Traffic in a language you have no pack for is still **recorded**, and still
transcribed if Whisper recognises it — only the translation is unavailable.

---

## Field Check

```bash
babelfishr field-check           # runs real smoke tests, downloads nothing
babelfishr field-check --json    # machine-readable
babelfishr field-check --no-smoke-tests   # fast, skips loading models
```

It reports, independently:

- audio backend, input devices, microphone permission (best effort — macOS has
  no reliable pre-flight API, so this warns rather than claims);
- recording directory writable, free storage;
- local ASR model **present**, **loadable**, and a **real transcription** of a
  bundled fixture;
- installed translation paths and a **real translation** through one of them;
- DSD-neo presence and version, if configured;
- SDR presence, if configured;
- that cloud processing and mock engines are disabled in this mode;
- an overall verdict: field ready / record only / not ready.

In the app this is **Tools ▸ Field readiness**, and the toolbar badge shows the
verdict at a glance.

---

## Zero-connectivity validation procedure

Run this after preparation and before relying on the app. It is the only way to
know offline operation is real rather than assumed.

1. **Prepare while online**, and confirm it finishes cleanly:
   ```bash
   babelfishr prepare-field --asr-model small --language es-en
   babelfishr field-check           # expect: READY for offline field operation
   ```

2. **Disconnect completely.** Turn off Wi-Fi *and* unplug Ethernet. On macOS:
   Control Centre ▸ Wi-Fi off. Verify:
   ```bash
   ping -c1 1.1.1.1        # must fail
   ```

3. **Re-run Field Check with no network:**
   ```bash
   babelfishr field-check
   ```
   It must still report **READY**. If it now fails, offline operation was not
   actually prepared — something was being fetched on demand.

4. **Set the mode and run a real replay offline:**
   ```bash
   babelfishr mode --set field-offline
   babelfishr replay some-recording.wav
   ```
   Confirm you get a real transcript, not placeholder text. Field Offline
   refuses mock engines, so if you see output at all it is genuine.

5. **Prove nothing tries to reach out.** Watch for network attempts while
   monitoring:
   ```bash
   # in one terminal
   sudo tcpdump -n -i any 'not host 127.0.0.1' -c 20
   # in another
   babelfishr listen --device <index>
   ```
   Key up a radio a few times. `tcpdump` should capture nothing attributable to
   BabelFishR.

6. **Monitor for ten minutes offline** with real traffic. Confirm transmissions
   appear, play back, transcribe and translate.

7. **Quit and relaunch, still offline.** Confirm previous sessions are present,
   search works, and export produces a bundle whose audio plays.

8. **Force a failure and confirm nothing is lost.** Rename the model directory,
   relaunch, and monitor. Recording must continue, the UI must say
   transcription is unavailable, and the recordings must be retryable once the
   directory is restored.

If any step fails, the honest position is that the machine is **Record Only**
ready, not field ready. Recording still works, which is the part that cannot be
repeated later.

---

## When readiness fails

The app never blocks recording because processing is unavailable. Choose
**Record Only**: every transmission is captured to an immutable WAV and logged,
and the recordings can be transcribed later once a model is prepared. Losing
traffic is permanent; a delayed transcript is not.
