# BabelFishR — Mac bench checklist (no Terminal needed)

**This checklist is for the next candidate build — one made from a commit that
includes these repairs.** Do not run it against the candidate you already have
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
