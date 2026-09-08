# BabelFishR — Mac bench checklist (no Terminal needed)

**This checklist is for the next candidate build — one made from a commit that
includes these features (sections I–O are new and are not in run 21).** Do not run it against the candidate you already have
installed: several steps below describe behaviour that build does not contain,
so they would fail for the wrong reason. Wait for a new download link.

This is written for someone who has never used Terminal and does not want to.
Everything below is done by clicking. Where a step says to type, it is typing
into an ordinary box inside BabelFishR, never a command.

Three things you will see a lot:

- **The menu row** is a row of four words — **File  View  Tools  Help** —
  drawn *inside the BabelFishR window*, directly under the window's own title
  bar and above everything else in the window. Every command in this list that
  says "View ▸ …" or "Tools ▸ …" is reached by clicking one of those four words
  and then the item in the small menu that drops down. (Earlier builds put
  these menus in the Mac's strip at the very top of the screen. You reported
  that the menus you found there were the desktop's, and that BabelFishR
  offered none of these commands that way. They now live in the window. The
  Mac's top strip still shows a **BabelFishR** menu with **Quit**; that part is
  the system's and is unchanged.)
- **The badge row** is the row of small coloured labels under the menu row
  and the toolbar: the **Operating mode** button, the readiness
  chip (**Ready**, **Partly ready**, **Not ready** or **Checking**) and the
  activity indicator (**Idle**, **Listening**, **Receiving**, **Transcribing**,
  **Translating**).
- **The ⋯ button** is the small three-dots button at the right-hand end of each
  message bubble. Every action on a message — play, edit, retry, transcribe
  anyway, export — is in the menu that opens from it. There is no right-click
  menu on a bubble.

Work through the steps in order. Each one says what to do, then what "good"
looks like. If something does not match, note the step number and what you saw
instead, and carry on unless the step says to stop.

You reported steps 1–8 of the earlier checklist good on the previous
candidate (install, launch, microphone permission, transcription and
translation). Those are not repeated here.

**About section letters.** The list you were given in conversation for the
run-20 candidate used letters A–G, and your results ("A: GOOD, B: GOOD,
C: GOOD, D: GOOD, E: NEGATIVE, F: GOOD, G: GOOD") are recorded under *those*
letters. Your E, by your own description, was the menu-dependent Search and
Review check — which is section **F** in this document. Those results stand;
you do not need to redo them. The only thing to retest on the next candidate
is the menu check, now in section F below.

---

## A. Install the new candidate over the old one

1. If BabelFishR is open: make sure the big button reads **Start monitoring**
   (if it reads **Stop monitoring**, click it once and wait for it to change).
   Then quit BabelFishR: press ⌘Q, or click **BabelFishR** in the Mac's strip
   at the top of the screen and choose **Quit BabelFishR**. Nothing is lost by quitting; recordings and
   Sessions are already saved.
2. Download the new `BabelFishR-macOS-arm64.dmg` from the link you are given.
   It lands in **Downloads**.
3. Double-click the downloaded file. A small window opens showing
   **BabelFishR**, **Uninstall BabelFishR** and a shortcut to **Applications**.
4. Drag **BabelFishR** onto the **Applications** shortcut in that window. When
   macOS asks whether to **Replace** the existing one, choose **Replace**.
   *Good:* the copy finishes with no error. Your recordings and Sessions live
   in a separate folder and are untouched.
5. Open **Applications** (in Finder: **Go ▸ Applications** in the menu bar) and
   double-click **BabelFishR**.
   - *If it opens*, continue to section B.
   - *If macOS says it cannot verify the developer, or that the app cannot be
     opened*, this is expected: the build is not notarized. Close that message.
     Then open **System Settings** (Apple menu ▸ System Settings), choose
     **Privacy & Security** in the left column, scroll down to the
     **Security** section, and click **Open Anyway** next to the note about
     BabelFishR. Confirm if asked. This is needed once per new build. Apple's
     own description of this is at https://support.apple.com/en-us/102445.
   - Do **not** change any setting that allows all apps from anywhere.
   *Good:* the BabelFishR window appears and your previous Sessions are listed
   as tabs.

> The checksum step from the earlier instructions is dropped. It needed
> Terminal and it did not show you a result. It is not required to use the
> application. Whether the earlier download was intact is therefore still
> unknown; nothing in this list changes that.

## B. The readiness chip says "Ready"

6. Look at the **badge row**. For a few seconds after launch the readiness chip
   reads **… Checking**.
   *Good:* it then changes to **✓ Ready**. (Older builds said "Field ready".)
   If it reads **◑ Partly ready** or **⚠ Not ready**, click the chip: a window
   opens listing every check and what failed. Note what it says and continue.

## C. The Record Only warning goes away when you leave Record Only

7. In the badge row, click the button that reads **Operating mode: …**. Choose
   **Record Only** from the menu.
   *Good:* a yellow warning line appears near the top of the window saying that
   in Record Only, transmissions are recorded but not transcribed.
8. Click **Operating mode: …** again and choose **Field Offline**.
   *Good:* the Record Only warning disappears. If the machine has a different
   problem at the same time (for example an audio input that stopped
   responding), that other warning stays — only the Record Only one goes.

## D. Messages show a date as well as a time

9. Look at any message bubble. The first item in its small grey header line
   used to be only a time like `14:32:07`.
   *Good:* it now reads `2026-09-07 14:32:07` — date, then time, in your Mac's
   own time zone. Scroll to a message from an earlier day. *Good:* it shows
   that earlier date, not today's.

## E. Rename a Session tab

There are three ways. Try whichever is comfortable; if it does nothing, try
another and note which ones worked.

10. **Right-click the tab** (or hold Control and click it). A small menu
    appears with **Rename Session…**. Choose it.
11. **Or** click the tab once to select it, then click the **Rename…** button
    to the right of the tabs.
12. **Or** double-click the tab.

    In every case: a box opens with the current name already filled in. Type a
    new name and press **Return**.
    *Good:* the tab shows the new name; a short message at the bottom edge of
    the window reads *Renamed "old" to "new"*; the messages in that tab are
    exactly the ones that were there before. If monitoring is recording into
    that Session, the notice *Recording into "…"* shows the new name.
13. Try once more and press **Escape** (or click **Cancel**) instead.
    *Good:* nothing changes, and the message at the bottom reads *Session name
    unchanged*.

> **Deleting a tab is not in this build.** It is waiting on your decision:
> when a Session tab is removed, should its messages and recordings be **kept**
> somewhere you can still open them, or **permanently erased**? Both are
> possible. Nothing will be built until you say which. Whether the **General**
> tab should be deletable at all is part of the same decision.

## F. Search and Review live in the View menu — inside the window

**View** is the second word in the menu row inside the BabelFishR window,
under its title bar. Search and Review both work **inside the thread you are
looking at**: they filter the current tab's messages in place, and **View ▸
Show all transmissions** brings the full thread back. No separate window opens.

14. First, make a phrase that exists in exactly one Session. Click a Session
    tab, press **Start monitoring**, and say clearly: *"purple giraffe
    seventeen"*. Wait for the bubble to show those words (or close to them),
    then press **Stop monitoring**. Note which tab this is — call it Tab A.
15. Stay on Tab A. Click **View** in the menu row inside the window, then
    **Search transmissions…**. A box titled **Search** opens.
    Type `giraffe` and press Return.
    *Good:* the thread shrinks to only the matching message(s), and the
    message at the bottom of the window says how many matched and that
    *View › Show all transmissions* goes back.
16. Go to **View ▸ Show all transmissions**.
    *Good:* the full thread for Tab A is back, newest at the top.
17. Go to **View ▸ Review queue**.
    *Good:* the thread shrinks to the messages in Tab A that BabelFishR is
    unsure about (low confidence). It may be empty; empty is a valid result.
    The message at the bottom says how many need review. Then **View ▸ Show
    all transmissions** to restore the thread.
18. Click a **different** Session tab (Tab B) and repeat step 15 with
    `giraffe`.
    *Good:* **0 matches** — search stays inside the tab you are on, and
    "giraffe" was only ever said in Tab A. Then **View ▸ Show all
    transmissions**.

## G. The activity indicator is truthful when nothing is being monitored

You do not need to catch the brief **Transcribing** flash. What matters is
where the indicator *ends up*.

19. With monitoring **stopped** (the big button reads **Start monitoring**),
    click the **⋯** button on any message bubble and choose **Transcribe
    anyway** (if that item is not offered, choose **Retry processing**). Wait
    about ten seconds.
    *Good:* the activity indicator ends on **○ Idle**. It must **not** read
    **Listening** — nothing is listening — and it must not stay on
    **Transcribing**. The big button still reads **Start monitoring**. The
    bubble's text may have changed; that is fine.
20. Press **Start monitoring**, say one short sentence, and press **Stop
    monitoring** straight away, before the words appear in the bubble.
    *Good:* within about ten seconds the transcript appears in the bubble, and
    the indicator ends on **○ Idle**, not **Listening**.
21. Press **Start monitoring** and leave it running for a moment without
    speaking.
    *Good:* the indicator reads **◉ Listening** while the button reads **Stop
    monitoring**. Press **Stop monitoring**. *Good:* it returns to **○ Idle**.

## H. Quit and reopen

22. Quit BabelFishR (⌘Q, or **BabelFishR ▸ Quit BabelFishR** in the Mac's top
    strip).
    Open it again from Applications.
    *Good:* the same Session tabs are there with the same names, the tab you
    had selected is selected again, and the messages are all present with
    their dates.

## I. Playback on a message bubble (new)

Every bubble that has a recording now has a small **▶ Play** button on its
bottom row, to the left of the other buttons.

23. Find a bubble whose recording is **shorter than five seconds** (the grey
    header line shows the length, e.g. `2.3s`). Click its **▶ Play**.
    *Good:* the recording plays; while it plays the button reads
    **Playing…** and is greyed out; no extra controls appear; when it ends the
    button reads **▶ Play** again and works again.
24. Find a bubble whose recording is **longer than five seconds** (e.g.
    `7.8s`). Click its **▶ Play**.
    *Good:* a row of controls opens across the bottom of *that bubble only*:
    **⏪ 5 s**, **⏸ Pause**, **⏹ Stop**, **⏩ 5 s**, and a time such as
    `0:03 / 0:07`.
25. Click **⏸ Pause**. *Good:* sound stops, the time stops counting, the
    button now reads **▶ Play**. Click **▶ Play** in that row. *Good:* sound
    continues from where it paused, not from the start.
26. Click **⏩ 5 s** and **⏪ 5 s**. *Good:* the time jumps forward and back
    by about five seconds and never goes past the end or before the start.
27. Click **⏹ Stop**. *Good:* sound stops, the control row disappears, and the
    bubble shows the small **▶ Play** again.
28. Click **▶ Play** on the long recording again and let it run to the end.
    *Good:* when it finishes, the control row disappears by itself and **▶
    Play** is back.
29. Start playing one long recording, then click **▶ Play** on a different
    bubble. *Good:* the first bubble's controls disappear and the second
    bubble's open; only one recording plays at a time.
30. While a long recording is playing, click a different Session tab.
    *Good:* the sound stops.

## J. Tab colours (new)

31. Right-click a Session tab and choose **Tab colour…**. Pick a colour and
    click **OK**.
    *Good:* a small coloured square appears beside that tab's name; the name
    itself is unchanged and readable; no other tab changes; the line at the
    bottom of the window says the colour was set.
32. Right-click the same tab and choose **Default tab colour**.
    *Good:* the square disappears. Right-click again: **Default tab colour**
    is greyed out because there is no colour to reset.
33. Set a colour again, rename the tab (right-click → **Rename Session…**),
    then quit and reopen BabelFishR.
    *Good:* the renamed tab still has its colour.

## K. Removing a message (new) — use a throwaway message

**Do this on a message you do not care about.** Make one first: press **Start
monitoring**, say "throwaway test one", wait for the bubble, press **Stop
monitoring**.

34. Click the **⋯** button on that bubble and choose **Remove message…**. A box
    offers two buttons and Cancel. Click **Cancel**.
    *Good:* nothing changes.
35. **⋯ → Remove message…** again, then click **Remove from thread (keep the
    recording and data)**.
    *Good:* the bubble disappears; the bottom line says it was removed and its
    data kept. Click **View → Search transmissions…**, type `throwaway`,
    Return. *Good:* 0 matches. **View → Show all transmissions**.
36. Click **View → Show removed messages** (a tick appears beside it).
    *Good:* the bubble is back, marked **Removed from thread — data kept**.
    Click its **⋯** and choose **Restore to thread**. Click **View → Show
    removed messages** again to turn it off. *Good:* the bubble is in the
    thread as normal.
37. Make a second throwaway message ("throwaway test two"). **⋯ → Remove
    message…**, then click **Delete permanently…**.
    *Good:* a second box lists exactly what will be deleted: the message, and
    the recording file(s) in BabelFishR's Recordings folder, by name. Click
    **Cancel**. *Good:* nothing changes. Repeat and this time click **Delete
    permanently**.
    *Good:* the bubble is gone; the bottom line says how many files were
    removed; **View → Show removed messages** does *not* bring it back; a
    search for `two` finds nothing. Quit and reopen: still gone.
    (If the box ever says some file could not be removed, note the wording,
    then use **Tools → Finish unfinished deletions…** and report what it
    says.)

## L. Removing a Session tab (new) — use a throwaway Session

**Do this on a Session you create for the purpose.** Click **+** beside the
tabs, name it `Throwaway`, and record one short message into it.

38. Right-click the **General** tab. *Good:* **Remove Session…** is greyed out;
    hovering it explains that General is kept for now.
39. Right-click the **Throwaway** tab and choose **Remove Session…**. The box
    says how many runs and messages it holds and offers **Hide this Session
    (keep everything)** or **Delete permanently…**. Click **Cancel**. *Good:*
    nothing changes.
40. Repeat and click **Hide this Session (keep everything)**.
    *Good:* the tab disappears; General is selected; the bottom line says it
    was hidden and everything kept.
41. Click **View → Show hidden Sessions**. *Good:* the tab reappears as
    **Throwaway (hidden)**. Right-click it → **Restore Session**. Turn **View
    → Show hidden Sessions** off. *Good:* the tab is back to normal with its
    message.
42. Right-click **Throwaway** → **Remove Session…** → **Delete permanently…**.
    *Good:* the second box lists the runs, messages and recording files by
    count. Click **Delete this Session permanently**.
    *Good:* the tab is gone; General is selected and unchanged; **View → Show
    hidden Sessions** does not show it. Quit and reopen: still gone.

## M. Stopping and quitting while a transcription is still running (new)

This behaviour is new and has only been exercised by automated tests on a
Linux machine; nothing here has been seen working on a Mac yet. It needs a
message long enough to still be transcribing when you act, so speak for ten
to fifteen seconds without a pause, then act immediately.

43. Start monitoring, speak for about fifteen seconds, stop speaking, and the
    moment the bubble shows **Transcribing…** click **Stop monitoring**.
    *Good:* the button changes to **Start monitoring** at once; the window
    still responds (scroll the thread, open a menu); the activity indicator
    still says **Transcribing** and the bottom line says how many
    transmissions are still finishing; then the transcript appears in the
    bubble and the indicator returns to **Idle**. *Bad:* the beachball, or a
    window that does not repaint for many seconds.
44. Repeat, and this time press **⌘Q** the moment the bubble shows
    **Transcribing…**. *Good:* the window stays open and responsive, the
    bottom line says it is quitting once the transmission finishes, and the
    application quits by itself when the transcript has been saved. Reopen
    it: the transcript is there. *Bad:* a frozen window, or a message whose
    transcript never arrived.
45. Repeat step 43, and while the indicator still says **Transcribing** click
    **Start monitoring** again. *Good:* a box explains that a saved
    recording is still being transcribed and to wait; nothing else changes.
46. Start monitoring, speak once for a few seconds and keep speaking, and
    press **⌘Q** while you are still talking. *Good:* the window stays open
    and responsive, then quits by itself. Reopen: the transmission that was
    still open when you quit is there, recorded and transcribed. *Bad:* a
    message that shows as captured but never transcribed, or no message.

## N. Search and Review stay true while monitoring (new)

Not in run 21 or any later build yet; needs a future candidate. Automated
tests cover it on Linux only.

47. Start monitoring and say a distinctive word - for example "pineapple" -
    once, clearly. Wait for its transcript. Then choose **View → Search…**,
    type `pineapple`, press Return. *Good:* only the message with that word
    is shown and the bottom line says "1 match(es)".
48. Still monitoring, say a sentence *without* the word. Wait for its
    transcript. *Good:* it does not appear in the search view and the bottom
    line still says "1 match(es)". Now say a sentence *with* the word.
    *Good:* it appears at the top and the bottom line says "2 match(es)".
    *Bad:* messages without the word appearing in the list.
49. Choose **View → Show all transmissions**. *Good:* every message is back,
    newest first, and new messages keep arriving at the top.

## O. Playback with a second message (new)

Not in run 21 or any later build yet; needs a future candidate.

50. With two messages longer than five seconds, press **▶ Play** on the
    older one and, while it plays, press **▶ Play** on the newer one.
    *Good:* the older one's controls collapse and the newer one's expand;
    one recording plays at a time. Press **■ Stop**. *Good:* the controls
    collapse and the sound stops.
    (The case of a message whose recording file has gone missing cannot be
    arranged by clicking alone; it is covered by automated tests only.)

---

## If something goes wrong

- In the menu row inside the window: **Tools ▸ Copy Diagnostic Report**. Paste
  the result into a message to whoever is helping you. BabelFishR itself sends
  nothing anywhere.
- **Tools ▸ Reveal Logs in Finder** opens the folder with the log file.

## What this checklist cannot test

No radio, USB radio interface, SDR dongle or FalconClaw PTT has been connected
to BabelFishR yet. Every step above uses the Mac's own microphone or recordings
already saved. Radio-frequency details in a message header, and anything about
a radio's behaviour, remain unverified until that hardware is available.
