# Digital post-processing with DSD-neo

Optional, local, and entirely separate from the SDR question: the input is the
transmission WAV already captured from your radio's accessory output.

**Status: unverified.** The integration is written against
[the documented CLI](https://github.com/arancormonk/dsd-neo/blob/main/docs/cli.md)
and tested against a stub that reproduces that interface. The real binary has
never run here, and no real digital traffic has been decoded. Treat every
result as unproven until you have tested with the actual tool and recordings
whose mode you know independently.

## Setup

```toml
# ~/Library/Application Support/BabelFishR/settings.toml
[analysis]
dsd_path = "dsd-neo"      # or an absolute path
timeout = 120.0
```

`babelfishr field-check` reports whether it was found and its exact version.
BabelFishR works fully without it.

## Use

In the timeline, a bubble's overflow menu offers:

- **Analyze as digital (hunt all profiles)** — passes `-fa`.
- **Analyze as a specific mode…** — every documented profile.

Or from a terminal:

```bash
babelfishr analyze <transmission-id>                  # -fa
babelfishr analyze <transmission-id> --protocol dmr-dual
```

## Profiles

| Preset | Flag | Meaning |
|---|---|---|
| `auto` | `-fa` | hunt all candidate profiles |
| `dmr-dual` | `-fs` | DMR simplex, dual-slot decoder |
| `dmr-mono` | `-fr` | DMR simplex, single-slot mono decoder |
| `p25p1` | `-f1` | P25 Phase 1 |
| `p25p2` | `-f2` | P25 Phase 2 (6000 sps) |
| `dstar` | `-fd` | D-STAR |
| `nxdn48` | `-fi` | NXDN48 (6.25 kHz) |
| `nxdn96` | `-fn` | NXDN96 (12.5 kHz) |
| `x2tdma` | `-fx` | X2-TDMA |
| `ysf` | `-fy` | System Fusion |
| `m17` | `-fz` | M17 |
| `provoice` | `-fp` | ProVoice |
| `dpmr` | `-fm` | dPMR |
| `edacs` / `edacs-esk` | `-fh` / `-fH` | EDACS/ProVoice, plain and ESK 0xA0 |
| `edacs-ea` / `edacs-ea-esk` | `-fe` / `-fE` | EDACS EA/ProVoice, plain and ESK |

### Why automatic hunting can miss

A full rotation through the candidate profiles takes **about six seconds at
48 kHz**. A shorter recording can finish before the correct profile is tried,
so a negative `-fa` result on a three-second clip means very little. Pick a
specific preset when you know the mode. BabelFishR attaches a warning to any
automatic attempt on a recording shorter than the rotation.

## Reading the outcome

| Outcome | Meaning |
|---|---|
| `voice-decoded` | real, non-silent audio **and** corroborating decoder output |
| `protocol-identified` | protocol plus metadata (talkgroup, NAC, colour code) |
| `protocol-candidate` | a protocol was mentioned, without confirmation |
| `metadata-only` | fields recovered, no protocol claim |
| `encrypted-or-unsupported` | reported as such; no attempt is made to break it |
| `insufficient-input-quality` | the input was not good enough |
| `no-result` | **no usable decode from this input** — not a lost recording |
| `analysis-failed` | the tool did not run, or crashed |

A decoded WAV counts as a voice decode only if it has real duration and
non-silent content; a file that merely exceeds its 44-byte header does not.

## Why a negative result says little

Ordinary speaker or accessory audio is usually the wrong input for a digital
decoder: it is band-limited, de-emphasised and AGC'd, and if the radio decoded
the traffic itself then what arrives is already analogue voice. Discriminator-
tap or SDR baseband audio is what DSD really wants.

## Guarantees

- The original recording is opened read-only and never modified.
- Conversions are written to a **per-attempt** derived file, so reruns and
  concurrent analyses cannot collide.
- Every attempt records the command, exit status, stdout/stderr, runtime and
  outcome, and accumulates on the transmission for comparison.
- No SDR is involved anywhere in this path.
- Encryption is reported, never attacked.
