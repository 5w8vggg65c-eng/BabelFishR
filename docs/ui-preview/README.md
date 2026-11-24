# BabelFishR interface: v5 field console

## Accepted target

Eric approved the **v5** concept on 2026-11-23 ("Alright, lets put it
together"). It supersedes every earlier direction on this branch: the
OpenDMR-inspired navy/cyan theme, the sans-serif and light variants, and the
Apple-style concepts.

| File | What it is |
|---|---|
| `target/v5-approved-with-waveform.png` | The authoritative target: the base layout plus a recording waveform. |
| `target/v5-base-without-waveform.png` | The same layout without the waveform. Eric explicitly liked this base. |

**Provenance.** Both are synthetic concept images (sample data only),
generated with Higgsfield by Codex on Eric's account and handed to this
branch. They are original BabelFishR concepts. Their visual influences were
Open25 Classic and Jake Garrett's UV-5RM menus; no art was copied from
either. They are a visual direction, not a specification of features: no RF,
transmit, PTT or radio-control function comes from them.

- `v5-approved-with-waveform.png`: SHA-256
  `2414b346ff1c111c965ec5f4c83e1ba6f733861cf9ccec8267fb51967af19392`
  (`hf_20261123_090001_c282edeb-…`).
- `v5-base-without-waveform.png`: SHA-256
  `81b2ac6085b87af2e90281193f424d44c2164ad626c2c6314accffc3a508b7f9`
  (`hf_20261123_084240_142b7198-…`).

**What the target asks for.**
- A black/charcoal ground, a teal identity/status band, and turquoise
  rectangular selections.
- Regular-width, normal-weight monospaced technical type, thin rules and
  compact controls.
- Two panes: Sessions, input and local engines on the left; the waveform,
  the original/translation review list, the selected audio and the actions
  on the right.

## The implementation

This is the real application window (`babelfishr/ui/main_window.py`), not a
mock-up. Every label shows live application state; nothing in the product
is sample data.

| Concept element | In the application |
|---|---|
| `BabelFishR` / `CONCEPT / SAMPLE DATA` | Identity row. The right side is empty in the app; only the preview script fills it ("UI PREVIEW / SAMPLE DATA"). |
| `FIELD OFFLINE` | The real operating-mode control (a menu of the three modes), showing whatever mode the app is in. |
| `SOURCE: RECORDED FILE` | What is really being read. During a replay it shows `SOURCE: RECORDED FILE · <file name>`, and `· ended` once the file is read through; a long or non-ASCII name is elided in the middle, with the full text in the tooltip. Otherwise it shows the selected input's live status. It never implies a microphone or radio is being received during a replay, and no "recording the room" warning appears then. |
| `IDLE` | The real monitoring state (Idle / Armed · Listening / Audio active (est.) / …), shown in capitals. "Audio active" is estimated from the audio level; no supported input reports receiver squelch or RX state. |
| Monitor · Sessions · Models · Settings · Help | The app's real menus: **File · View · Receiver · Tools · Help** (see deviations). |
| SESSIONS list | The named Sessions (the former tab row), drawn as a list. Rename, colours, context menu and hide/remove work as before. |
| Saved sessions · Search | **Hidden sessions** (View ▸ Show hidden Sessions) and **Search** (focuses the search field). |
| INPUT | The real input chooser and status, the `source > target` language line, the radio profile (or the warning that none is selected, so no channel or frequency is recorded), the receiver status line, and Calibrate. |
| LOCAL ENGINES | `Speech:` / `Translation:` as the app selected them, plus field readiness (click for the report). |
| RECORDING WAVEFORM | The selected message's **own** recording, drawn from its PCM samples (see below). |
| All messages · Needs review · Bookmarked · Search | Show all, the Review queue, a Bookmarked view, and Search: all real store queries. They stay true as traffic arrives. |
| TIME / ORIGINAL / TRANSLATION / REVIEW | The message list as columns. REVIEW shows `CHECK` (needs review), `FAILED`, or `–`. The selected row is turquoise; an amber edge marks review, a red edge marks failure. |
| SELECTED AUDIO | The selected message's start–end time on this computer's clock and its length, plus **Play original** / **Bookmark** / **Add note**. These use the same controller, flags and dialogs as each message's own controls. The hint text appears only when it applies. |
| Open recording · Start monitoring · Session options · Export | File ▸ Replay WAV, the real Start/Stop, the Session Options panel (radio profile and languages), and File ▸ Export session bundle. |
| Footer | Messages shown, how many need review, and the recording setting as `Keep original audio: on/off`. That is the setting, not a claim that every older file still exists. The app's transient status messages appear on the left. |

### Waveform

`babelfishr/ui/waveform.py`:
- **Source:** a min/max envelope streamed from the WAV with the standard
  `wave` module, in 65,536-frame chunks. Memory stays bounded however long
  the file is.
- **Fidelity:** each of up to 1,200 buckets keeps its true signed minimum
  and maximum, so transients survive and silence stays flat. A display gain
  (at most ×20) applies only above −40 dBFS, so hiss is never inflated into
  "signal". When a clip is drawn larger than recorded, the title says so
  ("shown ×N").
- **Short files:** a file shorter than its own header is labelled "file
  shorter than its header: N s readable".
- **Off the UI thread:** loading runs on a background thread. A result for
  a message that is no longer selected is discarded.
- **Honest states:** no selection, no recording, missing file, unreadable
  file and empty clip are each stated in words. A substitute waveform is
  never drawn.
- **Ruler:** clip-relative, starting at 00:00. Each message is its own
  recording, so no session clock or session range is shown.
- **Playback marker:** appears only while *this file* is playing or paused,
  through the existing playback controller. The same message's decoded
  audio is a different file and moves no marker here. **Play original**
  starts the original even while that decoded audio plays, and only pauses
  the original itself.
- **Loading:** one worker per waveform, holding only the latest request.
  Selecting another message, clearing, closing the window or deleting the
  widget cancels the read in progress at its next chunk. Reads are bounded
  in bytes (4 MiB), whatever channel count the header declares. Nothing on
  the UI thread waits for a read.
- **Selection:** clicking anywhere in a row, including its transcript text
  or its buttons, selects that row. Pressing the filter that is already on
  screen does nothing, so playback and the selection survive.
- **Read-only:** no click-to-seek. A truthful read-only waveform was
  preferred over interaction the playback service doesn't fully support
  yet.

### Deviations from the target, and why

1. **Menu names.** The concept shows Monitor/Sessions/Models/Settings/Help;
   the app keeps **File/View/Receiver/Tools/Help**. Those are the real menus
   with real actions, and renaming them would claim pages that don't exist.
   The styling (turquoise underline on the active item) follows the concept.
2. **Band colour.** `#287882` instead of the concept's `#338F99`, so the
   band's off-white text reaches 4.7:1 contrast (the concept's teal gives
   3.5:1).
