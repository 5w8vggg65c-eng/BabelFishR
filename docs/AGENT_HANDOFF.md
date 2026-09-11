# Agent handoff — BabelFishR (post-alpha.2 repair pass)

Written for the next agent or reviewer picking this up cold. It records what
changed, what was verified and how, and — at least as importantly — what was
not.

## Identifiers

| | |
|---|---|
| Audited base commit | `bc2c72d` (released as `v0.3.0-alpha.1`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Final branch commit | the tip of this branch — this document is its last commit |
| Corrective commit | `4048943` — the models.py:582 fix |
| Alpha 2 status | **PUBLISHED** (prerelease) |
| Release tag | `v0.3.0-alpha.2` at `4048943` |
| Release URL | https://github.com/5w8vggg65c-eng/BabelFishR/releases/tag/v0.3.0-alpha.2 |
| Actions run | run 12 `33276712136` — https://github.com/5w8vggg65c-eng/BabelFishR/actions/runs/33276712136 (success) |
| Earlier attempt | run 11 `33276322107` — failed on the models.py:582 defect |
| DMG | `BabelFishR-macOS-arm64.dmg` |
| DMG size | 281,983,274 bytes |
| DMG SHA-256 | `8fb849cbab824fee7a66d4d3024b2e7fa6f98bbeed3a66b6648744525205c9f0` |

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

**540 passed, 9 skipped, 0 failed** on the Linux development host, with the
models.py correction (`4048943`) in place.

On the macOS arm64 runner, run 11 gave **534 passed, 1 failed, 9 skipped** -
the enum defect above. Run 12, carrying the fix, went green: the suite is the
first gate in the build step, and every stage after it (bundle, verification,
signing, independence check, disk image, publish) completed.

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

## Release status — alpha 2 is published

`v0.3.0-alpha.2` was published as a prerelease at 2026-08-29T21:47:14Z by
Actions run `33276712136`, built from `4048943` on a `macos-26` Apple Silicon
runner. It carries `BabelFishR-macOS-arm64.dmg` (281,983,274 bytes) and its
`.sha256`. The signature is ad-hoc and the build is **not notarized**; the
release notes say so.

The branch has one further commit, `df4cb78`, which is this document only. No
code differs between the released build and the branch tip.

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

Run 12, at `4048943`, carried the fix and succeeded: the whole suite passed on
the runner, the bundle was built, verified, ad-hoc signed and proven
standalone, the disk image was produced and the prerelease was published.

Do not judge a run's health by how long it feels like it has been going - read
`created_at` against `updated_at` from the API. A healthy run on this pipeline
is three to six minutes by that clock.

## Repair pass: Argos HTTPS on a real Mac

Reported from the frozen Apple Silicon app after alpha 2: Whisper Medium
downloaded, loaded offline and passed its transcription smoke test, but every
Argos route failed, `settings.toml` still said `small` with
`setup_complete = false`, and the log's first real error was

```
SSLCertVerificationError: certificate verify failed:
unable to get local issuer certificate
```

followed by repeated retries ending in
`RecursionError: maximum recursion depth exceeded`.

### Root cause

**Two independent defects, one visible symptom.**

1. **No trust store in the bundle.** A PyInstaller bundle carries its own
   Python and its own OpenSSL but not the system's roots, and macOS keeps its
   roots in the Keychain rather than in a PEM file OpenSSL can read. OpenSSL
   fell back to a compiled-in default path that does not exist inside the
   bundle, found no roots, and rejected every certificate — including valid
   ones. That is the certificate error.
2. **Unbounded recursion hid it.** argostranslate's
   `get_available_packages()` calls `update_package_index()` when the index
   file is missing, and that path comes back into `get_available_packages()`.
   With HTTPS failing the index was never written, so the loop ran until the
   interpreter gave up — burying the real cause under hundreds of identical
   lines, once per requested language pair.

`RecursionError` was the noise. The certificate failure was the fault.

### Files changed

| File | Change |
|---|---|
| `babelfishr/certificates.py` | **new** — resolves a CA bundle (certifi, or an administrator's existing `SSL_CERT_FILE`), sets `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`, and binds `ssl._create_default_https_context` to it. Verification is never disabled. |
| `babelfishr/modes.py` | `bootstrap_environment()` configures certificates before any library that opens a socket is imported. |
| `babelfishr/providers/argos.py` | `refresh_package_index()` — one bounded attempt, then checks the index file exists and parses, and only then calls upstream. Result cached, so five pairs cause one attempt. New `PackageIndexUnavailable` carries the original exception. |
| `babelfishr/preparation.py` | An index failure stops the language loop; remaining pairs are marked "not attempted" rather than repeating the error. `PreparationResult.asr_ok` / `succeeded()` / `failures()`. |
| `babelfishr/ui/setup_assistant.py` | `_persist_partial_success()` keeps a verified model when translation fails; the model list preselects the configured model instead of always `small`. |
| `babelfishr/ui/main_window.py` | Permanent **Copy Diagnostic Report** and **Reveal Logs in Finder** actions under Tools. |
| `packaging/babelfishr.spec` | Ships `certifi`'s `cacert.pem` explicitly; the build **fails** if certifi is absent. |
| `packaging/app_entry.py` | `--selftest-https` — a real verified HTTPS fetch from inside the bundle. |
| `packaging/verify_independence.sh` | Runs it; a certificate failure is fatal, no egress is not. |
| `pyproject.toml` | `certifi` declared in the `asr`, `translate` and `all` extras. |

### What is persisted after a partial success

Only the fact, never the claim. `asr_model` becomes `medium`;
`setup.completed` stays `false`; `setup.language_pairs` is untouched; the
operating mode is unchanged, so **Field Offline remains unreachable** while
translation is unavailable. Readiness reports record=yes, transcribe=yes,
translate=no. Reopening setup preselects `medium`.

One pre-existing test, `test_partial_preparation_persists_nothing`, asserted
the opposite and was rewritten as
`test_partial_preparation_persists_the_model_but_not_the_claim`. That was a
deliberate behaviour change, not a test bent to fit: the old rule cost an
operator a working 1.5 GB download.

### Tests and results

New: `tests/test_argos_index_failure.py` (8), `tests/test_partial_preparation.py`
(8), `tests/test_certificates.py` (8), plus 4 diagnostic-action tests in
`tests/test_input_panel.py` and 2 pipeline tests in
`tests/test_release_pipeline.py`.

```
Focused    (argos index + partial preparation + certificates + pipeline):  55 passed
Full suite:                                        570 passed, 9 skipped, 0 failed
```

Skip reasons are unchanged and listed under *Tests* below.

**Not vacuous:** restoring the pre-fix behaviour — dropping the index-presence
check and calling `get_available_packages()` directly — fails
`test_a_failed_index_refresh_terminates_without_recursion` and
`test_available_packages_never_asks_upstream_without_an_index`, with the
original `SSLCertVerificationError` escaping.

### Frozen macOS verification — what was and was not proven

**Proven here (Linux dev host):** `--selftest-https` performed a *real* HTTPS
fetch of the actual Argos package index
(`raw.githubusercontent.com/argosopentech/argospm-index`), verified the
certificate against the configured CA bundle, and read 34,612 bytes / 100
packages. The certificate mechanism works end to end and is not a mock.

**Proven by CI (macOS arm64), if the run is green:** that the *frozen bundle*
contains certifi's CA data and can complete the same verified fetch, because
`verify_independence.sh` runs `--selftest-https` against the built app and a
certificate failure fails the build.

**NOT proven anywhere:** that this fixes it on *the operator's* Mac. The
reported failure was on their machine, on their network. A hosted runner has
different egress and may not exercise the same path. If their network inspects
TLS, certifi's roots will not be enough and `SSL_CERT_FILE` must point at their
organisation's bundle — the error message now says so.

**Also note:** if CI has no egress, `--selftest-https` prints
`Certificate verification was NOT exercised` and exits 0. A green build is
therefore not by itself proof the fetch happened; read the line.

### Remaining real-Mac validation needed

1. Install the next build, run first-run preparation, and confirm the Argos
   packages actually download.
2. If they still fail, confirm the error is now a single actionable line naming
   the real cause — no `RecursionError`, no hundreds of duplicates.
3. Confirm `settings.toml` keeps `asr_model = "medium"` and
   `setup_complete = false` across a restart.
4. Confirm reopening setup offers Medium, not Small.
5. Confirm Tools ▸ Copy Diagnostic Report and Reveal Logs in Finder work after
   the setup assistant has been closed.
6. Everything in `docs/MACOS_VALIDATION.md` §3a remains outstanding.

## The confirmed failure and its exact correction

**Failure**, GitHub Actions run `33276322107` (run 11), macOS arm64:

```
babelfishr/models.py:582: AttributeError: 'NoneType' object has no attribute 'value'
1 failed, 534 passed, 9 skipped
```

The failing line was `d["content_class"] = self.content_class.value` inside
`Transmission.to_dict()`.

**Root cause**, `Transmission.from_dict()`: every enum field used the pattern
`if d.get(field): d[field] = Enum(d[field])`. A falsy stored value — `None`
from a NULL column, or `""` — failed that guard, was left in the dict, and was
then passed into the dataclass constructor, *overriding the declared default*
with a value that is not a member of the enum. The next `to_dict()` on that
object raised.

**Why it matters**: capture-first writes every event to disk and the database
*before* anything classifies it, so a row with no content class yet is the
normal intermediate state, not corruption. Such a row could not be serialised,
so it could not be exported or displayed — and a transmission cannot be
received twice.

**Correction**, commit `4048943`, `babelfishr/models.py`:

- `_coerce_enum(data, key, enum_type)` — on the way in. A falsy value means
  "nothing was recorded", so the key is *removed* and the dataclass default
  applies. An unrecognised value is also dropped, with a warning, so a row
  written by a later version cannot make an event unreadable.
- `_enum_value(value, default)` — on the way out. Returns the enum's value, or
  the default's value if the field somehow holds `None` or a bare string, so
  serialisation is never the step that loses a transmission.
- Applied to `state`, `content_class`, `source_language_mode` and the four
  provenance fields, in both `to_dict()` and `from_dict()`.
- Also fixed `source_language_mode` in `Transmission.from_dict`, which had kept
  the old pattern: the identical two lines appear in `Session.from_dict` and
  only the first occurrence had been replaced.

A value that *was* genuinely recorded still round-trips exactly; a test asserts
that, because a fallback that quietly flattened real data would be its own
defect.

## Test results for this correction

**Targeted** — the three tests reproducing the exact failure, run once:

```
tests/test_models.py::test_an_unset_enum_column_falls_back_to_the_default[None]                     PASSED
tests/test_models.py::test_an_unset_enum_column_falls_back_to_the_default[]                         PASSED
tests/test_models.py::test_an_unset_enum_column_falls_back_to_the_default[a-value-from-a-later-version] PASSED
tests/test_models.py::test_serialisation_never_raises_on_a_field_that_lost_its_type                 PASSED
tests/test_models.py::test_a_real_value_still_round_trips_exactly                                   PASSED
5 passed
```

**Full local suite**, run once on the Linux development host:

```
540 passed, 9 skipped, 0 failed
```

Skip reasons are unchanged and listed under *Tests* above.

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

---

# Follow-up pass: Argos failure fidelity, "Prepare again", and a real uninstaller

Branch `claude/radio-decoder-translator-0oslya`, starting from
`7cd2297beb79dd8cbd1ad95edc19ea06968bc562`. Alpha 1 and Alpha 2 were not
touched. Alpha 3 was **not** published.

## What the previous CI run actually proved about HTTPS

Run [33280131395] (`macos-26`, arm64) finished **success**: 570 passed, 9
skipped. The relevant line is that `--selftest-https`, run from inside the
frozen bundle, performed **the real verified fetch** — not the "Certificate
verification was NOT exercised" path:

```
CA bundle: .../dist/BabelFishR.app/Contents/Frameworks/certifi/cacert.pem (applied)
fetching https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json
ok: verified HTTPS fetch, 34612 bytes, 100 packages in the index
```

So the certifi fix is proven end-to-end on Apple Silicon, in the shipped
bundle, against the real Argos index host, with verification on. The same run
also showed the CoreAudio probe returning real devices for the first time
(`Apple Virtual Sound Device`, `Null Audio Device`), which confirms the ctypes
probe works against a real CoreAudio, though not against real hardware.

## Argos correction 1 — match how upstream really fails

The earlier regression test made `update_package_index()` *raise* the SSL
error. Real `argostranslate` does not: it catches its own network error, logs
it, and returns having written no index. Every caller then sees "no packages"
with the cause already swallowed, which is exactly how a certificate problem
turned into a `RecursionError` on the operator's Mac with the real reason
nowhere in the UI.

The fix is not a better message; a generic "the index was not written" would
still lose the cause. BabelFishR now **owns the request**:

- `fetch_package_index()` in `babelfishr/providers/argos.py` makes its own
  `urllib` request over HTTPS after `configure_certificates()`, parses the
  JSON body **before** writing anything, then writes atomically
  (`tempfile.mkstemp` in the destination's own directory, then `os.replace`),
  removing the temporary file if anything goes wrong. TLS verification is
  never relaxed — there is no `ssl._create_unverified_context`, no
  `CERT_NONE`, and a test asserts this against the module source.
- `refresh_package_index()` tries our fetch first precisely *because* it keeps
  the exception, and falls back to upstream `update_package_index()` second.
  Whatever fails, the raised `PackageIndexUnavailable` carries the original
  exception as `__cause__` and names it in the message.

New tests in `tests/test_argos_index_failure.py`:
`test_a_silently_swallowed_error_still_surfaces_the_real_cause` (upstream
returns quietly and BabelFishR still reports the real `SSLCertVerificationError`),
`test_our_own_fetch_is_tried_before_upstream`,
`test_upstream_is_still_used_as_a_fallback`,
`test_the_fetch_writes_atomically_and_rejects_a_bad_body`,
`test_the_fetch_never_relaxes_tls_verification`.

## Argos correction 2 — "Prepare again" now really retries

The cached index outcome is what stops five requested language pairs making
five identical network attempts. It was also what made "Prepare again" replay
the remembered failure after the operator fixed their network.

`prepare_field()` now calls `reset_package_index_state()` **once, at the top of
each run, and nowhere else** — not between language pairs. An operator who
fixed their Wi-Fi and pressed Prepare again gets a genuinely new request; five
pairs in one run still share one attempt.

Tests: `test_prepare_again_makes_a_new_index_request` (first run fails, second
run makes one new request and can succeed) and
`test_the_reset_happens_once_per_run_not_per_pair`.

## The uninstaller

A separate double-clickable **`Uninstall BabelFishR.app`**, shipped inside the
DMG beside `BabelFishR.app` and the `Applications` shortcut. Bundle identifier
`org.babelfishr.uninstaller`. It is self-contained: no Python, no Terminal, no
script to find. There is deliberately **no in-app self-destruct command** —
`tests/test_uninstaller.py::test_the_uninstaller_is_not_reachable_from_inside_the_app`
asserts the main window, setup assistant, CLI and app object never reference
it.

New files: `babelfishr/uninstall.py` (all the logic, no Qt),
`babelfishr/ui/uninstall_window.py` (the one window),
`packaging/uninstaller_entry.py`, `packaging/uninstaller_entitlements.plist`.

### Exact removal scope

Fifteen allowlisted paths, joined from constants to one home directory:

```
~/Library/Application Support/BabelFishR/Recordings          ← cannot be recovered
~/Library/Application Support/BabelFishR/babelfishr.sqlite3  ← transcripts, translations
~/Library/Application Support/BabelFishR/models              ← Whisper models
~/Library/Application Support/BabelFishR/language-packs      ← Argos packs
~/Library/Application Support/BabelFishR/Logs                ← logs, diagnostic reports
~/Library/Application Support/BabelFishR/settings.toml
~/Library/Application Support/BabelFishR                     ← the folder itself
~/.config/babelfishr
~/Library/Caches/org.babelfishr.app
~/Library/Caches/BabelFishR
~/Library/HTTPStorages/org.babelfishr.app
~/Library/Preferences/org.babelfishr.app.plist
~/Library/Saved Application State/org.babelfishr.app.savedState
/Applications/BabelFishR.app
~/Applications/BabelFishR.app
```

Plus a best-effort `tccutil reset Microphone org.babelfishr.app`, whose
success or failure is reported either way.

### Safety properties, and where each is enforced

| Property | Where |
|---|---|
| Exact paths shown before anything is deleted | `describe_plan()`, shown verbatim in the window |
| Recordings warned as unrecoverable | the red banner, `describe_plan()`'s `← CANNOT BE RECOVERED` |
| Both an acknowledgement box **and** typing `DELETE` | `confirmation_ready()`, re-checked inside `perform()` |
| Cancel changes nothing | Cancel is `close()`; removal only ever runs from `perform()` |
| Refuses while BabelFishR is running | `uninstall()` raises unless the check returns exactly `False`; "could not tell" counts as running. The window offers a graceful quit, then verifies it stopped |
| Allowlist only | `_assert_allowlisted()`, checked at plan time *and* again per item |
| Never reads untrusted configuration | `build_plan()` joins constants to a home; settings.toml is never opened |
| Never follows symlinks | a symlinked item has its **link** unlinked and its target left alone, reported separately; trees go through `shutil.rmtree`, which unlinks nested symlinks rather than descending them |
| No unsafe shell interpolation when escalating | `_authorization_command()` validates every path against a strict pattern and refuses rather than escaping cleverly; the command is a fixed `/bin/rm -rf --` with quoted, validated arguments, run through the standard macOS authorization prompt |
| Honest report | `UninstallReport.complete` is false whenever anything failed; the summary says "was NOT completely removed" and names each leftover |

The uninstaller may stay on the mounted disk image; it never needs installing.

### Packaging

- `packaging/babelfishr.spec` builds both bundles. The uninstaller's `Analysis`
  excludes `faster_whisper`, `ctranslate2`, `argostranslate` and `sounddevice`.
- Its `Info.plist` has **no** `NSMicrophoneUsageDescription` and its
  entitlements have no audio-input key. `sign_macos.sh` takes the entitlements
  file as its third argument and **fails the build** if a bundle signed without
  the microphone request ends up with that entitlement sealed in — a swap of
  the two plists would otherwise be invisible. It reads the entitlements with
  PlistBuddy, not grep, so a comment cannot be mistaken for a request.
- `build_macos.sh` verifies the uninstaller's identifier and plist, runs
  `--selftest-dry-run` against a scratch home (a non-destructive exercise of
  the packaged binary), and signs both bundles.
- `make_dmg.sh` stages both apps and the `Applications` symlink, and **fails**
  unless the mounted image contains both executables and the shortcut.
- The workflow re-verifies the uninstaller independently of the build script,
  including that the sealed signature has no microphone entitlement, and runs
  the dry run against a probe home containing a file it asserts still exists
  afterwards. The release notes explain how to run it and state that it
  permanently deletes recordings.

## Tests and results

`tests/test_uninstaller.py` — 34 tests, all against temporary fake homes:
cancellation deletes nothing (both the dry run and closing the window with
both confirmations already given); every allowlisted item is removed;
neighbouring files (`SomeOtherApp`, `com.apple.finder.plist`, `~/Music`,
`~/.config/otherapp`, `~/Applications/Other.app`) survive; a symlinked
`Recordings` loses only the link while its target keeps its contents; a symlink
nested inside a removed tree is not followed; removal refuses while the app
runs *and* when the running state cannot be determined; a leftover is reported
and never called complete; a failed `tccutil` is reported, not hidden; a
hostile path is refused by the authorization command rather than escaped; an
item smuggled onto a plan is refused. `test_no_test_in_this_file_can_reach_the_real_home`
parses this test file's own syntax tree and fails if any test calls
`build_plan` with no argument, calls `Path.home()` or `expanduser`, or writes a
real Application Support path as a literal — so no test can touch the
operator's real data.

`tests/test_release_pipeline.py` — 11 new tests covering the second bundle, its
identifier, its missing microphone entitlement (and that the app still has
one), the signing swap-detection, the build's dry run against a scratch home,
the DMG failing without either app, the workflow's independent verification,
and the release notes' uninstall instructions and recording warning.

Focused runs: `tests/test_uninstaller.py` 34 passed;
`tests/test_argos_index_failure.py` 15 passed;
`tests/test_release_pipeline.py` 41 passed.

Full suite: **621 passed, 9 skipped** in 77s (skips: CoreAudio needs a real
macOS host, PlistBuddy is macOS-only, and the real-engine tests need a
prepared model and language pack).

## Files changed in this pass

```
babelfishr/providers/argos.py            our own verified fetch, atomic write
babelfishr/preparation.py                one index-state reset per run
babelfishr/uninstall.py                  NEW  removal logic
babelfishr/ui/uninstall_window.py        NEW  the uninstaller window
packaging/uninstaller_entry.py           NEW  uninstaller entry point
packaging/uninstaller_entitlements.plist NEW  no microphone
packaging/babelfishr.spec                second bundle
packaging/sign_macos.sh                  selectable entitlements + swap check
packaging/build_macos.sh                 build, verify, dry-run and sign both
packaging/make_dmg.sh                    stage and verify both apps
.github/workflows/macos-release.yml      uninstaller verification, release notes
README.md                                "Removing BabelFishR"
tests/test_uninstaller.py                NEW  34 tests
tests/test_argos_index_failure.py        swallowed-failure and retry coverage
tests/test_release_pipeline.py           uninstaller packaging coverage
```

## Still requiring a real Mac

Nothing here has been run on physical hardware. Specifically unproven:

1. That `Uninstall BabelFishR.app` opens by double-click from a mounted DMG on
   a machine with Gatekeeper active (unnotarized alpha: right-click ▸ Open).
2. That the running-app check finds a real launched BabelFishR — `pgrep -f
   BabelFishR.app/Contents/MacOS` is matched against the real process
   command line, and the graceful-quit path via `osascript` has only been
   tested with a fake runner.
3. That `tccutil reset Microphone org.babelfishr.app` actually clears the
   grant; recent macOS versions sometimes require the app to have been
   launched at least once for the entry to exist.
4. That the administrator authorization prompt appears and completes for a
   genuinely permission-blocked path. The suite never runs it for real.
5. That a real, prepared installation — several gigabytes of models — is
   removed completely, and that Field Offline is reported honestly afterwards.
6. Whether Argos preparation now succeeds against the real index from the
   frozen app on a real network, and that a first failure followed by
   "Prepare again" really retries.

Item 5 is the one that matters most: run it on a Mac that has a full prepared
installation, and read the itemized report rather than trusting the absence of
an error.

---

# Release-blocking correction: self-matching process check, test isolation, Argos paths

Branch `claude/radio-decoder-translator-0oslya`, from
`cf5c3bd493097b97c9a7f935f594d78052529b37`. Alpha 1 and Alpha 2 untouched;
Alpha 3 not published.

## 1. The uninstaller found itself and refused every removal

**Defect.** `app_is_running()` ran `pgrep -f BabelFishR.app/Contents/MacOS`.
That string is a substring of

```
Uninstall BabelFishR.app/Contents/MacOS/UninstallBabelFishR
```

so the *running uninstaller* matched the pattern, `uninstall()` concluded the
application was still open, and every removal was refused. There was no way for
an operator to get past it: quitting BabelFishR could not help, because the
process being found was the uninstaller itself.

**Correction.** No substring is used anywhere. Two independent exact checks:

1. `pgrep -x BabelFishR` — exact process-name match. `BabelFishR` is never
   equal to `UninstallBabelFishR`.
2. Each matching pid is confirmed with `ps -o comm=`, comparing the **last
   component** of the executable path with `==` against `MAIN_EXECUTABLE_NAME`.

Both tools failing, an unexpected `pgrep` exit code, or `pgrep` naming pids
that `ps` cannot confirm all return `None`, which `uninstall()` treats as "it
is running" and refuses. Fail closed in every direction.

**Tests** (`tests/test_uninstaller.py`) drive a `process_table()` fake that
models the real `pgrep` semantics — `-x` matches the last path component
exactly, `-f` matches the whole command line as a substring — so a revert to a
pattern is caught rather than papered over:

- `test_the_running_uninstaller_is_not_mistaken_for_the_application` (only
  `UninstallBabelFishR` running → stopped)
- `test_the_running_application_is_found`
- `test_both_running_is_reported_as_running` (both orderings)
- `test_a_process_inspection_failure_fails_closed` (pgrep unavailable, and ps
  unavailable) and `test_an_unexpected_pgrep_exit_code_fails_closed`
- `test_the_command_is_an_exact_match_not_a_substring_pattern` asserts the
  **real argv** — `["/usr/bin/pgrep", "-x", "BabelFishR"]` — and that no call
  carries `-f` or a `Contents/MacOS` fragment
- two end-to-end tests through `uninstall()` itself

Reverting `app_is_running` to the old `pgrep -f` line fails three of these,
including the end-to-end removal.

## 2. Destructive tests could have deleted a real installed application

**Defect.** `build_plan(tmp_path)` still put the literal
`/Applications/BabelFishR.app` into the plan. Every destructive test in
`tests/test_uninstaller.py` called `uninstall()` on such a plan, so running the
suite on a developer's Mac with BabelFishR installed would have deleted their
actual application. The existing "fake homes only" guard did not catch it: the
home *was* fake, the applications root was not.

**Correction.** Runtime application roots are now separated from injected ones.

- `build_plan()` with no argument — the real plan: the operator's home, plus
  `/Applications`. `plan.roots()` is `[home, /Applications]`.
- `build_plan(home)` — an injected root: `system_applications` defaults to
  `None`, so `/Applications` is not in the plan at all and every path lies
  inside the supplied root. A caller that genuinely wants another applications
  root has to name it (`system_applications=`), and it is still not
  `/Applications` unless written out.

`UninstallPlan` gained `system_applications`, `roots()` and
`contains_only_paths_within_its_roots()`. `_assert_allowlisted()` now takes the
plan, so the allowlist is derived from that plan's roots rather than from a
constant.

`packaging/uninstaller_entry.py --selftest-dry-run <home>` passes the scratch
home through, so it neither names nor stats the real `/Applications`; with no
argument it builds the real runtime plan.

**Tests.**
- `test_a_plan_built_on_a_fake_home_never_leaves_that_home` — the guard the
  requirement asks for: `roots() == [home]`, `system_applications is None`, and
  every path inside the injected root.
- `test_a_real_application_path_is_refused_by_a_fake_home_plan` — checked at
  the allowlist; that test never calls `uninstall()`.
- The file guard now also parses the syntax tree and fails if the literal
  `/Applications/BabelFishR.app` appears as a string constant anywhere in the
  destructive test file.
- The real runtime plan is asserted in a **separate, non-destructive** file,
  `tests/test_uninstaller_runtime_paths.py`, which ends with a guard rejecting
  any call that could change the disk (`uninstall`, `rmtree`, `unlink`,
  `rmdir`, `remove`, `mkdir`, `write_text`, …). That is the only file that
  builds a real plan, and it cannot delete anything.

## 3. Argos data now lives under BabelFishR

**Defect.** `argostranslate.settings` (1.11.0) resolves three roots from the
XDG variables **at import time** and `os.makedirs` them immediately:

```
data_dir   = $XDG_DATA_HOME/argos-translate    default ~/.local/share
config_dir = $XDG_CONFIG_HOME/argos-translate  default ~/.config
cache_dir  = $XDG_CACHE_HOME/argos-translate   default ~/.local/cache
```

`ARGOS_PACKAGES_DIR` moves the installed packages and nothing else. So the
local package index (`data_dir/index.json`), the downloads cache
(`cache_dir/downloads`) and the configuration were all outside
`~/Library/Application Support/BabelFishR` — invisible to the operator and
invisible to the uninstaller.

**Correction.** `babelfishr/argos_home.py` sets the three XDG roots and then
imports `argostranslate.settings` itself, inside a context manager that
restores the previous environment afterwards. Setting them permanently would
relocate every other XDG-aware library in the process and would be inherited by
every child process BabelFishR launches, which is not ours to change.
`modes.bootstrap_environment()` calls it before anything can import Argos.

**Final managed layout**, all under `~/Library/Application Support/BabelFishR`:

```
argos/data/argos-translate              data_dir
argos/data/argos-translate/index.json   local_package_index
argos/config/argos-translate            config_dir
argos/cache/argos-translate             cache_dir
argos/cache/argos-translate/downloads   downloads_dir
language-packs                          package_data_dir (ARGOS_PACKAGES_DIR)
```

Installed packages stay in `language-packs/` — `ARGOS_PACKAGES_DIR` wins over
`data_dir/packages`, and alpha 2's packs are already there.

`--selftest-argos-paths` proves this **in the frozen bundle**: it bootstraps,
reads the paths back out of the imported `argostranslate.settings`, and fails
if any resolves outside the managed root. `verify_independence.sh` runs it and
treats a stray path as fatal. `_selftest_independence` now bootstraps before
importing its module list, because it imports `argostranslate` and would
otherwise create the very directories this fixes.

**Test against the real library, not a fake.**
`tests/test_argos_home.py::test_the_real_argos_resolves_every_path_inside_the_injected_root`
runs a fresh interpreter with `HOME` and `BABELFISHR_HOME` pointed at a
temporary directory, calls `bootstrap_environment()`, imports the actually
installed argostranslate 1.11.0, and asserts that all six resolved paths are
inside the injected root, that none of the three home-directory folders was
created, and that the XDG variables were left unset afterwards.

### Legacy cleanup

`clean_legacy_argos(home)` tidies what earlier builds left behind, called
best-effort from `bootstrap_environment()` and again from `uninstall()`:

1. Only **exact named files** are deleted, and only when a content check
   confirms they are what the name says — currently just
   `.local/share/argos-translate/index.json`, and only if it parses as a JSON
   list of package dictionaries.
2. A legacy directory is removed only when **empty**, with `os.rmdir`, which
   fails rather than recursing. There is no `rmtree` in that function and
   `test_the_cleanup_can_never_recurse` parses the module's syntax tree to
   assert no `rmtree`, `removedirs`, `system` or `run` call exists anywhere in
   it.
3. Shared parents (`~/.local/share`, `~/.config`, `~/.local/cache`) are never
   touched, and a symlinked legacy directory is neither followed nor removed.
4. Anything left is reported by **exact path** with the reason, and
   `LegacyCleanup.complete` is false — nothing is ever described as removed
   unless it is gone.

Tests: known artifacts cleaned; a stranger's `packages/translate-fr_en-1_9`
and a hand-written `settings.json` survive and are named in the report; a
file called `index.json` that is not an Argos index is left alone; a symlinked
legacy directory's target survives; the dry run changes nothing.

## Tests and results

Focused: `tests/test_uninstaller.py` 45 passed ·
`tests/test_uninstaller_runtime_paths.py` 5 passed ·
`tests/test_argos_home.py` 10 passed · `tests/test_release_pipeline.py` 43
passed.

Full suite: **649 passed, 9 skipped** in 73s (same nine skips as before:
CoreAudio needs a real macOS host, PlistBuddy is macOS-only, and the
real-engine tests need a prepared model and language pack).

Non-vacuity: reverting `app_is_running` to `pgrep -f BabelFishR.app/Contents/MacOS`
fails `test_the_running_uninstaller_is_not_mistaken_for_the_application`,
`test_the_command_is_an_exact_match_not_a_substring_pattern` and
`test_the_uninstaller_removes_while_only_itself_is_running`.

## Files changed

```
babelfishr/uninstall.py                  exact process identity; injected roots
babelfishr/argos_home.py                 NEW  managed XDG roots, legacy tidy
babelfishr/modes.py                      bootstrap configures Argos, tidies legacy
packaging/app_entry.py                   --selftest-argos-paths; bootstrap first
packaging/uninstaller_entry.py           scratch home never names /Applications
packaging/verify_independence.sh         Argos paths are a fatal check
README.md                                argos/ directory, legacy behaviour
tests/test_uninstaller.py                process table, injected-root guard
tests/test_uninstaller_runtime_paths.py  NEW  real plan, non-destructive
tests/test_argos_home.py                 NEW  real argos 1.11, legacy cleanup
tests/test_release_pipeline.py           the new self-test and import order
```

## Final commit

One commit, on `claude/radio-decoder-translator-0oslya`, whose parent is
`cf5c3bd493097b97c9a7f935f594d78052529b37`. A commit cannot contain its own
hash, so read it with:

```
git rev-parse claude/radio-decoder-translator-0oslya
```

It is also the head SHA of the verification run dispatched for this pass.

## Still requiring a real Mac

Unchanged from the previous section, minus nothing, plus:

- That `pgrep -x BabelFishR` matches a genuinely launched frozen bundle — the
  process name comes from the bundle's executable, which is `BabelFishR`, but
  that has only been modelled here, never observed.
- That `--selftest-argos-paths` passes inside the signed bundle on the runner.

---

# Correction: preserved legacy content no longer counts as complete removal

Branch `claude/radio-decoder-translator-0oslya`, from
`41b9e701fe3646257ed7db9744745932fa604400`. One issue, nothing else touched.

## The defect

`clean_legacy_argos()` was already right: it preserves content in the shared
Argos folders that it cannot attribute to BabelFishR, and names it. The bug was
one level up. `uninstall()` folded that result into `report.notes`, and notes do
not block anything — `UninstallReport.complete` was `not self.failed`, so a run
that deliberately left `~/.local/share/argos-translate` in place still ended
with:

```
BabelFishR was completely removed.
```

with a legacy Argos folder still on the machine. The same applied when the
cleanup raised: the exception became a note, and the run still claimed
completion despite having no idea what was in those folders.

## The correction

`UninstallReport` gained two fields:

- `preserved: List[Tuple[Path, str]]` — left on purpose because it may not be
  ours. Deliberately **not** merged into `failed`: a failed item is
  BabelFishR's and a retry or an administrator prompt is the fix, while a
  preserved item must never be deleted at all. `leftovers()` therefore still
  returns only `failed`, which is what `elevate_removal()` is handed — a
  preserved path can never reach a privileged `rm`.
- `legacy_uncertain: Optional[str]` — set when legacy cleanup raised, so the
  outcome is simply unknown.

`complete` is now `not failed and not preserved and legacy_uncertain is None`,
and the summary's final verdict reads `self.complete` rather than re-testing
`failed`. Two new summary sections keep the distinction visible:

```
COULD NOT BE REMOVED (n):          ✗  ours, and it did not go
LEFT IN PLACE ON PURPOSE (n):      !  in a folder Argos shares between
                                      installations; BabelFishR cannot prove
                                      it wrote it, so it was not touched
UNVERIFIED: the older Argos folders could not be checked (…)
```

`remaining()` returns everything still on the computer for either reason; the
uninstaller window's closing line counts that instead of `leftovers()`, and
says so plainly when the only problem is that the folders could not be checked.

Behaviour that did not change: unknown content is still never touched, the
allowlist, process detection, injected-root isolation, the Argos directory
layout, packaging and the release workflow are all untouched, and a known-only
legacy tree that is cleaned safely still reports complete.

## Tests

Six integration-level tests in `tests/test_uninstaller.py`, all through the
full `uninstall()` rather than `clean_legacy_argos()` alone — the defect was
never in the cleanup:

- `test_unknown_legacy_content_survives_and_blocks_the_complete_claim` — a
  stranger's `packages/translate-fr_en-1_9/model.bin` survives byte-for-byte,
  the directory is in `preserved_paths()` and not in `leftovers()`,
  `complete is False`, its exact path is in the summary, and
  `"BabelFishR was completely removed"` is absent.
- `test_a_preserved_legacy_path_is_never_offered_to_the_authorization_prompt` —
  `elevate_removal` is never called with it.
- `test_a_legacy_cleanup_exception_prevents_a_complete_claim` — the raise sets
  `legacy_uncertain`, blocks the claim, prints `UNVERIFIED`, and the rest of
  the removal still happened.
- `test_a_known_only_legacy_tree_still_reports_complete` and
  `test_no_legacy_directories_at_all_still_reports_complete` — honesty has not
  become a permanent "incomplete".
- `test_a_preserved_path_is_not_merely_a_note` — the regression guard the
  defect needs. Recording preserved content in `notes` again would leave the
  path visible in the summary, so a text-only assertion would keep passing;
  this one asserts the path is on a field that blocks completion, that it is
  *not* in `notes`, and demonstrates the difference by building the same
  report with the entry demoted to a note and showing it claims completion.

Reverting `uninstall()` to the note-only handling fails four of these,
including the exception case.

Focused: `tests/test_uninstaller.py` 51 passed ·
`tests/test_argos_home.py` 10 passed ·
`tests/test_uninstaller_runtime_paths.py` 5 passed.

Full suite: **655 passed, 9 skipped** in 78s — the same nine skips as before.

## Files changed

```
babelfishr/uninstall.py            preserved / legacy_uncertain; complete()
babelfishr/ui/uninstall_window.py  the closing line counts what remains
tests/test_uninstaller.py          six integration regression tests
```

## Final commit

One commit on `claude/radio-decoder-translator-0oslya`, whose parent is
`41b9e701fe3646257ed7db9744745932fa604400`. Read it with
`git rev-parse claude/radio-decoder-translator-0oslya`. No workflow was
dispatched for this pass, and Alpha 3 was not published.

---

# Alpha 3 field-test repair pass

Branch `claude/radio-decoder-translator-0oslya`, from
`c9299e06e8731db1645441da3677cf24358243dd` (the commit `v0.3.0-alpha.3` was
built from). No release was published, no workflow dispatched, and the
alpha 1/2/3 tags, releases and assets were not touched.

## What the operator saw, and why

Five symptoms on a real Mac. Four of them were one product decision that was
wrong, and one query that was subtly wrong.

**1. Voice through the MacBook microphone was called "possibly a digital
burst", and that stopped Whisper.** The classifier is a heuristic over a few
seconds of audio, and on this hardware it put ordinary speech in
`DIGITAL_SUSPECTED`. `DetectedTransmission.should_auto_transcribe()` then
looked that class up in `settings.auto_process_digital`, which defaulted to
`False`, so the recording was marked SKIPPED and never transcribed. No
transcript meant Argos had nothing to translate — symptom 2 was not a separate
bug, it was this one.

The fix is not a better classifier. It is that a classification no longer gets
to make this decision:

- `DIGITAL_SUSPECTED` returns `True` from `should_auto_transcribe()`
  unconditionally — not a settings lookup, so no configuration can put the
  veto back.
- `auto_process_digital` is **removed** from `DetectorSettings` and
  `DetectorConfig`, and added to `RETIRED_OPTIONS`. That second part matters
  as much as the first: an alpha 3 `settings.toml` carrying
  `auto_process_digital = false` is now dropped on load, so upgrading cannot
  reinstate the defect.
- `auto_process_noise` now defaults to `True`. Radio static very often has a
  weak voice under it, and a transmission that is never transcribed is a worse
  outcome than an ASR call spent on nothing.
- `TONE` remains the one suppressible class, still defaulting to off. A steady
  unmodulated carrier or courtesy beep cannot contain speech. It is still
  recorded, still in the database, and still forcible.
- Capture-first is untouched: the WAV and the row are written before anything
  classifies, and the original bytes are never rewritten.
- The misleading comments went with it — `DetectorSettings`' "automatic ASR on
  a suspected digital burst is usually pointless", the config comment, and the
  `digital-suspected` entry in `_skip_reason` that told the operator their
  speech was "possibly a digital burst".

**3. "Transcribe anyway" claimed it needed a running session.** It did, in the
code: `app.transcribe_anyway()` returned `False` whenever `self.pipeline` was
`None`, and the pipeline only existed during capture. A WAV on disk does not
need a microphone.

`BabelFishRApp._processing_pipeline()` returns the live pipeline while
monitoring and otherwise builds a standalone one on demand — same store, same
event bus (so the same bubble updates through the ordinary
`updated`/`state`/`error` events), its own worker thread (so the window never
freezes), engines chosen by `select_engines()` for the current operating mode.
It creates **no Session row and opens no audio device**: it is not a fake live
session. `start_session()` retires it first so two pipelines never work the
same rows. Everything the processing needs — source-language mode, target
language, session metadata — already lives on the transmission row, which is
why this works after a relaunch too.

`app.processing_problem(tx_id)` returns the precise obstacle, and the dialog
shows it: the transmission is gone, no audio was recorded, the file is missing
(named), Record Only has transcription switched off, or no engine is available
in this mode (with the engine warnings). It never says monitoring must be
running, because that is never the reason.

**4. Starting monitoring made earlier transmissions vanish.**
`MainWindow._start_monitoring()` called `self.timeline.clear()`. The files and
rows survived; the operator's log appeared to have been wiped. That call is
gone, and the window now loads the thread on open.

Underneath it was a second, quieter defect: `list_transmissions(limit=500)`
orders `started_at ASC` and then limits, which returns the five hundred
*oldest* rows. After a few days of use the thread would have opened on ancient
traffic with the operator's most recent transmission missing. New
`Store.recent_transmissions()` selects the newest set `DESC` and reverses it
for display; `app.recent_transmissions()` wraps it with `HISTORY_LIMIT = 500`.
`list_transmissions` keeps its old behaviour and now documents what its limit
actually means.

"Show current session" is now **"Show all transmissions"** (Ctrl+Shift+A), and
search and the review queue both say to use it to get back.

**5. The waveform card was not the product.** The bubble is a radio message
now: the transcript is the primary text with no language prefix competing with
it, "Transcribing…" appears in that same bubble while it is in flight, and the
translation is a clearly separated second line **only when the languages
actually differ** (`en` vs `en-GB` counts as the same, so no "already in
English" row under an English transcript). `WaveformWidget` is not constructed
at all, and Play is no longer a permanent button — Play original, Play decoded,
Export audio, Analyze as digital, edit, tags, note and bookmark all live in the
ellipsis menu. "possibly digital" stays as a small chip beside the words.
Transcript correction, notes, tags, bookmarks and export are unchanged.

**The false "Record only" badge.** `ReadinessReport.can_transcribe` required a
`PASS` from the smoke test, and the startup check ran with
`run_smoke_tests=False` for speed — so a machine with a working model and
working language packs reported SKIP, which the badge rendered as "Record
only", and it stayed wrong until a restart. "Not tested" is not "not
available". `field_ready_unknown` is the honest third answer: assets present,
smoke tests not run. The badge shows "… Checking" for that state, and the
window now runs the real check with smoke tests on a worker thread at startup,
so it resolves to "Field ready" by itself.

**First-run setup did not refresh the window.** `_show_assistant()` refreshed
four things; the automatic first-run assistant in `ui/run()` refreshed nothing.
Both now call one shared `MainWindow.refresh_after_setup()`, which re-selects
engines and refreshes devices, engines, mode, state, readiness and the
timeline. No restart.

**The release-notes heredoc.** `cat > release-notes.md <<NOTES` is unquoted —
it has to be, because `$TAG`, `$SHA256` and `$GATEKEEPER` must expand — so the
backticked `` `DELETE` `` in the *Removing BabelFishR* section was command
substitution. The shell ran `DELETE`, logged `command not found`, and
substituted its empty output, which is why alpha 3's notes read "type .". The
backticks are now escaped (`` \`DELETE\` ``), as the code fences already were.
Alpha 3's published notes were deliberately left alone.

## Deliberate behaviour changes to existing tests

Three tests asserted the old routing and were rewritten rather than deleted,
each carrying the reason in its docstring:

- `test_capture_invariant.py::test_classification_gates_asr_but_not_persistence`
  → `..._does_not_gate_asr_but_still_never_gates_persistence`. It asserted
  `engine.calls == len(speech)`. That assertion **was** the defect.
- `test_pipeline.py::test_noise_is_not_sent_to_the_transcription_engine`
  → `test_an_operator_who_opts_out_of_noise_costs_no_asr_call`. The default
  flipped; the knob did not disappear, so the test now drives it.
- `test_detect.py::test_static_is_classified_but_always_retained` and
  `test_operator_can_opt_into_transcribing_noise` → the opt-*out* direction.
- `test_settings_separate_recording_from_processing` now asserts
  `auto_process_digital` does **not** exist.
- `test_ui.py::test_bubble_separates_original_from_translation` — the
  transcript no longer carries a language prefix.
- `test_ui.py::test_digital_result_is_shown_with_decoded_playback` — checks the
  menu action, not a button.

## Files changed

```
babelfishr/detect.py              digital is advisory; noise default flipped
babelfishr/config.py              field removed; RETIRED_OPTIONS migration
babelfishr/pipeline.py            skip reasons; start(session=None)
babelfishr/app.py                 standalone pipeline, processing_problem,
                                  recent_transmissions, HISTORY_LIMIT
babelfishr/storage.py             recent_transmissions (newest N, chronological)
babelfishr/readiness.py           field_ready_unknown, *_unverified
babelfishr/ui/main_window.py      no clear(), thread on open, honest dialogs,
                                  Checking badge, refresh_after_setup()
babelfishr/ui/timeline.py         text bubble: no waveform, no Play button
babelfishr/ui/__init__.py         first-run assistant refreshes the window
.github/workflows/macos-release.yml   escaped backticks in the heredoc
tests/test_alpha3_repairs.py      NEW  26 regression tests
tests/{test_capture_invariant,test_pipeline,test_detect,test_ui}.py
                                  rewritten for the changed behaviour
```

## Test results

Focused (`test_alpha3_repairs`, pipeline, capture-invariant, detect, storage,
ui, gui_setup, offline, release_pipeline): **206 passed**.

Full suite: **681 passed, 9 skipped** in 57s — the same nine environment skips
as before (CoreAudio host, PlistBuddy, and the real-engine tests that need a
prepared model and language pack).

`test_reverting_the_routing_restores_the_alpha_3_failure` monkeypatches the old
lookup back in and asserts the digital-suspected recordings return to SKIPPED
with no transcript, so the fix's test is not vacuous.

## Still requiring a real Mac

Nothing below has been run on physical hardware:

1. **Spoken English through the MacBook microphone** — that it is transcribed,
   whatever the classifier calls it, and that the bubble shows the words.
2. **Spoken Spanish translated to English** — that the second line appears and
   the first does not repeat it.
3. **Stop monitoring, then transcribe a saved item** — and again after quitting
   and reopening the app.
4. **Stop/start and relaunch persistence** — that the thread keeps every
   earlier transmission and appends new ones.
5. **The text-only bubble** — no waveform, no Play button, playback still
   reachable from the ellipsis menu.

Also unverified on hardware: that the readiness badge settles from "Checking"
to "Field ready" on a genuinely prepared machine, and that completing first-run
setup updates the window without a restart.

---

# Audit correction on the alpha 3 repair pass

Branch `claude/radio-decoder-translator-0oslya`, from
`cfdc214ec5189a0d6121ce59cb63df65dd30dbc5`. Three findings against the previous
commit, all valid, all confirmed against the source before anything changed.
No workflow dispatched, nothing published, alpha 1/2/3 untouched.

## Finding 1 — the noise fix did not survive an upgrade

The previous pass flipped `auto_process_noise` from `False` to `True`. That
fixes a fresh install and nobody else: alpha 3 wrote `auto_process_noise =
false` into every `settings.toml` it saved, and a persisted value beats a
changed default. The exact upgrade hole that had just been closed for
`auto_process_digital` was left open next to it.

`NOISE` is now handled the same way `DIGITAL_SUSPECTED` is:

- `should_auto_transcribe()` returns `True` for both **unconditionally**, not
  through a settings lookup.
- `auto_process_noise` is removed from `DetectorSettings` and `DetectorConfig`
  and added to `RETIRED_OPTIONS`, so an alpha 3 file loads, drops the key, and
  keeps the rest of its `[detector]` section.
- `TONE` and events under 0.25 s remain the only suppressible cases.
- The now-unreachable `"noise"` entry in `_skip_reason` is gone.

**Tests.** `test_an_alpha_3_settings_file_cannot_suppress_noise_or_digital`
loads a config carrying *both* keys as `false` and then runs a real capture
through the pipeline with those settings in force, asserting that the static
burst and the digital burst are both transcribed. A second test loads an actual
`babelfishr.toml` from disk and checks the retired keys are dropped without
taking `threshold_dbfs` with them. `test_detect.py` and `test_pipeline.py` lost
their "operator can opt out of noise" tests, since there is no longer a knob to
turn off; both were replaced with tests that assert the knob's absence and that
static still reaches the engine.

## Finding 2 — a cached processor outlived its operating mode

The standalone pipeline introduced in the previous pass is cached on the app.
`set_mode()` cleared `self.transcription` and `self.translation` but left
`standalone_pipeline` holding its own references to those same engine objects.
So: build a standalone processor in Online/Setup with a cloud-capable engine,
switch to Field Offline, process another saved recording — and
`_processing_pipeline()` returned the cached online processor. Field Offline is
the one mode that promises audio never leaves the machine.

Fixed at the application layer:

- `mode_change_problem()` is checked **before anything is mutated**. Monitoring
  running, or a standalone job in flight, returns a precise sentence and
  `set_mode()` raises `ModeChangeRefused` with `config.mode`, the engines and
  the badge all untouched.
- `_retire_processing()` stops the standalone pipeline and closes its engines,
  and it runs *before* `config.mode` moves — so there is no instant at which a
  cloud-capable processor is reachable while the mode says Field Offline.
- Each pipeline records the mode it was built under (`_standalone_mode`,
  `_session_mode`). `_processing_pipeline()` refuses to hand back a pipeline
  whose mode no longer matches, which also covers a direct write to
  `config.mode` that bypasses `set_mode()` entirely.
- `_discard_standalone_pipeline()` no longer waits up to thirty seconds when
  Start Monitoring meets in-flight work. It retires an idle processor
  instantly, and otherwise raises `ProcessingBusy` immediately — before a
  Session row is written. The GUI shows that message; nothing freezes.
- The GUI disables the processing-mode combo *and* the clickable mode badge
  while monitoring, and `_apply_mode()` catches `ModeChangeRefused`, shows it
  and re-syncs the combo. The app-layer guard remains the enforcement; the
  greying-out is the courtesy.
- Recorded WAVs and database rows are untouched throughout, and a test asserts
  the bytes are identical after a refused start.

**Tests** use a `_SentinelCloudEngine` routed through the real
`build_transcription_engine` factory — assigning `app.transcription` directly
would not work, because `_processing_pipeline()` re-selects engines for the
current mode, which is the behaviour under test. All of it happens on **one**
`BabelFishRApp` instance. Removing `_retire_processing()` from `set_mode()` and
the mode check from `_processing_pipeline()` fails four of these tests.

## Finding 3 — the readiness test checked a name that does not exist

`translation_unverified` looked for a check called `"Translation packages
installed"`. `field_check()` emits `"Installed translation paths"`. The
property therefore answered `False` for every real report, and the unit test
that "covered" it invented the same name and passed without ever touching a
real one.

- The property now uses the production name.
- The hand-built report is gone. `_prepared_report()` drives the real
  `field_check(run_smoke_tests=False)` with only the three things this
  environment cannot supply stubbed — the PortAudio backend, the Whisper model
  and the Argos route — and proves a prepared-but-untested installation is
  `field_ready_unknown` rather than Record Only.
- A separate test proves a machine with **no** translation route is
  definitively not ready and recommends `record-only`: unknown is for untested,
  never for absent.
- `test_the_unverified_properties_use_names_that_field_check_emits` reads the
  quoted names out of the properties' own source and asserts each one appears
  in a real report, so this class of mistake cannot recur silently.

`_refresh_readiness()` now writes "… Checking" to the badge **before** the
worker is launched. It previously started blank and only reached "Checking" if
a *finished* report happened to contain a skipped smoke test, so during the
seconds the check actually takes the toolbar said nothing. A test replaces the
worker factory with one that never calls back and asserts the badge visibly
says Checking throughout; it also drives `_render_readiness` to Field ready and
to Not ready, so the honest end states are still covered.

That test was vacuous on the first attempt — the readiness check started by
`MainWindow.__init__` could land on the badge mid-test — so it now waits for
that report to arrive before it begins. Confirmed by mutation: with the
immediate `setText` removed, it fails.

## Non-vacuity

Each fix was reverted in isolation and the suite re-run:

| Mutation | Tests that fail |
|---|---|
| Put the `NOISE: False` veto back | 2 |
| `set_mode` stops retiring, `_processing_pipeline` stops checking the mode | 4 |
| Restore the fabricated readiness check name | 2 |
| Remove the immediate "… Checking" badge write | 1 |

## Files changed

```
babelfishr/detect.py            NOISE unconditional; setting removed
babelfishr/config.py            auto_process_noise retired and migrated
babelfishr/pipeline.py          the unreachable noise skip reason removed
babelfishr/app.py               ModeChangeRefused, ProcessingBusy,
                                mode_change_problem, _retire_processing,
                                mode-bound pipelines, no 30s wait on start
babelfishr/readiness.py         the real translation check name
babelfishr/ui/main_window.py    _apply_mode/_sync_mode_box, mode controls
                                disabled while monitoring, immediate Checking
tests/test_alpha3_repairs.py    +11 tests (39 total)
tests/{test_detect,test_pipeline,test_capture_invariant}.py
                                the noise knob is gone, not re-defaulted
```

## Test results

Focused (alpha3 repairs, capture-invariant, detect, pipeline, offline,
offline-integration, storage, ui, gui_setup, providers, release_pipeline,
acceptance): **279 passed**.

Full suite: **694 passed, 9 skipped** in 54s. The nine skips are unchanged and
all environmental:

- `test_coreaudio.py:255` — needs a real macOS host with CoreAudio (1)
- `test_packaging.py:373` — PlistBuddy is macOS-only (1)
- `test_real_engines.py:32` — no prepared Whisper model (5)
- `test_real_engines.py:107` — no Argos language pack installed (2)

## Remaining physical-Mac validation

Everything from the previous pass still stands, plus these, none of which has
run on hardware:

1. Spoken English through the MacBook microphone is transcribed whatever the
   classifier calls it — now including anything it calls static.
2. Spoken Spanish translated to English.
3. Stop monitoring, then transcribe a saved item; and again after quitting and
   reopening.
4. Stop/start and relaunch persistence of the message thread.
5. The text-only bubble.
6. **New:** switching to Field Offline on a machine that has been running in
   Online/Setup, and confirming the engine list in Tools ▸ Engine status
   changes with it rather than reporting the old selection.
7. **New:** pressing Start Monitoring while a saved recording is transcribing —
   the refusal should be instant, with the window still responsive.
8. **New:** that the readiness badge visibly passes through "… Checking" on
   launch and settles on "Field ready" on a prepared machine.

An upgrade path worth exercising deliberately: launch this build over an
existing alpha 3 `~/Library/Application Support/BabelFishR/settings.toml` and
confirm it starts, that the two retired keys are gone from the file the next
time settings are saved, and that static and "possibly digital" transmissions
are transcribed.

---

# Alpha 4 work: newest-first thread, named Sessions, honest RF metadata

Branch `claude/radio-decoder-translator-0oslya`, from
`7a42cfc1307911cf241d983857de23f04ff2fe8b`. No workflow dispatched, no tag, no
release; alpha 1/2/3 untouched.

## The candidate build was validated on a real Mac

The non-publishing candidate from workflow run **33319489154**, built from
`7a42cfc`, was installed over alpha 3 on the operator's Apple Silicon Mac.
Confirmed **physically**, on hardware:

- Existing alpha 3 assets survived the upgrade.
- Field readiness reached **Field ready**.
- Field Offline transcription worked through the MacBook microphone.
- Spanish speech was transcribed and translated into English, locally.
- One received event produced one text bubble.
- The permanent waveform cards were gone.
- Older transmissions remained available.

That closes the outstanding items from the previous two passes. It does **not**
touch any of the following, none of which has ever been exercised: **FalconClaw
PTT, a USB radio interface, any radio, any SDR dongle, RF metadata from real
hardware, or digital radio identifiers from a real decoder.** Everything in
section C below was built and tested against deterministic fakes and the
existing recorded-IQ reference path. There is still no tested physical SDR
driver, and this pass did not write one.

## A — the thread reads newest-first, and stays where the operator put it

`TimelineView` inserts at position 0 and `set_transmissions()` sorts
newest-first and opens at the top. Every automatic scroll is gone: there is no
`_scroll_to_bottom` and nothing follows new traffic.

The part that took the work is viewport stability. With newest-first, arriving
traffic is inserted *above* everything on screen, so a reader scrolled into
history would have the text under their eyes pushed down — and again every time
a bubble above them grew through Captured → Transcribing → Translating →
Complete, or gained a translation line.

Every mutation now runs inside `TimelineView._anchored()`, which records the
topmost visible bubble and its exact pixel offset and restores it afterwards.
Two details are load-bearing:

- The anchor is a **widget plus offset**, not a scroll value. Scroll values are
  measured from the top of the content, and the top of the content is exactly
  what moves.
- The restore runs immediately **and** on the next few turns of the event loop
  (`_ANCHOR_SETTLE_PASSES = 3`). Qt lays a newly inserted widget out on the next
  pass, and a wrapped label settles its height over a resize round-trip, so a
  single synchronous correction lands short. Three turns is microseconds; it is
  over long before a person could scroll.

Measured in a real `QScrollArea` with 30 bubbles overflowing a 400px viewport:
insertion above the viewport moves the anchor by **0 px**, and so does each of
the three state changes and the appearance of a translation.

Ordering elsewhere is deliberate and documented: search and the review queue
replace the thread and are reachable back via View ▸ Show all transmissions;
exports are **chronological**, because an export is a record of what was
received and when.

## B — named Session tabs, over the unchanged Session row

`Session` still means one monitoring run. The new `Conversation` is the named
thread the interface calls a Session; the low-level run is never shown.

- A tab bar above the thread, a permanent **General** tab, `+` to create, and
  Rename (button or double-click).
- Tabs, names and the last selected tab survive relaunch
  (`config.session.conversation_id`).
- Start Monitoring pins the destination in `app._capture_conversation_id`.
  Switching tabs mid-watch is allowed and changes nothing about where traffic
  is filed; the header shows "● Recording into <name>" when the viewed tab is
  not the capture tab.
- `MainWindow._belongs_here()` gates incoming `transmission`/`updated` events,
  so live traffic cannot appear in a thread it does not belong to.
- One `CaptureService` and one `ProcessingPipeline`, globally, unchanged. Tabs
  create no receivers and no processors; a second `start_session` still raises.
- Export was taking `store.list_sessions(1)` — the newest capture run. Both
  bundle and text exports now cover **every run** in the selected named
  Session, sorted chronologically.

No tab deletion in this pass, deliberately: there is no need to add a
data-loss path to make tabs useful.

### Schema migration, 3 → 4

Explicit, additive and idempotent. `CREATE TABLE IF NOT EXISTS` is a no-op
against an existing table, so it is not a migration on its own:

1. `_SCHEMA` creates anything absent (including the new `conversations` table).
2. `_ADDED_COLUMNS` is applied one column at a time with `ALTER TABLE ... ADD
   COLUMN`, guarded by `PRAGMA table_info`. SQLite fills existing rows with the
   declared default, so nothing is rewritten:
   `sessions.conversation_id`, and on `transmissions`: `snr_db`,
   `snr_provenance`, `squelch_code`, `squelch_code_provenance`, `talkgroup`,
   `talkgroup_provenance`, `unit_id`, `unit_id_provenance`, `protocol`,
   `protocol_provenance`, `signal_metadata`.
3. `_POST_MIGRATION_SCHEMA` adds `ix_sess_conv` — separately, because indexing
   `conversation_id` before step 2 fails against a schema-3 database and takes
   the whole migration with it. (That is not hypothetical: it happened during
   development and is why the index moved.)
4. `_backfill_default_conversation()` guarantees General exists and assigns
   every session with no conversation to it.

The migration test builds a genuine schema-3 database from **the previous
commit's own DDL** (`git show 7a42cfc:babelfishr/storage.py`), not a
hand-written approximation, then opens it through the new `Store` and asserts
every audio path, transcript, translation, both corrections, notes, tags,
bookmarks and frequency provenance survive, that all three old runs land in
General, and that reopening three more times changes nothing.

## C — RF and transmitter metadata, with provenance on every value

New per-transmission fields, each with its own provenance: `snr_db`,
`squelch_code` (CTCSS/PL or DCS), `talkgroup`, `unit_id`, `protocol`, plus
`signal_metadata` holding raw decoder output verbatim. Existing
frequency/channel/RSSI/modulation fields keep their provenance.

`Transmission.signal_summary()` returns only values that exist — an absent
value produces no entry at all, never a dash an operator could read as a
measurement — and marks anything not measured (`462.5750 MHz (profile)`,
`unit 4021 (decoded)`). `Provenance.is_measured` remains SDR and RADIO only.

`has_supplied_unit_id` is True only when an SDR, a decoded protocol, a radio or
the operator supplied it. **A squelch tone is not an identity**: CTCSS/DCS is
channel access, shared by every radio set to it, and is labelled `squelch`.
Nothing infers a speaker from a voice, and nothing derives frequency, tone,
talkgroup or unit ID from microphone or line-level audio — a test drives a real
capture and asserts every one of those fields stays empty.

`babelfishr/signal_metadata.py` promotes genuinely decoded values:
`apply_decoded_metadata()` maps DSD-neo's `talkgroup`/`source_id` onto the
transmission with `Provenance.DSD` and keeps the whole raw dict (including
`color_code` and `nac`, which identify a system rather than a transmitter and
are kept but not promoted). `apply_source_metadata()` does the same for a
signal source, and a source that states no provenance gets `UNKNOWN` — it does
not get to claim it was measured.

## D — the operating mode is a control, not a label

The header's `QLabel` with a `mousePressEvent` is replaced by a
`QToolButton` reading **"Operating mode: FIELD OFFLINE ▾"** with a checkable
menu of all three modes, each keeping its description. It takes focus, opens
from the keyboard, and announces itself. Field readiness stays a separate chip.
The duplicate "Processing" combo in the collapsible panel is **removed**, so
exactly one selector exists — asserted by walking every `QComboBox` in the
window. The control is disabled while monitoring; the app-layer guard from
`7a42cfc` remains the enforcement and is untouched.

## E — Session Options, and a panel that collapses cleanly

Renamed from "Session setup". `_toggle_setup_panel()` used to call
`setVisible()` on everything `findChildren(QWidget)` returned — which includes
each combo box's popup view and each scroll area's scrollbars and viewport, and
is exactly why dropdowns and stray scrollbars appeared after a collapse. There
is now one `setup_content` container and the toggle shows or hides that single
widget.

The regression test drives four real collapse/expand cycles on the actual
window and asserts no combo popup and no scrollbar becomes visible and no
control loses its value. A second test parses `_toggle_setup_panel`'s syntax
tree and fails if `findChildren` reappears.

## F — nothing regressed

Capture-first persistence, WAV immutability, device pinning and ambiguous-device
refusal, saved-recording transcription with monitoring stopped, history across
stop/start and relaunch, noise and digital-suspected audio reaching ASR, tone
and sub-0.25 s suppression, Field Offline privacy enforcement, mode-bound engine
retirement, immediate refusal when saved processing is busy, Argos certificates
and managed paths, uninstaller containment — all still covered and passing.

## Files changed

```
babelfishr/models.py               Conversation; RF/transmitter fields;
                                   signal_summary(); has_supplied_unit_id
babelfishr/storage.py              schema 4, explicit ALTER migration,
                                   conversation CRUD and queries
babelfishr/config.py               session.conversation_id
babelfishr/app.py                  conversation selection, capture destination,
                                   conversation-scoped thread
babelfishr/signal_metadata.py      NEW  decoded/source metadata promotion
babelfishr/export.py               export a whole named Session
babelfishr/ui/timeline.py          newest-first, viewport anchoring,
                                   metadata in the bubble header
babelfishr/ui/main_window.py       Session tabs, mode tool button, one
                                   selector, Session Options panel fix,
                                   Session-scoped exports
tests/test_alpha4_thread_and_sessions.py  NEW  30 tests
tests/{test_ui,test_alpha3_repairs}.py    updated for the new mode control
```

Two existing UI tests were rewritten rather than patched around:
`test_mode_badge_shows_the_operating_mode` became
`test_the_operating_mode_control_shows_the_mode`, and eight
`TimelineView().add(...)` temporaries now hold the view. Those temporaries only
survived before because `add()` left a `QTimer` holding a reference to the
view; removing the auto-scroll removed that accidental lifeline, which is a
test-hygiene fix, not a behaviour change.

## Test results

Focused (alpha4, alpha3 repairs, capture-invariant, detect, pipeline, offline,
offline-integration, storage, ui, gui_setup, models, providers,
release_pipeline, acceptance, packaging): **366 passed, 1 skipped**.

Full suite: **724 passed, 9 skipped** in 70s. Exact skips:
`test_coreaudio.py:255` needs a real macOS host with CoreAudio (1);
`test_packaging.py:373` PlistBuddy is macOS-only (1);
`test_real_engines.py:32` no prepared Whisper model (5);
`test_real_engines.py:107` no Argos language pack installed (2).

## Non-vacuity

Each behaviour was reverted in isolation and the suite re-run:

| Mutation | Failing tests |
|---|---|
| Restore bottom insertion | 3 |
| Remove viewport-anchor compensation | 2 |
| Restore recursive descendant visibility in the panel toggle | 2 |
| Restore the second Processing selector | 1 |
| Remove Session filtering from the thread query | 3 |
| Remove the schema migration and backfill | 2 |
| Present an operator/profile value as measured | 1 |
| Treat an unprovided unit ID as identified | 1 |

## Still unverified on physical hardware

Never exercised, by this pass or any before it: **FalconClaw PTT, a USB radio
interface, any radio, any SDR dongle, real RF metadata, and digital radio
identifiers from a real decoder.** The SignalSource contract and the
recorded-IQ reference path exist; there is no tested physical SDR driver and
this pass deliberately did not fabricate one.

Not yet seen on a Mac from this commit: the newest-first thread with real
arriving traffic, viewport stability under a live watch, the Session tabs
(creating, renaming, switching mid-watch, and the "Recording into" notice),
the upgrade of a real alpha 3 database to schema 4, the new operating-mode
button, and the repaired Session Options panel.

---

# Audit repair: three integration defects in 17ffad82

Branch `claude/radio-decoder-translator-0oslya`, from
`17ffad82d5388bf62802ab00697c32fd7bed47f6`. Three defects found by a code
audit, all reproduced against the source before anything was changed. No
workflow dispatched, no tag, no release; alpha 1/2/3 untouched.

All three share a shape worth naming: **each seam was between two pieces that
were individually correct and individually tested.** A helper that was written
and never called. A query that was made per-Session and reached from a global
entry point. A value pinned for the life of a capture that nothing unpinned.
Unit tests of each piece passed. That is why the new tests drive production
paths — `CaptureService`, `BabelFishRApp.search`, `MainWindow._stop_monitoring`
— rather than the helpers underneath them.

## 1 — source metadata never reached the capture path

`babelfishr/signal_metadata.py` had `apply_source_metadata()`, and
`CaptureService._apply_measured_metadata()` still had its own hand-written copy
of three fields. Against a real `CaptureService` with a source reporting
frequency, RSSI, SNR and modulation: frequency, RSSI and modulation arrived;
**`snr_db` stayed `None` and `signal_metadata` stayed `{}`**. The old code also
stamped `Provenance.SDR` on everything it copied, so a recorded replay's values
would have been labelled as measured.

`_apply_measured_metadata()` now delegates to the shared helper. Metadata
failure remains strictly non-fatal — the whole call is inside one `try`, and a
source that raises still produces a recording with the fields empty.

`SignalMetadata` gained the rest of an honest contract: `squelch_code`,
`talkgroup`, `unit_id`, `protocol`, `source` (the key the raw record is stored
under) and `extra`, alongside the existing frequency/RSSI/SNR/modulation and
`provenance`. The source's stated provenance is preserved rather than
hardcoded; a source that states none is `UNKNOWN`, and `frequency_is_measured`
is then false.

`to_dict()` and the promotion path both coerce to JSON-serialisable values, so
a driver handing back a numpy scalar or its own object cannot be what stops a
transmission being written. Verified through a real database round trip.

Ordinary audio is unchanged: a microphone replay still produces no frequency,
RSSI, SNR, squelch code, talkgroup, unit ID, protocol or raw record, and an
empty `signal_summary()`.

## 2 — Search and Review Queue ignored the selected Session

`MainWindow._search()` called `app.search(text)` and `_show_review_queue()`
called `app.review_queue()`; both returned every matching row in the database.
Opened from a Session tab, they showed other Sessions' traffic — the same
misfiling the thread itself refuses.

`Store.search()` and `Store.review_queue()` both take `conversation_id`, and
filter through `Transmission → session_id → sessions.conversation_id`, so a
named Session spans every monitoring run inside it. `BabelFishRApp.search()`
and `review_queue()` default to the viewed thread; passing
`conversation_id=None` explicitly still searches everything.

One thing worth recording because it nearly shipped silently: in
`review_queue()` the scope clause sits **before** the two confidence
thresholds in the statement, so the parameters have to be bound in that order.
The first version appended the conversation id after the thresholds and the
query matched nothing. The test caught it ("nothing was reviewable, so this
proves nothing" is an assertion in the test for exactly this reason).

## 3 — the capture destination survived the capture

`stop_session()` left `_capture_conversation_id` populated, so
`capture_conversation_id` kept naming a destination after capture had ended,
and a window on another tab kept showing "● Recording into …" with nothing
recording. (The clear had been written in the previous pass as an unasserted
string replacement that silently did not match — a lesson about `str.replace`
without an assertion.)

It is now cleared on every stop path: normal stop, `close()` (which routes
through `stop_session`), and failed startup. The start was split so the pinned
destination is set and then everything after it runs inside a `try` that clears
the pin, the session and any half-built pipeline before re-raising — the
window between pinning and writing the Session row is real and is now covered.

Pinning during capture is unchanged and deliberate: switching tabs mid-watch
reviews history and never redirects live traffic.

## Also fixed

`test_nothing_scrolls_to_the_bottom_by_itself` ended in
`assert abs(bar.value() - where) < 4 or True`, which asserts nothing. It now
anchors on a visible bubble and asserts it has not moved a pixel after five
consecutive arrivals. The exact viewport-anchor tests were not touched.

## Files changed

```
babelfishr/sources.py             SignalMetadata: squelch/talkgroup/unit/
                                  protocol/source/extra; JSON-safe to_dict
babelfishr/pipeline.py            capture delegates to the shared promotion
babelfishr/signal_metadata.py     JSON-safe raw record
babelfishr/storage.py             conversation_id on search and review_queue
babelfishr/app.py                 both default to the viewed Session; capture
                                  destination cleared on every stop path
tests/test_alpha4_integration_repairs.py  NEW  17 tests
tests/test_alpha4_thread_and_sessions.py  the vacuous assertion replaced
```

## Test results

Focused (integration repairs, alpha4 thread/sessions, storage, pipeline,
capture-invariant, ui, offline, offline-integration, acceptance, alpha3
repairs, models): **236 passed**.

Full suite: **741 passed, 9 skipped** in 62s. Exact skips, all environmental
and unchanged: `test_coreaudio.py:255` needs a real macOS host with CoreAudio
(1); `test_packaging.py:373` PlistBuddy is macOS-only (1);
`test_real_engines.py:32` no prepared Whisper model (5);
`test_real_engines.py:107` no Argos language pack installed (2).

`git diff --check` clean, `compileall` clean over `babelfishr`, `tests` and
`packaging`, all five packaging scripts pass `bash -n`, the PyInstaller spec
parses and the workflow YAML loads.

## Non-vacuity

| Mutation | Failing tests |
|---|---|
| Restore the old manual metadata path in `_apply_measured_metadata` | 4 |
| Remove `conversation_id` filtering from search and review_queue | 3 |
| Retain `_capture_conversation_id` after stop | 4 (including the UI label) |

## Still unverified on physical hardware

Unchanged and worth repeating plainly: **no SDR dongle, no radio, no USB radio
interface and no FalconClaw PTT has ever been connected to this software.**
Defect 1 is about the path a real source's metadata would travel; it is
exercised with a source that wraps the production replay source and reports
metadata as a driver would. That proves the plumbing, not the driver — there
is still no tested physical SDR driver, and this pass did not write one.

Not yet seen on a Mac from this commit: Session-scoped search and review from
a tab, the "Recording into …" notice clearing on stop, and any real RF
metadata reaching a bubble.

---

# Audit repair: provenance defaults, source scalars, abandoned runs

Branch `claude/radio-decoder-translator-0oslya`, from
`d323ccc3c4909f9c40861a92b4b76f240ac7e5b2`. Three defects, all reproduced
against that commit before anything changed. No workflow dispatched, no tag,
no release; alpha 1/2/3 tags unmoved.

## 1 — an omitted provenance was read as measured

`SignalMetadata.provenance` defaulted to `Provenance.SDR`. Reproduced:
`SignalMetadata(tuned_frequency_hz=462_562_500.0)` with no provenance argument
gave `provenance: sdr-measured`, and after promotion
`frequency_provenance: sdr-measured` with `frequency_is_measured: True`. A
recorded replay, a configured constant, or a driver that had not got round to
saying where its numbers came from would all have read as a live receiver's
measurements — directly against the rule that RF metadata must not be
represented as measured when it was not genuinely supplied.

The default is now `Provenance.UNKNOWN`. Measured status requires an
affirmative claim: a live SDR passes `Provenance.SDR`, a radio reporting its
own tuning passes `RADIO`, and anything silent or invalid stays `UNKNOWN` and
renders as *unverified*. `RecordedIQSource`'s fallback metadata makes no claim,
because replaying a recording is not taking a measurement. No existing
`Provenance` member changed meaning; only the default did.

**Existing tests changed (both in `tests/test_analysis.py`), and why:**

- `test_recorded_iq_source_satisfies_the_signal_interface` asserted
  `metadata.provenance is Provenance.SDR` on a `SignalMetadata` that never
  claimed it — it was asserting the defect. It now asserts `UNKNOWN` for the
  silent source and adds a second source that passes `Provenance.SDR`
  explicitly, so the measured arm stays covered.
- `test_signal_source_metadata_reaches_the_transmission` stands for a genuinely
  measuring receiver, so it now supplies `provenance=Provenance.SDR`
  explicitly. Its assertions are unchanged.

## 2 — a NumPy scalar reached the column and broke the bubble

Reproduced through the production capture path with `numpy.float32` RSSI and
SNR: capture completed, SQLite stored both as **blobs**
(`typeof(rssi_dbm) = 'blob'`), reload returned `bytes`, and `signal_summary()`
raised `TypeError: unsupported format string passed to bytes.__format__`. The
raw JSON record was being coerced; the promoted `Transmission` fields were not.

`_as_measurement()` now normalises at the boundary — in
`apply_source_metadata`, so every future driver is protected without knowing it
needs to be. It returns a native `float` for ordinary numbers and NumPy
scalars, and `None` for anything that is not a measurement: booleans (`True` is
not −73 dBm), non-numeric values, and NaN or ±infinity. Rejected values are not
promoted, and the raw report is still kept in JSON-safe `signal_metadata` for
diagnostics. `_jsonable` now converts numeric scalars to real numbers rather
than stringifying them, so the diagnostic record stays faithful.

Nothing about this can cost a recording: an invalid measurement leaves the WAV,
the row and the rest of the metadata intact.

## 3 — a failed start left a run open forever

Reproduced: `save_session()` succeeds, `ProcessingPipeline.start()` raises,
`start_session()` clears `app.session` and the capture destination — and the
database keeps the run with `ended_at` NULL, with nothing left that can close
it, because the caller's later `stop_session()` has no session to work on. A
monitoring run that never began read as one still in progress.

`_abandon_failed_start()` now runs on any failure after the pin. Invariants
after it: `capture_conversation_id == ""`, `session`, `capture` and `pipeline`
all `None`, partially started workers stopped (`pipeline.stop(wait=True)`), and
no run left open. Every cleanup step is inside its own guard so a cleanup
failure cannot replace the operator's real error with a less useful one — the
original exception is always what propagates.

**Implementation choice — closed, not deleted.** This was explicitly not an
Eric-stated decision and is not presented as one. The abandoned run is closed
(`ended_at` set) with a note recording why: *"Monitoring failed to start:
<error>"*. Reasons: it keeps the audit history of an attempt that happened;
deleting rows on an error path is how data gets lost if the model later changes
and such a row is no longer guaranteed empty; and closing is the smaller change.
The row holds no transmissions, so it never appears in a thread and cannot be
mistaken for a successful operator Session. If a future decision prefers
removal, the change is confined to that one method.

## Files changed

```
babelfishr/sources.py                       provenance defaults to UNKNOWN;
                                            _jsonable shares the promotion path
babelfishr/signal_metadata.py               _as_measurement(); numeric fields
                                            normalised before persistence
babelfishr/app.py                           _abandon_failed_start()
tests/test_alpha4_metadata_and_startup.py   NEW  30 tests
tests/test_analysis.py                      two tests updated (see above)
```

No schema change was needed.

## Test results

Focused (this file, alpha4 integration repairs, alpha4 thread/sessions,
analysis/source metadata, storage, pipeline, capture-invariant, ui, models,
offline-integration, acceptance): **234 passed** after the two `test_analysis`
updates.

Full suite: **771 passed, 9 skipped** in 92s. Exact skips, unchanged and all
environmental: `test_coreaudio.py:255` needs a real macOS host with CoreAudio
(1); `test_packaging.py:373` PlistBuddy is macOS-only (1);
`test_real_engines.py:32` no prepared Whisper model (5);
`test_real_engines.py:107` no Argos language pack installed (2).

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests` and
`packaging`; all five packaging scripts pass `bash -n`; the spec parses and the
workflow YAML loads.

## Non-vacuity

| Mutation | Failing tests |
|---|---|
| Restore `SignalMetadata`'s `SDR` default | 2 |
| Remove numeric normalisation | 9 |
| Restore the `d323ccc` partial-start cleanup | 3 |

The d323ccc repairs are re-asserted here too — helper wiring, Session-scoped
search and review, capture-destination clearing — and ordinary replay audio
still invents no RF metadata.

## Limitations carried forward, unchanged

**No SDR dongle, radio, USB radio interface or FalconClaw PTT has ever been
connected to this software.** These repairs concern the path a real driver's
metadata would travel; the tests use a wrapper around the production replay
source that reports metadata the way a driver would. That proves the plumbing,
not a driver. There is still no physical SDR driver and this pass did not write
one — and a NumPy scalar reaching SQLite as a blob is exactly the class of
problem that only appears once real hardware is attached, which is why the
normalisation sits at the boundary rather than in any one caller.

Still not seen on a Mac from any commit on this branch: Session-scoped search
and review from a tab, the "Recording into …" notice clearing on stop, real RF
metadata reaching a bubble, and the newest-first viewport behaviour under live
traffic.

---

# Audit repair: independent cleanup guards, honest frequency labels

Branch `claude/radio-decoder-translator-0oslya`, from
`122ae9ed05d21abc0f42ad03c90970151d16da44`. Two defects, both reproduced
against that commit before anything changed. No workflow dispatched, no tag,
no release; Alpha 1/2/3 tags unmoved. No schema change.

Both defects share a shape worth naming for whoever picks this up: each is a
**claim that outran the code**. One was a docstring promising per-step guards
over two operations sharing a single `try`. The other was a data model that
had learned six kinds of provenance paired with a UI that still asked it one
boolean question. Neither is visible from inside the function that got it
wrong, which is why the new tests drive `start_session()` and
`TransmissionBubble` rather than the helpers underneath them.

## 1 — the cleanup steps were not independent

`_abandon_failed_start()` said "every cleanup step is inside its own guard",
but `store.save_session(session)` and `store.close_session(session.id, …)`
sat in one `try` block. Reproduced against `122ae9e`:

1. the initial `save_session()` succeeds and the run is written;
2. `ProcessingPipeline.start()` raises *"worker startup failed"*;
3. the cleanup `save_session()` — the explanatory note — is made to raise;
4. `close_session()` is **never attempted**;
5. the original worker error propagates correctly, as designed, so nothing
   looks wrong from the caller's side;
6. and the row stays open with `ended_at` NULL, with `self.session` already
   cleared, so the operator's later `stop_session()` has nothing to close it
   with.

That is the same "a run that never began reads as one still in progress"
failure the previous pass fixed — reintroduced through the one path the
previous pass's own docstring claimed was safe.

**The repair.** The note and the closure are now two separate `try` blocks,
**closing first**. Priority is deliberate: the note is a nicety, the open row
is the defect. `session.ended_at` is set before either, so both carry it.

**Behaviour when either operation fails:**

| Failure | Result |
|---|---|
| Note persistence fails | `close_session()` still runs; row closed; note lost |
| `close_session()` fails | `save_session()` still runs and writes `ended_at`; row closed; note kept |
| Both fail | The row may stay open — nothing else can be done from here — but the **original startup exception still propagates**, and `session`, `capture`, `pipeline` are `None` and `capture_conversation_id` is empty |

In no case does a cleanup failure replace the operator's real error. The
storage layer was not redesigned; the change is confined to that one method.

**Implementation choice, restated — closed, not deleted.** Unchanged from the
previous pass and still explicitly **not an Eric-stated requirement**. The
abandoned run is closed with the note *"Monitoring failed to start: <error>"*
rather than removed: it keeps the audit history of an attempt that happened,
and deleting rows on an error path is how real data gets lost if the model
later changes and such a row is no longer guaranteed empty. It holds no
transmissions, so it never appears in a thread.

## 2 — the bubble called every unmeasured frequency "entered"

`Transmission.signal_summary()` renders `UNKNOWN` correctly as *unverified*.
`TransmissionBubble.update_from()` never called it — it built its own label
from `suffix = "" if tx.frequency_is_measured else " (entered)"`. Since
`is_measured` is true only for `SDR` and `RADIO`, every other origin became
"entered": a value an SDR supplied without stating its provenance, one the
software inferred, and one DSD-neo decoded were all labelled as something the
operator typed. All three claims were false, and the actual UI disagreed with
the data model that had been carefully taught the distinction.

The bubble now maps provenance to a label through one table:

| Provenance | Suffix |
|---|---|
| `SDR`, `RADIO` | *(none)* |
| `OPERATOR`, `PROFILE` | `(entered)` |
| `INFERRED` | `(inferred)` |
| `DSD` | `(decoded)` |
| `UNKNOWN`, or anything unrecognised | `(unverified)` |

`OPERATOR` and `PROFILE` keep "(entered)" for compatibility with what
operators already read. The fallback is deliberately `(unverified)` rather
than blank: a provenance the table does not know must not be silently promoted
to looking like a measurement. No `Provenance` member changed meaning and no
other metadata layout changed.

The end-to-end test for this drives the production source-metadata path with a
source that reports `SignalMetadata(tuned_frequency_hz=462_562_500.0,
source="mystery-driver")` and no provenance, and asserts the bubble it
eventually produces reads *unverified* — the omitted-provenance default from
the previous pass and this label repair, checked together.

## Files changed

```
babelfishr/app.py                          _abandon_failed_start(): the note
                                           and the closure split into two
                                           independent try blocks, closing first
babelfishr/ui/timeline.py                  _FREQUENCY_SUFFIX table and
                                           _frequency_suffix(); update_from()
                                           uses it instead of the boolean
tests/test_alpha4_cleanup_and_labels.py    NEW  18 tests
```

**No existing test was changed in this pass**, and no schema changed.

## Test results

Focused (this new file, alpha4 metadata/startup, alpha4 integration repairs,
alpha4 thread/Sessions, analysis, storage, pipeline, capture-invariant, ui,
models, offline-integration, acceptance): **252 passed** in 143s.

Full suite: **789 passed, 9 skipped** in 92s. Exact skips, unchanged from the
previous pass and all environmental:

- `tests/test_coreaudio.py:255` — needs a real macOS host with CoreAudio (1)
- `tests/test_packaging.py:373` — PlistBuddy is macOS-only (1)
- `tests/test_real_engines.py:32` — no prepared Whisper model (5)
- `tests/test_real_engines.py:107` — no Argos language pack installed (2)

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests` and
`packaging`; all five packaging scripts pass `bash -n`; `packaging/babelfishr.spec`
parses; `.github/workflows/macos-release.yml` loads as YAML.

## Non-vacuity

Both mutations were applied to the repaired code and reverted after measuring.

| Mutation | Failing tests |
|---|---|
| Recombine the note and the closure into one `try` block | **3** — `test_a_failed_note_still_closes_the_abandoned_run`, `test_a_valid_run_works_after_a_cleanup_whose_note_failed`, `test_the_two_cleanup_operations_are_in_separate_guards` |
| Restore `suffix = "" if tx.frequency_is_measured else " (entered)"` | **6** — the `inferred`, `dsd` and `unknown` parametrisations, `test_unknown_inferred_and_decoded_are_never_called_entered`, `test_the_bubble_agrees_with_the_data_model`, `test_a_source_with_no_provenance_ends_up_labelled_unverified` |

The first mutation fails behavioural tests, not only the AST check, which is
the point: the AST test names the cause, but the guarantee is that the run is
closed. One subtlety worth recording for whoever writes the next such test —
the harness must break only the **cleanup** `save_session` (conditional on
`session.ended_at is not None`). Breaking it unconditionally makes the
*initial* save fail before `ProcessingPipeline.start()` is ever reached, and
the test then proves nothing about cleanup at all. The first draft of
`test_both_cleanup_operations_failing_still_reraises_the_real_error` had
exactly that bug and failed with `sqlite3.OperationalError` instead of the
expected `RuntimeError`.

## Limitations carried forward, unchanged

**No SDR dongle, radio, USB radio interface or FalconClaw PTT has ever been
connected to this software.** These repairs concern the path a real driver's
metadata would travel; the tests wrap the production replay source and report
metadata the way a driver would. That proves the plumbing, not a driver —
there is still no physical SDR driver and this pass did not write one. Worth
noting plainly: a NumPy scalar reaching SQLite as a blob is exactly the class
of problem that surfaces only once real hardware is attached, which is why the
normalisation sits at the boundary rather than in any one caller.

Still not seen on a Mac from any commit on this branch: Session-scoped search
and review from a tab, the "Recording into …" notice clearing on stop, real RF
metadata reaching a bubble, and the newest-first viewport behaviour under live
traffic. **A macOS candidate build remains undispatched** — it is blocked on
nothing now that these two defects are repaired, but dispatching one was
outside this pass.

---

# Build repair: the migration baseline no longer reads Git at run time

Branch `claude/radio-decoder-translator-0oslya`, from `41a2c09`. A test-harness
repair only. No production code, workflow, packaging, schema or release-notes
change; Alpha 1/2/3 tags unmoved; no Alpha 4 tag or release.

## The failure

Run **33445974543** (run #18, attempt 1) of `.github/workflows/macos-release.yml`
on `41a2c09`, dispatched non-publishing with `runner_label: macos-26`,
`publish_prerelease: false`, empty `release_tag`, **failed** at step 6 — *Build,
test, verify, sign and package* — at the pre-packaging test gate. Result on the
runner: **2 failed, 787 passed, 9 skipped**.

```
tests/test_alpha4_thread_and_sessions.py:402: in schema_3_database
    source = subprocess.run(
E   subprocess.CalledProcessError: Command '['git', 'show', '7a42cfc:babelfishr/storage.py']'
    returned non-zero exit status 128.
```

Both `test_an_alpha_3_database_upgrades_without_losing_anything` and
`test_the_migration_is_idempotent` error in the helper, before either test body
runs.

**Root cause.** `schema_3_database()` built its schema-3 starting point by
reading the DDL out of Git at run time. The workflow's checkout is a bare
`actions/checkout@v5` with no `fetch-depth`, so the runner has a one-commit
shallow clone containing only the head commit. `7a42cfc` is not in it, `git show`
exits 128, and the tests cannot construct their baseline. This is deterministic,
not a flake: it reproduces on every run of that workflow, and passes locally only
because a development clone happens to have full history. Nothing about the two
repairs in `41a2c09` is involved — those tests are green on the runner.

Because the gate is *before* packaging, no bundle, DMG, signing report or
verification report was produced. Every report printed `(not produced)`.

## The repair, and why this one

The fix is **not** to deepen the checkout. Fetching history would make the
symptom go away while leaving the test dependent on something a test has no
business needing: a migration test must be able to build its own starting point
from a source tree alone — a shallow clone, an exported archive, a directory
with no `.git` at all. Deepening the clone hides the dependency; removing it
removes the whole class of failure. So the workflow is untouched.

The schema-3 DDL is now checked in at **`tests/fixtures/schema_3.sql`**.

**Provenance, verified in this pass rather than assumed.** The file is the exact
contents of the `_SCHEMA` string literal in `babelfishr/storage.py` at the final
schema-3 revision. Extracted independently from both revisions with the same
slice the helper used to use, the two are byte-for-byte equal:

| Revision | Length | SHA-256 of the DDL |
|---|---|---|
| `v0.3.0-alpha.3` = `c9299e06e8731db1645441da3677cf24358243dd` | 3787 | `af7dc8a1b94a78cdeace5f4a7519d32ed32a945dece44d7de39a1a6b0522d4da` |
| final schema-3 = `7a42cfc1307911cf241d983857de23f04ff2fe8b` | 3787 | `af7dc8a1b94a78cdeace5f4a7519d32ed32a945dece44d7de39a1a6b0522d4da` |

`7a42cfc` really is the last schema-3 revision: it is the final commit where
`storage.py` carried `SCHEMA_VERSION = 3`, and the next commit to touch that
file, `17ffad8`, raised it to 4. Both facts were checked directly, not taken on
report.

The fixture carries that provenance in its own header comment, including the
instruction not to regenerate it from the current schema — it is schema 3 on
purpose, because a migration validated against a reconstruction of its own
output validates nothing.

## What changed in the test module

- `schema_3_ddl()` — new. Reads the fixture and strips the leading run of `--`
  lines, which is the whole header and nothing else, so what it returns is the
  deployed DDL byte for byte. That exactness is what lets the digest pin it.
- `schema_3_database()` — reads the fixture instead of shelling out. Retains a
  guard that the baseline really is schema 3 (`conversation_id`, the schema-4
  addition, must be absent).
- `import subprocess` removed; it is now unused.
- The module docstring no longer claims to read the previous commit's DDL from
  Git, because it no longer does.
- **Neither migration test was weakened.** Their bodies, and every preservation
  and idempotence assertion in them, are unchanged.

Two new regressions guard the repair:

- `test_the_schema_3_baseline_is_the_real_deployed_ddl` — pins the fixture by
  SHA-256 and length, and asserts schema 3's shape positively (the four tables
  present; no `conversation_id`, no `conversations`).
- `test_the_migration_tests_do_not_depend_on_repository_history` — parses this
  module with `ast` and asserts `subprocess` is not imported, and that the
  fixture resolves relative to the test file. The AST check rather than a text
  search, because a test that greps its own source for `import subprocess`
  matches its own assertion and can never pass.

## History-free validation

The point of the repair is that these tests work without a repository, so that
is what was measured — a copy of the source tree with `.git` excluded entirely,
run on a `PATH` containing no `git` binary at all:

```
tar --exclude=.git --exclude=__pycache__ -cf - . | (cd "$TREE" && tar -xf -)
cd "$TREE" && PATH="$BIN" QT_QPA_PLATFORM=offscreen python3 -m pytest \
  tests/test_alpha4_thread_and_sessions.py::test_an_alpha_3_database_upgrades_without_losing_anything \
  tests/test_alpha4_thread_and_sessions.py::test_the_migration_is_idempotent \
  tests/test_alpha4_thread_and_sessions.py::test_the_schema_3_baseline_is_the_real_deployed_ddl \
  tests/test_alpha4_thread_and_sessions.py::test_the_migration_tests_do_not_depend_on_repository_history \
  -v
```

Result: **4 passed in 0.16s**, with `git rev-parse --git-dir` reporting *"not a
git repository"* in that tree and `command -v git` finding nothing on that PATH.

**Directional proof, same tree.** Restoring `41a2c09`'s version of the test file
into that identical `.git`-free tree and running the two migration tests:
**2 failed**, `FileNotFoundError: [Errno 2] No such file or directory: 'git'`.
Old code fails, new code passes, everything else held constant.

## Files changed

```
tests/fixtures/schema_3.sql                NEW  the deployed schema-3 DDL + provenance
tests/test_alpha4_thread_and_sessions.py   fixture loader; subprocess import and the
                                           Git claim removed; 2 regressions added
docs/AGENT_HANDOFF.md                      this section
```

Nothing under `babelfishr/`, `.github/` or `packaging/` was touched — verified
with `git diff --name-only` over those three paths, which is empty.
`SCHEMA_VERSION` is still 4 and no migration behaviour changed.

## Test results

Focused (alpha4 thread/Sessions, alpha4 cleanup/labels, alpha4
metadata/startup, alpha4 integration repairs, storage, packaging, release
pipeline, models, acceptance): **240 passed, 1 skipped**.

`tests/test_alpha4_thread_and_sessions.py` alone: **32 passed** (30 before,
plus the two new guards).

Full suite: **791 passed, 9 skipped** in 51s — the 789 from `41a2c09` plus the
two new tests. Exact skips, unchanged and all environmental:

- `tests/test_coreaudio.py:255` — needs a real macOS host with CoreAudio (1)
- `tests/test_packaging.py:373` — PlistBuddy is macOS-only (1)
- `tests/test_real_engines.py:32` — no prepared Whisper model (5)
- `tests/test_real_engines.py:107` — no Argos language pack installed (2)

Note for whoever reads the next runner log: the hosted macOS skip set is a
different *composition* of the same total. There `test_coreaudio.py:76` and
`:148` skip because the host **has** CoreAudio, while `:255` and
`test_packaging.py:373` run.

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests` and
`packaging`; all five packaging scripts pass `bash -n`;
`packaging/babelfishr.spec` parses; `.github/workflows/macos-release.yml` loads
as YAML. The fixture also executes standalone into an in-memory SQLite database,
producing exactly `meta`, `profiles`, `sessions`, `transmissions`.

## What this pass does not change

A downloadable macOS candidate still does not exist. This repair removes the
reason the last attempt never reached packaging; it does not itself prove the
build succeeds, because no workflow was dispatched in this pass. The next
dispatch is the thing that would establish that.

## Limitations carried forward, verbatim

"No SDR dongle, radio, USB radio interface or FalconClaw PTT has ever been
connected to this software. This run would not have validated any of that
regardless; a hosted runner has no audio or RF hardware.

Still not seen on a Mac from any commit on this branch: Session-scoped search
and review from a tab, the 'Recording into …' notice clearing on stop, real RF
metadata reaching a bubble, and the newest-first viewport behaviour under live
traffic. A downloadable candidate build still does not exist — this run produced
no DMG."

---

# Operator feedback pass: five things the first live Mac test surfaced

Branch `claude/radio-decoder-translator-0oslya`, from `ae962cc`. Repair and
review only: no workflow dispatched, no tag, no release; Alpha 1/2/3 unmoved,
no Alpha 4. No schema change.

## Historical record, updated

The previous section ended: *"A downloadable candidate build now exists … but
it has not yet been installed or exercised on Eric's Mac."* **That is
superseded.** Eric installed the run-19 candidate (`BabelFishR-macOS-arm64.dmg`,
SHA-256 `b9c92ce2…9050e6`) and reports bench steps 1–8 good: install over the
existing app, launch, microphone, Field Offline transcription and translation.
His photographs show working transcript and translation bubbles.

Three tiers, kept apart on purpose:

- **Eric reported:** steps 1–8 good. Step 9 (Session-scoped search) was
  blocked because he could not find the View menu — the instructions never
  explained that macOS puts it at the top of the screen, outside the window.
  The Terminal checksum step showed no result; its cause is unknown and is not
  guessed at here. He could not rename tabs; the reason is unknown.
- **Automated coverage:** everything in this section, offscreen, on Linux and
  the hosted Mac runner. It drives the same controls but is not a Mac.
- **Unavailable hardware:** no radio, USB radio interface, SDR dongle or
  FalconClaw PTT has ever been connected. Unchanged.

Eric's screenshots do not identify his installed binary's exact digest, since
the checksum step produced nothing. That is noted, not resolved.

## What Eric asked for, and what was done

### 1. The Record Only warning stayed after leaving Record Only — fixed

Reproduced through `MainWindow._apply_mode()`, the method the mode menu calls:
enter Record Only, the banner explains it; leave for Field Offline, the banner
still says Record Only. Cause, as Codex found: `_report_engines()` wrote
`summary.warnings` to the banner when present and never touched it otherwise,
and nothing anywhere ever hid the banner after startup.

Repair: the banner now holds **one warning per source** — `audio-input`,
`audio-backend`, `engines`, `general` — rendered together. `_report_engines()`
sets or clears the `engines` slot on every call; a device dropping out sets
`audio-input` and a `connected`/`reconnected` status clears it;
`_refresh_devices()` sets or clears `audio-backend`. Clearing one cannot hide
another that is still true, which is the constraint Codex flagged: the banner
also carries audio-input trouble, and hiding the whole label to fix the stale
text would have hidden that. A genuine Record Only selection is still
explained. Offline enforcement is untouched: Field Offline on placeholder
engines still warns, with the right warning.

### 2. Tabs could not be edited — investigated; no failure reproduced

"Edit" is read as **rename**, per Codex's recommendation; no other editable
property was named. Exercised through the real controls, offscreen:

- a `QTest.mouseClick` on the **Rename…** button → dialog opens pre-filled with
  the current name → tab text changes → the change is in the database when a
  fresh `Store` opens the same file;
- a `QTest.mouseDClick` on the tab itself → same;
- a right-click context menu on the tab (new) → same.

All three work here. **That does not prove Eric's button works on his Mac**, and
this section does not claim it does. The reason he could not rename remains
unknown. What was changed, as clarity improvements that are also honest about
outcomes:

- a **right-click menu on the tab** offering *Rename Session…*, because a
  double-click on a trackpad is easy to miss and a small toolbar button is easy
  to overlook;
- the button reads **Rename…** and its tooltip, and the tab bar's, say how;
- **every outcome is visible**: cancel or a blank name → status bar says
  *Session name unchanged*; a save that raises or returns nothing → a warning
  dialog saying it was not renamed and the data is untouched; success → status
  bar says *Renamed "old" to "new"*. A rename that silently did nothing is
  indistinguishable, to the operator, from a button that does not work.

Invariants proven: same conversation id after rename; messages and monitoring
runs still filed under it; a capture pinned to that Session mid-run keeps its
pin and the *Recording into "…"* notice shows the new name; renaming the
default General Session does not create a second General (it is found by flag,
not by name).

**Deletion is not implemented.** It is an open decision for Eric — see below.

### 3. "Field ready" → "Ready" — done, wording only

`_render_readiness()` now shows **✓ Ready** for `report.field_ready`, and only
there. The branch logic is untouched; a test renders four report shapes,
asserts each lands where its own properties say, and asserts the report is
unchanged after rendering. A skipped smoke test is still *Checking*, never
Ready. Accessible description and tooltip now carry a sentence, not the chip
text.

**Design choice, mine:** the `can_record`-only branch used to read **Record
only**. That chip describes what is installed and working; *Record Only* is an
operating mode the operator selects, and the same two words in a readiness chip
read as though they had selected it. It now reads **◑ Partly ready**, which is
what the readiness dialog already calls that state, with an accessible
description that says it describes what is installed, not the operating mode.
The setup assistant's *Record only for now* button is a genuine mode choice and
is unchanged.

### 4. A date with every timestamp — done, from the stored value

Bubble headers now open with `2026-09-07 10:41:20`: the transmission's own
`started_at`, converted to the computer's local zone by `astimezone()`. No
network time source, no render-time clock. Tests pin a historical transmission
(2025-03-15 23:30 UTC) and show it renders as `2025-03-15 23:30:00` under
`TZ=UTC` and as `2025-03-16 12:30:00` under `TZ=Pacific/Auckland` — the date
crosses the local boundary with the zone. Updating the transcript leaves the
date alone; the stored timestamp and newest-first order are unchanged.

Format is a single field `YYYY-MM-DD HH:MM:SS`. Codex suggested
`2026-09-07 · 10:41:20`; the header already uses ` · ` between fields, so a
second dot inside one field would read as two fields. Not Eric's specified
format — he did not specify one.

### 5. "Listening" with nothing listening — fixed at both ends

Reproduced through the real window and event queue, two ways: (a) *Transcribe
anyway* on a saved recording with monitoring stopped ends with the badge on
**Listening** and the button on *Start monitoring*; (b) stop monitoring, and
the events the run left in the queue are drained on the next timer tick and
overwrite the *Idle* that `_stop_monitoring()` had just set.

Cause: `ProcessingPipeline._process()` published `LISTENING` unconditionally
when it finished, a claim about the microphone it has no way to check.

Repair, two layers:

- **Pipeline:** publishes `COMPLETE` when finished. That is all it can honestly
  say.
- **Window:** `_set_state()` reconciles through `_truthful_state()`. `COMPLETE`
  becomes the capture's own current state if a capture exists, else `IDLE`.
  `LISTENING`/`RECEIVING` with no capture become `IDLE`. Everything else passes
  through — *Transcribing* and *Translating* are shown whether or not a
  microphone is open, because they are true either way.

The event queue is **not** discarded: stale state events are reconciled, while
`updated`/`transmission` events still reach their bubbles. Tests cover saved
processing with no capture, completion after Stop, injected stale states from
an earlier run alongside a late transcript update (the update lands, the badge
stays Idle), and a live run (Listening and Receiving still display; COMPLETE
mid-run returns to what the capture is doing).

## Open decision for Eric — Session tab deletion

Not built. Two concrete behaviours are possible and they are not
interchangeable:

- **Keep:** the tab disappears from the row; its Sessions, messages and WAV
  recordings stay in the database and on disk, reachable again (for example by
  a "Show hidden Sessions" control that does not yet exist). Nothing is lost.
- **Erase:** the tab, every monitoring run under it, every message, and every
  recording file under it are permanently deleted. Not recoverable.

General, the default Session, would be exempt either way. Nothing in between
(archive, move-to-General, orphaned history) is proposed unless Eric asks for
it. Which one is Eric's call.

## Files changed

```
babelfishr/ui/main_window.py           warnings per source; Ready / Partly ready
                                       wording; _truthful_state(); tab context
                                       menu; rename outcomes visible
babelfishr/ui/timeline.py              _local_stamp(): local date and time
babelfishr/pipeline.py                 _process() publishes COMPLETE, not LISTENING
tests/test_alpha4_operator_feedback.py NEW  24 tests
tests/test_ui.py                       one test updated (below)
tests/test_alpha3_repairs.py           one assertion updated (below)
docs/MAC_BENCH_CHECKLIST.md            NEW  operator checklist, no Terminal
docs/AGENT_HANDOFF.md                  this section
```

**Existing tests changed, and why:**

- `tests/test_ui.py::test_state_is_not_conveyed_by_colour_alone` called
  `_set_state(RECEIVING)` on a window with no capture. That now honestly renders
  Idle. The test is about how a state is *drawn*, so it sets `_state` and calls
  `_refresh_state_badge()` directly; the reconciliation has its own tests.
- `tests/test_alpha3_repairs.py::test_the_badge_shows_checking_while_the_check_is_still_running`
  asserted `"Field ready"`; it now asserts `"Ready"` and that `"Field ready"` is
  gone. Eric's request.

Preserved and re-run green: capture-first recording, offline enforcement,
pinned Session ownership, scoped search and review, persistent history,
viewport anchoring, and the history-independent schema-3 fixture with its
migration checks.

## Test results

New file alone: **24 passed**. Focused (new file, ui, alpha3 repairs, all four
alpha4 files, pipeline, acceptance, offline, offline-integration, storage,
capture-invariant, models, gui-setup, input-panel): **355 passed**.

Full suite: **815 passed, 9 skipped** in 88s — the 791 from `ae962cc` plus the
24 new tests. Exact skips, unchanged and environmental: `test_coreaudio.py:255`
needs a real macOS host with CoreAudio (1); `test_packaging.py:373` PlistBuddy
is macOS-only (1); `test_real_engines.py:32` no prepared Whisper model (5);
`test_real_engines.py:107` no Argos language pack (2). Linux, Python 3.11,
`QT_QPA_PLATFORM=offscreen`.

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests`,
`packaging`; all five packaging scripts pass `bash -n`; the spec parses; the
workflow YAML loads.

## Non-vacuity

Each repair mutated back, the new file re-run, repaired code restored:

| Mutation | Failing tests |
|---|---|
| Never clear the `engines` warning | 2 |
| "Field ready" and "Record only" restored | 3 |
| Header time-only again | 3 |
| Window accepts every state as-is | 4 |
| Pipeline publishes LISTENING again | 1 |
| Failed rename shows nothing | 1 |

## Two things worth knowing about the tests

`QMenu.exec` cannot be monkeypatched on the PySide6 class — the first draft of
the context-menu test did that, the real menu opened offscreen, and the test
hung until the timeout. The menu is now built by `_build_session_tab_menu()`
and shown by `_session_tab_menu()`, so the test drives the built menu's action.
And `QInputDialog.getText` is patched as a `staticmethod`; its positional
arguments are `(parent, title, label, echo, text)`, so the pre-filled name is
argument 4, not 3 — the first draft asserted on the echo mode.

## Limitations carried forward

No SDR dongle, radio, USB radio interface or FalconClaw PTT has ever been
connected to this software. The hosted runner has no audio or RF hardware, and
Eric's bench test used the MacBook microphone and saved recordings.

**Superseding the earlier statement:** the run-19 candidate *has* been
installed and exercised on Eric's Mac — steps 1–8 reported good. Still not
exercised there from any commit on this branch: Session-scoped search and
review from a tab (blocked on finding the View menu, now explained in
`docs/MAC_BENCH_CHECKLIST.md`), the *Recording into …* notice clearing on
stop, real RF metadata reaching a bubble, the newest-first viewport behaviour
under live traffic, quit-and-reopen persistence of Session tabs, and every
repair in this section. No candidate has been built from this commit.

---

# Completing the activity-state repair, and a checklist that matches the app

Branch `claude/radio-decoder-translator-0oslya`, from `a1263bf`. Bounded
follow-up to the previous section: the Listening repair was incomplete in two
ways Codex found by driving the real app, worker, store and event queue, and
the operator checklist described controls that do not exist. No workflow
dispatched, no tag, no release; Alpha 1/2/3 unmoved, no Alpha 4. No schema
change.

## Reporting preference — Eric's requirement, carried forward

Final reports for BabelFishR handoffs go to Eric as **one fenced plain-text
block** containing everything: SHA, HEAD/origin/worktree, files, reproductions,
fixes, test results and environment, changed tests, unresolved decisions,
limitations, and workflow/tag/release status. No nested fences, no fragments,
no qualifications outside the block, no link in place of the report. Whoever
picks this up next should do the same.

## Repair A — activity recovers on every path, and only a current capture counts

### What was wrong, reproduced before editing

Through the real `BabelFishRApp`, the production engine factory, the standalone
worker, SQLite, the event queue and the actual `MainWindow` (offscreen):

| Path | Transmission after | Pending | Badge (before) | Badge (after) |
|---|---|---|---|---|
| ordinary transcript | COMPLETE | 0 | Idle | Idle |
| empty output (no speech) | COMPLETE, no error | 0 | **Transcribing** | Idle |
| ASR exception | FAILED, error kept | 0 | **Transcribing** | Idle |
| missing WAV | FAILED, engine never called | 0 | **Transcribing** | Idle |
| capture created, not started; LISTENING injected | — | — | **Listening** | Idle |
| capture running and LISTENING; stale RECEIVING injected | — | — | **Receiving** | Listening |
| same, stale TRANSCRIBING injected, nothing pending | — | — | **Transcribing** | Listening |

Cause 1: `ProcessingPipeline._process()` published COMPLETE only after
`_transcribe()` returned True. The empty result, the handled engine error and
the missing recording all return False before that line — with their own
transmission outcome correctly saved and published — so no "finished" signal
ever followed the TRANSCRIBING one.

Cause 2: `_truthful_state()` accepted LISTENING/RECEIVING whenever
`app.capture` was not None. The existence of a capture object says nothing
about what it is doing now.

### The repair — design choice, stated

Smallest change that handles every demonstrated case:

- **Pipeline:** the body of `_process()` is wrapped in `try/finally`, and the
  `finally` publishes `COMPLETE`. Every path out — success, empty, handled
  error, missing audio, even an unexpected exception re-raised to the worker —
  ends with the activity signal. The transmission's own state and error are
  saved and published *before* it, unchanged.
- **Window:** `_truthful_state()` now reconciles against **what the capture is
  doing now**, not whether it exists. Baseline = `capture.state` when a capture
  exists (the service sets that itself: IDLE before `start()` and after
  `stop()`, LISTENING/RECEIVING while running), else IDLE. Any
  LISTENING/RECEIVING/COMPLETE event resolves to the baseline. TRANSCRIBING and
  TRANSLATING are shown only while a processing pipeline actually holds queued
  or in-flight work (`_processing_active()`, which reads `pending` on the live
  and standalone pipelines); otherwise they too resolve to the baseline.

So a queued event is treated as a *prompt to look*, not as the truth. The
truth is read from the objects at drain time. The event queue is not discarded:
`updated` and `transmission` events still reach their bubbles, and still only
in the Session they belong to.

Why not deeper: no event versioning, no per-run tokens, no new event kinds.
The state the badge needs is already held by the capture service and the
pipelines; reading it is smaller than inventing a protocol to reconstruct it.

### Tests, and how they avoid proving nothing

- **Engines through the production factory.** A fixture registers
  `test-empty`, `test-failing`, `test-blocking` and `test-blocking-empty` with
  `providers._transcription_factories`, records every instance it constructs,
  and the tests select them through `config.asr.engine` exactly as a real
  engine is selected. Each test then asserts the standalone pipeline's engine
  *is* one of the recorded instances with the expected id, and that `calls`
  is 1 (or 0 for the missing-WAV case, where the engine must never be
  reached). This matters because `_processing_pipeline()` calls
  `select_engines()`, which **replaces** any preassigned `app.transcription`
  with the factory's choice — a test that handed the app an engine object
  would be testing an engine the pipeline never used.
- **Blocking engines.** `test-blocking` holds `transcribe()` on an Event, so
  the window is observed *committed* to Transcribing (pending == 1) before the
  release, and back at Idle after. `test-blocking-empty` does the same and
  then returns no speech: this is the test that catches the pipeline mutation
  at the window level. With fast engines the worker finishes before the GUI
  timer drains, and the in-flight check alone would make the badge right even
  with no COMPLETE — which is exactly what the first round of mutation testing
  showed (1 failing test, source-level only) and why this test exists.
- **Stale events are asserted immediately.** The earlier "events from an
  earlier run" test published LISTENING, RECEIVING, then COMPLETE, and asserted
  once — the COMPLETE could conceal a wrong intermediate display. It now
  drains and asserts after *each* stale event (LISTENING, RECEIVING,
  TRANSCRIBING, TRANSLATING), then delivers a late update and asserts it
  landed.
- **A genuinely running capture.** `CallbackAudioSource` + `begin_capture()`,
  waiting until `capture.state == LISTENING` and asserting `_running`. A stale
  RECEIVING and a stale TRANSCRIBING are injected and asserted at once: the
  badge stays Listening. The run's own transition to RECEIVING (via the
  service's `_set_state`) still shows.
- **A created-but-unstarted capture.** `start_session()` without
  `begin_capture()`: LISTENING and RECEIVING injected, badge stays Idle.
- **Correct Session.** A late update for a transmission owned by another
  Session is not drawn into the viewed one; switching to its Session and
  draining the same update lands it there, with the badge Idle throughout.
- **Source level.** With `test-empty` and `test-failing`, the last state event
  the pipeline publishes is COMPLETE, LISTENING never appears, and the last
  `updated` carries COMPLETE or FAILED respectively.

### Existing tests changed, and why

- `test_live_capture_states_still_display_while_monitoring` used a replay
  session that was never started and called `_set_state(LISTENING)` directly.
  Under the repair that capture is honestly Idle, so the test now runs a real
  `CallbackAudioSource` capture, drives transitions through the service's own
  `_set_state`, and checks COMPLETE mid-run returns to the run's state. It no
  longer tests Transcribing with a bare `_set_state`, since the blocking-engine
  tests cover that against real pending work.
- `test_stale_capture_states_from_an_earlier_run_are_ignored_but_updates_land`
  — asserts after every stale event; the masking COMPLETE is gone.

Nothing weakened, nothing skipped.

### Mutation results (repaired code restored after each)

| Mutation | Failing tests |
|---|---|
| COMPLETE only after a successful transcription (no `finally`) | 2 — the blocking-empty window test and the source-level test |
| Capture *existence* is enough for LISTENING/RECEIVING (a1263bf behaviour) | 2 — unstarted capture; stale RECEIVING over a listening run |
| TRANSCRIBING/TRANSLATING accepted unconditionally | 3 — earlier-run stale states; in-flight-only; stale over a listening run |

## Repair B — the checklist now describes the application that exists

`docs/MAC_BENCH_CHECKLIST.md` was walked against the source, item by item, and
rewritten where it did not match:

- **Wrong control:** it said to right-click a bubble for *Transcribe anyway*.
  There is no bubble context menu; every message action is in the menu from
  the **⋯** button at the bubble's right edge (`TransmissionBubble._build_menu`).
  Fixed, and the ⋯ button is introduced up front with the menu bar and badge row.
- **Wrong model of Review:** it said a list opens and to close it.
  `_show_review_queue()` and `_search()` both replace the *current thread in
  place* and the status bar says *View › Show all transmissions to go back*.
  The checklist now says so and returns through that menu item after both.
- **Non-unique search phrase:** the negative cross-Session check used "a word
  from the first Session", which may exist in the second. The operator now
  speaks *"purple giraffe seventeen"* into one Session first and searches for
  `giraffe`, so 0 matches elsewhere is meaningful.
- **Unobservable success:** it asked the operator to catch the Transcribing
  flash. Success is now defined by where the indicator *ends up* — Idle, not
  Listening, not stuck on Transcribing — after about ten seconds.
- **Install order:** stop monitoring and quit BabelFishR before replacing the
  application; previously unstated.
- **Which candidate:** the list opens by saying it is for the *next* build,
  from a commit containing these repairs, and not to run it against the
  installed one, where several steps would fail for the wrong reason.
- **Launch block:** it promised an *Open* button in the first dialog. Apple's
  documented path for an unidentified-developer block is: try to open, then
  System Settings ▸ Privacy & Security ▸ Open Anyway
  (https://support.apple.com/en-us/102445). The step now describes that
  conditionally — *if* macOS blocks it — and says not to change any setting
  that allows all apps from anywhere.
- **Preserved:** the explanation of where the macOS menu bar is (top edge of
  the screen, same line as the Apple logo and clock, outside the window); no
  Terminal anywhere; the note that the earlier checksum step is dropped and
  that the earlier download's integrity is therefore still unknown; the
  tab-deletion note now also says the General question is part of Eric's
  decision.

Reviewed against source and offscreen tests. **Not** a physical Mac
validation, and not presented as one.

## Open decisions and unknowns, unchanged

- **Tab deletion:** not implemented. Keep-or-erase is Eric's decision;
  whether General is deletable is part of it, not a settled exemption. No
  archiving, erasure, moving of recordings or orphaning has been introduced.
- Why Eric could not rename, and why the checksum attempt showed nothing,
  remain unknown.

## Files changed

```
babelfishr/pipeline.py                  _process(): COMPLETE in a finally
babelfishr/ui/main_window.py            _truthful_state() reads capture.state and
                                        pending work; _processing_active()
tests/test_alpha4_operator_feedback.py  +9 tests (24 -> 33); 2 existing updated
docs/MAC_BENCH_CHECKLIST.md             rewritten against the actual controls
docs/AGENT_HANDOFF.md                   this section
```

## Test results

New-file total: **33 passed** (24 before, 9 added). Focused (the same sixteen
files as the previous section): **364 passed**. Full suite: **824 passed,
9 skipped** in 96s — the 815 from `a1263bf` plus the 9 new tests. Skips
unchanged and environmental: `test_coreaudio.py:255` (1),
`test_packaging.py:373` (1), `test_real_engines.py:32` (5),
`test_real_engines.py:107` (2). Environment: Linux, Python 3.11,
`QT_QPA_PLATFORM=offscreen`, mock and test engines only. These are this
session's results, not an independent rerun.

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests`,
`packaging`; all five packaging scripts pass `bash -n`; the spec parses; the
workflow YAML loads. The history-independent schema-3 fixture and its
migration checks are untouched and green; capture-first recording and offline
enforcement tests are in the focused set and green.

## Limitations carried forward

No SDR dongle, radio, USB radio interface or FalconClaw PTT has ever been
connected to this software. The hosted runner has no audio or RF hardware, and
Eric's bench test used the MacBook microphone and saved recordings.

Eric installed the run-19 candidate (built from `ae962cc`) and reports steps
1–8 of that earlier checklist good. That report stands. The exact mapping from
those eight steps to individual features is not on file here, so coverage of
any specific behaviour by that test is **unconfirmed** rather than absent.
Separately: **nothing from `a1263bf` or this commit has been run on his Mac**,
and no candidate has been built from either. Still unexercised there in any
form that is confirmed: Session-scoped search and review from a tab (he could
not find the View menu; the checklist now explains where it is), the
*Recording into …* notice clearing on stop, real RF metadata reaching a bubble,
the newest-first viewport under live traffic, quit-and-reopen persistence of
Session tabs, and every repair in the last two sections.

---

# Menu access: the menus move inside the window; Eric's radio-audio results

Branch `claude/radio-decoder-translator-0oslya`, from `ec4c302`. Access
repair only. No workflow dispatched, no tag, no release; Alpha 1/2/3 unmoved,
no Alpha 4. No schema change.

## Eric's results on the run-20 candidate — recorded under his own labels

Eric installed the run-20 candidate (built from `ec4c302`) and reported, for
the A–G list he was given in conversation:

    A: GOOD, B: GOOD, C: GOOD, D: GOOD, E: NEGATIVE, F: GOOD, G: GOOD.

For E he said the menus he had been told to use were desktop menus, and that
BabelFishR offered none of those settings or options through the instructed
route. **His E is the menu-dependent Search / Review check** — by his own
explanation, not by inference. `docs/MAC_BENCH_CHECKLIST.md` letters its
sections A–H, so his letters and the document's do not line up (his E is the
document's F). His labels are preserved as given; the document now says so.
Which document sections his other letters correspond to is not on file, so
per-feature coverage from A–D, F, G is not asserted beyond his words.

He also confirmed that the application distinguished conversation breaks and
transcribed and translated during use, and clarified the path:

> "live translation & transcription still came from my laptop's microphone,
> but i held my work radio up to it"

**Tested path, precisely:** work radio speaker → acoustic sound → laptop
microphone → BabelFishR. This is the first time real radio audio has reached
the software; it did so acoustically. Direct electrical radio, USB radio
interface, FalconClaw PTT, SDR, RF metadata and transmitter identification
remain untested.

## The access failure, and what is and is not known about it

Independently inspected source at `ec4c302` (Codex): `_build_menu()` created
File, View, Tools and Help through `self.menuBar()`; View held Search
transmissions, Review queue and Show all transmissions; Tools held readiness,
setup, Copy Diagnostic Report and Reveal Logs in Finder; nothing requested an
in-window menu bar. The one existing window test for search and review called
`app.search()` / `app.review_queue()` and filled the timeline directly — it
never touched a menu, so menu accessibility was untested.

**Root cause on Eric's Mac: not established.** It has not been reproduced on
a physical Mac, and it is not attributed here to activation, Finder focus,
Cocoa, packaging or the operator. What is established is an access-testing gap
and an operator who could not reach the commands where he was told to look.

## The repair — Codex's proposed design, adopted; smallest change

`_build_menu()` now calls `bar.setNativeMenuBar(False)` on the window's menu
bar. Qt documents this property as the switch that keeps a menu bar in its
parent window rather than handing it to the system:
https://doc.qt.io/qt-6/qmenubar.html#nativeMenuBar-prop. Same actions, same
handlers, same business logic — nothing duplicated, no Settings subsystem.
The View and Tools actions are kept as attributes (`search_action`,
`review_action`, `show_all_action`, `readiness_action`, `assistant_action`) and
the menus carry object names, so a test can aim a click at them.

Known consequence on macOS: the system's own application menu (BabelFishR ▸
Quit) still appears in the top strip; File/View/Tools/Help are drawn inside
the window under its title bar. No technical objection was found to the
placement.

## Tests — clicks on the visible bar, never a direct call

`tests/test_alpha4_menu_access.py`, 8 tests. Every command is reached by two
real `QTest.mouseClick`s: one on the bar at the menu title's geometry, then
one on the popup at the item's geometry, asserting in between that the popup
actually opened and afterwards that it closed. No `.trigger()`, no `_search()`.

- **Bar inside the window (this platform):** shown window; bar visible,
  enabled, non-zero size, contained in the window rect; central content
  starts below it; titles exactly File / View / Tools / Help, each non-empty.
- **Request made (wiring):** an `ast` walk of `_build_menu` finds
  `setNativeMenuBar(False)`. Stated plainly in the test: Qt returns
  `isNativeMenuBar() == False` on Linux regardless, so the property is not
  evidence here; the request is. Whether macOS honours it is untested.
- **Session Options collapse/expand** leaves the bar visible, enabled, same
  geometry, all top-level actions enabled.
- **Search:** Alpha holds the unique phrase "purple giraffe seventeen"; Bravo
  holds ordinary traffic. View ▸ Search "giraffe" on Alpha narrows the thread
  to exactly that one transmission and the status bar says 1 match; View ▸
  Show all restores Alpha's full thread; switching the tab to Bravo and
  searching again gives 0 bubbles and "0 match"; Show all restores Bravo.
- **Review queue:** one Alpha transmission has confidence 0.2, every other is
  0.95. View ▸ Review queue shows exactly that one (count asserted 1, so a
  perpetually empty queue cannot pass); Show all restores; on Bravo the queue
  is honestly empty.
- **Cancelled search** changes nothing.
- **Tools:** Field readiness and Setup assistant open their dialogs (their
  `exec` patched on the Python subclasses); Copy Diagnostic Report writes the
  report file to the logs directory; Reveal Logs is present.
- **Help:** Where are my recordings? opens its Storage dialog.

### Mutations (repaired code restored after each)

| Mutation | Failing tests |
|---|---|
| Search item not wired to `_search` | 1 |
| Review queue item not wired | 1 |
| Show all item not wired | 2 (search and review tests both rely on it) |
| Field readiness item not wired | 1 |
| `setNativeMenuBar(False)` removed | **1 — the source check only.** The geometry tests still pass on Linux, where the bar is in-window regardless. This is the platform limit stated above, measured. |

No existing test was changed. `test_alpha3_repairs.py` and
`test_input_panel.py` already read `menuBar().actions()` and still pass.

## Checklist

`docs/MAC_BENCH_CHECKLIST.md`: the menu explanation now describes the menu
row inside the window (File View Tools Help under the title bar), notes that
the Mac's top strip still shows BabelFishR ▸ Quit, records Eric's A–G results
and that his E is the document's F, and tells him only the menu check needs
retesting on the next candidate. Every "menu bar at the top of the screen"
instruction for View and Tools was replaced.

## Replacement check for Eric (click-only, next candidate)

1. Open BabelFishR. Look directly under the window's title bar for the four
   words **File  View  Tools  Help**. Good: they are there, inside the window.
2. Click a Session tab that has messages. Click **View**, then **Search
   transmissions…**. Type a word from one of that tab's messages, press
   Return. Good: the thread shrinks to the matching message(s) and the bottom
   line says how many matched.
3. Click **View**, then **Show all transmissions**. Good: the full thread is
   back.
4. Click **View**, then **Review queue**. Good: the thread shows only
   low-confidence messages, possibly none; the bottom line says how many.
   Then **View ▸ Show all transmissions**.
5. Click **Tools**. Good: Field readiness…, Setup assistant…, Copy Diagnostic
   Report and Reveal Logs in Finder are listed.
Nothing else from A–G needs repeating.

## Files changed

```
babelfishr/ui/main_window.py       setNativeMenuBar(False); actions as attributes;
                                   menu object names
tests/test_alpha4_menu_access.py   NEW  8 tests
docs/MAC_BENCH_CHECKLIST.md        in-window menu row; Eric's labels recorded
docs/AGENT_HANDOFF.md              this section
```

## Test results

New file: **8 passed**. Focused GUI set (menu access, operator feedback, ui,
alpha3 repairs, alpha4 thread/Sessions, alpha4 integration repairs, input
panel, gui setup, acceptance): **225 passed**. Full suite: **832 passed,
9 skipped** in 106s — the 824 from `ec4c302` plus the 8 new tests. Skips
unchanged and environmental: `test_coreaudio.py:255` (1),
`test_packaging.py:373` (1), `test_real_engines.py:32` (5),
`test_real_engines.py:107` (2). Linux, Python 3.11,
`QT_QPA_PLATFORM=offscreen`. These are this session's results, not an
independent rerun, and not Cocoa or a physical Mac.

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests`,
`packaging`; all five packaging scripts pass `bash -n`; the spec parses; the
workflow YAML loads.

## Unresolved ledger, updated

- Whether the in-window menu bar appears on Eric's Mac: **untested**. The
  next candidate build is what would show it.
- Root cause of the missing native menu access on his Mac: unknown, not
  reproduced, not attributed.
- Search / Review behaviour on his Mac: unconfirmed (his E was blocked before
  the commands could be reached).
- Session tab deletion: Eric's decision (keep or erase; General's treatment).
  Not implemented.
- The earlier Rename interaction problem and the Terminal checksum attempt:
  unexplained.
- Newest-first scroll anchoring under live arrivals and the "Recording into …"
  notice clearing on stop: no specifically identified operator result.

## Limitations carried forward

No SDR dongle, radio, USB radio interface or FalconClaw PTT has been
electrically connected to this software. The hosted runner has no audio or RF
hardware.

Eric installed the run-19 candidate (from `ae962cc`) and reported steps 1–8
of that earlier checklist good; the exact step-to-feature mapping is not on
file, so specific coverage from that test is unconfirmed rather than absent.

**Current status, superseding "never exercised":** Eric installed the run-20
candidate (from `ec4c302`) and reports A–D, F and G good and E — the
menu-dependent Search / Review check — negative, blocked by menu access. Real
radio audio has now reached the software acoustically: work radio speaker →
laptop microphone → BabelFishR, with conversation breaks distinguished and
transcription and translation working. Direct electrical radio/PTT/USB
connections, SDR operation, RF metadata and transmitter identification remain
unverified. No candidate has been built from this commit; the in-window menu
bar has not been seen on a Mac.

---

# Playback controls, Session colours, message and Session removal

Branch `claude/radio-decoder-translator-0oslya`, from `8cb2df5`. Four
reviewable commits, one per portion. No workflow dispatched, no tag, no
release; Alpha 1/2/3 unmoved, no Alpha 4. Schema **5** (two columns on
`conversations`, one on `transmissions`, one new tombstone table; all added
with the guarded `ALTER TABLE` path; the schema-3 fixture and every migration
test still run from a source tree alone).

## Eric's run-21 results, recorded as given

Run-21 checks: **1 GOOD, 2 GOOD, 3 N/A, 4 N/A, 5 GOOD, 6 GOOD.** So the menu
row inside the window, Search from that menu, the cross-Session search check
and the listed Tools commands now have his physical-Mac observation. Checks 3
(Show all restores the thread) and 4 (Review queue) are **untested** - he
explained them by the absence of deletion controls, which neither check needs;
they are not recorded as passed or failed. The radio-audio path stands as
before: work radio speaker → acoustic sound → laptop microphone → BabelFishR,
with conversation breaks, transcription and translation working.

## What is complete, what is decided, what is Eric's to decide

| Portion | Commit | Status |
|---|---|---|
| B. Session tab colours | `d69a0ba` | **Complete.** No open decision. |
| A. Playback on the bubble | `c749b5c` | **Complete and working**, with three behaviours implemented on Codex's recommendation pending Eric's word (below). |
| C. Message removal | `b987b55` | **Complete and working.** The retained-content path (hidden flag + View › Show removed messages) is Codex's recommended shape, implemented so the requirement is met; Eric can change it. |
| D. Session removal | `9ed8640` | **Complete for every Session except General.** Keep-or-erase is offered to the operator by name at removal time rather than decided in code. General is refused with the reason: a pending decision, not an approved exemption. |

Nothing here is a placeholder. Every control does what its label says.

### A. Playback — `babelfishr/ui/playback.py`, `babelfishr/ui/timeline.py`

Each bubble with a recording has a compact **▶ Play**. Recordings longer than
five seconds expand a bar across the bubble's bottom: **⏪ 5 s · ⏸ Pause /
▶ Play · ⏹ Stop · ⏩ 5 s · 0:03 / 0:09**. Stop collapses it. Shorter ones play
through (button reads *Playing…*, disabled) and re-arm Play when done.

The old `_Player` had no notion of which recording it was playing; each bubble
asked "is the player playing?", so bubble B offered Pause because A's recording
was playing. A `PlaybackController` now owns the recording's id and state and
every bubble renders from it. One recording at a time; starting another retires
the previous bubble's controls. Pause keeps the position; Play resumes the same
file without reloading. Completion, Stop, a missing file and a backend error
all leave Play usable with the reason in the status line. Leaving a thread
stops its audio. Expansion and collapse run inside the view's existing
anchoring (reader's bubble within 1 px, tested). A transcript update touches
neither the player nor the bar.

Backend seam: `QtMultimediaBackend` maps QMediaPlayer's own
`playbackStateChanged`, `positionChanged`, `durationChanged`,
`mediaStatusChanged` (EndOfMedia → finished; InvalidMedia → error) and
`errorOccurred`. Completion comes from the player, never a timer.
`SystemOpenBackend` (QtMultimedia absent) hands the file to the OS player and
reports `controllable = False`; the bubble then shows only Play, never a
Pause/Stop/seek it could not honour. **This development environment has
PySide6 Essentials only - no QtMultimedia** - so the two QtMultimedia tests
skip here with that reason and run in the packaged app's environment. No test
here or on the runner puts sound through a speaker.

Implemented on recommendation, **awaiting Eric** (one constant each in
`playback.py`):
- exactly 5.000 s counts as *short* (`LONG_RECORDING_SECONDS`);
- rewind/fast-forward are fixed 5-second skips, labelled as such (`SKIP_MS`),
  not held seeking;
- natural completion of a long recording collapses its bar.

### B. Colours — `storage.py`, `models.py`, `main_window.py`

`conversations.color` (`#rrggbb` or ""). Tab menu: **Tab colour…** (Qt colour
dialog, opens on the current colour) and **Default tab colour** (enabled only
when set). Drawn as a swatch beside the name - design choice: the name stays
in the theme's text colour and stays readable; selection is Qt's own marking,
unchanged. Bound to the Session id: survives rename, reorder, switching,
relaunch; never touches another tab. Only a real hex value is stored.

### C. Message removal — `storage.py`, `pipeline.py`, `app.py`, `timeline.py`, `main_window.py`

Bubble **⋯ → Remove message…** asks one question with two named answers and
Cancel:

- **Remove from thread (keep the recording and data)** — sets
  `transmissions.hidden`. Out of the thread, search, review queue and ordinary
  exports; row, recording and every field kept. **View › Show removed
  messages** shows them marked *Removed from thread — data kept* with **Restore
  to thread** in the menu. Nothing is stranded.
- **Delete permanently…** — asks again with the exact scope, then deletes.

**Exact deletion semantics.** Files are inventoried before anything is removed
and sorted: *owned* (inside the Recordings folder by realpath, a regular file,
not a symlink, referenced by no other kept message) are deleted; *external*
(a replayed WAV from the operator's folder, an export, a backup, anything
outside) are named as not deleted and never touched; *shared* (still used by
another message) are kept. Paths come only from the message's own fields and
every analysis attempt's artifacts and derived input - nothing is globbed,
walked or recursively removed; a symlink is never followed. Order: tombstone
+ row + index in one transaction, then files one by one. `save_transmission()`
refuses a tombstoned id, so a worker still holding the message cannot recreate
it. A file that will not unlink is recorded on the tombstone by path and
reason, shown to the operator, and the deletion is **not** called complete;
**Tools › Finish unfinished deletions…** retries. Refused with the reason: a
message still in flight (`ProcessingPipeline.is_in_flight`). Playback of the
file is stopped first. The capture's pinned destination is untouched. Not
promised: secure erasure, or removal of copies exported or shared elsewhere.

### D. Session removal — same files

Tab menu **Remove Session…** states the run and message counts and offers
**Hide this Session (keep everything)** or **Delete permanently…**. Hiding sets
`conversations.hidden`; **View › Show hidden Sessions** shows the tab as
*Name (hidden)* with **Restore Session**; viewing a hidden Session falls back to
General. Deleting lists runs, messages, owned files, and what will not be
deleted (shared, external), then runs each message through the single-message
path (tombstone and checks each), then deletes the runs, then the Session.
Other Sessions untouched. Refused with the reason: General (item disabled;
tooltip explains the pending decision), a Session monitoring is recording
into, a Session with an in-flight message.

## Open questions for Eric — recommendations beside each

1. **Exactly five seconds:** simple play-through or expandable controls?
   *Implemented: simple (≤ 5.000 s is short). Recommendation: keep.*
2. **Forward/rewind:** fixed skips or held seeking; how much? *Implemented:
   fixed 5-second skips, labelled. Recommendation: keep unless you want a
   scrub bar.*
3. **Natural completion of a long recording:** collapse the bar? *Implemented:
   yes. Recommendation: keep.*
4. **Retained content after removing a message or Session from view:**
   *Implemented: kept in place, shown via View › Show removed messages / Show
   hidden Sessions, restorable in one click. Recommendation: keep; the
   alternative is a separate "Removed items" screen.*
5. **General:** removable, clearable, or kept as the default tab?
   *Implemented: kept and refused with that reason. Recommendation: allow
   "Clear General (delete its messages)" but never remove the tab, since every
   orphaned run is filed under it. Not built until you say.*

## Tests

Four new files, 50 tests; all in disposable temporary homes. Modal dialogs
are substituted through one seam (`MainWindow._choose`, `QColorDialog.getColor`);
what each answer does is what is asserted. Real clicks drive the compact Play,
the bar's buttons, the tab menu items, and the ⋯ menu (the instant popup runs a
nested loop, so the item click is scheduled to fire inside it).

| File | Tests | Mutations (each reverted) → failing tests |
|---|---|---|
| `test_alpha5_session_colors.py` | 9 | swatch never drawn → 4; colour not persisted → 7 |
| `test_alpha5_playback.py` | 16 + 2 skip here | owner-agnostic state → 2; bar for every recording → 2; resume reloads → 1; completion never retires → 1 (the scripted backend also emits *stopped*, which the controller retires on; both paths removed would be needed to break completion - stated); no anchoring → 1 |
| `test_alpha5_message_removal.py` | 15 | no tombstone check → 1; everything owned → 2; hidden ignored by search/review → 1; no in-flight check → 1 |
| `test_alpha5_session_removal.py` | 10 | General unprotected → 1; hidden still listed → 2; no capture-pin refusal → 1; runs left behind → 1 |

Known non-empty cases precede every removal or filtering assertion (search
index holds the phrase before deletion; three owned files present before
deletion; two runs and messages per Session before hiding).

**Existing tests changed:** `test_alpha3_repairs.py::test_the_default_bubble_has_no_waveform_and_no_play_button`
→ renamed `…_has_no_waveform`; the "no Play button" half is gone at Eric's
request, the waveform half stays, and it now asserts a bubble *without* a
recording offers no Play. `test_alpha4_thread_and_sessions.py` asserts schema
version 5. Nothing weakened or skipped to pass.

## Test results

Full suite: **882 passed, 11 skipped** in 107s — the 832 from `8cb2df5` plus
50 new. Skips: the nine environmental ones as before (`test_coreaudio.py:255`,
`test_packaging.py:373`, `test_real_engines.py:32` ×5, `test_real_engines.py:107`
×2) plus **two new**: `test_alpha5_playback.py:516` and `:531`, *"QtMultimedia
is not installed here; the packaged app has it"*. Those two exercise
`QtMultimediaBackend` directly (a missing-file error; reading a 9-second WAV's
duration from the player's own signal) and will run on the Mac runner, where
PySide6 Addons is installed; the duration test skips there too if the host
has no decoder, saying so. Linux, Python 3.11, `QT_QPA_PLATFORM=offscreen`,
mock engines. This session's results, not an independent rerun.

`git diff --check` clean; `compileall` clean over `babelfishr`, `tests`,
`packaging`; all five packaging scripts pass `bash -n`; the spec parses; the
workflow YAML loads.

## Checklist

`docs/MAC_BENCH_CHECKLIST.md` gained sections **I** (playback), **J** (tab
colours), **K** (removing a message) and **L** (removing a Session), all
GUI-only and marked as not present in run 21. K and L instruct Eric to create
throwaway messages and a throwaway Session first and to act only on those.

## Unresolved ledger

- The five open questions above; recommendations given; nothing built on
  General until Eric decides.
- Run-21 checks 3 and 4 (Show all; Review queue): untested, not failed.
- Playback: no test here or on the hosted runner puts sound through a speaker;
  Eric's Mac is the first place that happens.
- None of playback, colours or removal has been run on Eric's Mac; no candidate
  has been built from these commits.
- Original native-menu failure and the earlier Terminal checksum attempt:
  unexplained.
- Live scroll anchoring under arrivals and the "Recording into …" notice
  clearing on stop: no specifically identified operator result.
- Direct electrical radio/PTT/USB connections, SDR, RF metadata, transmitter
  identification: unverified.

---

# First repair pass after the independent corroboration — worker lifetime, shutdown, shared recordings

Written for whoever picks this up next. Two repairs, separately reviewable:
**A** (lifecycle: `pipeline.py`, `app.py`, `ui/main_window.py`) and **B**
(shared recordings: `storage.py`, the retry report in `ui/main_window.py`, one
existing test's assertion). Nothing else in scope was touched: no playback,
removal-policy, General, paging or dependency-pinning decisions were made.

## Identifiers

| | |
|---|---|
| Base | `21be6b0` — verified equal to `origin/claude/radio-decoder-translator-0oslya` at the start of the pass, worktree clean |
| Repair A commit | `3dbef42` (3dbef42c9c74a47980561dd71e1cc9a8b22094aa) |
| Repair B commit | `4e6d84d` (4e6d84d10819fd9c42dc0d189cb32b1472057be3) |
| Docs commit | the commit carrying this section (the branch tip) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release | none dispatched, retried, created, moved or published |

## Repair A — ownership and the shutdown sequence

**Accounting.** A transmission id is *in flight* from the moment
`ProcessingPipeline.submit()` accepts it until the worker that ran it has
finished with it: queued, dequeued-but-not-yet-running, and running are all
the same set (`_in_flight`), and `pending` is its size. The earlier
`queue.qsize() + _active` read zero between `queue.get()` and the `_active`
increment — the hand-off the corroboration reproduced. Admission and
accounting share one lock; `submit()` refuses (`ProcessingStopped`) once a
stop has begun and returns `False` for an id already in flight instead of
queueing it twice.

**Stopping.** `stop()` closes admission first, then signals and joins, and
*keeps* any thread that outlives the join (`finished`, `worker_threads()`,
`shutting_down`); it returns whether every worker actually ended. It no longer
clears the thread list after a timed join. `stop_if_idle()` is the atomic
form: idle check and admission close under the same lock, so nothing can land
between "nothing pending" and "stopped". A worker told to stop refuses to
call an engine for a stage it has not started (`_engine_gate`): the stage is
recorded as failed, for that reason, and the message stays retryable — the
transcript already saved is kept.

**Ownership, in order.** `BabelFishRApp` owns the capture, the live pipeline,
the standalone pipeline, the engines and the store.

1. `stop_session()` stops the capture (bounded join; a thread that does not
   return is kept as `_lingering_capture`, and finishes the run itself), then
   hands the live pipeline over: idle → stopped at once; busy, or a lingering
   capture may still hand it a final transmission → it carries on as the
   *standalone* pipeline, still accepting Retry / Transcribe anyway, counted
   by every check, and stopped only once idle. Nothing is waited for on the
   caller's thread. The 30-second wait and 10-second join are gone.
2. `set_mode()` → `mode_change_problem()` (pending work, then
   `shutdown_problem()`), then `_retire_processing()` uses `stop_if_idle()`;
   a straggler or late work raises `ModeChangeRefused` **before**
   `config.mode` moves and before any engine is closed.
3. `start_session()` refuses (`ProcessingBusy`) while a stopped processor's
   worker or a lingering capture is still alive, then discards an idle
   standalone processor or refuses on a busy one (message unchanged).
4. `_abandon_failed_start()` stops the just-started workers and tracks a
   survivor in `_retired` rather than forgetting it.
5. `close(wait, timeout)` — capture, then the standalone processor's
   outstanding work, then its workers, then engines, then the store. Each
   step waits for the previous to be *complete*, never merely timed out. A
   live worker keeps engines and store open and `close` returns `False`.
   `wait=True` (CLI, tests) blocks until done or `timeout`; `wait=False`
   (the window) never blocks.

**The window.** `_stop_monitoring()` returns as soon as the capture has
stopped; if work is outstanding the badge stays **Transcribing** and the
status line says how many are finishing (`_truthful_state` now keeps a
processing state when the capture's final Idle arrives while a processor
still holds work). `closeEvent()` calls `app.close(wait=False)`; if that is
not yet possible the event is ignored, the status line says it is quitting
once N transmissions finish, and a 250 ms timer retries until the application
closes in order — then the event drain stops (`_shutdown_complete`) so no
timer callback reaches the closed store. A repeated Quit lands in the same
path and changes nothing.

**The closed-database traceback, identified.** The corroboration saw
`sqlite3.ProgrammingError: Cannot operate on a closed database` from
`_drain_events → _belongs_here → store.is_deleted`. That frame is the
window's 100 ms `QTimer` slot, i.e. the GUI (main) thread, not a worker:
the old `close()` had closed the store while the drain timer kept firing.
`test_the_event_drain_never_touches_a_closed_store` reproduces it on the old
tree (the drain raised) and asserts `threading.current_thread() is
threading.main_thread()` at the drain.

**Capture.** `CaptureService.stop()` returns `True` only when the audio
thread has ended; the final detector flush, safety-recording close and Idle
state run exactly once (`_finish_once`), on whichever side gets there — the
caller after a completed join, or the thread on its way out. The last
detected transmission and its WAV are written either way.

## Repair B — shared recordings

`Store.retained_references()` reads every row's `audio_path`,
`processed_audio_path` and decoded `analysis_attempts` (each attempt's
`input_path`, derived or not, and every artifact `path`) and keys them by
`os.path.realpath` — the ownership check's own normalisation, so `/./`, `..`
and a symlink to the same file match. The earlier `LIKE '%path%'` over stored
JSON missed any path with a quotation mark (`\"`) or a non-ASCII character
(`\uXXXX`), exactly as Codex found; the claim that it could only
over-estimate sharing was wrong and is withdrawn. A row whose record cannot
be decoded is remembered as *unreadable*: while one is retained, every
candidate counts as possibly shared, with the reason recorded
(`DeletionInventory.reasons`).

`delete_transmission_permanently()` runs entirely under the store lock:
inventory, tombstone, row delete and unlink are one step against every other
writer, and a cached reference map is reused only while the store's write
stamp (`_writes`, bumped on every save) has not moved. Session deletion
builds the map once and forgets each deleted id.

`retry_leftover_deletions()` re-checks ownership *and* sharing as they stand
now and returns `LeftoverRetry(still, preserved, removed, already_gone)`: a
leftover a retained message has since come to use is preserved, taken off
the leftover list and reported as kept — never unlinked, never reported as
removed. **Tools › Finish unfinished deletions** says "Kept N file(s) that
another message now uses". Containment, symlink refusal, tombstones,
external-file exclusion and General's policy are unchanged.

## Tests

`tests/test_alpha5_lifecycle.py` (12) and `tests/test_alpha5_shared_recordings.py`
(11). Substitutions, precisely: a holding transcription engine and a fake
cloud translator are registered with the **production factories** under their
own ids (the fake cloud under `"claude"`, so `guard_cloud` applies to it as
to the real one) and selected through `config`; `pathlib.Path.unlink` is
replaced only to make one named file refuse to go; a `CallbackAudioSource`
subclass whose `read` ignores its timeout stands for a stuck input. The
dequeue hold wraps the pipeline queue's `get` so the id is dequeued and not
yet running when the mode change is attempted.

Against the pre-repair tree (production files restored from `21be6b0`, tests
unchanged): **22 of 23 fail**; the one that passes,
`test_an_unshared_owned_file_still_deletes`, is a guard that the ordinary case
still works, not a defect test. Against the repaired tree: 23 pass.

### Existing tests changed

- `tests/test_alpha5_session_removal.py::test_an_unremovable_file_is_reported_and_left_for_retry`:
  `assert app.retry_leftover_deletions() == {}` → `.still == {}`. Same intent
  (everything removed on retry); the method now returns a report rather than
  a dict, because a preserved file is neither removed nor still failing.

### Mutations (repaired code restored after each; pristine copies compared byte-for-byte)

| # | Mutation | Caught by |
|---|---|---|
| M1 | `pending` counts the queue only (the old hand-off gap) | `test_a_job_between_the_queue_and_the_worker_still_counts` |
| M2 | `stop()` clears the thread list after a timed join | `test_a_worker_that_outlives_the_stop_timeout_stays_tracked` |
| M3 | `submit()` accepts work after a stop has begun | `test_a_stopped_processor_refuses_new_work_and_is_replaced` |
| M4 | the window closes the store whatever `app.close` says | `test_quit_waits_on_the_event_loop_and_closes_in_order` |
| M5 | `stop_session` waits for processing on the GUI thread again | `test_stop_monitoring_returns_at_once_and_the_window_keeps_breathing` |
| M6 | retry trusts the original inventory (no sharing re-check) | `test_retry_preserves_a_file_a_retained_message_has_since_come_to_use` |
| M7 | references compared by spelling, not by file | `test_equivalent_spellings_of_the_same_file_count_as_the_same_file` |
| M8 | an unreadable analysis record counts as no reference | `test_an_unreadable_analysis_record_protects_rather_than_permits` |
| M9 | artifact paths not read from the analysis record | `test_analysis_artifact_paths_with_accents_and_quotes_protect_the_file` |
| M10 | sharing decision and unlink no longer one locked step | `test_a_save_that_races_the_deletion_waits_for_it` |

All ten caught (each named test failed); after each, the file was compared
byte-for-byte with the saved repaired copy and found identical.

## Test results

Full suite after both repairs: **905 passed, 11 skipped** in 158.67 s
(882 before this pass plus the 23 new tests). Skips are the same eleven as
before: `test_alpha5_playback.py:516` and `:531` (QtMultimedia is not
installed here; the packaged app has it), `test_coreaudio.py:255` (needs a
real macOS host with CoreAudio), `test_packaging.py:373` (PlistBuddy is only
available on macOS), `test_real_engines.py:32` ×5 (no prepared Whisper model
at `~/.local/share/BabelFishR/models/small`) and `test_real_engines.py:107` ×2
(no Argos language pack installed). Focused runs first: the seven existing
suites nearest the change (172 tests) after Repair A; the removal, storage
and lifecycle suites (53) after Repair B; the two new files (23). Linux,
Python 3.11, `QT_QPA_PLATFORM=offscreen`, `-p no:cacheprovider`, mock and
stand-in engines. `git diff --check` and `compileall` over `babelfishr`,
`tests` and `packaging` clean. This session's results, not an independent
rerun.

## Unresolved ledger, updated

- **F1 / F5** — repaired here; on Eric's Mac unverified (checklist section M).
- **F2** — repaired here, including the JSON-escaping and path-spelling holes
  Codex found; unverified on the Mac.
- **F3** live traffic in Search/Review, **F4** other-bubble playback error —
  deferred to the next correctness pass, unchanged.
- **F6/F7** scaling, **F8/F9** simplification and pinning — recommendations;
  not worked.
- Playback boundary (5.000 s, skips, collapse), removal semantics, General:
  unapproved, unchanged; this pass is not approval.
- No candidate containing this or the playback/colour/removal work has been
  built or confirmed on Eric's Mac.
- A worker that never returns is now *visible* (Quit waits and says why,
  Start and mode change refuse); there is still no forced-cancellation or
  forced-exit policy — deliberately, per the directive.

---

# Finishing the shutdown repair — Codex's four counterexamples

Codex re-read `c004ff5` and exercised the production core in isolation
(Linux, Python 3.12.13, no Qt, no pytest): (A) Stop still blocked its caller
for 5.5 s on a held source, because `CaptureService.stop()` joined the audio
thread on the GUI thread; (B) Quit retired the processor before a lingering
capture had handed over its final transmission, which was then left
Captured while `outstanding_work()` said zero and the next close succeeded;
(C) a digital analysis running on the window's worker saved into a store
`close()` had already closed; (D) `_finish_shutdown()` treated an exception
from `app.close()` as success. All four confirmed here and repaired. The
earlier claim that `close(wait=False)` "never blocks" was wrong for (A) and
is withdrawn: polling a function from a timer does not make blocking calls
inside it asynchronous.

## Identifiers

| | |
|---|---|
| Base | `c004ff5` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section (the branch tip) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release | none dispatched, retried, created, moved or published |

## The rule, and the order

Shutdown accounts for every producer and every user of the store, and closes
in this order, each step *complete* before the next, never merely timed out:

1. **Capture settles.** `stop_session()` calls `CaptureService.stop_async()`:
   the join, the source close and the final flush run on the capture's own
   stopper thread (`babelfishr-capture-stop`); when the audio thread has
   already returned there is nothing to wait for and the finish runs at once.
   Until `settled` (audio thread ended, stopper ended, finish run) the capture
   is `_lingering_capture`: a producer that can still hand over its final
   transmission, counted as one in `outstanding_work()`, and the live
   pipeline is kept accepting as the standalone processor for exactly that.
   `CaptureService.stop()` stays as the blocking form for the command line.
2. **Nothing accepted is stranded.** A transmission the capture saved that
   no processor accepted is recorded (`CaptureService.unprocessed`), counted
   in `outstanding_work()`, and handed to a processor by `close()`
   (`_adopt_unprocessed`, through `_processing_pipeline(for_shutdown=True)`)
   before that processor is retired. If no processor can be built it is
   logged by id, never reported as processed.
3. **The processor finishes what it accepted**, then its workers end;
   stragglers end.
4. **Every other store user finishes.** `app.activity(name)` registers one
   (the digital analysis is wrapped for its whole run, result save included);
   `close()` waits for `active_operations()` to empty, and the final check and
   the refusal of new registrations happen under the same lock.
5. **Engines close, each once and dropped as they close; then the store.**
   A failure part-way is retried from where it stopped, never re-closing.

**Quit pending.** The first `close()` sets `closing`. From then on
`start_session` raises `ProcessingBusy("BabelFishR is quitting…")`,
`_processing_pipeline()` returns None to everyone but the shutdown path (so
Retry / Transcribe anyway return False and `processing_problem` says why),
`analyze_digital` returns None and the window's analysis action says it was
not started. Accepted work and the final capture hand-off finish. Stop
without Quit sets nothing: saved-recording processing remains available.

**Failure is failure.** `_finish_shutdown()` catches an exception from
`app.close()`, records it, keeps both timers running, does not set
`_shutdown_complete`, and the status line says "Could not finish quitting:
… Retrying - nothing still in use has been closed." `close()` refuses to
overlap itself (`_close_in_progress`).

## Reproductions, before → after (real Qt window, offscreen)

- **A** held source, production `stop_timeout` 5.0 unchanged, asserted:
  Stop returned in < 1 s (was 5.5 s), a 20 ms heartbeat kept ticking through
  Stop and through the pending Quit; `capture_finishing()` True;
  `outstanding_work() ≥ 1`; the processor still accepting; repeated Quit
  gives the same answer.
- **B** open voice transmission + held capture + Quit: before release the
  store is open and the ASR uncalled; after release the WAV exists, the ASR
  is called once, the transmission is saved COMPLETE *before*
  `store.close`, and the window closes by itself. Stop-only control case
  kept: the final transmission is processed, the badge returns to Idle, and
  `retry()` still works afterwards. Two transmissions (one held inside the
  engine, one open in the detector): both COMPLETE, none Captured, the
  holding engine closed exactly once.
- **C** analysis held inside `analyse()` through the real window path
  (`_on_analyze_digital` → thread pool → `app.analyze_digital`): Quit is
  refused while it runs, the status names the analysis, the store stays
  open, and on release the attempt is saved before `store.close`.
- **D** `store.close` made to raise through the real Qt close: `window.close()`
  False, `_shutdown_complete` False, both timers alive, status names the
  error, engines closed once; retries keep failing without re-closing the
  engine; when the fault is removed the window closes on the next tick.
- New work while Quit is pending is refused through app methods (start,
  transcribe anyway, retry, processing_problem, analyze_digital) and the
  window's analysis action.

## Tests

`tests/test_alpha5_shutdown.py` (7, all real Qt offscreen) and one lifecycle
test rewritten to the production timeout
(`test_a_capture_thread_that_ignores_its_stop_is_kept_in_view`); one
lifecycle test now waits for the capture to settle before looking for the
final transmission (`test_capture_shutdown_keeps_the_final_transmission…`),
since Stop no longer blocks — same intent. Substitutions: a
`CallbackAudioSource` subclass whose read blocks ignoring its timeout once
held; a fake analyser at the `DsdNeoAnalyser.from_config` seam; a holding
transcription engine installed on the app; `store.close` replaced only to
fail once. Nothing lowers `stop_timeout`.

### Fail-before (production files restored from `c004ff5`, tests unchanged)

All seven shutdown tests FAIL on `c004ff5` (one also errors at teardown, the
window it left open closing over a store the fixture had closed); the two
rewritten lifecycle tests fail (the lingering-capture test on the blocking
Stop, the final-transmission test on the missing `capture_finishing`); the
ten unchanged lifecycle tests pass there as they did before. On the repaired
tree: 19 of 19 pass in 12.8 s.

### Mutations (repaired code restored after each; byte-compared)

| # | Mutation | Caught by |
|---|---|---|
| N1 | `stop_session` blocks on `capture.stop()` again | `test_stop_and_quit_keep_the_gui_alive_with_a_held_source` |
| N2 | `close()` retires the processor before the capture settled | same |
| N3 | `outstanding_work()` ignores a capture that can still produce | same |
| N4 | `close()` ignores other store users | `test_a_running_digital_analysis_saves_before_the_store_closes` |
| N5 | a close exception is treated as done | `test_a_failed_close_is_not_reported_complete_and_retries_safely` |
| N6 | `start_session` does not refuse while quitting | `test_new_work_is_refused_while_quit_is_pending` |
| N7 | saved-recording processing ignores a pending Quit | same |
| N8 | engines closed again on a retried close | `test_a_failed_close_is_not_reported_complete_and_retries_safely` |

All eight caught.

### Test results

Focused first: the eight nearest existing suites after the patches (183
passed); the two new-pass files (19 passed). Full suite: **912 passed, 11 skipped** in 160.00 s (905 before this pass plus the 7 new tests); the eleven skips are unchanged (`test_alpha5_playback.py:516`, `:531`, `test_coreaudio.py:255`, `test_packaging.py:373`, `test_real_engines.py:32` ×5, `:107` ×2). `git diff --check`
and `compileall` over `babelfishr`, `tests`, `packaging`: clean. Linux,
Python 3.11, PySide6 Essentials, `QT_QPA_PLATFORM=offscreen`, mock and
stand-in engines. This session's results, not an independent rerun; no Mac.

## Unresolved ledger, updated

- A thread that never returns is visible and waited for; there is still no
  forced cancellation or forced exit — Eric's decision, not taken here.
- CLI `close()` without a timeout waits indefinitely (Ctrl-C works).
- Immediately after Stop on a live device, Start and mode change are
  refused for the fraction of a second the capture takes to settle, with the
  message "The previous run has not released the audio input yet".
- F3, F4 deferred; F6/F7, F8/F9 outside; playback, removal semantics and
  General unresolved; no Mac validation of any of this (checklist M,
  steps 43–46, says so now).

---

# Completing non-blocking Stop/Quit — the terminal cleanup phase

Codex, at `20be9b7`, passed the final-voice/Quit and held-analysis probes and
then measured three remaining paths that still held the main thread: (1)
`CaptureService.stop_async()` closed the source and flushed on the caller
when the audio thread had already ended - 1.200 s with a slow `source.stop()`;
(2) `stop_session()` called `stop_if_idle(timeout=5.0)`, a worker join - 1.201 s
with the worker held at its stop sentinel; (3) `close(wait=False)` called
`engine.close()` and `store.close()` inline - 1.201 s with a slow engine
close. Those were counterexamples to the previous report's "returns
promptly"; the phrase "fraction of a second" there was a characterisation,
not a measurement, and is withdrawn.

## Identifiers

| | |
|---|---|
| Base | `20be9b7` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section (the branch tip) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release | none dispatched, retried, created, moved or published |

## What changed

- **Capture.** `stop_async()` starts its stopper thread whenever anything
  remains to do - a live audio thread, a source not yet stopped, a finish not
  yet run - and returns without work only when all three are already done
  (`_source_stopped` tracks the source; `run_to_completion` sets it). The
  source close and the final flush therefore never run on the caller.
- **Idle worker retirement.** `ProcessingPipeline.stop_if_idle(wait=False)`
  closes admission atomically and queues the sentinels without joining; it
  returns True when the stop *began*. `stop_session()` uses it and tracks a
  worker that has not yet returned in `_retired`; the worker ends on its own
  (the sentinel makes `queue.get` return at once) and `_reap()` drops it.
  `shutdown_problem()` and `_processing_pipeline()` now refuse only for a
  retired worker that still holds work (`pending > 0`); one that has finished
  its work and is merely leaving blocks nothing new, but `close()` still
  waits for it before the final cleanup.
- **Final cleanup.** `close()` hands the last step - engines, each once and
  dropped as closed, then the store - to a cleanup thread
  (`_FinalCleanup`, "babelfishr-cleanup"). `app.cleaning` is set the moment
  that thread is started and never cleared: from then on the store may close
  at any instant. A second `close(wait=False)` while it runs starts nothing.
  A failure is recorded (`cleanup_error`), `_closed` stays False, and the next
  call retries from what is still open. `close(wait=True)` (CLI, tests)
  starts the same thread and joins it, re-raising a failure.
- **Window.** `_finish_shutdown()`: when `app.cleaning` first becomes true it
  stops the event-drain timer, marks the store off limits (`_drain_events`
  returns at once) and disables the central widget, while the window itself
  keeps repainting and the quit timer keeps asking; `cleanup_error` is shown
  as before ("Could not finish quitting: … Retrying"). An idle window's first
  Quit therefore returns False and completes on the next tick - nothing
  closes on the GUI thread any more.

Not changed, deliberately, and recorded as remaining synchronous *bounded*
paths outside Stop/Quit: `_retire_processing()` (mode change) and
`_discard_standalone_pipeline()` (Start) still `stop_if_idle(timeout=5.0)`
an idle processor with a join, and `_abandon_failed_start()` still joins the
workers of a start that failed. Each is a join of an idle worker that exits
on its sentinel; each is bounded by 5 s; none is Stop or Quit.

## Tests

`tests/test_alpha5_cleanup.py` (5, real Qt offscreen): (1) already-ended
capture + held `source.stop()`: Stop < 1 s, heartbeat through the hold,
capture still "finishing", nothing flushed until the source closed, then one
COMPLETE transmission; (2) worker held at its stop sentinel: Stop < 1 s and
Quit < 1 s with the heartbeat alive, worker tracked in `_retired`, an idle
leaving worker blocks nothing new, cleanup does not begin until it has gone;
(3) held `engine.close()`: Quit < 1 s, heartbeat through the hold, drain
timer stopped, central widget disabled, a published event and a direct
`_drain_events()` reach the store zero times (counted on `store.is_deleted`),
store still open (engines first), one cleanup thread, engine closed once;
(4) held `store.close()`: same, plus repeated Quit starts no second cleanup;
(5) repeated Quit before and during the cleanup: one thread, each engine
closed once, `close()` after completion is a no-op.

Substitutions: a `CallbackAudioSource` subclass that reports itself finished
once its queue is empty after the test says so (the audio thread ends like a
replay's) and whose `stop()` blocks until released; the live pipeline queue's
`get` wrapped to hold after the sentinel; `MockTranscriptionEngine.close` and
`Store.close` wrapped on the instance to block until released. Production
timeouts unchanged (`stop_timeout` asserted 5.0; the 5 s join is never
reached because nothing joins on the GUI thread).

### Existing tests changed

- `test_alpha5_lifecycle.py::test_a_straggler_blocks_starts_and_mode_changes_until_it_has_left`:
  the stand-in straggler now reports `pending = 1`. An idle worker merely
  leaving no longer blocks starts or mode changes; one still holding work
  does - which is what the test is about.
- `test_alpha5_lifecycle.py` (two tests) and `test_alpha5_shutdown.py` (two
  tests): `assert window.close() is True` became "close, then pump until
  hidden", because the final cleanup now completes on its own thread a tick
  later; the failed-close test waits for the asynchronous outcome (status
  text, engine closed once) instead of asserting it synchronously. Same
  intents.

### Fail-before (production files restored from `20be9b7`, tests unchanged)

Three of the five cleanup tests FAIL outright on `20be9b7`; the fourth
(held `engine.close()`) did not fail - it *hung*: pytest's 120 s timeout
fired with the main thread inside `_finish_shutdown → app.close →
engine.close`, which is the blocking path itself; the fifth was not reached.
On the repaired tree all five pass (26 s), as do the 19 tests of the two
previous new-pass files (23 s).

### Mutations (repaired code restored after each; byte-compared)

| # | Mutation | Result |
|---|---|---|
| P1 | `stop_async` closes the source and flushes on the caller when the thread has ended | caught (slow-source test) |
| P2 | `stop_session` joins the idle worker again | caught (idle-worker test) |
| P3 | `close()` runs the final cleanup on the caller's thread | caught (slow-engine test) |
| P4 | the window keeps draining while the store is closing | caught (slow-engine test) |
| P5 | `_FinalCleanup.start()` no longer refuses while running | **not caught — equivalent mutant**: `close()` checks `running` before calling `start()`, so the inner guard alone is redundant |
| P5b | both guards removed (a second Quit really starts a second cleanup) | still not caught with two guards removed - a third check of `running` sits before `start()` in `finish()`; with **all three** removed (P5c) the test fails: 6 cleanup threads for 1, the engine closed 3 times for 1. The test catches a genuine overlap; P5 and P5b are equivalent mutants of layered guards |
| P6 | `close()` cleans up before the retired worker has finished | caught (idle-worker test) |
| P7 | a cleanup failure is treated as done | caught (failed-close test) |

### Test results

Focused first: the ten nearest existing suites (223 passed); the two previous new-pass files (19 passed); this pass's file (5 passed). Full suite: **917 passed, 11 skipped** in 181.88 s (912 before this pass plus the 5 new tests); skips unchanged (`test_alpha5_playback.py:516`, `:531`, `test_coreaudio.py:255`, `test_packaging.py:373`, `test_real_engines.py:32` ×5, `:107` ×2). `git diff --check` and `compileall` over `babelfishr`, `tests`, `packaging` clean. Linux, Python 3.11, PySide6 Essentials, `QT_QPA_PLATFORM=offscreen`, mock and stand-in engines. This session's results, not an independent rerun; no Mac.

## Logo — preserved for the assets pass, not integrated

Eric supplied a logo sketch and asked for white shapes on black; Codex
generated artwork; Eric shared the design here:
https://chatgpt.com/s/m_6aa0578e4c248191bc16f0fd776186e4 . Nothing in
packaging or the application was changed for it in this pass.

## Unresolved ledger, updated

- Stop/Quit no longer wait on the GUI thread for capture, source, worker,
  engine or database cleanup, as measured by the tests above on Linux; on a
  Mac this is untested (checklist M, steps 43-46).
- Mode change, Start and a failed start still join an idle worker for up to
  5 s on the GUI thread (bounded; not Stop/Quit; noted above).
- A permanently stuck source, worker, engine close or database close is an
  explicitly reported unresolved operation: Quit waits and says what for; no
  forced termination or cancellation exists (Eric's decision).
- CLI `close()` without a timeout waits indefinitely (Ctrl-C works).
- F3, F4 deferred; F6/F7, F8/F9 outside; playback boundary/seek/collapse,
  removal semantics and General unresolved; logo not integrated.

---

# Finishing the GUI/database shutdown boundary, and the end-of-run write

Codex, at `62972f7`, independently ran the 24 lifecycle/shutdown/cleanup tests
(all passed; Linux, Python 3.12.13, PySide6 Essentials 6.8.3, offscreen) and
found two further defects. **A:** during the held final cleanup the central
widget was disabled and the drain stopped, but the menu bar and its actions
stayed enabled - two real clicks (View, then Review queue) raised
`ProgrammingError: Cannot operate on a closed database` in the interval after
the real `Store.close` and before cleanup returned. **B:** `stop_session()`
still called `Store.close_session()` synchronously; with the store's lock
held by another thread the real Stop handler blocked the main thread for
1.201 s with zero heartbeat ticks. Both reproduced here and repaired.

## Identifiers

| | |
|---|---|
| Base | `62972f7` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section (the branch tip) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release | none dispatched, retried, created, moved or published |

## A — the boundary, now a handshake

`close(wait=False, start_cleanup=False)` runs every precondition and, when
only the final cleanup is left, sets `app.ready_for_cleanup` and returns
False *without* starting the cleanup thread. The window then shuts every
route it has to the store (`_shut_store_routes`): the menu bar, every
`QToolBar`, the central widget, and every `QAction` it owns - which also
disarms their shortcuts and the tab and bubble context menus - plus the event
drain; and every handler that reads the store after a dialog, or from a
queued path, now checks `_store_gone()` first (Search, Review queue, Show all
/ reload, Session tabs refresh, New / Rename / Remove Session, Remove message
/ Delete permanently, Replay, Export clip / Session / text, Operating mode,
Finish unfinished deletions). Only then does it call
`app.start_final_cleanup()`. If a modal dialog is open at that moment
(`QApplication.activeModalWidget()`), it waits: the dialog's handler
continues on this thread when it closes and must not meet a closing store;
the status line says "Quitting once the open dialog is closed." The
command-line `close(wait=True)` keeps `start_cleanup=True` - it has no GUI
routes to shut.

## B — the end of the run, recorded off-thread and tracked

`stop_session()` fixes `session.ended_at = utcnow()` and the Session id, then
starts a `_SessionEnd` thread ("babelfishr-session-end") that calls
`store.close_session(id, ended_at=…)`; the `"session"` event is published at
once with that time. `close()` step 5b waits for every pending end (joining
only when `wait=True`), retries a failed one, and never proceeds to the
final cleanup while one is outstanding - a run is not left open under a
closed store. `session_end_pending()`, `persistence_error` and
`wait_for_session_ends()` expose it; the window shows "the end of the run is
recorded" while waiting and "Could not finish quitting: … Retrying" on a
failure. `_abandon_failed_start()` still writes synchronously (a failed
start, not Stop/Quit; recorded).

## Reproductions, before → after (real Qt window, offscreen)

- **A** saved message; before Quit, Ctrl+F reached Search and a real click
  on View opened the menu. Quit with `Store.close` run for real and then
  held: menu bar, tool bars, central widget and every action disabled;
  heartbeat alive; a real click on View did not open it; Ctrl+F,
  Ctrl+Shift+A, Ctrl+Shift+R, `trigger()` on the Review/Show all/Search
  actions, direct calls to the Review and reload handlers, a published
  event and a direct drain: **zero** queries from the main thread (counted
  on the store's connection), Search not reached, status "was not done";
  window closed on release. Open dialog: routes shut, cleanup not begun,
  store still open, `_store_gone()` True, status names the dialog; cleanup
  begins once the dialog is gone.
- **B** store lock held by another thread, live session with an open
  transmission: Stop returned in < 1 s (was 1.201 s), heartbeat alive,
  `ended_at` not yet written, `session_end_pending()` True; on release the
  stored `ended_at` equals the time fixed at Stop, the final transmission
  reached COMPLETE, the window still worked (Stop is not Quit). Quit under
  the held lock: returned in < 1 s, heartbeat alive, repeated Quit did not
  start a second write; on release `close_session` ran exactly once for
  that run and before `store.close`, the final save preceded `store.close`,
  `ended_at` persisted. Failed write (raises): `persistence_error` set,
  status "Could not finish quitting: … locked for good", retried each tick,
  not closed; on restore the write landed with the original time and the
  window closed.

## Tests

`tests/test_alpha5_boundary.py` (5, real Qt offscreen). Six tests: (1) menus and shortcuts during a held cleanup after the real
`Store.close` - zero queries from the main thread; (2) an open dialog defers
cleanup; (3) Stop under a held store lock returns, heartbeat alive, end
recorded later with the time fixed at Stop; (4) Quit under the held lock -
end recorded once, before `store.close`, after the final save; (5) a failing
end-of-run write is reported, retried, and lands with the original time once
it can; (6) with only the end-of-run write outstanding, cleanup does not
begin and the store does not close until it has landed.

### Fail-before (production files restored from `62972f7`, tests unchanged)

All six FAIL on `62972f7` (five in the first run - 144.72 s, several stuck
on the old synchronous paths until their asserts - and the sixth in its own
run); on the repaired tree all six pass in about 9 s, and the whole boundary
file was run three further times without a failure.

### Mutations (repaired code restored after each; byte-compared)

| # | Mutation | Result |
|---|---|---|
| Q1 | menu bar stays enabled once the store may close | caught |
| Q2 | actions (and so their shortcuts and context menus) stay enabled | caught |
| Q3 | handlers past a dialog no longer check the boundary | caught |
| Q4 | cleanup starts under an open dialog | caught |
| Q5 | cleanup starts before the window has shut its routes | caught |
| Q6 | the end of the run is written on the caller's thread again | caught |
| Q7 | `close()` no longer waits for the end-of-run write | **missed** by the lock-held tests (the capture's own wait masked it); test (6) was added for exactly this, and catches it |
| Q8 | a failed end-of-run write counts as done | caught |

### Existing tests changed

- `test_alpha5_cleanup.py` (slow engine close, slow database close, repeated
  Quit): waits for the cleanup thread by pumping the event loop rather than
  by a blocking `Event.wait` - with the end-of-run write now asynchronous,
  the quit timer has to tick for `close()` to progress, and a blocking wait
  starved it (2 of 6 runs failed before the change, 8 of 8 passed after);
  the replay's own events are drained before Quit; the `is_deleted` read
  counter counts only once cleanup has begun. Same intents.
- No assertion in any pre-existing suite changed; the previous 917 pass
  unmodified.

### Test results

Focused first: the twelve nearest suites (208 passed, one timing assertion
in the new failing-write test then made deterministic); the boundary file
(6). Full suite: **923 passed, 11 skipped** in 144.77 s (917 before this pass plus the 6 new tests)****; skips unchanged (`test_alpha5_playback.py:516`,
`:531`, `test_coreaudio.py:255`, `test_packaging.py:373`,
`test_real_engines.py:32` ×5, `:107` ×2). `git diff --check` and `compileall`
over `babelfishr`, `tests`, `packaging` clean. Linux, Python 3.11, PySide6
Essentials, `QT_QPA_PLATFORM=offscreen`, mock engines. This session's
results, not an independent rerun; no Mac.

## Unresolved ledger, updated

- Stop/Quit: capture, source, worker, engine and database cleanup, the
  end-of-run write, and every GUI route to the store are now off the GUI
  thread or shut before the store can close, as measured by these tests on
  Linux. This is a statement about the paths tested, not about the whole
  application; a Mac has not seen any of it (checklist M, steps 43-46).
- Still synchronous, bounded, outside Stop/Quit: mode change and Start join
  an idle worker (5 s bound); a failed start closes its Session row inline.
- A permanently stuck source, worker, engine close, database close or
  end-of-run write is an explicitly reported unresolved operation; no
  forced termination or cancellation (Eric's decision).
- CLI `close()` without a timeout waits indefinitely (Ctrl-C works).
- Logo (white shapes on black; https://chatgpt.com/s/m_6aa0578e4c248191bc16f0fd776186e4)
  recorded for the assets pass; not integrated.
- F3, F4 deferred; F6/F7, F8/F9 outside; playback boundary/seek/collapse,
  removal semantics and General unresolved; no candidate contains any of
  the shutdown passes.

---

# Filtered live views (F3) and playback ownership (F4)

Both defects were reproduced again by Codex at `f2c6b80` and reproduced here
against the same baseline before any production change (the corroboration
scripts from the earlier pass: the Search view `['tx1']` became
`['tx3','tx1']` on a non-matching same-Session arrival and the Review view
gained a confident record; B's missing file left A reading STOPPED with its
controls collapsed while the backend went on playing A with no stop call).
Two separately reviewable commits.

## Identifiers

| | |
|---|---|
| Base | `f2c6b80` — verified equal to the remote tip at the start, worktree clean |
| F3 commit | `336923d` |
| F4 commit | the commit carrying this section (`git log -1 -- docs/AGENT_HANDOFF.md`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging / logo | nothing dispatched, retried, created, moved, published or changed |

## F3 — the view admits only what its query admits (my design, following Codex's recommendation)

The window remembers what it shows (`_view_kind`: thread, search or
review; `_view_query`). Every admitted transmission or update goes through
`_admit()`: the Session's thread takes arrivals at the top and updates in
place, as before; a Search or Review view re-runs the very store query that
opened it (`app.search(query)` / `app.review_queue()` - same scope, same
semantics, same limit) and reconciles the view to that answer: a record
that qualifies is placed by `TimelineView.place()` where its time puts it
(updated in place if shown), one that does not, or has stopped qualifying,
is removed, anything the bounded result no longer holds is removed, and
the status count is re-announced. No separate matcher; the Store remains
the behavioural reference, including `review_queue`'s own rules
(`reviewed = 0 AND hidden = 0 AND (failed OR confidence below threshold)`),
which are not `Transmission.needs_review`. Show all, Session switch and
the removed-messages toggle reload the thread and reset the view; a
cancelled Search changes nothing. Cost: one store query per admitted event
while a filtered view is open - a choice made over rebuilding widgets
(F7), noted, not measured here.

## F4 — a failed request touches only its recording

`_fail()` retires the recording only when it is the owner; otherwise
A's owner, state, position and controls stay as the backend has them and
B records and shows its own error. `play()` now checks, after `load()` and
after `play()`, whether the backend recorded an error for this request
synchronously (`tx_id in last_error`) and returns False rather than
pressing play and claiming success; a backend that *finishes*
synchronously - the fire-and-forget system player - records no error and
remains a success (that distinction was found by the existing system-
backend test failing against a first, state-based check). A bubble whose
playback error has cleared (a retry succeeded) returns its status line to
the message. The five-second threshold, seek amount, collapse at natural
completion and removal semantics are untouched.

## Tests

`tests/test_alpha5_filtered_views.py` (5) and
`tests/test_alpha5_playback_ownership.py` (6); all real Qt offscreen; the
filter tests reach Search and Review through two real menu clicks (the
Search text dialog substituted to type the word) and deliver persisted
records through the production event queue drained by the window's timer;
the playback tests use two real bubbles, real button clicks and the
scripted backend from `test_alpha5_playback.py` (imported by path), with
explicit assertions on controller state, backend state, owner, position,
controls and backend stop calls. The anchoring test measures the anchored
bubble's viewport y before and after a filtered arrival lands above it and
a bubble above grows; list order is not used as a substitute.

Fail-before (production files restored from `f2c6b80`): 8 of 11 fail; the
three that pass are preservation guards (Session switch restores the
thread; anchoring, which the old code also kept; owner failure, natural
end, Stop and takeover). Test hygiene: the tautological
`assert "match" in … or window.timeline.count() >= 0` in
`test_menus_and_shortcuts_cannot_reach_the_store_during_final_cleanup`
became `window.timeline.order() == [t.id for t in app.review_queue()]` plus
the "need review" status - a known result. No other existing assertion
changed.

## Evidence (this pass)

Environment: Linux container, Python 3.11, PySide6 Essentials (no
QtMultimedia - the QtMultimedia-backed playback tests skip here, as
before), `QT_QPA_PLATFORM=offscreen`, fake engines, isolated temporary
homes and recordings, no cloud provider; nothing of Eric's touched.

| Check | Result |
|---|---|
| F3 reproduction, `f2c6b80` | Search view `['tx1']` -> `['tx3','tx1']` on a non-matching arrival; Review view gained a confident record. After repair: not reproduced (view unchanged, count unchanged) |
| F4 reproduction, `f2c6b80` | B's missing file -> A `STOPPED`, owner `None`, controls collapsed, backend still playing A, no stop call. After repair: A `PLAYING`, owner `a`, position kept, controls shown, B shows its own error |
| *Correction (next pass)* | The row above says owner `None`; that is wrong. The old `_fail()` set the shared state to `STOPPED` for B's failure and left A's owner as `a` - `state_for("a")` read `STOPPED` because the shared state did, not because ownership was cleared. The fail-before output of that pass shows it (`assert ('a' == 'a' and 'stopped' == 'playing')`), as does Codex's own `f2c6b80` reproduction. Original wording left in place; corrected here rather than rewritten. |
| New tests | 11 (5 filtered views, 6 playback ownership), all pass |
| Fail-before (production restored from `f2c6b80`) | 8 of 11 fail; guards that pass: Session switch, anchoring, owner-failure/natural-end/Stop/takeover |
| Focused | filtered views + ownership + boundary: 17 passed; ownership + `test_alpha5_playback.py`: 27 passed, 2 skipped; the 30 lifecycle/shutdown/cleanup/boundary tests with the nearest suites: 150 passed, 2 skipped |
| Full suite | 934 passed, 11 skipped, 0 failed, 172 s |
| Skips (11) | 2 QtMultimedia not installed; 1 CoreAudio needs macOS; 1 PlistBuddy macOS only; 5 no prepared Whisper model; 2 no Argos language pack |
| `git diff --check`, `compileall` | clean |

Mutations (one reversal at a time, the named test run, files byte-restored):

| Mutation | Test | Verdict |
|---|---|---|
| R1 `_admit` ignores the view's query, every arrival added (F3 root reversed) | `test_search_view_admits_only_what_the_search_admits` | caught |
| R2 the explicit `else: remove(tx.id)` branch dropped | same | equivalent - the reconcile loop already removes it; the branch was removed from production as redundant |
| R2b the reconcile loop removed | same | caught (`['tx5','tx3','tx1','tx0'] != ['tx5','tx3','tx0']`) |
| R3 `place()` always inserts at the top | same | caught |
| R4 Review view reconciled against the thread, not the review queue | `test_review_view_admits_only_what_the_review_queue_admits` | caught |
| R5 status count not refreshed | `test_search_view_admits_only_what_the_search_admits` | caught |
| R6 `_fail` sets shared STOPPED for any recording (F4 root reversed) | `test_bs_missing_file_leaves_a_playing_a_long_recording` | caught |
| R7 `play()` presses play and claims success after a failed load | `test_a_backend_that_rejects_the_file_on_load_gives_no_false_start` | caught |

No uncaught, non-equivalent mutation.

## Unresolved ledger, updated

- F3 and F4 repaired here; not seen on a Mac (checklist N and O, steps
  47-50, marked for a future candidate).
- F6/F7 measured, unrepaired; F8/F9 recommendations; playback boundary,
  seek steps, collapse at natural completion, removal semantics, General:
  unresolved; logo (white on black,
  https://chatgpt.com/s/m_6aa0578e4c248191bc16f0fd776186e4) pending for the
  assets pass; no candidate contains the shutdown or these passes; run 21
  historical; no forced cancellation/exit.
- The filtered view re-queries the store per admitted event; its cost at
  large thread sizes belongs with F6/F7. *(Superseded by the next section:
  one query per drained batch, and the membership is the complete bounded
  result.)*

---

# Filter reconciliation completed, and failed-resume reporting

Codex's independent check of `c904284` (57 passed, 2 skipped across the
seven nearest suites, Python 3.12, PySide6 6.8.3, offscreen) confirmed the
F3 and F4 repairs and found three things the first pass left incomplete.
All three were reproduced here against `c904284` before any edit, with the
same scripts that now stand as tests:

- **A. Capped results not fully reconciled.** 201 qualifying rows, view
  opened through the View menu, the newest shown row made to stop
  qualifying (Search: transcript changed and `updated` published; Review:
  `app.correct()`). Store: 200 rows including `tx0`; timeline: 199 rows,
  `tx0` missing; status: "200". `_admit()` placed only the event's own row
  and never added a record newly entering the bounded result.
- **B. GUI removal bypassed the Search filter.** Show removed messages on,
  Search "giraffe", the bubble's ⋯ → Remove message → Remove from thread
  (only the question's answer substituted): the row hidden in the store,
  `app.search` empty, `_view_kind` still "search", the hidden bubble still
  displayed. `_on_remove_message()` updated the widgets directly. A second,
  event-path probe (store hide + `updated` event) showed `_belongs_here()`
  rejecting the update so the shown widget stayed.
- **C. Failed resume returned True.** A paused at 700 ms, a backend whose
  `play()` emits `errorOccurred` and reports STOPPED, `controller.play("a",
  path)` → `True` with owner `None`, state STOPPED and the error shown. The
  PAUSED resume branch returned True straight after `backend.play()`.

## Identifiers

| | |
|---|---|
| Base | `c904284` — verified equal to the remote tip at the start, worktree clean |
| F3 follow-up commit | `a71c2fc` |
| F4 follow-up commit | the commit carrying this section (`git log -1 -- docs/AGENT_HANDOFF.md`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging / logo / FTS schema / dependency locking | nothing dispatched, retried, created, moved, published or changed |

## A and B — the Store's bounded answer is the membership (my design, on Codex's recommendation)

`_admit()` no longer draws anything from a filtered view's event; the
event only says the answer may have changed (anything shown, or anything
of this Session's - hidden or deleted included). The drain collects the
ids touched and calls `_refresh_view(touched)` **once per drained batch**:
the very store query that opened the view is run again, and its complete
bounded result is the membership - shown rows it does not hold are
removed, rows it holds that are not shown are placed by
`TimelineView.place()` where their time puts them (a replacement for one
that left a full result lands at the bottom), shown rows whose event
arrived are updated to the row as the store has it, and the count is
re-announced. Widgets that stay are the same widgets. The Session's own
thread keeps its arrival/update behaviour and now also drops a shown row
whose update says it is hidden (removed messages not shown) or that was
deleted. `_belongs_here()` is split: `_in_this_conversation()` is the
Session test; the visibility tests stay in `_belongs_here()` for the
thread.

The operator's own changes go through the same reconciliation:
`_after_local_change()` (Remove from thread, Restore to thread) refreshes
a filtered view against its query and treats the thread as before;
permanent deletion refreshes the view after removing the widget, so a full
result gets its replacement. Operator messages in a filtered view carry the
view's count ("1 match(es) for 'giraffe'. Message removed from the
thread..."), so the status line stays truthful about the view it describes.
`_refresh_view()` returns at once when the store routes are shut; the
drain already did. Toggling View › Show removed messages still reloads the
thread (as Show all does) - preserved, tested, and not a decision about
whether Search should include hidden rows: the Store's rule is unchanged.

`TimelineView.place()` is now a single anchored insertion (`_insert()`,
shared with `add()`), not an insertion at the top followed by a move; and
the deferred anchor passes are coalesced into one chain. Two anchored
changes in one turn (a row removed, its replacement placed) used to start
two chains, spending the three settle passes twice as fast - before the
scroll area had resized the container to its new content, while a full
200-row thread was still squeezed to the old height - and the reading
position settled a bubble's height (57 px in the test) off. With one chain
the position is back within a pixel by the third turn. **Limitation:** in
a full 200-row view the correction is visible as a transient of up to two
event-loop turns; measured here offscreen, not on a screen.

Query count, measured: five same-Session arrivals published and drained
in one batch → **1** store query (was 5, one per event); three
other-Session arrivals → **0** queries. No FTS or schema change; F6/F7
are not addressed.

## C — resume reports like a fresh start

The PAUSED branch of `PlaybackController.play()` clears any stale error,
presses `backend.play()`, and returns `tx_id not in self.last_error`: a
backend that rejects the resume synchronously has already retired the
recording through `_on_error` and recorded why, and the caller is told
False. A resume that works returns True and continues from the paused
position with a single `play()` call - no reload, no stop. Cross-bubble
protection, takeover, Stop, natural completion and the system-player
fallback are unchanged (`test_alpha5_playback.py`,
`test_alpha5_playback_ownership.py` all pass). A scripted signal order
shows the controller's contract, not what a QtMultimedia build emits;
QtMultimedia is not installed here.

## Tests

`tests/test_alpha5_view_reconciliation.py` (8) and
`tests/test_alpha5_playback_resume.py` (3), real Qt offscreen, helpers
imported by path from the earlier suites. Search and Review at 201 rows
through the real menu, a shown row made to stop qualifying by the
production paths (`updated` event; `app.correct()`), membership asserted
equal to the Store's result and the count read from the status line; the
surviving bubbles asserted to be the same widget objects and the anchored
bubble's viewport y measured before and after (±1 px); Remove from thread
through two real clicks on the bubble's ⋯ menu with only `_choose`
substituted, with removed messages shown; Delete permanently through the
two real questions, bringing in the next record the limit kept out; the
event path for hidden and deleted rows in both a Search and the thread;
the removed-messages toggle preserved; one query per drained batch and
none for other Sessions; no query against a closing store
(`_shut_store_routes()`, the production shutter). No test removes a widget
itself to manufacture a result.

## Evidence (this pass)

Environment: Linux container, Python 3.11, PySide6 Essentials (no
QtMultimedia), `QT_QPA_PLATFORM=offscreen`, mock engines, isolated
temporary databases and recordings, no cloud provider; nothing of Eric's
touched. Substitutions: the Search text dialog (`QInputDialog.getText`)
types the word; the removal question (`MainWindow._choose`) is answered;
the scripted playback backend stands in for QtMultimedia. Everything else
is the production window, menus, bubbles, store and event queue.

| Check | Result |
|---|---|
| A, B, C on `c904284` | all three reproduced as Codex described (A: store 200 incl. `tx0`, timeline 199, status "200", Search and Review alike; B: `['tx0']` shown, `app.search` empty, view "search"; C: `True`, owner `None`, STOPPED, error shown). After repair: A timeline 200 incl. `tx0`, count 200; B view empty, count 0; C returns `False` |
| New tests | 11 (8 reconciliation, 3 resume), all pass |
| Fail-before (production from `c904284`) | 8 of 11 fail; the 3 that pass are guards: removed-messages toggle returns to the thread; a working resume; a paused recording surviving B's failure and yielding to a valid B. `test_reconciliation_never_queries_a_closing_store` fails before because `_refresh_view` does not exist there, not because the old drain queried a closing store |
| Focused (11 suites: reconciliation, resume, filtered views, ownership, playback, boundary, cleanup, shutdown, lifecycle, message removal, shared recordings) | 94 passed, 2 skipped |
| Full suite | 945 passed, 11 skipped, 0 failed, 192 s |
| Skips (11) | 2 QtMultimedia not installed; 1 CoreAudio needs macOS; 1 PlistBuddy macOS only; 5 no prepared Whisper model; 2 no Argos language pack |
| `git diff --check`, `compileall` | clean |

Reversals (one at a time, the named test run, files byte-restored and verified):

| Reversal | Test | Verdict |
|---|---|---|
| M1 `_refresh_view` places only the touched row (finding A reversed) | `test_a_full_search_replaces_a_row_that_stops_qualifying` | caught (`tx0` missing) |
| M1b the same, Review queue | `test_a_full_review_queue_replaces_a_row_the_operator_reviews` | caught |
| M2 removal updates the widgets directly (finding B reversed) | `test_removing_a_shown_message_leaves_the_search_even_while_removed_messages_are_shown` | caught (`['tx1','tx0'] != ['tx0']`) |
| M2b deletion does not refresh the view | `test_removing_and_deleting_from_a_full_search_bring_in_replacements` | caught (`tx-1` missing) |
| M3 a filtered view is invalidated only by events `_belongs_here` admits | `test_a_shown_row_that_is_hidden_or_deleted_by_an_event_leaves` | caught |
| M3b the thread ignores a shown row now hidden | same | caught (`['tx2'] != []`) |
| M4 refresh per event, not per batch | `test_one_query_per_drained_batch_and_none_for_other_sessions` | caught (5 queries != 1) |
| M5 no closing-store guard in `_refresh_view` | `test_reconciliation_never_queries_a_closing_store` | caught (2 queries) |
| M6 deferred anchor passes not coalesced | `test_a_full_search_replaces_a_row_that_stops_qualifying` | caught (moved 57 px) |
| M7 failed resume returns True (finding C reversed) | `test_a_resume_the_backend_rejects_returns_false_and_shows_the_error` | caught |

No surviving reversal. Existing tests changed: none (two new files only).
One test-harness lesson recorded: a scratch script named `profile.py`
next to a probe shadowed the standard-library `profile` module and ran a
second window inside the probe process; it distorted one timing probe
before it was found and renamed. No repository file was involved.

## Provenance corrections

- The previous pass's chat report closed with "Carried forward verbatim
  from the directive: ... acoustic radio test not performed ...". That was
  a summary, not a verbatim carry-forward, and the acoustic statement was
  wrong as written. **Eric's acoustic radio test is recorded** (above, in
  "Eric's run-21 results" and the run-20 status): work radio speaker →
  acoustic sound → laptop microphone → BabelFishR, with conversation
  breaks, transcription and translation observed by Eric, who clarified
  "live translation & transcription still came from my laptop's
  microphone, but i held my work radio up to it". What is untested is the
  direct electrical radio/PTT/USB path, SDR operation, RF metadata and
  transmitter identification - and no acoustic or hardware test was run in
  any of these coding sessions. The two must not be conflated again.
- The F4 baseline description "owner `None`" in the previous section is
  corrected in place (annotation row under its table): the old `_fail()`
  changed the shared state only; A's owner remained `a`.

## Unresolved ledger, carried forward

- F1 worker/offline safeguards and F2 shared-recording protection stand.
- F5 shutdown repairs green in Codex's latest focused run; physical Mac
  validation pending. Permanently stuck operations have no
  forced-cancellation policy; CLI close without a timeout can wait
  indefinitely; Start/mode-change bounded synchronous paths remain;
  unreadable retained analysis records conservatively prevent file
  deletion; the historical "fraction of a second" wording is not a
  measured bound.
- F3: contamination repaired (previous pass); capped replacement and GUI
  removal repaired here. F4: cross-bubble ownership repaired (previous
  pass); failed-resume return repaired here. Neither seen on a Mac
  (checklist N, O, P; steps 47-51; future candidate).
- F6 search-index write amplification and F7 large-thread costs
  unrepaired; F8 selective cleanup and F9 dependency locking remain
  recommendations.
- Exactly-five-second playback boundary, seeking and natural-completion
  collapse: unresolved product decisions. Removal semantics, General's
  treatment, forced cancellation/exit: unresolved; preserving or testing
  existing code does not approve those choices.
- Eric's white-on-black logo integration pending.
- No candidate contains these repairs.
- Direct electrical radio/PTT/USB, SDR, RF metadata and transmitter
  identification unverified. Acoustic radio testing IS recorded (above).

---

# F6 — search-index write amplification

Codex's audit of `71ada52` (94 passed, 2 skipped across the seven nearest
suites; independent probes of the capped-result, removal and resume
scenarios) found no new F3/F4 blocker and scoped this pass to F6: every
accepted save deleted and rewrote the message's search-index entry, found
by scanning the whole index for its UNINDEXED id, and every deletion
scanned it again.

## Identifiers

| | |
|---|---|
| Base | `71ada52` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section, `git log -1 -- tests/test_alpha5_search_index.py` (storage change, upgrade and tests together: the rowid maintenance needs the map, so the migration is not separable) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging / logo / dependency locking | nothing dispatched, retried, created, moved, published or changed |
| Schema | **6** (one new table, `transmissions_fts_map`; meta key `fts_layout` = 2). The schema-3 fixture is untouched. |

## Baseline, measured before editing (this container, Python 3.11, SQLite 3.45.1, FTS5 compiled in)

- SQL trace of a replayed fixture through the real app on mock engines: 5
  messages, 23 saves, **46 index writes** (a DELETE and an INSERT per save;
  9.2 per message) although each message's searchable text changes at most
  twice.
- Query plan of the maintenance lookup `DELETE FROM transmissions_fts WHERE
  id = ?`: `SCAN transmissions_fts VIRTUAL TABLE INDEX 0:` - a scan of the
  content table for the UNINDEXED id. The rowid form reads `... INDEX 0:=`;
  the `=` is the rowid-equality constraint (both contain "SCAN", so no test
  here rejects that word).
- Cost per save on fully indexed histories, median of 40 saves × 3 runs:
  500 rows ≈ 1.1 ms, 5,000 ≈ 2.5 ms, **50,000 ≈ 19 ms** - linear in the
  history. Indexing 50,000 rows one save at a time took 424 s (the scan
  per insert; quadratic).

## Design (mine, on Codex's recommendation; SQLite facts verified here, source cited where it could be)

- **Map by rowid.** `transmissions_fts_map (id TEXT PRIMARY KEY, fts_rowid
  INTEGER NOT NULL UNIQUE)` remembers which index row is which message's.
  Maintenance goes through it: `SELECT ... FROM transmissions_fts WHERE
  rowid = ?`, `UPDATE ... WHERE rowid = ?`, `DELETE ... WHERE rowid = ?` -
  each planned as `INDEX 0:=`. The FTS5 content table is declared `id
  INTEGER PRIMARY KEY` (SQLite source, `ext/fts5/fts5_storage.c`; this
  build's `sqlite_master` shows `'transmissions_fts_content'(id INTEGER
  PRIMARY KEY, c0..c5)`), so the rowid is explicit and survives VACUUM -
  checked here by deleting rows, VACUUMing and comparing. The map is the
  only place the rowid is kept; it is derived from the messages, like the
  index, and `rebuild_search_index()` recreates both.
- **Write only on change.** `_index_content()` reads the searchable text
  the index actually holds (by rowid) and compares it with the text being
  saved - the six fields as before: transcript, translation, both
  corrections (joined), notes, tags (joined). Equal → nothing written.
  Different → `UPDATE` in place. Nothing searchable → no entry (an existing
  one is deleted). The Transmission object is never the reference: callers
  mutate and save the same instance.
- **Deletion** (retention and permanent) removes the entry by rowid through
  the map, and the map row. Tombstones are untouched; a late save is still
  refused before it reaches the index.
- **Upgrade** (`_upgrade_fts_layout`, on open, when `fts_layout` ≠ 2 or the
  map does not cover the index): in one transaction - adopt the existing
  rows (map ← `id, MAX(rowid)` per id that still names a message), delete
  orphans and duplicates by rowid, then for every message bring its entry
  to its current text (index the never-indexed, correct the stale, leave
  the rest) and write the `fts_layout` mark. Failure → ROLLBACK, mark not
  written, `fts_enabled = False` for that process (search falls back to
  LIKE, saves persist normally), retried at the next start. Messages,
  Sessions and files are never touched. Repeatable: at layout 2 with the
  map covering the index (same row count and same highest rowid on both
  sides) the open does no index work. An index a layout-1 build rewrote by
  id (rows above every mapped rowid) is reconciled the same way; a map
  entry whose row is gone is also repaired at save time, by clearing that
  id's rows (the one scan, on that repair path only).
  *Correction (next pass):* the claim two sentences up - that a layout-1
  build's rewrite is detected unless it re-used the very rowid it freed,
  "in which case the map is still right" - was wrong. Codex reproduced the
  case: the old build deletes message A (row 1) and indexes a new message
  B, which takes row 1; the map's "A -> 1" then names B's row while row
  count and highest rowid still agree, and editing B leaves its old
  wording searchable under a second row. The check now verifies the
  association itself (see "F6 follow-up" below). Original wording left in
  place.
- Assumption corrected while testing: FTS5's `'delete-all'` command is for
  contentless/external-content tables only (error text from this SQLite);
  the rebuild uses `DELETE FROM transmissions_fts`.
- `save_transmission` still commits every save and still bumps `_writes`
  (the retained-file-reference cache stamp) on every save.

## After

- Pipeline scenario (same replay): 23 saves, **8 index writes** (5
  INSERTs when the transcripts arrived, 3 UPDATEs when translations did;
  no DELETE) plus 5 map inserts; every transcript and translation found.
- Maintenance statements traced from real saves and deletions all read
  `WHERE rowid = <n>` and plan as `INDEX 0:=`.

## Measurements (`bench_fts.py`, fully populated and fully indexed temporary databases; setup separate; medians of 40 operations - 20 for deletions - per run, three runs; index writes are top-level statements against `transmissions_fts`, map writes shown separately)

| history | operation | baseline median ms (3 runs) | repaired median ms (3 runs) | index writes per run: baseline → repaired (+ map writes) |
|---|---|---|---|---|
| 500 | setup: bulk rows, then index (baseline: one save-path index write per row; repaired: `rebuild_search_index()`) | 0.36 s | 0.08 s | - |
| 500 | state only | 1.793 / 1.063 / 1.126 | 0.849 / 0.57 / 0.614 | 80 → 0 (+0) |
| 500 | text change | 1.829 / 1.111 / 1.141 | 0.971 / 0.831 / 0.732 | 80 → 40 (+0) |
| 500 | new insert | 1.027 / 0.81 / 0.779 | 0.694 / 0.698 / 0.689 | 80 → 40 (+40) |
| 500 | retention delete | 0.977 / 1.012 / 0.8 | 0.825 / 0.627 / 0.612 | 20 → 20 (+20) |
| 500 | permanent delete | 6.95 / 4.312 / 4.185 | 4.013 / 3.661 / 3.781 | 20 → 20 (+20) |
| 5,000 | setup: bulk rows, then index (baseline: one save-path index write per row; repaired: `rebuild_search_index()`) | 4.62 s | 0.84 s | - |
| 5,000 | state only | 2.465 / 2.339 / 2.667 | 0.605 / 0.76 / 0.749 | 80 → 0 (+0) |
| 5,000 | text change | 2.479 / 2.389 / 2.628 | 0.773 / 0.9 / 0.896 | 80 → 40 (+0) |
| 5,000 | new insert | 2.179 / 2.388 / 2.359 | 0.826 / 0.805 / 0.73 | 80 → 40 (+40) |
| 5,000 | retention delete | 2.118 / 2.184 / 2.395 | 0.683 / 0.592 / 0.705 | 20 → 20 (+20) |
| 5,000 | permanent delete | 35.002 / 36.257 / 35.455 | 32.668 / 33.161 / 33.868 | 20 → 20 (+20) |
| 50,000 | setup: bulk rows, then index (baseline: one save-path index write per row; repaired: `rebuild_search_index()`) | 424.52 s | 8.32 s | - |
| 50,000 | state only | 18.887 / 19.024 / 19.91 | 0.678 / 0.791 / 0.678 | 80 → 0 (+0) |
| 50,000 | text change | 19.32 / 19.183 / 20.8 | 0.837 / 1.011 / 0.841 | 80 → 40 (+0) |
| 50,000 | new insert | 18.758 / 19.786 / 19.781 | 0.915 / 0.714 / 0.756 | 80 → 40 (+40) |
| 50,000 | retention delete | 18.93 / 18.183 / 18.741 | 0.632 / 0.637 / 0.761 | 20 → 20 (+20) |
| 50,000 | permanent delete | 375.555 / 377.37 / 382.576 | 362.982 / 365.013 / 361.182 | 20 → 20 (+20) |

Query plans, both builds: `DELETE ... WHERE id = ?` → `SCAN transmissions_fts VIRTUAL TABLE INDEX 0:`; `... WHERE rowid = ?` → `SCAN transmissions_fts VIRTUAL TABLE INDEX 0:=`. The repaired code issues only the rowid form (traced statements, `test_index_maintenance_addresses_entries_by_rowid...`). Per-save index cost went from linear in the history (1.1 → 2.5 → 19 ms) to flat (≈0.6-0.8 ms at every size); a state-only save writes nothing to the index at all. The baseline's 424 s setup at 50,000 is the quadratic cost of indexing one save at a time; the repaired rebuild of the same 50,000 rows took 8.3 s (it is also what the first start after the upgrade would do only if the existing rows could not be adopted - adoption itself writes nothing for rows that are already right).

Limits of the measurements: one Linux container, temporary databases on
its disk, medians of 40 operations × 3 runs; they show the shape (constant
vs linear) and are not Mac timings or a cross-platform guarantee. Permanent
deletion stays at ~360 ms at 50,000 rows because it rebuilds the
retained-file-reference map (F2's shared-file protection, a full scan of
`transmissions`), not because of the index; out of this pass's scope.

## Tests (`tests/test_alpha5_search_index.py`, 13)

State-only saves persist (state, confidence, path) with zero index writes
and the `_writes` stamp still moving; the same mutated instance updates the
entry in place under the same rowid; every searchable field added, changed
and cleared (notes-only and tags-only found; nothing left → no entry);
traced maintenance statements address rowids and plan as `INDEX 0:=`; a
coarse growth check (20 vs 4,000 indexed messages); retention and
permanent deletion remove entry and map row and a tombstoned late save
cannot bring either back; a populated layout-1 database (with a stale
duplicate, an orphan and a never-indexed message) upgrades with every row
byte-identical, results preserved, duplicates and orphan gone, the missing
message found, rowids adopted not rebuilt, and reopening does no index
work; the schema-3 fixture gains a searchable index; a failure half-way
through the upgrade leaves no mark, no map, all messages, LIKE search and
normal saves, and the next start completes it; an index rewritten by an
older build is reconciled on open; `rebuild_search_index()`; operation
without FTS5 (the CREATE VIRTUAL TABLE made to fail as a missing module
would); and the production pipeline scenario on the standard fixture.
Substitutions: none beyond the mock engines and the two deliberate
failures (a raising `_reconcile_fts`, a missing FTS module).

## Evidence

| Check | Result |
|---|---|
| Fail-before (storage.py from `71ada52`, new tests run) | 13 of 13 fail. Behavioural: state-only saves write 2 index statements each; the pipeline replay writes 46; deletion/rewrite go by id. The rest fail because the map table, `rebuild_search_index()` and the layout mark do not exist there (the test file collects against the old code; `FTS_LAYOUT` is read tolerantly). |
| Reversals (one at a time, named test, file byte-restored) | R1 rewrite on every save → caught (state-only test); R1b same via the pipeline replay → caught; R2 lookup by id instead of rowid → caught (plan test); R3 deletion leaves the entry → caught; R4 failed upgrade marked complete → caught; R5 map trusted on open without the coverage check → caught (older-build rewrite test); R6 stale entry kept when nothing searchable remains → caught. None survived. |
| New tests | 13 passed |
| Focused (search index, storage, thread/sessions migration, session colours migration, message removal, shared recordings, acceptance, pipeline, offline, offline integration, filtered views, view reconciliation, shutdown, cleanup, lifecycle, boundary) | 220 passed, 0 skipped |
| Full suite | 958 passed, 11 skipped, 0 failed, 196 s |
| Skips (11) | 2 QtMultimedia not installed; 1 CoreAudio needs macOS; 1 PlistBuddy macOS only; 5 no prepared Whisper model; 2 no Argos language pack |
| `git diff --check`, `compileall` | clean |

Environment: Linux container, Python 3.11.15, SQLite 3.45.1 with FTS5, PySide6 Essentials (no QtMultimedia), offscreen Qt, mock engines, temporary databases and recordings; nothing of Eric's touched. Not a Mac.

## Existing tests changed

`schema_version == 5` → `== SCHEMA_VERSION` in
`test_alpha4_thread_and_sessions.py` (1) and `test_alpha5_session_colors.py`
(3), with the constant imported: the version is 6 now. No other existing
assertion changed.

## Ledger after F6

- F1, F2, F5 preserved (full suite green; deletion/shared-recording and
  shutdown suites in the focused run).
- F3/F4 repaired and independently verified by Codex; Mac pending.
- **F6 repaired here**; not seen on a Mac (checklist Q, steps 52-54, future
  candidate).
- F7 large-thread memory/rendering costs pending; F8 selective cleanup and
  F9 dependency locking deferred.
- Product decisions unresolved: exactly-five-second playback, seeking,
  natural-completion collapse, removal policy, General's treatment, forced
  cancellation/exit. Logo pending. No candidate contains these passes.
- Hardware: acoustic radio test recorded (Eric); electrical radio/PTT/USB,
  SDR, RF metadata, transmitter identification unverified.
- Carried forward verbatim from the previous ledger: permanently stuck
  operations have no forced-cancellation policy; CLI close without a
  timeout can wait indefinitely; Start/mode-change bounded synchronous
  paths remain; unreadable retained analysis records conservatively
  prevent file deletion; the historical "fraction of a second" wording is
  not a measured bound; in a full 200-row view the anchoring correction is
  a transient of up to two event-loop turns, measured offscreen only;
  whether the packaged QtMultimedia backend emits a synchronous
  errorOccurred is unverified here.

---

# F6 follow-up — the two search-recovery gaps

Codex audited `26b7b17` (215 passed, 3 skipped, 2 failed across the 16
nearest suites in its environment - the two failures are
`test_offline.py`'s Whisper-availability assertions with faster-whisper
absent, failing identically on `71ada52`; all 13 F6 tests passed) and
confirmed the normal-path efficiency (zero index writes for state-only
saves, one update for changed text, flat per-save cost at 500 / 5,000 /
50,000; at 50,000: 9.1 ms → 0.085 ms state-only, 9.1 → 0.12 ms text
change, its Linux runtime). It reproduced two recovery gaps, both
reproduced here on `26b7b17` before any edit:

- **A - wrong association accepted after rowid re-use.** New build indexes
  A ("albatross") at row 1, map A -> 1. The real `71ada52` Store (checked
  out separately) deletes A and saves B ("bramble"): FTS5 re-uses row 1
  for B; map still A -> 1. New build reopens: row count 1 = 1, highest
  rowid 1 = 1, so the old check accepted it. Editing B to "cobalt" then
  produced rows (1, B, bramble) and (2, B, cobalt), map A -> 1, B -> 2;
  search "bramble" returned B, whose transcript is "cobalt"; it survived a
  restart. Older-build round trip, not ordinary same-version use, no
  recording lost.
- **B - fallback writes never reach the index.** An existing limitation,
  present on `71ada52` too (Codex reproduced it there as well). With FTS5
  unavailable the store fell back to LIKE correctly, edits and new
  messages were found through LIKE, and after FTS5 returned the index and
  map still agreed with each other, so reconciliation was skipped: search
  found "obsolete" for A (transcript "replacement") and nothing for
  "replacement" or "juniper".
- Found while reproducing B here: `CREATE VIRTUAL TABLE IF NOT EXISTS ...
  USING fts5` is accepted for a table that already exists whether or not
  the module is loadable, so on an existing database without FTS5 the
  store believed it had FTS5 and the first save raised "no such module:
  fts5" instead of falling back. (Codex's reproduction failed the schema
  step itself, as a new database would.)

## Identifiers

| | |
|---|---|
| Base | `26b7b17` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section (`git log -1 -- tests/test_alpha5_search_recovery.py`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging / logo / dependency locking / F7 | nothing dispatched, retried, created, moved, published or changed |

## Repairs (mine, on Codex's recommendation; smallest forms I found)

- **A, at start:** `_fts_index_consistent()` replaces the count-and-
  highest-rowid comparison: one pass over the index LEFT JOINed to the map
  on rowid, requiring every row's map entry to name the same message id,
  and the map to hold nothing more (`total == matched == mapped`). Measured
  here: 29 ms at 50,000 rows, once per start (the old check
  was 9.5 ms). Inconsistent → the existing transactional `_reconcile_fts()`
  (adopt by id, drop orphans, correct text) before the index is used.
- **A, per operation:** `_mapped_row()` reads the row's `id` together with
  its text and returns it only if it is this message's. Another message's
  row → the map is repaired on the spot (this id's entry dropped, the
  row's real owner mapped with `INSERT OR REPLACE`) and the caller takes
  its "no valid entry" path, which clears any row still carrying this id
  (the one scan, on that repair path only) before inserting. So neither a
  save nor a deletion can update or remove another message's entry through
  a stale map. The healthy path reads one extra column and nothing else.
- **B:** a durable stale mark. With FTS5 unavailable, `save_transmission`
  and `_unindex_fts` (retention and permanent deletion) write
  `meta('fts_stale', '1')` inside the same transaction as the row. On a
  start with FTS5, the mark - like a missing layout mark or an inconsistent
  map - triggers `_reconcile_fts()`; the mark is deleted in the same
  transaction as the reconcile and the layout mark, so a failure rolls all
  of it back, keeps the mark, disables indexed search for that process
  (LIKE) and retries at the next start. Authoritative rows are never
  written by the repair.
- **Availability probe:** after the schema script, `SELECT rowid FROM
  transmissions_fts LIMIT 0` - the first statement that actually needs
  the module - inside the same try, so an existing index whose module is
  missing enters the fallback (and marks stale) instead of raising on the
  first save.
- Not done: no rebuild on healthy starts (the healthy start still writes
  nothing to the index); no change to Store search semantics, removal
  semantics, F7, dependencies, logo, packaging, workflows. Running an
  older application version against a newer database remains unsupported;
  what is established is that derived search data recovers from it.

## Tests (`tests/test_alpha5_search_recovery.py`, 8)

The layout-1 build's index maintenance is reproduced in SQL, statement for
statement, from `babelfishr/storage.py` at `71ada52` (`OldBuild` in the
test file, provenance in its docstring); no git history is needed at test
time. FTS5 unavailability is produced at the connection boundary only, two
ways: `NoFTS5AtInit` (the schema script fails, as for a new database -
Codex's boundary) and `NoFTS5` (the schema step is accepted for an existing
table and every statement touching it fails "no such module: fts5" - what
SQLite does). Everything after is production code.

- A: the exact sequence; reopening establishes B -> 1; editing B leaves
  one row (1, B, cobalt), "bramble" gone; correct after another restart;
  A stays deleted and tombstoned.
- The per-operation guard, with a deliberately swapped map (labelled as
  such: not the A sequence): P's save does not overwrite Q's row, Q's
  deletion does not remove P's.
- B: on an already-marked layout-2 database, edit, create, clear all
  searchable text, and delete while unavailable (mark asserted after the
  first save); after FTS5 returns, results agree with the rows (A found by
  "replacement" not "obsolete", B found, C not found by anything, D gone
  and tombstoned), map equals the index, and the following start writes
  nothing. Deletion-only variant. Codex's initialisation-failure sequence.
- The existing-table/missing-module detection (falls back; saves succeed).
- Recovery failure (a raising `_reconcile_fts` after the stale mark was
  set): mark kept, index left as it was, `fts_enabled` False, LIKE search
  answers from the rows, `transmissions` byte-identical before and after;
  the next start completes it.
- A healthy start reads the index and writes nothing.

Substitutions: the two connection classes above at `sqlite3.connect`; the
raising `_reconcile_fts`; nothing else (mock engines are not involved).

## Evidence

| Check | Result |
|---|---|
| Reproductions on `26b7b17`, before editing | A: with the real `71ada52` Store in a separate checkout - FTS `(1, B, bramble)`, map `A -> 1`, check True; after editing B: rows `(1, B, bramble)`, `(2, B, cobalt)`, map `A -> 1, B -> 2`, "bramble" → B, survives restart. B: reproduced at the connection boundary (`NoFTS5AtInit`): after FTS5 returned, "replacement" → [], "juniper" → [], "obsolete" → A. At the other boundary (`NoFTS5`, existing table) `26b7b17` raised "no such module: fts5" from the first save instead of falling back. |
| After | A: reopening gives map `B -> 1`; editing B leaves `(1, B, cobalt)` only; "bramble" → []; same after restart. B: stale mark set by the first fallback save; after FTS5 returns: "replacement" → A, "juniper" → B, "obsolete" → [], mark cleared. Missing module on an existing table → fallback, saves succeed. |
| Fail-before (storage.py from `26b7b17`, the 8 new tests) | 8 of 8 fail. Behavioural: A (stale association accepted), the guard (Q's entry lost), B at both boundaries ("the edit made during fallback is not searchable"; "no such module" raised from save). Not behavioural proof: the healthy-start test fails on the missing `_fts_index_consistent` attribute; the recovery-failure test fails at the missing-module boundary before reaching the recovery. |
| Reversals (one at a time, named test, byte-restored) | RA1 count-and-highest-rowid check restored → caught (A sequence). RA2 mapped row trusted without checking its id → caught (guard). RB1 no stale mark on fallback saves → caught (B, mark asserted after the first save). RB1b no stale mark on fallback deletions → caught (deletion-only test). RB2 failed recovery clears the mark → caught. RB3 no availability probe → caught. Two earlier reversal attempts (RB1/RB1b against the combined B test) were masked by the other write path marking stale; the tests were split so each path is proved on its own. None survived. |
| New tests | 8 passed (21 with the 13 F6 tests) |
| Focused (17 suites: search recovery, search index, storage, both migrations, message removal, shared recordings, acceptance, pipeline, offline, offline integration, filtered views, view reconciliation, shutdown, cleanup, lifecycle, boundary) | 228 passed, 0 skipped, 0 failed (faster-whisper is installed here, so `test_offline.py` passes) |
| Full suite | 966 passed, 11 skipped, 0 failed, 207 s |
| Skips (11) | 2 QtMultimedia not installed; 1 CoreAudio needs macOS; 1 PlistBuddy macOS only; 5 no prepared Whisper model; 2 no Argos language pack |
| `git diff --check`, `compileall` | clean |
| Normal path preserved | the 13 F6 tests unchanged and green: zero index writes for state-only saves with `_writes` advancing, one rowid UPDATE for changed text, rowid deletes, no per-save scan; healthy start writes nothing (new test) |
| Cost of the new start-up check | 29 ms at 50,000 rows (old check 9.5 ms); a healthy open at 50,000 rows, everything included, 137 ms here. Not a rebuild; not Mac timings. |

Environment: Linux container, Python 3.11.15, SQLite 3.45.1 with FTS5, offscreen Qt, temporary databases and recordings; nothing of Eric's touched. Existing tests changed: none.

## Ledger after the F6 follow-up

- F6 normal maintenance confirmed by Codex; recovery gaps A and B closed
  here (B was an existing limitation, now addressed); the older-build
  round trip is still not a supported way to run the application.
- Everything else as in the F6 ledger above: F1/F2/F5 preserved; F3/F4
  repaired, Mac pending; F7 pending, F8/F9 deferred; product decisions
  (exactly-five-second playback, seeking, natural-completion collapse,
  removal policy, General, forced cancellation/exit) open; logo pending;
  run 21 the latest candidate and it predates these repairs; Eric's
  acoustic radio test recorded; electrical radio/PTT/USB, SDR, RF
  metadata, transmitter identification unverified. Shutdown limitations
  as recorded (no forced-cancellation policy; CLI close without a timeout
  can wait indefinitely; Start/mode-change bounded synchronous paths;
  unreadable retained analysis records prevent file deletion; "fraction of
  a second" not a measured bound). Anchoring transient of up to two
  event-loop turns in a full view, offscreen only. QtMultimedia signal
  order unverified here.

---

# F7 first pass — playback notification and history-layout work

Codex's independent verification of `1f25c1b` closed F6 within its tested
scope (117 tests; the original delete-A/create-B sequence with the real
`71ada52` Store; interrupted recovery rolls back and retries; a healthy open
traced from connection creation with zero index/map writes; FTS5 module
unregistered through the public SQLite API confirms the availability
probe). It then measured F7 on the same commit, with 500 real bubbles and
the scripted backend: loading = 1,000 insertWidget + 500 removeWidget;
one controller change = 1,000 `_render_playback` calls (two per bubble);
a real Play click = 1,000 renders; one backend position event = 500 bubble
callbacks though one recording plays. Sources: every bubble connected the
controller's `changed` to its own `_render_playback` while the view's
`_on_playback_changed()` rendered every bubble again; every bubble received
`positionChanged`; `append_older()` inserted at the top, removed and
re-inserted at the bottom.

## Identifiers

| | |
|---|---|
| Base | `1f25c1b` — verified equal to the remote tip at the start, worktree clean |
| Commit | the commit carrying this section (`git log -1 -- tests/test_alpha5_playback_routing.py`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Not done | history cap, pagination, virtualization, removal/General/playback policies, F8/F9, logo, packaging, workflow, tag, release |

## Design (mine, on Codex's scope)

- `PlaybackController.changed` is now `Signal(object)` carrying a frozenset
  of the recordings a change concerns: `play()` names the previous owner
  and the new one; `_fail()` the failed request; `_retire()` the owner
  that stopped (Stop, natural end, error); a backend state change the
  owner. `positionChanged` already carried the owner.
- `TimelineView.set_playback(controller)` is the one place the view is
  wired to a controller (`changed` → `_on_playback_changed(affected)`,
  `positionChanged` → `_on_playback_position`). The view redraws only the
  affected bubbles, inside `_anchored()` as before, and hands a position
  report to the owner's bubble alone. Bubbles created by the view are
  constructed with `follow_player=False` and listen to nothing; a
  `TransmissionBubble` built on its own keeps `follow_player=True`, listens
  for itself and redraws when its id is named. Cleared or removed bubbles
  are simply absent from `_bubbles`, so nothing is routed to them.
- `append_older()` → `_insert(len(self._order), tx)`: one anchored
  insertion in the final place. Layout invariant kept: bubbles contiguous
  from index 0, then the hidden empty-state label, then the stretch
  (checked by the insertion test).
- Chosen over alternatives: keeping per-bubble listeners with an id filter
  (still one callback per bubble per change - the all-bubble dispatch
  hidden behind an early return); or dropping only one of the two render
  paths (leaves the other scaling with N).

## Measurements (`probe_f7.py`: fresh process per label and size; real TimelineView, real QTest clicks, scripted backend; `_render_playback` and `_on_playback_position` counted at class level; layout `insertWidget`/`removeWidget` counted; deferred deletes processed between loads so counts are for live bubbles; timings are single probes in this container and only their shape is claimed)

| | baseline 500 | repaired 500 | baseline 5,000 | repaired 5,000 |
|---|---|---|---|---|
| load, 3 runs (s) | 3.61 / 1.89 / 1.92 | 2.03 / 1.74 / 1.74 | 59.6 / 48.1 / 48.4 | 56.6 / 45.2 / 44.1 |
| layout ops per load | 1,000 insertWidget + 500 removeWidget | **500 insertWidget + 0 removeWidget** | 10,000 + 5,000 | **5,000 + 0** |
| renders during load (one per bubble, legitimate) | 500 | 500 | 5,000 | 5,000 |
| `clear()` (s) | 2.3 | 2.0 | **198** | **175** |
| one `changed` notification: renders | 1,000 (13.8 ms) | **1** (0.17 ms) | 10,000 (136 ms) | **1** (0.18 ms) |
| real Play click: renders / position callbacks | 1,000 / 500 (122 ms) | **1 / 1** (98 ms) | 10,000 / 5,000 (1,393 ms) | **1 / 1** (1,348 ms) |
| one backend position event: callbacks | 500 (1.6 ms) | **1** (0.11 ms) | 5,000 (19 ms) | **1** (0.12 ms) |
| Pause click: renders | 1,000 (15 ms) | **1** (1.0 ms) | 10,000 (196 ms) | **1** (9.7 ms) |
| resume click: renders | 1,000 (67 ms) | **1** (54 ms) | 10,000 (1,145 ms) | **1** (912 ms) |
| takeover click: renders / position callbacks | 2,000 / 500 (83 ms) | **3 / 1** (62 ms) | 20,000 / 5,000 (1,378 ms) | **3 / 1** (1,021 ms) |
| Stop click: renders | 1,000 (126 ms) | **1** (138 ms) | 10,000 (1,591 ms) | **1** (1,434 ms) |
| RSS before → after load (MB) | 90 → 216 | 90 → 215 | 90 → 1,320 | 90 → 1,315 |

Reading: dispatch work is now proportional to the recordings affected
(1 to 3 redraws, 1 position callback) at every thread size. The wall time
of a Play, resume, takeover or Stop click did not fall in proportion: at
5,000 bubbles it stays around 1-1.4 s, and that time is Qt relaying out
the one QVBoxLayout that holds every bubble when a bubble's height
changes (controls expanding or collapsing; a Pause, which changes no
height, dropped from 196 ms to 10 ms). Loading is dominated by widget
construction and first layout (~9 ms per bubble here), not by the removed
remove/re-insert cycle. `clear()` at 5,000 bubbles takes about three
minutes in both builds: one `removeWidget` per bubble on a 5,000-item
layout plus 5,000 deferred deletions - untouched by this pass and recorded
below. Memory is ~0.25 MB per bubble (RSS of a fresh process, before and
after loading; not high-water marks).

## Tests (`tests/test_alpha5_playback_routing.py`, 11)

Real Qt offscreen, real QTest clicks, scripted backend. Render and
position callbacks are counted per bubble by wrapping the bubble's slots at
class level, so an all-bubble dispatch would be counted even if it did
nothing. Play/Pause/resume/Stop keep controller, backend and every bubble's
visible controls in agreement and redraw the owner only (the load-time
duration report is one legitimate position callback to the owner);
takeover redraws the previous and new owner, each at most twice (the
backend stop, then the takeover); B's missing file redraws B only while A
keeps playing at 700 ms, and the retry after the file appears clears B's
error; a late duration report, five ticks and natural completion reach the
owner only; the same work at 6 and at 240 bubbles (equal totals and the
same affected set); cleared and removed bubbles receive nothing and a
switched thread still plays; a standalone bubble still follows its
controller (and hears every report, as before, being alone); a transcript
update mid-playback keeps position and controls; history is inserted once
per record at its final index (50 inserts, 0 removes, indices 0..49),
final order, membership and layout invariant checked, ties by id, an
existing record updated not duplicated, empty state restored; the reading
position measured in pixels (±1) holds through a record placed above, a
bubble growing above, and playback controls expanding and collapsing
above. Substitutions: the scripted backend; nothing else.

Existing tests changed: the three helpers that swapped in a scripted
controller by hand (`test_alpha5_playback.make_view`,
`test_alpha5_message_removal`, `test_alpha5_session_removal`) now call
`view.set_playback(controller)`, because the hand wiring reconnected only
`changed` and the view now also routes `positionChanged`. No assertion
changed.

## Evidence

| Check | Result |
|---|---|
| Reproduced before editing (500 bubbles, this container) | loading 1,000 insertWidget + 500 removeWidget; one change = 1,000 renders; Play click = 1,000 renders + 500 position callbacks; one position event = 500 callbacks - as Codex measured |
| Fail-before (`timeline.py`, `playback.py` and the `make_view` helper from `1f25c1b`) | 8 of 10 fail (all the routing, thread-size, lifecycle, standalone-count and insertion tests, on their work counts or on the old zero-argument `changed`); the two that pass are guards: a transcript update mid-playback, and the pixel anchoring, which the old code also kept |
| Reversals (one at a time, named test, files byte-restored) | RV1 bubbles in the view listen for themselves again → caught (work grew 36→1,440 renders); RV2 view redraws every bubble → caught; RV3 position reports to every bubble → caught; RV4 append_older via add-then-move → caught (100 inserts, 50 removes); RV5 playback redraw outside the anchoring → caught (pixel test); RV6 takeover names only the new owner → survived against the scripted backend (its synchronous stop report covered the previous owner) and is **caught** by the added asynchronous-stop test; RV7 a failed request names nobody → caught. None survive. |
| New tests | 11 passed |
| Focused (14 suites: routing, playback, ownership, resume, filtered views, view reconciliation, message removal, session removal, alpha-3 repairs (standalone bubbles), shutdown, cleanup, lifecycle, boundary, thread/sessions) | 174 passed, 2 skipped |
| Full suite | 976 passed, 11 skipped, 0 failed, 205 s |
| Skips (11) | 2 QtMultimedia not installed; 1 CoreAudio needs macOS; 1 PlistBuddy macOS only; 5 no prepared Whisper model; 2 no Argos language pack |
| `git diff --check`, `compileall` | clean |

Environment: Linux container, Python 3.11.15, PySide6 Essentials (no QtMultimedia), offscreen Qt, scripted backend, temporary files; nothing of Eric's touched; not a Mac.

## What remains of F7 (ledger)

- One QVBoxLayout holds every bubble: a height change anywhere (controls
  expanding or collapsing, a transcript growing) still costs one relayout
  of the whole container - about 100 ms at 500 bubbles and 1-1.4 s at 5,000 in this container, for a Play, resume, takeover or Stop - and so does each anchored
  correction. That is the remaining per-action cost at large thread sizes
  and it is Qt layout work, not our dispatch; reducing it means fewer
  widgets in the layout (a cap, pagination or virtualization), which is a
  product decision not taken here.
- Memory: ~0.25 MB of RSS per bubble here (90 → 215 MB for 500, 90 → 1,315 MB for 5,000, fresh processes), unchanged by this pass. Widgets are heavy; a thread that grows without bound
  grows without bound. This pass does not establish an acceptable
  capacity or approve a history cap.
- Loading time is dominated by widget construction and first layout, not
  by the removed remove/re-insert cycle (see the table).
- `clear()` - leaving or switching a thread - takes about three minutes at
  5,000 bubbles in both builds (one `removeWidget` per bubble on the full
  layout plus the deferred deletions). Not touched here; a candidate for
  the next F7 pass, and further reason the widget count itself is the
  remaining question.
- The controller does not correlate a backend state report with the
  recording it belongs to: a stop report arriving *after* a takeover
  (possible if a backend reports asynchronously) would retire the new
  owner. The scripted backend reports synchronously; QtMultimedia's actual
  ordering is unverified here. Recorded, not changed.
- Long-session memory and broader history-loading costs remain separate
  questions.

---

# SDR receiver integration: SDR++ receives, DSD-neo decodes, BabelFishR records

Eric's requirements: recognise the connected RTL-SDR Blog V3 once its
software is installed; a conventional receiver window for band/frequency;
Start monitoring then records, transcribes and translates; digital
demodulation and voice decoding; reuse existing components; no Terminal.
Codex's proposed design (adopted, as an architecture to prove, not a proven
integration): SDR++ is the receiver window and owns the USB receiver; its
network audio sink and rigctl server are the connection; DSD-neo decodes
digital voice; BabelFishR manages the connection into its own capture
pipeline.

## Identifiers

| | |
|---|---|
| Base | `d24aa10` (the F7 first pass, complete and pushed) — verified equal to the remote tip, worktree clean |
| Commit | the commit carrying this section (`git log -1 -- babelfishr/receiver`) |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging | nothing dispatched, tagged or published; the .app spec is unchanged (nothing new is bundled) |

## Component contract, pinned (babelfishr/receiver/contract.py)

- **SDR++** master, files read 2026-09-11 (raw.githubusercontent.com; the
  commit id of master could not be read - api.github.com and the Releases
  pages are blocked here): `sink_modules/network_sink/src/main.cpp` -
  config in `<root>/network_sink_config.json` keyed by *stream* name, keys
  hostname/port/protocol(TCP=0, UDP=1)/sampleRate/stereo/listening; TCP mode
  listens and accepts ONE client; int16 LE, mono unless stereo, raw PCM, no
  framing; `if (startNow) startServer()` when listening is true.
  `misc_modules/rigctl_server/src/main.cpp` - `<root>/rigctl_server_config.json`
  keyed by instance name, keys host/port(4532)/tuning/recording/autoStart/vfo/
  recorder; `postInit: if (autoStart) startServer()`; commands F, f, M, m,
  V, v, \start, \stop, q, \dump_state; `F` tunes the named VFO without
  checking whether the SDR is started. `core/src/core.cpp` defConfig:
  moduleInstances[name] = {module, enabled}; streams["Radio"] = {muted, sink,
  volume}; `command_args.cpp`: `--root` default `$HOME/Library/Application
  Support/sdrpp` (macOS bundle) / `$HOME/.config/sdrpp` (Linux); `--autostart`.
- **dsd-neo** `630a123e2d7dd52d66b8444314049c50d904a41e` (project version
  2.9.0), built here from source with mbelib-neo
  `be5992dab7589aec6f3a45fa1881139c0caa2a97` (2.1.0): `-i tcp:host:port` (a
  client of the PCM producer), `-s 48000`, `-o -` = s16le 8000 Hz 2 channels
  (left slot 1, right slot 2 - **observed**, the channels differ), `-w`,
  `-P -7 <dir>` per-call WAVs (observed names
  `20260911_061358_37692_DMR_CC_2__TGT_0_SRC_0.wav`), `-V <slot>`, events on
  stderr (`Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1`). Observed:
  connection refused → exit 0 at once; producer closing → dsd-neo stays up
  until terminated. `--iq-replay <fixture.iq.json>` replays the project's
  IQ fixtures (cu8, 48 kHz, tuner 851.375 MHz).
- **Stream formats verified explicitly**: SDR++ sink → 48 000 Hz mono s16le;
  dsd-neo out → 8 000 Hz stereo s16le (one channel taken); analog recordings
  are 48 kHz mono WAV, digital recordings 8 kHz mono WAV (checked in tests).

## What was run against the real decoder (this container, no receiver)

dsd-neo's own IQ fixtures (GPL-3 project test data; not copied into this
repository) through two paths:

| Fixture | `--iq-replay` (IQ path) | FM-discriminated to 48 kHz audio, `-i file.wav` (the network-sink shape) | `-i tcp:` from a Python TCP producer (the SDR++ sink shape) |
|---|---|---|---|
| dmr_voice.iq (2 s), `-fs` | DMR sync, VC frames, 0.42 s decoded 8 kHz stereo (rms 0.059) | DMR sync, 0.96 s decoded (rms 0.051) | same 0.96 s, on stdout (30 720 bytes = 0.96 s stereo 8 kHz), stderr events, one connect |
| p25p1_c4fm_vc.iq (3 s), `-f1` | not run | P25p1 sync, 1.44 s decoded (rms 0.063); `LDU2 ALG ID: 0x80` (clear) | not run |
| noise_floor.iq (10 s), `-fa` | not run | **no decode: 44-byte WAV** | not run |

Then the full software path: fake SDR++ (rigctl + TCP sink) looping the
FM-discriminated DMR fixture → real dsd-neo (`-i tcp ... -fs -V 1 -o -`) →
`DecodedVoiceSource` → CaptureService → recording → mock transcription:
`test_real_dsd_neo_decodes_a_digital_fixture_into_speech_for_the_pipeline`
passes (recording 8 kHz mono, >2 s, rms > 0.01, transcript present,
protocol DMR, colour code 02, SDR provenance). The looped fixture decodes
as one continuous call (detector hang time 0.8 s), so the transmission
closes when monitoring stops; real traffic has gaps.

Resource use over an 8 s digital run here (from /proc, one sample):
dsd-neo 1.2 s CPU, 29 MB RSS; the fake SDR++ 0.06 s, 12 MB; the BabelFishR
process (no Qt) 0.45 s, 31 MB. SDR++ itself, faster-whisper and Argos were
not running; a full-stack measurement on a Mac remains to be taken.

Protocol coverage, stated separately:
- **Demonstrated through demodulated audio (the connection used here):** DMR
  (dual-slot decoder), P25 Phase 1 C4FM.
- **Supported by dsd-neo but not tried here:** NXDN48/96, D-STAR, YSF, M17,
  dPMR, ProVoice/EDACS, X2-TDMA, P25 Phase 2 - available as presets.
- **Expected to need the IQ path, not covered by this connection:** P25
  CQPSK/LSM (simulcast) - dsd-neo's own fixtures for it are IQ.
- **Not attempted, not claimed:** scanning, trunk tracking, encrypted
  traffic (dsd-neo mutes it; events with a non-0x80 ALG ID or "ENC" set an
  `encrypted` flag on the metadata).

## Design (mine, on Codex's proposal)

- `babelfishr/receiver/`: `contract.py` (above); `sdrpp.py` - `find_sdrpp`
  (configured path → `/Applications/SDR++.app/Contents/MacOS/sdrpp` → PATH),
  `SdrppConfigurator.ensure()` (writes only the named keys in the three
  files, backs each up once as `*.before-babelfishr.json`, never overrides a
  chosen source, idempotent), `RigctlClient` (F/f/M/m/\start/\stop/q),
  `SdrppProcess` (launch with `--autostart`; terminate only what we
  launched), `rtl_sdr_present()` (macOS system_profiler / Linux sysfs;
  None when it cannot tell); `stream.py` - `PcmTcpSource` (SDR++'s TCP sink
  client, int16 → float blocks, reader thread, generation counter so
  `flush()` after a retune drops audio buffered under the old frequency),
  `DecodedVoiceSource` (spawns dsd-neo, reads its stdout, takes one slot's
  channel, parses stderr events into protocol/slot/colour code/talkgroup/
  unit/encrypted, reports `decoder-exited`), `TuningState` (requested vs
  confirmed frequency; SDR provenance only when confirmed by `f`);
  `controller.py` - `ReceiverController` (the one tuning authority:
  attach if rigctl answers, else configure + launch; tune → read back;
  \start; build the source; release the source at Stop; shutdown at Quit)
  and `receiver_status()`.
- Config: `ReceiverConfig` (sdrpp_path/root, launch, hosts/ports, sample
  rate, frequency, mode, bandwidth, digital, protocol preset, slot,
  timeout); `InputSelection.kind = "receiver"`; `Config.record_receiver_input()`.
- App: `app.receiver`; `_build_source` returns the receiver's source when
  the receiver was chosen by name (never as a fallback); `input_status()`
  state `receiver`; `stop_session` releases our consumers (SDR++ stays for
  the operator); `_close` shuts the receiver down first (SDR++ terminated
  only if we launched it).
- Window: the Audio input list offers "SDR receiver — SDR++ with the RTL-SDR"
  when SDR++ is installed or running (or was chosen before); a Receiver
  menu (Open receiver window, Tune receiver…, Receiver status…); the status
  line shows SDR++/DSD-neo state and the requested tuning; `decoder-exited`
  and `stream-error` raise the existing input warning; Field Check gains an
  "SDR receiver (SDR++ / DSD-neo)" row.
- Invariants kept: capture-first recording of the received/decoded audio;
  Session ownership, timestamps and metadata provenance unchanged; offline
  enforcement untouched (nothing here reaches the network beyond
  127.0.0.1); shutdown ordering: the receiver's processes and sockets are
  tracked and stopped before the store closes; no microphone fallback
  anywhere (an unavailable receiver raises `ReceiverUnavailable` and the
  window says so).

## Tests (`tests/test_sdr_receiver.py`, 16; stand-ins `tests/stubs/fake_sdrpp.py`, `tests/stubs/fake_dsd_stream.py`)

Configurator writes exactly the needed keys, keeps the rest, backs up once,
is idempotent, sets the RTL-SDR source only on a fresh root; `find_sdrpp`;
rigctl protocol against the fake (which reads the files we wrote and starts
its servers only if `autoStart` is true and the sink is TCP); a refused
tune is an error, nothing confirmed; the PCM stream arrives as float blocks
at 48 kHz with SDR provenance and no RSSI/SNR; a retune drops buffered
audio; SDR++ dying ends the stream with `disconnected` and nothing takes
over; without SDR++ monitoring refuses (`ReceiverUnavailable`), no session
row is left open; an SDR++ already running is attached to and left running;
analog end to end (real app, mock engines: recorded 48 kHz WAVs, transcripts,
a translation, confirmed frequency with SDR provenance, SDR++ terminated at
Quit but not at Stop); digital end to end with the fake dsd-neo (8 kHz mono
recording, protocol/talkgroup/unit metadata, slot 1, dsd-neo gone after the
run); the decoder dying is reported (`decoder-exited`) and the run stays the
operator's to stop; decoder events → metadata with clear P25 (ALG ID 0x80)
not called encrypted; the **real dsd-neo** end to end (skipped unless
`BABELFISHR_DSD_NEO` and `BABELFISHR_DSD_FIXTURE_WAV` are set - both were,
here); the window offers the receiver, Tune… saves through the real dialog,
Start monitoring starts from it, the status line shows the confirmed
frequency, Stop and close work and the launched SDR++ is gone.

Substitutions: the two stand-ins (SDR++ and, except in one test, dsd-neo),
mock engines, `QDialog.exec` for the Tune dialog. No hardware, no Mac, no
audio backend.

## Evidence

| Check | Result |
|---|---|
| New tests | 16 in `tests/test_sdr_receiver.py`: 15 pass with the stand-ins; the real-dsd-neo test passes when `BABELFISHR_DSD_NEO`/`BABELFISHR_DSD_FIXTURE_WAV` are set (done here) and is skipped otherwise |
| Nearest suites (input panel, source selection, lifecycle, shutdown, cleanup, boundary, acceptance, pipeline, analysis, dsd cli, alpha-4 metadata/startup and integration repairs, offline, offline integration) | 308 passed |
| Full suite (before the one test update below) | 991 passed, 1 failed, 12 skipped, 242 s - the failure was `test_the_menu_bar_is_drawn_inside_the_window` asserting the exact menu list `File, View, Tools, Help`; the window now has a Receiver menu |
| After updating that assertion | its file and the receiver suite: green (`git diff --check`, `compileall` clean) |
| Skips (12) | the 11 as before (QtMultimedia ×2, CoreAudio, PlistBuddy, Whisper model ×5, Argos ×2) + 1 real-dsd-neo test when the environment does not name a binary |

Existing tests changed: the menu-list assertion in `tests/test_alpha4_menu_access.py` (`test_the_menu_bar_is_drawn_inside_the_window`) gains "Receiver" between View and Tools. No other assertion changed.

Environment: Linux container, Python 3.11.15, PySide6 Essentials offscreen, no audio backend, no receiver, no display; dsd-neo 2.9.0 built from `630a123e` with mbelib-neo 2.1.0 (`be5992da`), audio backend none, terminal UI off, RTL-SDR support on (for `--iq-replay`); apt: libsndfile1-dev, librtlsdr-dev. GitHub Releases and api.github.com were blocked by the egress proxy (source files via raw.githubusercontent.com were not).

## Reported separately, as asked

- **Automated tests:** above, all green here with the stand-ins and with the
  real dsd-neo.
- **Real-component tests:** dsd-neo (built from the pinned revision) on its
  own DMR and P25 fixtures, through the IQ path and through discriminated
  audio over TCP - decoded speech reached BabelFishR and was transcribed by
  the mock engine. Not SDR++: no display, no receiver here; its behaviour is
  taken from its source only.
- **Packaged Mac tests:** none. No candidate contains this work; run 21
  predates it; nothing was built or published.
- **Eric's hardware observations:** none yet for the SDR path. His acoustic
  radio test stands (work radio speaker → laptop microphone → BabelFishR).

## Packaging, licences, installation

Nothing new is bundled. SDR++ (GPL-3.0) and dsd-neo (GPL-3.0-or-later,
portions ISC; mbelib-neo GPL-2.0-or-later) are separate installs, so no
redistribution files are added to the .app (MIT). The README tells Eric
what to download and where to put it; BabelFishR finds
`/Applications/SDR++.app` and `dsd-neo` on PATH or at the configured path.
Unverified here: the exact Release asset names (SDR++ macOS package;
dsd-neo `dsd-neo-macos-arm64-portable-<version>.dmg` per its README),
whether the SDR++ macOS build ships the network_sink and rigctl_server
modules (both are in-tree), Gatekeeper prompts for either, and whether the
RTL-SDR needs a driver step on macOS (librtlsdr is inside SDR++). Bundling
either program inside BabelFishR.app would bring GPL obligations (source
offer, licence texts) and is a decision not taken here.

## Open questions and limitations

- Which digital protocol Eric's eventual BTECH/Baofeng uses is unknown; DMR
  (the likeliest) is the one demonstrated. The IQ path (needed for CQPSK
  P25) is not wired: dsd-neo would have to own the dongle then, which
  conflicts with SDR++ owning it - a separate milestone.
- Conventional-channel monitoring is what is built. Scanning and trunk
  tracking are not; dsd-neo has them, this connection does not use them.
- Two-slot DMR: one slot per run. Transcribing both needs a second capture
  per session - not built.
- Retuning during reception: audio buffered before the retune is dropped
  (tested); a message in progress at that moment is cut where the buffer
  was, under the new frequency's metadata if it completes after the retune
  - the boundary is the retune, not a resynchronised timestamp.
- SDR++ already running keeps its own in-memory settings; our files are
  written only when we launch it. If the operator had never enabled the
  network sink or rigctl in a running SDR++, Start reports "did not answer"
  rather than reconfiguring it under them.
- The RTL-SDR USB probe uses `system_profiler` on macOS; not exercised here.
- FIELD OFFLINE: the receiver path uses only 127.0.0.1 sockets; not tested
  under the offline enforcement suite specifically beyond the existing
  suites passing.
- No CPU/RAM figures for SDR++ or the real engines alongside; container
  figures for dsd-neo and the app are above.
- The click-only installation and every operator step in checklist S are
  untested on a Mac.

---

# SDR receiver: corrections before Eric's hardware bench

Codex audited the receiver integration (items A1-A2, B4-B7, C8-C9, D10-D13)
and asked for each to be corrected at its boundary, for the real SDR++ to be
exercised where a file source made that possible, and for concrete,
click-only Mac instructions. This section records what was found, what was
changed, what ran against the real programs, and what still has not.

## Identifiers

| | |
|---|---|
| Base | `fef259b19cc54a2d0374e0328c0aa5fa0989c54f` (the receiver integration, verified equal to the remote tip, worktree clean before editing) |
| Commit | the commit carrying this section |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging | nothing dispatched, tagged or published; no packaging change; run 21 cannot test this |
| SDR++ source | `8c9f5ee8fe405775bfcd62c8c8f8c0fc928a64af`, cloned and **built** here (v1.3.0; core + file_source, network_sink, radio, rigctl_server, rtl_sdr_source) |
| dsd-neo | `630a123e` (2.9.0) built earlier here with mbelib-neo `be5992da` |

## Source inspection at the pinned revision (what Codex said, checked)

- **A1** `misc_modules/rigctl_server/src/main.cpp` 588-593: `\start` and
  `\stop` call `gui::mainWindow.setPlayState()` and write nothing back.
  `RigctlClient.start()` previously waited for a reply line. Corrected:
  `_send()` fire-and-forget; `TuningState.play_requested` records the
  request; `radio_start_confirmed` is `None` in every metadata record and
  the status dialog says "requested (SDR++ does not confirm it; arriving
  audio does)". The fake writes no reply either.
- **A2** rigctl_server reads its seven keys typed whenever the instance
  exists (defaults only for a wholly absent instance); `sink.cpp
  loadStreamConfig()` reads `sink`, `volume`, `muted` typed; `core.cpp`
  upgrades a string-valued module instance. The configurator wrote a
  five-key rigctl instance, a `streams.Radio` without `volume`/`muted`, and
  no `Radio` module instance. Corrected: all seven rigctl keys (`recording`
  false, `recorder` "" only when absent), stream `volume` 1.0 / `muted`
  false when missing, `Radio` instance added, string-form instances
  upgraded, everything else untouched. The fake's loader performs the same
  typed reads (a missing/null key is fatal, as nlohmann's typed get would
  throw); the corrections test runs it over a fresh root and over a partial
  one that fails before and loads after.
- **B4/B5** `app.stop_session` and `_close` called the receiver's cleanup
  inline; `start_session` built the source (launching SDR++) before the
  closing check. Corrected: `stop_session` only releases the source (the
  capture's stopper thread stops and settles it, one owner); `_close` step
  3b takes a `ReceiverShutdown` handle on the receiver's own thread, waits
  only when asked, returns False while unsettled, and on failure reports
  once and **returns False** (the window shows "Could not finish quitting:
  the receiver did not shut down: …" and retries; the controller keeps the
  process that did not end so the retry tries again). Closing checks come
  before `_build_source`; the window's receiver handlers check
  `_receiver_blocked()` before and after their dialogs.
- **B6** A closed rigctl port is not proof SDR++ is absent. Corrected:
  `sdrpp_processes(executable, root)` (Linux `/proc` comm/argv match with
  `--root` compared; macOS `pgrep -x sdrpp`); a running instance without
  rigctl raises `ReceiverUnavailable` naming the Module Manager steps, no
  second launch, no file rewrite (mtimes checked in the test). One rigctl
  connection is held and reused (upstream serves one client at a time -
  confirmed on the real SDR++: a second client connects at TCP level and
  gets no reply while the first is open).
- **B7** `receiver_status()` ran `system_profiler`/`dsd-neo --version`
  inline. Corrected: `Discovery` runs the slow probes on a thread with a
  30 s cache; snapshots return at once with `usb_probed`; four facts kept
  apart (software found / USB detected / control connected / audio
  receiving, the last from the reader's arrivals, not synthesised silence);
  `LOOPBACK_HOSTS` enforced unless `allow_remote_receiver`.
- **C8** `open_source()` tuned to the stored frequency over whatever the
  operator set in SDR++'s window. Corrected: Start reads back (`f`, `m`)
  and adopts it; the stored frequency is applied only when the operator
  asked in Tune receiver (`pending_tune`) or when we launched SDR++ with
  one; a 1 s poll follows later changes made in the SDR++ window and marks a
  boundary. Confirmed against the real SDR++: a stale 155.1 MHz in config
  lost to SDR++'s 851.375 MHz.
- **C9** A retune flushed the queue but left the detector open, so the
  transmission was labelled with the new frequency, and a read begun
  before the flush could deliver old samples under the new generation.
  Corrected: `_Reader` tags each block with the generation current *before*
  its read; `retuned()` drops the queue and enqueues a `StreamBoundary`
  carrying the ending epoch's metadata; the pipeline closes the open
  transmission under that metadata and resets the detector. rigctl `f` is
  the VFO frequency: `center_frequency_hz` is now `None` (unknown), not the
  VFO value.
- **D10** Slot 2 events replaced slot 1 identifiers; the encrypted flag
  stuck. Corrected: `CallTracker` keeps calls per slot, attaches
  identifiers only to the active slot's current call (2 s hold), clears
  the flag on ALG ID 0x80, starts every call clean.
- **D11** `_Reader` derived time from the sample count while dsd-neo emits
  audio only during voice. Corrected: the decoded source keeps wall-clock
  time and fills idle gaps with zero blocks (`IDLE_FILL_SECONDS`), so two
  calls 1.2 s apart are two transmissions and an idle decoder does not end
  the stream. The fake decoder now emits nothing for quiet input.
- **D12** Producer loss with dsd-neo alive was invisible. Corrected:
  dsd-neo's own stderr lines drive `upstream-lost` / `upstream-restored`
  (window warnings). Read further in `dsd_symbol.c symbol_read_sample_tcp()`:
  after ONE failed retry it prints "Connection to TCP Server Disconnected."
  and opens its own audio input (`symbol_open_pulse_input_and_reconfigure_output`)
  - a sound device, when it has one. From then on its output is not the
  receiver. Corrected: that line ends the stream (`receiver-lost`), stops
  dsd-neo, and the reader enqueues nothing more. The fake decoder plays
  this part with a loud tone after "Disconnected."; the test shows none of
  it is recorded. Also read: dsd-neo's TCP input carries a 1.5 s receive
  timeout (`dsd_rigctl.c Connect()`), so SDR++ pausing its radio for longer
  than that counts as a loss to dsd-neo.
- **D13** Unbounded PCM queues. Corrected: `QUEUE_SECONDS` (10 s) bound,
  drop-oldest with `dropped_frames` in the metadata and `audio-dropped` /
  `audio-resumed` reports.

## Real-component runs (this container: Linux, Xvfb, no RTL-SDR, no Mac)

SDR++ v1.3.0 built from `8c9f5ee8` (apt: libglfw3-dev, libfftw3-dev,
libvolk-dev, libzstd-dev, librtlsdr-dev …), launched headless
(`xvfb-run … sdrpp --root <root> --autostart`) on a root written by our
`SdrppConfigurator.ensure()` plus `"source": "File Source"` and a
`file_source_config.json` naming a 20 s IQ WAV made from dsd-neo's
`dmr_voice.iq` fixture (48 kHz, named `baseband_851375000Hz_…wav` so the
File Source takes 851.375 MHz as centre). Then:

| Step | Result |
|---|---|
| Configured root loads | rigctl 4632 and network sink 7455 listening within 1 s; modules File Source, Network Sink, RTL-SDR Source, Radio, Rigctl Server initialised |
| `RigctlClient`: `f`, `m`, `F 851375000`, `M FM 12500`, read-back | 851375000.0, ('FM', 12500) - confirmed; startup VFO was SDR++'s own 851399000 WFM 150000 (its default), and after our tune SDR++ persisted 851.375 FM 12500 into its own config across a restart |
| `\start` | returned in 0.000 s, no reply (A1 confirmed); a second rigctl client connected but received no reply while the first was open (single client confirmed) |
| Audio after `--autostart` | **none**; `\start` alone: none (already "playing"); `\stop` then `\start`: 5.4 MB in 2 s. Cause read in `file_source/src/main.cpp`: the WAV reader is created when the source menu first renders, so the autostart's `start()` found `reader == NULL`. A File-Source quirk; says nothing about the RTL-SDR source |
| Network sink to a raw client | int16 mono, ~24x real time (the File Source is unpaced through the network sink; `ConnClass::write` blocks, so the client paces it) |
| Real dsd-neo `-i tcp:127.0.0.1:7455 -s 48000 -fs -o -` at **FM 12500** | 1252 DMR sync lines, every frame `VOICE CACH/EMB ERR`, 0 bytes of voice; the sink's audio captured to a file: rms 0.526, **peak 1.000 (clipped)** |
| at FM 15000 (file) | some clipping (0.56 % of samples), 4.7 s voice decoded from 20 s |
| at FM 20000 (file) | no clipping, 8.4 s voice, 30 CACH errors |
| at FM 25000 (file, and live over TCP) | no clipping; live: **65 s of decoded DMR voice PCM in 25 s wall time**, Color Code 02, slots 1 and 2, 0 interruptions |
| Cross-check | an independent FM discriminator (numpy) of the same WAV → dsd-neo: 8.5 s voice, 30 CACH errors - the recording is fine; the 12.5 kHz result is SDR++'s demodulator output for this signal |
| `ReceiverController` against that SDR++ (digital, real dsd-neo) | `ensure_sdrpp` attached in 0.001 s (no launch); `open_source` adopted SDR++'s 851.375 FM 25000 over a stale 155.1 MHz in config (C8); `DecodedVoiceSource` delivered 14.9 s of voice blocks plus dsd-neo's own quiet output in 30 s, first voice 0.3 s after start; metadata: tuned 851375000.0, FM, SDR provenance, protocol DMR, colour code 02, 1575 sync lines; `tune(+12.5 kHz)` produced a `StreamBoundary` carrying 851375000.0 / epoch 0, then metadata 851387500.0 / epoch 1 (C9); `release_source` 0.000 s; `stop`+`settle` 0.42 s, exit 0; `begin_shutdown` 0.000 s, settled without error; SDR++ left running (attached) |
| `ReceiverController` analog (`PcmTcpSource`) | 86 s of 48 kHz audio in 3 s wall (unpaced source), 0 dropped, metadata tuned 851375000.0 FM bw 25000, `center_frequency_hz` None |
| `sdrpp_processes()` | found the real process by comm and by `--root`; `[]` for another root |
| Real dsd-neo through the fake SDR++ (existing test, env set) | passes |

Talkgroup/unit identifiers: the real dsd-neo printed no `TGT=`/`SRC=` lines
for this recording (`SLCO CRC ERR` on the embedded LC), so those fields
stayed empty; the fake prints them, which is why the stand-in tests can
assert them. Real traffic may or may not carry them.

Consequence in the product: Tune receiver has a **Filter width** choice
(12.5 / 15 / 20 / 25 kHz); ticking Digital voice moves 12.5 → 20 kHz
(`SDRPP.digital_voice_bandwidth_hz`, evidence in `contract.py`). One
recording, one protocol; nothing is claimed for the other presets.

## Tests

- `tests/test_sdr_receiver_corrections.py` (18): one or more per item
  above, plus the window guards and the filter-width default. Run against
  the base commit's package in a worktree (with these stand-ins and guarded
  imports): see the evidence table for the fail-before count.
- `tests/test_sdr_receiver.py` (16): configurator expectations updated to
  the complete form; the encrypted-event test adjusted (no call → nothing
  to attach to); the window test records `QMessageBox.critical` instead of
  blocking on it. 15 pass, 1 skipped unless `BABELFISHR_DSD_NEO` and
  `BABELFISHR_DSD_FIXTURE_WAV` are set (set here: passes).
- `tests/test_alpha4_menu_access.py`: the Tools-menu failure was the test
  holding a `QMenu` from a temporary `QAction` expression (binding
  lifetime); the test now keeps the action; the menu checks are unchanged.
- Stand-ins: `fake_sdrpp.py` gains typed loading, no reply to `\start`/`\stop`,
  one rigctl client at a time, `FAKE_SDRPP_DROP_AUDIO_AFTER`,
  `FAKE_SDRPP_AUDIO_STOP_AFTER`, `FAKE_SDRPP_NO_RIGCTL`,
  `FAKE_SDRPP_TUNE_FILE` (the operator's dial); `fake_dsd_stream.py` is
  silent for quiet input, prints dsd-neo's loss/retry lines, gives TCP up
  after one failed retry and then plays a tone (its "own input"),
  `FAKE_DSD_SIGTERM_DELAY`.

## Evidence

| Check | Result |
|---|---|
| `tests/test_sdr_receiver_corrections.py` | 18 passed (108 s) |
| The same tests against the base commit `fef259b` in a worktree, with the new stand-ins and guarded imports, one test per process with a 150 s limit | run 1 (stand-ins as committed): 18/18 fail - 17 within 0.5 s because the baseline's incomplete config files end the typed-reading fake at once (item A2 alone), the window test ended without a summary line. Run 2 (fake made lenient about missing keys): 18/18 fail - most after the 3 s rigctl timeout, because the baseline client waits for a reply to `\start` that the fake, like SDR++, never writes (item A1 alone). Run 3 (fake also replying `RPRT 0` to `\start`/`\stop`, so A1 and A2 are out of the way): 18/18 still fail, each on its own item - e.g. B4 "Stop waited 1.52 s on the receiver's processes", C8 "Start put the stored 155.1 MHz back over the operator's 162.55 MHz", D12/D13/C9 on missing boundary/queue/abandonment behaviour, D10 on slot 2 replacing slot 1 |
| `tests/test_sdr_receiver.py` + `tests/test_alpha4_menu_access.py` | 23 passed, 1 skipped (the real-dsd-neo test without its env) |
| The real-dsd-neo test with `BABELFISHR_DSD_NEO`/`BABELFISHR_DSD_FIXTURE_WAV` set | 1 passed (10.5 s) - the real decoder ran |
| Full suite, nothing else running | **1010 passed, 12 skipped, 0 failed**, 248 s |
| Full suite while the baseline loop was also running | 1 failure, `test_the_pcm_stream_arrives_as_float_blocks_at_the_sink_rate_and_stops_cleanly` ("only silence arrived"): it reads 4 s of the fake's real-time stream whose first 1.6 s are quiet, and under that load fell short; it passed alone twice and in the quiet full run. Pre-existing timing sensitivity, not a regression |
| Skips (12) | as before: QtMultimedia ×2, CoreAudio, PlistBuddy, Whisper model ×5, Argos ×2, plus the real-dsd-neo test when its env is unset |
| `git diff --check`, `compileall` | clean |

Environment: Linux container, Python 3.11, PySide6 offscreen, Xvfb for SDR++, no audio backend, no receiver, no Mac. Mock ASR/translation throughout; nothing here is recognition.

## Mac setup and bench (for Eric)

README "SDR receiver" and checklist section S: SDR++ from its GitHub
Releases into Applications (or **Receiver › Choose SDR++ application…**),
dsd-neo from its Releases `.dmg` and **Receiver › Choose DSD-neo program…**
- no PATH, no Terminal. Release pages and api.github.com are blocked from
this container, so the asset names are the projects' documented ones, not
downloaded ones; the checklist says to note any difference. Initial
reception with an existing signal (FM broadcast in WFM, NOAA weather in
FM), no handheld assumed, no protocol assumed for a future BTECH/Baofeng.
The stale footer ("every step uses microphone audio") is corrected.

## Limitations (exact)

- No RTL-SDR, no over-the-air signal, no macOS SDR++ binary was used. The
  `--autostart` finding is about the File Source; the RTL-SDR source opens
  its device in its own start path (not run here).
- The File Source is unpaced, so the timing of the real runs is not the
  timing of live reception; the boundary and lifecycle checks do not depend
  on pacing.
- dsd-neo's 1.5 s TCP receive timeout means an SDR++ whose radio is
  stopped for longer than that loses dsd-neo (now reported as receiver
  lost and stopped, never substituted); a silent-but-running channel is not
  affected because SDR++ streams silence while the radio runs.
- Filter width measured for DMR only; P25 and the rest untested through
  SDR++'s demodulator.
- Mock ASR/translation in every automated run: transcripts there are mock
  output, not recognition.
- Run 21 cannot test any of this; the next Mac candidate must be built from
  this commit and remain non-publishing. No tag or release was created or
  moved.

---

# SDR receiver: the remaining transition faults, closed

Codex reproduced six faults on `5bfa6a3` (its environment: Linux, Python
3.12, PySide6 6.8.3 offscreen, stand-ins, no radio, no Mac) and asked for
each to be closed at its boundary with fail-before/pass-after tests. This
section records the reproductions, the changes, what ran against the real
decoder, and what is still unknown.

## Identifiers

| | |
|---|---|
| Base | `5bfa6a340bb575581c4109587bf679df11d35b7d` (verified equal to the remote tip, worktree clean before editing) |
| Commit | the commit carrying this section |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging | nothing dispatched, tagged or published; no packaging change; run 21 cannot test this |

## Faults, causes and changes

- **A1 (input identity).** Codex fed zero PCM, stopped the fake sink for
  good, held the handling of "Connection to TCP Server Disconnected." for
  1.2 s while stdout reading went on, and the fake decoder's substitute tone
  was saved as a 1.46 s SDR++/DSD-neo recording. Read in
  `dsd_symbol.c symbol_read_sample_tcp()`: dsd-neo opens its alternate audio
  input *before* printing that line, so no diagnostic handling can prevent
  the transition. **Change:** BabelFishR owns the SDR++ network-sink
  connection and feeds dsd-neo through stdin (`-i -`, raw s16le mono at
  `-s`, docs/cli.md line 80). `symbol_read_sample_stdin()` requests
  shutdown on end-of-file and has no other input. The pump reconnects to
  the sink for `RECONNECT_SECONDS` (5 s; dsd-neo merely waits on its pipe)
  and otherwise closes the pipe: dsd-neo ends, the stream ends
  (`receiver-lost`), nothing else is decoded. `_watch_stderr` only reports.
  `receiving` for the digital source now means PCM arriving at the pump
  (the receiver's audio), `decoding` means dsd-neo produced speech.
- **B1 (ordinary journey).** Open → the operator tunes SDR++ to 162.55 →
  Start restored the stored 155.1 because `pending_tune` stayed set from
  the launch. **Change:** the stored choice is applied *at launch*, once
  SDR++ answers (`_apply_stored_tuning`), and consumed there; Start reads
  back and adopts whatever SDR++ is tuned to; an explicit Tune receiver
  request while SDR++ runs is applied at once; `pending_tune` survives only
  when the launch-time tune failed and only for an SDR++ we launched; an
  attached SDR++ is read back, never retuned. The rigctl connection is held
  only while a run is on (SDR++ serves one client), closed with the run.
- **B2 (backdated recordings).** `detector.reset()` restarted the sample
  count while `AudioBlock.offset` stayed stream-relative, so `stream_start =
  timestamp - offset` restored the old origin and the next recording was
  dated 0.74 s after stream start again. **Change:** the detector
  re-anchors its origin on every block (`stream_start = block.timestamp -
  (samples_seen + pending)/rate`), so a reset, and the decoded path's
  wall-clock offsets with gaps, keep recordings at their true times. Stored
  `started_at`/`duration` are checked across three retunes on both paths.
- **B3 (a second boundary erased the first).** `_boundary()` cleared the
  buffer including earlier markers, and `read()` skipped anything from an
  older generation. **Change:** the buffer is a deque under a condition;
  a retune drops the *audio* of the ending epoch and keeps every marker;
  `read()` delivers markers regardless of generation; overflow drops the
  oldest audio block, never a marker; and the capture drains what the
  source still holds (audio and markers, in order) before its final flush,
  so a retune just before Stop still closes the open transmission under
  the tuning it was heard on. Two retunes before a drain, a held consumer
  and 12 s of overflow pressure are tested.
- **C1 (Stop waited on the poller's lock).** `release_source()` took the
  controller lock that `read_back()` holds across rigctl I/O; with a reply
  delayed 1.2 s, Stop took 1.2 s and the 20 ms heartbeat saw no tick.
  **Change:** a separate state lock for handoffs (never held across I/O);
  the poller is bound to a run id and to its source, and a loop whose run
  ended does nothing to the next run's source even when its read outlives
  it; `_stop_polling` never joins; the shutdown thread joins the poller
  with the rigctl timeout as bound; closing the run's rigctl socket is not
  waited on. Tested with a real window, a 20 ms heartbeat, a fake SDR++
  delaying every reply 1.2 s, two Stop rounds, a restart and Quit.
- **C2 (cleanup exceptions lost ownership).** `begin_shutdown()` took the
  resources out of the controller first; an exception from
  `process.terminate()` skipped the restoration, so the second close found
  nothing and reported done while SDR++ lived. **Change:** nothing is
  taken up front; each resource is cleared only after its own step
  succeeded (source settled, rigctl closed, process ended); an exception,
  a termination that returns with the process alive, or a settle that
  fails leaves that resource owned and the handle errored; the next
  begin_shutdown works on what is left and does not repeat what ended;
  `app.receiver_error` is shown by the window while Quit retries. Tested
  with an injected OSError, a no-op termination and a failing settle.
- **The exact-PID assertion** in
  `test_a_running_sdrpp_whose_rigctl_is_off_is_neither_duplicated_nor_rewritten`
  matched by identity or by `NSpid` (Codex's container showed another PID
  namespace's numbers in `/proc`); the refusal/no-second-launch checks are
  unchanged.

## Real decoder (this container; dsd-neo 2.9.0 built from `630a123e`)

- Fed raw s16le at 48 kHz on stdin (`-i - -s 48000 -fs -V 1 -o -`) from
  the DMR discriminator fixture: 63 sync lines, decoded speech on stdout;
  a 2.5 s pause with no data: still alive, still waiting (no fallback,
  no "Interrupted"/"Disconnected" lines); stdin closed: exit 0 after
  0.42 s, "NOTICE: Exiting." on stderr.
- `test_real_dsd_neo_decodes_from_stdin_and_ends_on_end_of_file` (skipped
  without `BABELFISHR_DSD_NEO`/`BABELFISHR_DSD_FIXTURE_WAV`; run here):
  through `DecodedVoiceSource` with the fake sink stopping for good after
  4 s - speech blocks arrived, `receiver-lost` reported, dsd-neo exited 0
  with "Exiting", no TCP loss lines. The existing real-decoder end-to-end
  test also passes through the new pump.
- Not repeated in this pass: the SDR++ File Source runs of the previous
  section (the pump's socket side is the same `PcmTcpSource` recv path they
  exercised; the stdin side is the run above).

## Tests

`tests/test_sdr_receiver_transitions.py` (11 + 1 real-decoder): A1 identity
with delayed diagnostics; B1 journey through `MainWindow._open_receiver()`,
the fake's window-tuning control and `app.start_session()` without flags,
plus the explicit-request and attach cases; B2 stored times on the analog
and decoded paths across three retunes; B3 two retunes before a drain,
overflow pressure, a held consumer through the real capture; C1 heartbeat
Stop ×2, restart, Quit against a 1.2 s rigctl delay, and an old poll loop
against a new run; C2 exception, timeout and failed settle. Existing tests
updated for the stdin command line, the buffer API (`buffered`/`capacity`),
the launch-time tuning (requested_hz recorded, epoch counts), `receiving`
vs `decoding`, and the wording of `receiver-lost`.

## Evidence

| Check | Result |
|---|---|
| `tests/test_sdr_receiver_transitions.py` | 11 passed, 1 skipped without the real-decoder env (passes with it) |
| The same tests against `5bfa6a3` in a worktree with the new stand-ins, one test per process, 150 s limit | B1: "the stored choice is applied when we start SDR++" (the baseline applied nothing at launch and restored it at Start); B2 analog and decoded: "stored started_at went backwards"; B3 buffer: `no attribute 'buffered'` (no marker-keeping buffer existed); B3 held audio: "voice heard on 155.100 MHz was saved under a later frequency" (155.16); C1: "Stop waited 1.87 s for the receiver lock"; C2 exception: "the live SDR++ was forgotten after the exception"; C2 failed settle: "SDR++ was ended although the decoder had not settled". Two pass on the baseline as guards (the old-polling-loop test; the no-op termination case, which the previous pass already handled). **A1 on the baseline fails only at the command-line check (`-i tcp:` vs `-i -`)**: under the schedule this test imposes (the "Disconnected" line held 1.2 s) the baseline saved no recording here, so Codex's 1.46 s substitute recording was **not** reproduced in this container; the closure rests on the decoder now having no other input (source-read and verified on the real binary below), not on a reproduced failure |
| `tests/test_sdr_receiver.py` + `tests/test_sdr_receiver_corrections.py` + transitions, together | 44 passed, 2 skipped (the two real-decoder tests without env) |
| Both real-decoder tests with `BABELFISHR_DSD_NEO`/`BABELFISHR_DSD_FIXTURE_WAV` set | 2 passed (14.9 s) |
| Full suite, nothing else running | **1021 passed, 13 skipped, 0 failed**, 342 s |
| Skips (13) | QtMultimedia ×2, CoreAudio, PlistBuddy, Whisper model ×5, Argos ×2, the two real-decoder tests when their env is unset |
| `git diff --check`, `compileall` | clean |
| Seen once, not reproduced | one run of the two B2 tests ended in a Python fatal-error traceback at interpreter exit (numpy extension frames, after the assertions had already failed for the timing reason since fixed); the same tests then ran clean three times and in the full suite |

Environment: Linux container, Python 3.11, PySide6 offscreen, no audio backend, no receiver, no Mac; dsd-neo 2.9.0 built from `630a123e`. Mock ASR/translation throughout.

## Installation names (from Codex's release-listing check)

`sdrpp_macos_arm.zip` under SDR++'s *nightly* release (mutable; note the
date) and `dsd-neo-macos-arm64-portable-v2.9.0.dmg` under dsd-neo v2.9.0.
Verified as names in release metadata only - not contents, compatibility,
signing or installation. README and checklist S name them and the Finder
routes (**Receiver › Choose SDR++ application…** / **Choose DSD-neo
program…**).

## Still unknown

- Everything on a Mac and with the RTL-SDR: the macOS SDR++ build, the
  nightly's contents, Gatekeeper, the dongle, over-the-air signals.
- File replay is not reception; the filter-width finding is one DMR
  fixture; mock recognition is not recognition.
- dsd-neo waiting on an empty pipe indefinitely is what the pinned source
  does; a receiver that streams nothing for a long time therefore keeps
  the decoder waiting rather than ending it - the poll and status lines
  say whether audio is arriving.
- Eric's acoustic radio test stands (work radio speaker → laptop
  microphone → BabelFishR); direct RTL-SDR reception and FalconClaw/PTT
  remain unverified.

---

# SDR receiver: stream cleanup and recording identity

Codex verified `025c4ee` (82 passed, 2 skipped in its environment) and
reported five further counterexamples in three areas. Each is reproduced
below in a test that fails on `025c4ee` and passes now, and each change is
confined to its area.

## Identifiers

| | |
|---|---|
| Base | `025c4ee3eb1fe402f6ef7ad0599c718c67f6b615` (verified equal to the remote tip, worktree clean before editing) |
| Commit | the commit carrying this section |
| Branch | `claude/radio-decoder-translator-0oslya` |
| Workflow / tag / release / packaging | nothing dispatched, tagged or published; no packaging change; run 21 cannot test this |

## A. Decoder pipe and reconnection lifetime

- **A1** A decoder that stops reading fills the pipe; the pump then sits
  inside `write()` holding `_stdin_lock`, and `stop()` waited for that lock
  in `_close_stdin()` before ever reaching `process.terminate()` - the
  capture's stopper hung there, the decoder lived, Quit stayed pending.
  **Change:** `stop()` never waits on the pump: it closes the socket, asks
  the process to end first (the process ending is what fails the blocked
  write and frees the lock), and closes the input only if the lock is free
  within 50 ms; the pump closes it on its way out, and `settle()` closes it
  once the pump has ended. Process-stop policy unchanged (terminate, then
  `settle()` kills after its timeout).
- **A2** A `create_connection` that returned after Stop was installed as
  the source's socket, "upstream-restored" was announced with
  `_running=False`, and `settle()`/`settled` said True while the pump and
  the socket lived. **Change:** the socket handoff is under `_sock_lock`
  with `_running` set under the same lock, so a connection that completes
  after cancellation is closed and never installed; `settle()` joins the
  pump, reader, event watcher and filler (bounded) and closes any held
  socket; `settled` is true only when process, pump and socket are all
  gone; "upstream-restored" is announced when PCM actually arrives again
  (`_awaiting_pcm`), not when a socket is accepted.
- Decoded timeline (found while closing C1 on the decoded path): the
  reader's wall-clock offsets left small uncovered gaps whenever voice
  resumed after idle, so sample time and clock drifted apart by ~0.1 s per
  resumption. `DecodedVoiceSource` now keeps one continuous timeline
  (`_timeline`): idle wall time is covered with silence before decoded
  audio is placed (`_place`), and the idle filler advances the same
  timeline; block offsets and the sample count agree, and the detector's
  discontinuity rule (below) is never tripped by the decoded path's own
  clock.

## B. Failed source cleanup under ordinary Stop / Quit

`CaptureService._stop_source` swallowed a `stop()` exception (and logged a
failed `settle()` as a warning) and marked the source stopped; the
controller had already detached it; the capture was reaped; Quit returned
True with the decoder alive. **Change:** the failure is kept
(`source_error`), `_source_stopped` stays False, `settled` requires it, a
`source-stop-failed` status is published once, and `retry_stop_source()`
runs only the part that did not succeed (a `stop()` that already succeeded
is not repeated; `settle()` is). `app.close` retries once per call through
the lingering capture, sets `capture_error`, and returns False until the
source has finished; the window shows the error while Quit retries.

## C. Recording time and frequency belong to the captured audio

- **C1** Re-anchoring the detector's origin on every block moved audio
  already buffered: a 3 s timestamp gap before the closing silence dated
  the 1.46 s recording 3 s late. **Change:** the origin is fixed by the
  first block after a start or reset and left alone; a block whose
  timestamp sits more than `DISCONTINUITY_SECONDS` (0.25 s) from where the
  sample count puts it is a break: what is open closes there, with its
  own time; the pre-roll from before the break is dropped; the origin is
  fixed afresh at that block, so later audio carries its own later time.
  `discontinuities` counts them.
- **C2** A block already in hand when a retune went by closed the
  recording through the ordinary path, which took `source.metadata()` -
  the new tuning. **Change:** every block handed out by `read()` carries
  `block.tuning`, a frozen `TuningState` for its generation (taken at the
  first read of that generation; the controller now writes the new tuning
  *before* marking the boundary and hands the boundary the ending epoch's
  metadata it took beforehand). `_handle_block` labels a recording it
  completes with that block's tuning (`source.metadata(tuning=...)`), and
  the final flush uses the last block's. Markers still close what they
  find open under their own metadata. Call identifiers stay current.

## Tests (`tests/test_sdr_receiver_cleanup.py`, 8)

A1 with a child that never reads stdin, through the application and
capture (pipe full, pump blocked in write, Stop returns, capture settles,
decoder gone, `settled` true); A2 with `socket.create_connection` held
after connecting until after Stop (late socket closed, no restored
announcement, `settled` false until the pump ends); restored announced only
after new PCM; B with `stop()` raising and with `settle()` returning False
twice, through `app.stop_session()` / `app.close()` (capture unsettled,
error kept and shown, event once, decoder alive until the retry succeeds,
`stop()` called once in the settle case); C1 with explicitly timestamped
blocks through a scripted source, the real capture, detector, recorder and
store (a 3 s break before the closing silence: recording dated 0.4-1.05 s
after stream start; and audio after a break dated at its own later time);
C2 holding an in-hand block that will close the open recording, retuning,
releasing: saved as 155.1 MHz, epoch 0. Also: the bounded-queue test's
drain assertion was a race (it read while the sink still sent) and now
waits until the reader has taken everything in; the recording-times test's
tautological boundary assertion is replaced by the fixture's own period
(bursts 2.8 s apart).

## Evidence

| Check | Result |
|---|---|
| `tests/test_sdr_receiver_cleanup.py` | 8 passed (24 s) |
| The same tests against `025c4ee` in a worktree, one test per process, 150 s limit | A1: "the capture's stopper never got past closing the decoder's input"; A2: "settled while the pump (and its connect) were still alive"; restored: "announced with no new PCM since the loss (96000 → 96000)"; B stop(): "a capture whose source did not stop reported itself settled"; B settle(): capture `settled` True with the decoder alive; C1: "recording dated 3.740s after stream start" (the review's number exactly); C2: "audio heard on 155.100 MHz was saved as 162.55 MHz". One passes on the baseline as a guard (audio after a break keeping its own later time - the old per-block re-anchoring gave later audio its later time; what it broke was the earlier audio) |
| Receiver suites (4 files) + lifecycle, shutdown, cleanup, boundary, cleanup-and-labels, menu suites | 108 passed, 2 skipped (220 s) |
| Full suite, nothing else running | **1029 passed, 13 skipped, 0 failed**, 355 s |
| Skips (13) | QtMultimedia ×2, CoreAudio, PlistBuddy, Whisper model ×5, Argos ×2, the two real-decoder tests when their env is unset |
| `git diff --check`, `compileall` | clean |
| Real decoder in this pass | not re-run: the changes are on BabelFishR's side of the pipe (stop ordering, socket handoff, settlement, timeline, capture ownership, detector timing, block tags); the stdin/EOF behaviour relied on is the previous section's verified run |

Environment: Linux container, Python 3.11, PySide6 offscreen, no audio backend, no receiver, no Mac. Mock ASR/translation throughout.

## Still unknown

Unchanged from the previous section: nothing here ran on a Mac or with the
RTL-SDR; the real-decoder evidence is the stdin/EOF run and the two
env-gated tests; file replay is not reception; mock recognition is not
recognition. Eric's acoustic radio test stands (work radio speaker →
laptop microphone → BabelFishR); direct RTL-SDR reception and
FalconClaw/PTT remain unverified.

---

# Candidate build attempt at 542ac7a (run 22): failed at the test gate

## What was dispatched

| | |
|---|---|
| Base for this pass | `dded5011ca6d12c71fc748358a7496398a3d9659` (verified equal to the remote tip, worktree clean, no tag at HEAD; tags v0.3.0-alpha.1-3 and their three prereleases unchanged) |
| Documentation commit | `542ac7a2f8b5077947efbf1cb76266bdacb6573d` - README and checklist S only (still-checking USB line, control connection not a precondition, "audio arriving" in digital mode, Stop-before-switching, DSD-neo line not a running indicator, a station carrying speech); application code at `dded501` |
| Real decoder recheck before dispatch | dsd-neo 2.9.0 built from `630a123e`, fixture `dmr_voice_disc48k.wav` (192 042 bytes): both env-gated tests passed (2 passed, 15.3 s) on the final tree |
| Workflow | `.github/workflows/macos-release.yml`, workflow_dispatch, ref `claude/radio-decoder-translator-0oslya`, `runner_label=macos-26`, `publish_prerelease=false`, `release_tag=""`; no run existed at 542ac7a; one run dispatched |
| Run | #22, id 34641195384, attempt 1, https://github.com/5w8vggg65c-eng/BabelFishR/actions/runs/34641195384 - checkout SHA `542ac7a2f8b5077947efbf1cb76266bdacb6573d` (verified from the run record), runner macos-26 (arm64 gate passed), Python 3.12.10 |
| Conclusion | **failure** in "Build, test, verify, sign and package" at the test gate: `11 failed, 1020 passed, 11 skipped in 759.53s`; no bundle, no signing, no DMG, no verification reports produced ("not produced" for every report); the reports artifact `BabelFishR-macOS-reports` (id 10280727106, 48 523 bytes, 30-day retention) holds only the test report. No DMG artifact, no SHA-256, nothing published. Not re-dispatched. |

## The eleven failures

1. **Production defect, reproduced and fixed here** - `test_the_qt_backend_reports_a_missing_file_as_an_error`,
   `test_the_qt_backend_learns_a_real_wav_duration`:
   `RuntimeError: Failed to connect signal "positionChanged(qlonglong)" to
   signal "positionChanged(int)"` in `QtMultimediaBackend.__init__`
   (babelfishr/ui/playback.py). The workflow installs an unpinned
   `PySide6>=6.5`; this run resolved 6.11.2, which refuses a signal-to-
   signal connection between `QMediaPlayer.positionChanged(qlonglong)` and
   the backend's `Signal(int)`. The backend could not be constructed at all,
   so playback would have been broken in the packaged app. Reproduced in
   this container after installing PySide6-Addons 6.11.2 (plus libpulse0):
   the two tests fail with exactly the runner's message; with the fix
   (position and duration relayed through slots that emit `int`) all 18
   playback tests pass, and the full suite is 1031 passed, 11 skipped.
2. **Receiver stand-in tests on a slow-timer machine** (never before run
   on macOS; nine failures):
   - `test_the_rigctl_client_speaks_sdrpps_protocol`: the fake's log was
     read before the fake had logged `\start`/`q` - a race in the test;
     it now waits for the line.
   - `test_an_idle_decoder_does_not_end_the_stream`: 0.5 s of silence fill
     covered 2.5 s of wall time - the idle filler emitted one 20 ms block
     per loop iteration, and `time.sleep(0.02)` on that runner woke late.
     The filler now covers everything uncovered in one block per wake.
   - `test_analog_reception…` (no two complete transmissions in 40 s),
     `test_digital_reception…` (call identifiers expired before capture),
     `test_recordings_after_retunes…` (recordings 13.5 s apart for 2.8 s
     bursts; a 5.66 s recording), `test_a_retune_during_a_transmission…`,
     `test_held_audio…`: the fake SDR++ paced its stream with
     `sendall(); sleep(0.02)`, so late wakes streamed audio at a fraction
     of real time. It now sends as many 20 ms pieces as the clock calls for.
   - `test_a_running_sdrpp_whose_rigctl_is_off…` (`DID NOT RAISE
     ReceiverUnavailable`): `sdrpp_processes()` on macOS used
     `pgrep -x sdrpp` only, so a Python stand-in was invisible; it now
     applies the same rule as on Linux through `ps -axo pid,comm,args`
     (the process's own name, or the executable given, with `--root`
     compared). Production on a Mac (binary named `sdrpp`) is unchanged in
     effect.
   Classification, corrected: the idle-filler catch-up and the macOS
   process detection are **production** changes (babelfishr/receiver/
   stream.py and sdrpp.py); the clock-paced fake and the log wait are
   stand-in/test changes. None of it had run on a Mac at that point. Locally: the four
   receiver files 52 passed, 2 skipped; full suite 1031 passed, 11 skipped.
   The run also left orphan Python processes (the stand-ins) that the
   runner terminated at job end.

## State after this pass

The candidate is not built. The fixes above are committed on the branch
(commit carrying this section); the next candidate must be dispatched
from that SHA with the same non-publishing inputs, and its test gate is
the first thing to read. Nothing was tagged, released or published; run 22
stays as the record of the failure.

## For Eric (once a candidate exists)

Installation, first bench and the acoustic-radio note are in README "SDR
receiver" and checklist section S; nothing there has been run on a Mac.
Eric's acoustic radio test stands (work radio speaker → laptop microphone
→ BabelFishR); direct RTL-SDR reception and FalconClaw/PTT remain
unverified.


---

# Mac process-path regression closed; candidate build #2

## Identifiers

| | |
|---|---|
| Base | `0b3d4ad8bec3311ca5a883f19fca19246eb0cd0e` (verified equal to the remote tip, worktree clean, no tag at HEAD) |
| Commit | the commit carrying this section |
| Branch | `claude/radio-decoder-translator-0oslya` |

## The regression (Codex, source-backed boundary test) and the repair

`sdrpp_processes()` on Darwin parsed `ps -axo pid=,comm=,args=` with
`line.split(None, 2)` and `parts[2].split()`: ps joins the arguments with
single spaces and the boundaries are gone (adv_cmds ps/print.c), so an
explicit `--root /Users/eric/Library/Application Support/sdrpp` compared as
`/Users/eric/Library/Application`, the running SDR++ was taken for another
receiver's, its files were rewritten and a second SDR++ was launched.
(BabelFishR's default launch passes no `--root`; this needed an explicit
root with a space in it.)

Repair (`babelfishr/receiver/sdrpp.py`): the words after `--root` are
joined as far as an existing directory reaches (`_root_from_flattened_words`,
the longest existing prefix); a root that cannot be established from ps is
**unknown, and unknown protects the running SDR++** - it counts as this
receiver's, never as proof of absence. The Linux `/proc` path keeps exact
arguments. Plain roots, a known different root, the SDR++.app binary named
`sdrpp` without `--root`, and the stand-in are all still matched or
excluded as before.

## Tests (`tests/test_sdr_receiver_mac_gate.py`, 5 - run on every host, so on the Mac gate too)

- Spaced root through the production controller with an owned stand-in on
  `…/Library/Application Support/sdrpp` and ps-shaped output at the
  subprocess boundary (`platform.system` → Darwin, `subprocess.run` for ps
  only): the process is found, `ensure_sdrpp` refuses naming the Rigctl
  Server, no second launch, settings byte-for-byte unchanged, the
  operator's process alive. **Fails on 0b3d4ad** ("the root split at its
  space was taken for a different receiver's: [] == [pid]").
- Plain root detected; a known different root excluded; a root nobody has
  excluded; the `sdrpp`-named app binary without `--root` counted; refusal
  when running without rigctl. Passes on 0b3d4ad too (guard).
- A root that reaches no existing directory protects the process. **Fails
  on 0b3d4ad** ("an unreadable root was taken as proof … [] == [777]").
- Delayed-wake regressions for the two timing repairs, with `time.sleep`
  forced to 0.5 s whatever is asked: the idle filler covers ≥ elapsed − 0.6 s
  of 2.5 s (**dded501: 0.10 s** - the review's number) and 2.5 s of audio
  reaches the stand-in's client within 3.6 s (**dded501: 0.48 s in 12 s**).
  Coverage and arrival time are measured, not timeouts extended.

## Evidence

| Check | Result |
|---|---|
| `tests/test_sdr_receiver_mac_gate.py` | 5 passed (9.7 s) |
| Fail-before, one test per process in worktrees | on `0b3d4ad`: spaced root fails (`[] == [pid]`), unknown root fails (`[] == [777]`), plain root passes; on `dded501`: idle filler 0.10 s of 2.50 s, stand-in 0.48 s of audio in 12 s |
| Focused suites with QtMultimedia 6.11.2 present (5 receiver files, playback, lifecycle, shutdown, cleanup, boundary, menu) | 113 passed, 2 skipped (the real-decoder env), 238 s |
| Full suite, nothing else running, QtMultimedia present | **1036 passed, 11 skipped, 0 failed**, 362 s; skips: CoreAudio (real host), PlistBuddy, Whisper model ×5, Argos ×2, real-decoder ×2 |
| `git diff --check`, `compileall` | clean |

Build #2 is recorded in the report that accompanies this commit (dispatched
from this commit's SHA with `runner_label=macos-26`,
`publish_prerelease=false`, `release_tag=""`, one run, not retried).
