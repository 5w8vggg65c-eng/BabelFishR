# macOS + FalconClaw validation procedure

Nothing in this document has been executed by the developer environment: it was
written on Linux with no radio attached. This is the procedure to run the first
time you connect real hardware, and it is where the project genuinely gets
tested.

Work through it in order. Each step tells you what "good" looks like and what
to do when it isn't.

---

## 0. Hardware chain

```
Radio
  └─ radio-specific FalconClaw downlead
      └─ FalconClaw FC-PTT
          └─ FalconClaw Nexus-to-3.5 mm AUX adapter, used in reverse
              └─ computer audio input
                  └─ BabelFishR
```

**Use a USB audio interface if you can.** Modern MacBooks have no dedicated
line input: the 3.5 mm jack is a headset (TRRS) port, and whether it presents a
usable microphone input depends on the adapter's wiring and impedance. A cheap
USB audio interface with a real line input gives predictable levels and removes
a whole class of intermittent problems.

Set the radio's volume to about a third before connecting anything. Accessory
outputs can be much hotter than a computer input expects.

---

## 1. Install and launch

Normally: download `BabelFishR-macOS-arm64.dmg` from Releases, open it, drag
BabelFishR to Applications, and launch it. While builds are unnotarized the
first launch needs right-click ▸ Open ▸ Open.

The packaged binary is also the CLI, so everything below works against the
installed app with no Python involved:

```bash
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR doctor
```

From source instead:

```bash
cd BabelFishR
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[gui,audio,asr]"
babelfishr doctor
```

`doctor` should report a PortAudio version, at least one input device, PySide6,
and a real transcription engine. Fix anything it flags before continuing.

## 2. Microphone permission

macOS gates *all* audio input behind a permission prompt, including line inputs
on a USB interface.

- Launched from a terminal: the prompt belongs to **Terminal** or **iTerm**,
  not to Python. Approve it there.
- If no prompt appears, go to **System Settings ▸ Privacy & Security ▸
  Microphone** and enable your terminal (or the packaged app).
- After changing the setting, **quit and relaunch the terminal.** The change
  does not apply to an already-running process.

Symptom of a denied permission: devices are listed, capture starts without
error, and every sample is digital silence.

## 3. Device list

```bash
babelfishr devices
```

Expect your USB interface (or `External Microphone` for the 3.5 mm jack) with a
non-zero input channel count. Note the index.

If the direct 3.5 mm connection does not appear: macOS only exposes an input on
that jack when it detects a TRRS device presenting a microphone. Many adapters
do not. Confirm in **System Settings ▸ Sound ▸ Input** — if macOS does not list
it there, BabelFishR cannot see it either. Use a USB interface.

## 3a. Input selection, persistence and disconnection

**This is the section that cannot be simulated, and it is the one that matters
most.** Everything about device identity has been tested only against a fake
device list. These steps are the first time any of it meets CoreAudio.

With the interface connected:

- [ ] The Audio input panel opens on **"Choose an audio input"**. Nothing is
      selected — not the MacBook microphone, not the system default.
- [ ] Your interface is listed by a name you recognise, marked as an external
      input, with a plausible channel count.
- [ ] The MacBook microphone is listed separately and marked *built-in
      microphone*.
- [ ] The system default is *labelled* as such but not selected.

Select your interface, then:

- [ ] The status line reads `INPUT: <your interface> — CONNECTED`.
- [ ] "Lock input to this device" is already ticked.
- [ ] Speaking into the radio moves the level meter beside that line.

Then, one at a time:

- [ ] **Quit and relaunch.** The same interface is selected again, by name.
- [ ] **Unplug it, then relaunch.** Nothing is selected. The line reads
      `NOT CONNECTED` and names the interface. Pressing Start refuses, offers
      *Rescan / Choose Different Input / Record Later*, and **does not** fall
      back to the MacBook microphone.
- [ ] **Replug it into a different USB port** and press Rescan. It is
      recognised and selected again, even though its position in the device
      list has changed.
- [ ] **Plug in a second, different USB audio device** while yours is
      unplugged. Still nothing is selected, and the line still names the
      missing interface.
- [ ] **Unplug it mid-watch.** The line turns red and reads
      `RADIO INPUT DISCONNECTED`. Nothing new is recorded. Everything already
      recorded is still in the timeline and still plays back.
- [ ] **Replug it mid-watch.** Monitoring resumes on the same interface. Both
      times appear in `~/Library/Application Support/BabelFishR/Logs/`.
- [ ] Confirm from the recordings that nothing captured during the disconnected
      period came from the MacBook microphone.

Then check the command line agrees with the window:

```bash
/Applications/BabelFishR.app/Contents/MacOS/BabelFishR input
```

- [ ] It names the same device, says how it was identified (`coreaudio-uid` on
      a Mac), and reports CONNECTED.

