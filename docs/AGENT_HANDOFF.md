# Agent handoff — BabelFishR v0.3.0-alpha.2

Written for the next agent or reviewer picking this up cold. It records what
changed, what was verified and how, and — at least as importantly — what was
not.

## Identifiers

| | |
|---|---|
| Audited base commit | `bc2c72d` (released as `v0.3.0-alpha.1`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Final branch commit | `4048943` |
| Release tag | **NOT PUBLISHED** — see *Release status* below |
| Release URL | none yet |
| Publish attempts | run 11 (failed, real defect) · run 12 (dispatched, outcome unconfirmed) |
| DMG | not produced for alpha 2 |
| DMG size | n/a |
| DMG SHA-256 | n/a |

**Alpha 1 was not mutated.** The `v0.3.0-alpha.1` tag still points at `bc2c72d`
and its two release assets are the ones published on 2026-08-29T20:41:55Z. No
tag was moved, no asset was replaced or deleted, and no release was edited.
Alpha 2 is a separate tag, a separate release and a separate pair of assets.

## What changed, and why

### P0 — indistinguishable audio inputs are refused, not guessed at

The defect: `resolve_identity` returned `composite[0]` when several connected
devices matched a saved identity, and `LiveAudioSource` logged `using the
first` and opened it. An operator with two of the same USB interface — no
CoreAudio UID, so their identities are byte-for-byte identical — who selected
the second one, the one with the radio on it, was recorded from the first.

Nothing observable distinguishes those two devices. That is exactly why
nothing may choose between them.

- `babelfishr/audio/devices.py`
  - `resolve_input()` is now the single resolution function and reports one of
    three states: `resolved`, `missing`, `ambiguous`. It never carries a device
    in the ambiguous state.
  - `resolve_identity()` is retained as a *safe wrapper*: it returns `None` for
    both missing and ambiguous, so any caller that only knows how to check for
    `None` is fail-closed by construction.
  - `DeviceMatch.ambiguous` was **removed**. A flag attached to a device is a
    flag a caller can forget to read, which is precisely how the old code went
    wrong.
  - New `AmbiguousInputDevice(AudioBackendUnavailable)` carrying `.identity`
    and `.candidates`.
- `babelfishr/audio/source.py` — `_resolve_device()` raises
  `AmbiguousInputDevice`; the reconnect watchdog therefore keeps waiting
  instead of opening a candidate. The refusal is written to `connection_log`,
  because those minutes were not received either.
- `babelfishr/app.py` — `_build_source()` resolves *before* a Session row and
  pipeline exist, so nothing has to be unwound and the operator is told
  monitoring never began rather than that it stopped. `input_status()` gained
  an `ambiguous` state with a `candidates` list.
- `babelfishr/ui/input_panel.py` — status reads `CANNOT IDENTIFY` in red, every
  candidate is named so the operator knows which to unplug, and
  `ready_to_monitor()` refuses.
- `babelfishr/ui/main_window.py` — catches `AmbiguousInputDevice` and offers
  the same **Rescan / Choose Different Input / Record Later** dialog.
- `babelfishr/readiness.py` — Field Check **fails** (was: warned).
- `babelfishr/cli.py`, `babelfishr/diagnostics.py` — both report the state.

### P1 — the misleading lock control is gone

`InputSelection.locked` was written, persisted and displayed, and changed
nothing: capture resolved the saved identity whether it was ticked or not. A
control that looks like a safety interlock and is not one is worse than no
control.

- The checkbox is removed from the panel; `--lock/--no-lock` are removed from
  `babelfishr input`.
- Every explicitly chosen device is pinned to its identity unconditionally.
- "Use the macOS system default input" survives unchanged as the one
  deliberately selected alternative, with its existing warning.
- **Migration:** `config.RETIRED_OPTIONS` accepts and drops the old key, so a
  settings file written by Alpha 1 still loads. Without this, an upgraded
  install would refuse to start with `unknown option 'locked'`.

### P1 — CoreAudio verification

- `tests/test_coreaudio.py` is on the branch and covers the four-character
  property codes byte for byte, the transport table, the `AudioBufferList`
  layout, channel summing, failure degradation, and the duplicate-name rule.
  All of it drives a **fake** framework.
- `coreaudio.probe()` / `format_probe()` exercise the **real** ABI: frameworks
  load, a size query for `kAudioHardwarePropertyDevices` returns status 0
  (a non-zero status is what a wrong selector or a wrong
  `AudioObjectPropertyAddress` layout produces), and any devices reported must
  be coherent enough to identify.
- It runs inside the shipped bundle via `BabelFishR --selftest-coreaudio`,
  called from `packaging/verify_independence.sh`, and is echoed into the build
  log.
- It deliberately does **not** require a device to exist. A hosted runner has
  no audio hardware; zero devices there is expected and is stated in the output
  as not being evidence that audio capture works.

### CoreAudio calls can no longer block

Every call in that module goes through the HAL to `coreaudiod` over Mach IPC.
If that daemon is absent, wedged or still starting, a call can block rather
than fail — and device enumeration runs on the GUI thread, so a block there
freezes the window an operator is watching a radio through.

Calls now run on a daemon thread with a 5 s limit (`CALL_TIMEOUT_SECONDS`).
ctypes releases the GIL around a foreign call, so a wedged call parks its own
thread; a daemon thread cannot hold up interpreter exit. Enumeration returns an
empty list on timeout, dropping back to composite identification — a
degradation the rest of the code already handles.
`verify_independence.sh` additionally puts a 90 s wall-clock watchdog around
the check, since the framework load itself is outside the Python guard.

**Honest note on why this was written.** The first run carrying the CoreAudio
self-test sat in its build step more than three times as long as any run before
it, and I attributed that to the new HAL call and cancelled the run. The next
run, with the 90 s watchdog capping that check, was **just as slow** — so that
attribution was wrong, and the cause of the slow builds is still unidentified
(see Unresolved risks). The timeout work stands on its own merits: a GUI freeze
on device enumeration is a real exposure regardless.

### P2 — release maintenance

- `actions/checkout@v5`, `actions/setup-python@v6`, `actions/upload-artifact@v5`
  (clears the Node 20 deprecation annotation). A test holds them at or above
  those majors.
- Unchanged, deliberately: the fatal `uname -m` arm64 gate as step 1,
  standalone-bundle verification, `.sha256` generation, the ad-hoc-signing
  disclosure, and every physical-hardware disclaimer.

## Tests

**540 passed, 9 skipped, 0 failed** on the Linux development host, at
commit `4048943`.

On the macOS arm64 runner, run 11 gave **534 passed, 1 failed, 9 skipped** -
the enum defect above, now fixed. Run 12 carries the fix; its figure is
unconfirmed. The last fully green macOS result remains `v0.3.0-alpha.1`'s
**473 passed, 7 skipped**.

Note the runner's skip list differs from Linux: two CoreAudio tests skip
*because the host has CoreAudio* (`test_coreaudio.py:76` and `:148` cover the
non-macOS fallback), and the real-ABI test runs there instead.

Skips, all with reasons:

| Count | Test | Reason |
|---|---|---|
| 5 | `tests/test_real_engines.py:32` | no prepared Whisper model; needs `babelfishr prepare-field` |
| 2 | `tests/test_real_engines.py:107` | no Argos language pack installed |
| 1 | `tests/test_packaging.py:371` | PlistBuddy is macOS-only (does not skip on the runner) |
| 1 | `tests/test_coreaudio.py:255` | needs a real macOS host with CoreAudio (does not skip on the runner) |

Markers collecting zero tests: `real_dsd`, `macos_audio`, `falconclaw`, `sdr`.
Nothing is marked as passing that did not run.

### The P0 tests were proven against the old behaviour

Reinstating `composite[0]` in `resolve_input` fails **eight** tests:

```
test_indistinguishable_devices_resolve_to_nothing
test_selecting_the_second_of_two_identical_interfaces_never_opens_the_first
test_an_ambiguous_identity_refuses_at_session_start
test_an_ambiguous_identity_refuses_after_a_restart
test_an_ambiguous_identity_refuses_on_profile_restoration
test_an_ambiguous_identity_refuses_on_reconnect
test_the_ambiguous_refusal_is_recorded_in_the_connection_log
test_readiness_fails_rather_than_warns_on_an_ambiguous_input
```

Coverage spans all four paths the audit named: initial start, restart
restoration, profile restoration, and reconnect. Six further tests in
`tests/test_input_panel.py` cover the window and panel, including that removing
the duplicate makes the input usable again.

## Release status — alpha 2 was NOT published

This is the one thing a reader must not be allowed to misunderstand.

All the code, test and documentation corrections below are complete, committed
and pushed to `claude/radio-decoder-translator-0oslya` at `fda9e5a`. The full suite
passes locally. **No `v0.3.0-alpha.2` tag, release or DMG exists.**

What happened, plainly: I dispatched eleven workflow runs. Runs 1-6 completed
normally (run 1 failed on a real macOS-only test defect, since fixed; runs 2,
3, 4 and 6 succeeded and run 6 produced alpha 1). Runs 7, 8 and 9 I cancelled,
believing each had hung. They had not: their `created_at` to `updated_at`
spans are two to three minutes, and `updated_at` is the moment my cancellation
landed. I had been estimating elapsed time from local `sleep` calls that bear
no relation to GitHub's clock, so I killed three healthy builds and then
constructed two false diagnoses to explain the imaginary hangs - first the
CoreAudio self-test, then `actions/checkout@v5`. Both were retracted in commit
`fda9e5a`.

Run 10 I cancelled deliberately and correctly: it was queued ahead of the
publish run and built from the commit whose premise I had just retracted.

Run 11 was the first alpha 2 publish attempt, at `fda9e5a`. It **failed on a
real defect**, and finding it justified the whole exercise:

    babelfishr/models.py: AttributeError: 'NoneType' object has no attribute 'value'
    1 failed, 534 passed, 9 skipped

`Transmission.to_dict()` called `.value` on a `content_class` that was `None`.
The cause was in `from_dict`: every enum field used
`if d.get(field): d[field] = Enum(...)`, so a falsy stored value fell straight
through into the constructor and overrode the dataclass default with something
that is not a member of the enum at all. The next `to_dict()` on that object
raised.

This sits directly on capture-first. Every event is written to disk and the
database *before* anything classifies it, so a row with no content class yet is
not corruption - it is the normal intermediate state. Reading one back and
being unable to serialise it means that transmission cannot be exported or
displayed, and a transmission cannot be received twice. It reproduces in one
line on any platform; the Linux suite simply never happened to construct the
object. Fixed in `4048943` with `_coerce_enum` / `_enum_value` and three
regression tests, including one asserting that a value which *was* recorded
still round-trips exactly.

Run 12 is the second publish attempt, at `4048943`, dispatched with
`publish_prerelease=true` and `release_tag=v0.3.0-alpha.2`. I could not observe
it to completion from this session and did not cancel it.

**To finish the release**: check run 12. If it published, alpha 2 is done and
this document's identifier table needs its DMG size and SHA-256 filled in from
the run summary. If it did not, dispatch the workflow again on the branch with
those same two inputs; nothing in the tree needs changing first.

Do not judge a run's health by how long it feels like it has been going - read
`created_at` against `updated_at` from the API. A healthy run on this pipeline
is three to six minutes by that clock.

## Unresolved risks

1. **No physical validation of anything.** No radio, no FalconClaw PTT, no USB
   audio interface, no SDR, no dsd-neo, no real audio input device, and no M5
   MacBook Air. No audio has ever been transcribed and no text translated by
   any build: Hugging Face is blocked in the development environment, so the
   first-run download has never run anywhere.
2. **The two-identical-interfaces refusal has never met two real interfaces.**
   It is driven entirely by a simulated device list. On a real Mac, if
   CoreAudio supplies distinct UIDs the situation should not arise at all; the
   refusal only triggers when identification has fallen back to composite.
   Which of those happens on real hardware is untested.
3. **Slow builds, cause unidentified.** Runs 7 and 8 took roughly three times
   as long in the build step as runs 2–6, and the CoreAudio hypothesis was
   disproved. Candidates not yet ruled out: runner or CDN variance, the
   per-file signing loop over ~3,500 items, or the action version bump. Worth
   timing the sub-steps before assuming it is benign.
5. **Ad-hoc signature, no notarization.** No Apple Developer ID exists.
   Gatekeeper requires right-click ▸ Open on first launch. Nothing in the
   repository claims otherwise.
6. **`--selftest-gui` has no wall-clock watchdog**, unlike the CoreAudio check.
   It constructs a real QApplication and reaches device enumeration, which is
   now internally bounded, but the Qt side is not.
7. **The build-phase timings have never been read.** They were added in
   `8f72751` to answer a question that turned out not to exist. They are
   harmless and will be useful the first time a build really is slow, but
   nobody has yet seen their output.
8. **The CoreAudio ctypes layer has still never returned a real device.** The
   probe proves the ABI is callable; it has never parsed an actual
   `AudioBufferList` from real hardware, and that is where a struct-layout
   error would show up.

## Physical validation still required

`docs/MACOS_VALIDATION.md` is the procedure. Section 3a is the part that
cannot be simulated and is the highest priority.

## Recommended Mac bench-test sequence

Do these in order. Stop at the first one that misbehaves.

1. Download `BabelFishR-macOS-arm64.dmg` from the release, verify the checksum
   with `shasum -a 256`, open it, drag to Applications.
2. Right-click ▸ Open ▸ Open (needed once; the build is unnotarized).
3. Approve the macOS audio-input permission prompt.
4. Confirm the Audio input panel opens on **"Choose an audio input"** with
   nothing selected — not the MacBook microphone, not the system default.
5. Run `/Applications/BabelFishR.app/Contents/MacOS/BabelFishR --selftest-coreaudio`
   and record whether it reports `coreaudio-uid` identification and how many
   devices it sees. This is the first time that code meets real hardware.
6. Select the MacBook microphone, speak, confirm a transmission appears and the
   level meter moves. This proves capture end to end before any radio is
   involved.
7. Connect the USB interface. Select it. Confirm
   `INPUT: <name> — CONNECTED` and that audio from the radio moves the meter.
8. Quit and relaunch. Confirm the same interface is selected again by name.
9. Unplug it, relaunch. Confirm nothing is selected, the line reads
   `NOT CONNECTED`, and Start refuses with Rescan / Choose Different Input /
   Record Later — and does **not** fall back to the microphone.
10. Replug into a *different* USB port, press Rescan. Confirm it is recognised.
11. Unplug mid-watch. Confirm `RADIO INPUT DISCONNECTED` in red, that already
    captured transmissions still play, and that nothing new is recorded.
12. Replug mid-watch. Confirm monitoring resumes on the same interface and that
    both times appear in `~/Library/Application Support/BabelFishR/Logs/`.
13. **If you have two identical interfaces:** connect both. Run
    `babelfishr input` and note whether it says `coreaudio-uid` or `composite`.
    If `composite`, the status must change to `CANNOT IDENTIFY`, both
    candidates must be named, and Start must refuse. Put the radio on one of
    them and confirm **by listening to the recording** which interface was
    actually captured.
14. Complete first-run preparation in the GUI (the one online step), run Field
    Check, then disconnect from the network and confirm Field Check still
    passes and a transmission is still transcribed and translated.

Items 5, 7 and 13 are the ones no test in this repository can stand in for.
