# Experimental signalling decoders

`babelfishr/experimental/` holds decoders from an earlier prototype:

| Decoder | What it reads |
|---|---|
| `ctcss` | Sub-audible CTCSS/PL tones (67–254 Hz) |
| `dcs` | DCS/DPL, 134.4 bps Golay(23,12) sub-audible word |
| `dtmf` | Touch-tone keypad signalling |
| `cw` | Morse, with adaptive speed estimation |
| `afsk1200` | Bell 202 AFSK → HDLC → AX.25 → APRS |
| `mdc1200` | Motorola MDC-1200 unit ID / emergency bursts |
| `pocsag` | POCSAG paging at 512/1200/2400 baud |
| `rtty` | Baudot RTTY |
| `digital-voice` | Classifies DMR/P25/D-STAR/NXDN bursts (no vocoder) |

## Status: unvalidated

**Every one of these was tested against synthetically generated signals only.**
None has been run against an off-air recording from a real radio. Synthetic
tests prove the algorithm is self-consistent; they say nothing about how it
behaves with real filtering, drift, fading, AGC, or a discriminator tap.

Some pieces do carry independent evidence:

- The DCS Golay(23,12) construction reproduces the code's defining properties
  (all 83 codewords divisible by the generator, minimum Hamming distance 7).
- The POCSAG BCH implementation validates the standard idle codeword
  `0x7A89C197`, which independently confirms the generator and framing.

MDC-1200 is the weakest: the framing is well documented, but published
descriptions differ on the interleave transpose and CRC convention. The decoder
tries the plausible variants and reports full confidence only on a CRC match,
falling back to a low-confidence "burst detected" result otherwise.

## Why they are quarantined

BabelFishR's supported job is analogue voice. A half-validated decoder in the
receive path is worse than none: it produces confident-looking wrong metadata.
Nothing in the receive pipeline imports this package — there is a test
(`tests/test_experimental.py`) that enforces it.

## Using them anyway

```bash
export BABELFISHR_EXPERIMENTAL=1
```

```python
from babelfishr.experimental import load_decoders

decoders = load_decoders()          # raises ExperimentalDisabled unless enabled
results = decoders["dtmf"].decode(audio, 8000)
```

Also settable as `experimental = true` in the config file.

## Promoting one out of here

1. Record off-air samples where you independently know the right answer (the
   transmitting radio's display, a second decoder, the sender themselves).
2. Build a fixture set covering strong signal, weak signal, and a fade.
3. Measure the decode rate and, crucially, the false-positive rate on ordinary
   voice traffic — a decoder that fires on speech is worse than useless.
4. Only then wire it into the pipeline, behind a setting, defaulting to off.

## Also here

`fm.py` (SDR IQ demodulation), `goertzel.py`, and `bandplan.py` (GMRS/FRS/MURS/
marine/amateur channel tables). Band plans may become useful as a picker for
radio profiles — as a convenience for *labelling*, never as something the
application claims to have measured.
