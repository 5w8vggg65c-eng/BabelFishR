# BabelFishR — Mac bench checklist (no Terminal needed)

This is written for someone who has never used Terminal and does not want to.
Everything below is done by clicking. If a step tells you to type, it is typing
into a normal box inside BabelFishR, not a command.

Two words you will see a lot:

- **The menu bar** is the strip along the *very top edge of the screen* — above
  the BabelFishR window, on the same line as the Apple logo in the top-left
  corner and the clock in the top-right. When BabelFishR is the active
  application, that strip reads **BabelFishR · File · View · Tools · Help**. It
  is *not* inside the BabelFishR window itself, which is why it is easy to miss.
  If you do not see "View" up there, click once anywhere on the BabelFishR
  window to make it the active application, then look again.
- **The badge row** is the row of small coloured labels just under the toolbar
  inside the BabelFishR window. It shows the operating mode, the readiness chip
  and the activity indicator (Idle / Listening / Receiving / Transcribing).

Work through the steps in order. Each one says what to do, then what "good"
looks like. If something does not match, note the step number and what you saw
instead, and carry on to the next step unless it says to stop.

You already confirmed steps 1–8 of the previous checklist (install, launch,
microphone, transcription and translation). This list does not repeat them.

---

## A. Install the new candidate over the old one

1. Download `BabelFishR-macOS-arm64.dmg` from the link you were given. It lands
   in your **Downloads** folder.
2. Double-click the downloaded file. A small window opens showing **BabelFishR**,
   **Uninstall BabelFishR** and a shortcut to **Applications**.
3. Drag **BabelFishR** onto the **Applications** shortcut in that same window.
   When macOS asks whether to **Replace** the existing one, choose **Replace**.
   *Good:* the copy finishes with no error. Your recordings and Sessions are
   kept — they live in a separate folder that this does not touch.
4. Open **Applications** (in Finder, choose Go ▸ Applications from the menu
   bar), find **BabelFishR**, then **right-click it and choose Open**. If macOS
   says it cannot check the developer, click **Open** again in that dialog.
   This is needed once per new build because the build is not notarized.
   *Good:* the BabelFishR window appears and your previous Sessions are listed
   as tabs.

> The checksum step from the earlier instructions is dropped from this list.
> It needed Terminal and it did not show you a result. It is not required to
> use the application.

## B. The readiness chip says "Ready"

5. Look at the **badge row**. For a few seconds after launch the readiness chip
   reads **… Checking**.
   *Good:* it then changes to **✓ Ready**. (Older builds said "Field ready".)
   If it says **◑ Partly ready** or **⚠ Not ready** instead, click the chip: a
   window opens listing every check and what failed. Note what it says and
   continue.

## C. The Record Only warning goes away when you leave Record Only

6. In the badge row, click the button that reads **Operating mode: …**. A menu
   appears. Choose **Record Only**.
   *Good:* a yellow warning line appears near the top of the window explaining
   that in Record Only, transmissions are recorded but not transcribed.
7. Click **Operating mode: …** again and choose **Field Offline**.
   *Good:* the Record Only warning line disappears. If the machine has a
   different problem (for example an audio input that stopped responding), that
   warning stays — only the Record Only one goes.

## D. Messages show a date as well as a time

8. Look at any message bubble in the thread. The first item in its small grey
   header line used to be just a time like `14:32:07`.
   *Good:* it now reads `2026-09-07 14:32:07` — the date, then the time, both in
   your Mac's own time zone. Scroll down to an older message from a previous
   day. *Good:* it shows that earlier date, not today's.

## E. Rename a Session tab

There are now three ways. Try whichever is comfortable; try a second one if the
first does nothing.

9. **Right-click the tab** (or hold Control and click it). A small menu appears
   with **Rename Session…**. Choose it.
10. **Or** click the tab once to select it, then click the **Rename…** button to
    the right of the tabs.
11. **Or** double-click the tab.

    In every case: a box opens with the current name already filled in. Type a
    new name and press **Return**.
    *Good:* the tab now shows the new name; a short message at the bottom of
    the window reads *Renamed "old" to "new"*; the messages in that tab are
    exactly the ones that were there before. If a run is being recorded into
    that Session, the notice that says *Recording into "…"* shows the new
    name too.
12. Try it once more and press **Escape** (or click **Cancel**) instead.
    *Good:* nothing changes, and the message at the bottom reads *Session name
    unchanged*.

> **Deleting a tab is not in this build.** It is waiting on a decision from
> you: when a Session tab is removed, should its messages and recordings be
> **kept** somewhere you can still open them, or **permanently erased**? Either
> is possible. Nothing will be built until you say which.

## F. Search and Review live in the View menu

13. Click a Session tab that has messages in it.
14. Go to the **menu bar at the very top of the screen** and click **View**.
    The menu lists **Search transmissions…**, **Review queue** and **Show all
    transmissions**.
15. Choose **Search transmissions…**. A box titled **Search** opens. Type a word
    you know appears in one of *this* Session's messages and press Return.
    *Good:* only messages from the Session whose tab is selected are shown, and
    at least one matches. Messages from other Sessions do not appear.
16. Go to **View ▸ Show all transmissions**.
    *Good:* the full thread for that Session is back, newest at the top.
17. Go to **View ▸ Review queue**.
    *Good:* a list opens of messages from this Session that BabelFishR is unsure
    about (low confidence). It may be empty; empty is a valid result. Close it.
18. Click a *different* Session tab and repeat step 15 with a word from the
    first Session.
    *Good:* it is **not** found, because search stays inside the tab you are on.

## G. Activity indicator is truthful when nothing is being monitored

19. With monitoring **stopped** (the big button reads **Start monitoring**),
    right-click any message bubble and choose **Transcribe anyway** (or
    **Retry processing**).
    *Good:* the activity indicator in the badge row briefly shows
    **Transcribing** and then returns to **○ Idle**. It must **not** say
    **Listening** — nothing is listening. The big button still reads **Start
    monitoring**.
20. Press **Start monitoring**, speak once so a message is captured, then press
    **Stop monitoring** while the message is still being transcribed.
    *Good:* the transcript still appears in the bubble a moment later, and the
    indicator ends on **○ Idle**, not **Listening**.

## H. Quit and reopen

21. Quit BabelFishR (BabelFishR ▸ Quit BabelFishR in the menu bar, or ⌘Q).
    Open it again from Applications.
    *Good:* the same Session tabs are there with the same names, the tab you had
    selected is selected again, and the messages are all present with their
    dates.

---

## If something goes wrong

- Go to the menu bar, **Tools ▸ Copy Diagnostic Report**. Paste the result
  into a message to whoever is helping you. Nothing is sent anywhere by
  BabelFishR itself.
- **Tools ▸ Reveal Logs in Finder** opens the folder with the log file.

## What this checklist cannot test

No radio, USB radio interface, SDR dongle or FalconClaw PTT has been connected
to BabelFishR yet. Every step above uses the Mac's own microphone or recordings
already saved. Radio-frequency details in a message header, and anything about
a radio's behaviour, remain unverified until that hardware is available.
