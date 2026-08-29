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

### In the app (the normal path)

On first launch the setup assistant appears by itself. Choose a speech model
and the languages you need, press **Prepare now**, and it downloads, verifies
and runs a real Field Check — on a background thread, with a live log and a
cancel button. Cancelling is safe: a partly downloaded model is detected as
*incomplete* and repaired next time, never mistaken for a good one.

Field readiness is only claimed, and the mode only switched to Field Offline,
once the model and the requested routes actually load with downloads disabled.

If you would rather not prepare yet, choose **Record only for now**. That
choice is remembered.

### From a terminal (the same work)

```bash
babelfishr prepare-field --asr-model small \
  --language es-en --language de-en --language uk-en
```

The bundled binary is the same program, so this works from an installed app:

```bash
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR prepare-field --asr-model small
```

This:

1. checks free space against the model's size;
2. downloads the Whisper model with
   `faster_whisper.utils.download_model(name, output_dir=<models>/<name>)`
   into the app's own folder — **not** a Hugging Face cache, so loading never
   depends on a cache surviving, the OS cannot purge it, and an app upgrade
   cannot delete it. The directory is verified to contain `model.bin`,
   `config.json` and a tokenizer asset before it counts as prepared;
3. installs the Argos language packs you named;
4. **re-opens the model with downloads disabled** and transcribes a fixture,
   which is the only way to know that offline loading actually works;
5. runs a real translation through each installed pair;
6. records a manifest of what was installed;
7. finishes by running Field Check as if the network were already gone.

Language packs are managed separately too:

```bash
babelfishr languages list                # usable routes, direct and pivoted
babelfishr languages list --available    # what could be installed (needs internet)
babelfishr languages install es en
babelfishr languages remove es en
```

Packs install into `~/Library/Application Support/BabelFishR/language-packs/`
(`ARGOS_PACKAGES_DIR`, set before Argos is imported — it resolves that
directory once, at import time). `languages list` prints the directory it is
reading and marks routes that work by pivoting through another language, since
Argos composes those and they are genuinely usable offline.

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

5. **Prove nothing tries to reach out.** The guarantee is architectural — in
   Field Offline no cloud provider is constructed and no download path is
   reachable — and the test suite proves it with sockets refusing
   (`pytest tests/test_offline_integration.py`). To confirm on your own
   machine, watch for network attempts while monitoring:
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