3. **Waveform range.** The concept shows a 30-second session recording with
   a highlighted message range. In the app each message has its own clip,
   so the waveform shows the selected clip and its own length. It shows no
   session range, because none exists.
4. **Row content.** Each row also shows the message's date, duration,
   language and any measured signal values, its status, and its `⋯`
   actions menu. These are existing functions the concept didn't show.
5. **Banners.** The app's real warnings (missing audio backend, engines
   unavailable) and the local-processing notice appear above the waveform
   when they apply.
6. **Input area.** A real device chooser, "Remember for profile", the live
   status line and the level meter replace the concept's static text lines.
7. **Light appearance.** The concept is dark only. When the system is in
   light appearance the same layout uses a daylight palette (the teal band,
   turquoise selection and amber review marker are kept).

## Screenshots

`screenshots/` holds genuine renders of the real window. They were made
offscreen on Linux with Qt's Fusion style (**not** macOS), using synthetic
sample data and synthetic PCM audio, by:

```bash
python tools/ui_preview.py --appearance dark --screenshot docs/ui-preview/screenshots
```

```bash
python tools/ui_preview.py --appearance light --screenshot docs/ui-preview/screenshots
```

`tools/ui_preview.py` opens the production window on a temporary database:
- `BABELFISHR_HOME` is a temporary directory; `HOME` is unchanged.
- The engines are off.
- Monitoring is never started and nothing is downloaded.
- The identity row says "UI PREVIEW / SAMPLE DATA".
