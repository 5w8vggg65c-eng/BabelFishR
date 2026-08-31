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