If your Mac reports `composite` rather than `coreaudio-uid`, the CoreAudio UID
could not be read and identification has fallen back to name plus host API plus
channel count. That still works, but two identical interfaces cannot be told
apart; say so before relying on a two-radio setup.

## 4. Levels

```bash
babelfishr level --device <index>
```

Key up a test transmission from a second radio.

| What you see | What it means | What to do |
|---|---|---|
| Bar barely moves, rms below -60 | nothing is arriving | check the cable, permission, and the selected device |
| Speech peaks near -12 dBFS | correct | continue |
| `CLIP!` appears | input overdriven | turn the radio's volume down, then the interface gain |
| Steady mid-level with no traffic | squelch open, or hum | close the squelch; for hum see step 9 |

## 5. Calibrate

With the channel idle and nobody talking:

```bash
babelfishr calibrate --device <index> --seconds 10
```

It reports the noise floor and suggests a detection threshold. A healthy idle
floor sits between -60 and -30 dBFS. The GUI has the same workflow behind the
**Calibrate** button, with an "apply" option.

## 6. Ten-second test recording

```bash
babelfishr test-record --device <index> --seconds 10 --output test.wav
```

Key up during the recording. Then **listen to `test.wav`**. This is the single
most informative check in the whole procedure: if the audio sounds wrong here,
no amount of software configuration will fix it downstream.

Then run that same file through the full pipeline:

```bash
babelfishr replay test.wav
```

## 7. Segmentation check

Record a deliberate test with a second radio:

1. Idle for 5 seconds.
2. Transmit "one, two, three" for about 3 seconds. Unkey.
3. Idle for 3 seconds.
4. Transmit a sentence with a deliberate 0.5 s pause in the middle. Unkey.
5. Idle for 3 seconds.
6. Transmit two words, briefly. Unkey.
7. Idle for 5 seconds.

```bash
babelfishr replay <recording>.wav
```

Expected: **three** transmissions, in order; the one with the internal pause
stays a single event; each recording starts before the first word; no
transmission ends with a burst of squelch noise.

| Problem | Fix |
|---|---|
| One transmission split in two | raise `detector.hang_time` |
| Several merged into one | lower `detector.hang_time` |
| First word clipped | raise `detector.pre_roll` |
| Squelch tail on the end | ensure `detector.trim_squelch_tail` is true |
| Quiet transmissions missed | lower `detector.open_margin_db`, or recalibrate |
| Noise recorded as traffic | raise `detector.open_margin_db` |

Write your settings out with `babelfishr config --write`.

## 8. Real transcription and translation

```bash
babelfishr replay <recording>.wav --asr faster-whisper --translate argos
```

The first run downloads the Whisper model. Confirm the transcript matches what
was actually said. If the model is too slow on your traffic, try `--asr` with a
smaller model configured, or a larger one for better accuracy — an M-series Mac
with 24 GB handles `small` and `medium` comfortably.

## 9. Troubleshooting

**Silence.** Permission (step 2); wrong device; radio volume at zero; a cable
in the wrong socket; an adapter that does not present a microphone to macOS.

**Hum or buzz.** A ground loop, common when the laptop is on mains power.
Try running on battery; if the hum vanishes, that was it. Use a ground-loop
isolator, or a USB interface with isolated inputs.

**Clipping.** Reduce the radio's volume first, the interface gain second.
The radio's accessory output is often line level, not headphone level.

**Wrong input selected.** macOS may switch inputs when a device is plugged or
unplugged. Re-check **System Settings ▸ Sound ▸ Input** and the device selector.

**Truncated or split messages.** See the segmentation table in step 7.

**Device disappears mid-session.** BabelFishR retries with backoff and reports
it in the status bar. If it does not come back, the USB device likely
re-enumerated under a new index — reselect it. Enable safety recording
(`audio.safety_recording.enabled`) so a gap like this does not lose traffic.

**Transcription is nonsense.** Check the recording sounds right first. Radio
audio is band-limited and often clipped; a larger Whisper model helps, and the
glossary helps a lot with callsigns and place names.

## 10. Before relying on it

- [ ] `babelfishr doctor` reports no problems
- [ ] Every box in section 3a is ticked, including the disconnect and replug
- [ ] `babelfishr input` names the interface and says how it identified it
- [ ] The test recording sounds correct when played back
- [ ] The segmentation test yields exactly three transmissions
- [ ] Pre-roll preserves the first word
- [ ] No recording ends in a squelch tail
- [ ] Real transcription matches what was said
- [ ] Translation is into the right language
- [ ] Recordings appear where Help ▸ "Where are my recordings?" says
- [ ] The session survives quitting and relaunching the app
- [ ] Export produces a bundle whose audio plays

Until every box is ticked, treat the output as unverified.
