# KingFishR

KingFishR = the BabelFishR core (record, transcribe and translate radio
audio, offline) + the specialised SDR features that are not part of
BabelFishR: the SDR++ receiver input, DSD-neo digital voice, the recorded-IQ
source and the experimental signalling decoders. Receive-only.

It is a separate distribution in this repository. It depends on
`babelfishr`; BabelFishR never includes, imports or discovers it.

## Starting it

```bash
pip install ./            # BabelFishR, from the repository root
pip install ./kingfishr   # KingFishR
kingfishr-gui             # or: kingfishr <command>, python -m kingfishr
```

Only these entry points activate KingFishR (`kingfishr.activate()`): they
set its product identity and register its hooks
(`babelfishr/extensions.py`). Starting `babelfishr` or `babelfishr-gui`
never loads KingFishR, even when both are installed.

## Its own data

KingFishR keeps its own settings, database, recordings, models and logs:

| | KingFishR | BabelFishR |
|---|---|---|
| macOS data folder | `~/Library/Application Support/KingFishR` | `~/Library/Application Support/BabelFishR` |
| Linux data folder | `~/.local/share/KingFishR` | `~/.local/share/BabelFishR` |
| Database | `kingfishr.sqlite3` | `babelfishr.sqlite3` |
| Config files | `kingfishr.toml`, `~/.config/kingfishr/` | `babelfishr.toml`, `~/.config/babelfishr/` |
| Environment | `KINGFISHR_HOME`, `KINGFISHR_DB`, ... | `BABELFISHR_HOME`, `BABELFISHR_DB`, ... |

It never opens BabelFishR's data by default and copies nothing from it -
no recordings, no database, no models. The database schema and recording
metadata are the same, so pointing KingFishR at a copy explicitly
(`--config`, or `KINGFISHR_HOME`) works; that is the operator's choice.

See [ROADMAP.md](ROADMAP.md) for what is built and what is not.

## Moved from the BabelFishR README

The sections below were moved from BabelFishR's README with the code. They
still say "BabelFishR" where they describe the shared core; the menus and
commands they name (Receiver ›, `analyze`) exist only in KingFishR.

## Digital voice

If your radio decodes DMR/P25/D-STAR/Fusion and puts intelligible voice out of
its accessory connector, BabelFishR treats it as ordinary audio and it works.

Raw digital bursts are a different problem requiring a protocol-specific plugin
and validation against recorded samples. BabelFishR does not attempt to decode
them, does not bundle a vocoder, and will not help recover encrypted traffic.

If a local `dsd-neo` is installed, a recording can be analysed after the fact.
A failure means *no usable decode from this input*, not *the recording was
lost* — and ordinary accessory audio is often the wrong input for a digital
decoder anyway, so a negative result says very little. See
[docs/DIGITAL_ANALYSIS.md](docs/DIGITAL_ANALYSIS.md).

## SDR receiver (RTL-SDR through SDR++ and DSD-neo)

BabelFishR can take its audio from an RTL-SDR receiver instead of an audio
cable. It does not drive the dongle itself: two existing programs do the
radio work and BabelFishR does the recording, transcription and translation.

- **SDR++** is the receiver window. It owns the USB receiver, shows the band,
  and is where you pick a frequency and mode. BabelFishR starts it for you
  (Receiver › Open receiver window) and, when it does, switches on two of
  SDR++'s own modules so the two programs can talk: the *Network* audio sink
  (the demodulated audio, sent to BabelFishR over a local connection) and
  the *Rigctl Server* (so Receiver › Tune receiver can set the frequency).
  SDR++'s other settings are left alone, and each file it changes is backed
  up first as `*.before-babelfishr.json`. An SDR++ that is already running
  is attached to, never started a second time and never reconfigured under
  you; if its Rigctl Server is off, BabelFishR says which panel to switch it
  on in.
- **DSD-neo** decodes digital voice (DMR, P25, NXDN, D-STAR, YSF, ...). When
  *Digital voice* is ticked in Tune receiver, BabelFishR runs DSD-neo between
  SDR++ and itself and records the decoded speech. BabelFishR itself holds
  the connection to SDR++ and feeds DSD-neo the audio through a pipe (its
  `-i -` input), so the decoder has exactly one possible input: when the
  receiver's audio ends, the pipe closes and DSD-neo ends with it. (Given a
  network address instead, DSD-neo would turn to a sound device of its own
  after losing that connection - read in its source and reproduced with a
  stand-in; that route is no longer used.) One TDMA slot is transcribed at
  a time, chosen in the same dialog; two simultaneous conversations are
  never mixed into one transcript.

**Installing, without Terminal.** Both programs come from their projects'
GitHub *Releases* pages (BabelFishR bundles neither; both are GPL-licensed
and are installed and updated on their own terms). The file names below
were read from GitHub's release listings by an independent review (the
listing names them; their contents, signing and installation were not
checked from here):

1. SDR++: https://github.com/AlexandreRouma/SDRPlusPlus/releases - the
   macOS download is **`sdrpp_macos_arm.zip`** under the *nightly* release
   (a nightly is rebuilt over time, so note the date you downloaded). Open
   the zip and put **SDR++.app** in **Applications**. BabelFishR looks for
   `/Applications/SDR++.app`; if it is somewhere else, use **Receiver ›
   Choose SDR++ application…** and pick it.
2. DSD-neo (only for digital voice): https://github.com/arancormonk/dsd-neo/releases
   - version **v2.9.0**, file **`dsd-neo-macos-arm64-portable-v2.9.0.dmg`**
   (2.9.0 is the version BabelFishR was tested against). Open it and drag
   the **dsd-neo** program (or its folder) to a place you can find again,
   such as **Applications**. Then use **Receiver › Choose DSD-neo program…**
   and pick the `dsd-neo` file. (Nothing needs to be "on the PATH".)
3. On first launch of either, macOS may say it cannot verify the developer:
   System Settings › Privacy & Security › **Open Anyway**, once per program.

**Receiver › Receiver status…** then shows, separately: whether the SDR++
software was found, whether an RTL-SDR is on the USB bus (the first look
may say *still checking* - reopen the box a few seconds later), whether
SDR++ is running, whether BabelFishR currently holds a control connection
to it (only during a run or a tune; *not connected* between runs is
normal and no reason to wait before Start), and whether audio is arriving.
*Audio arriving* means SDR++'s audio is reaching BabelFishR - with Digital
voice ticked, the audio going into DSD-neo, not decoded speech. The DSD-neo
line names the program and its version; it is not a running indicator.
Those are different things, and the window never runs them together.

**Using it.** Plug in the RTL-SDR, choose *SDR receiver* in the Audio input
list, open the receiver window, tune - in SDR++'s own window or with
Receiver › Tune receiver - then press **Start monitoring**. To switch
between analog and digital decoding, press Stop monitoring, wait for the
activity indicator to read Idle, change the Digital voice tick, then Start
again. Start reads what
SDR++ is tuned to *now* and uses that; tuning you did in the SDR++ window is
respected, and a change you make there while monitoring is picked up within
a second and marked on the messages. Each message records what SDR++
*confirmed* it was tuned to, separately from what you asked for; a confirmed
frequency is not proof that speech is arriving. A retune while a
transmission is being heard closes that transmission under the frequency it
was heard on before audio under the new one is taken. If SDR++ is not
running, cannot be started or stops during a watch, monitoring refuses or
stops and says so - BabelFishR never records from the laptop microphone in
the receiver's place, and when SDR++'s audio stops for good the pipe into
DSD-neo is closed, so DSD-neo ends rather than listening to anything else.
Stop and Quit never wait on the receiver: a slow or silent SDR++ cannot hold
the window, and a shutdown step that fails is reported and retried rather
than counted done.

**Filter width for digital voice.** Tune receiver has a *Filter width*
choice. Ticking *Digital voice* moves it from 12.5 kHz to **20 kHz**: with
the real SDR++ playing a DMR recording, its FM output at 12.5 kHz was
clipped and DSD-neo decoded no speech, while at 20 and 25 kHz it decoded.
That was measured with one DMR recording; other protocols were not tried.

**What is proven and what is not.** Three levels, kept apart:

- *Automated, with stand-ins:* the files SDR++ reads, its rigctl line
  protocol (including that `\start` writes no reply and that one client is
  served at a time), its raw network-sink stream, DSD-neo's command line,
  output and event lines - exercised by stand-ins that reproduce what was
  read in the two programs' sources, in `tests/test_sdr_receiver.py` and
  `tests/test_sdr_receiver_corrections.py`.
- *Real programs, no radio:* SDR++ (v1.3.0, built from the pinned source
  revision, run headless on Linux with its **File Source** playing DSD-neo's
  own DMR voice IQ recording) was configured by BabelFishR's configurator,
  tuned over rigctl, and its network sink fed the real DSD-neo, which decoded
  DMR voice; BabelFishR's receiver controller attached to that SDR++, adopted
  its tuning, took the decoded speech, marked a retune boundary and shut down
  in order. The real DSD-neo was also fed through the pipe BabelFishR now
  uses: it decoded, merely waited through a gap with no data, and ended by
  itself when the pipe closed. Also observed there: `--autostart` alone produced no audio from
  the File Source until a stop/start over rigctl (a File-Source quirk, not
  seen with hardware, which was absent), and that source runs unpaced.
- *Not yet:* an RTL-SDR, over-the-air reception, the macOS SDR++ build,
  the click-only installation on a Mac and every step of checklist section
  S. Digital protocols that need the raw IQ path (P25 CQPSK/LSM simulcast,
  for one) are not covered by the audio connection used here. Encrypted
  traffic is not decoded and is not presented as speech. DSD-neo did not
  report talkgroup or radio identifiers for the DMR recording used, so those
  fields may stay empty on real traffic too.

## Experimental decoders

An earlier prototype produced signalling decoders (CTCSS, DCS, DTMF, CW,
AFSK1200/APRS, MDC-1200, POCSAG, RTTY). They are preserved under
`kingfishr/experimental/`, kept out of the receive path, and disabled unless
you set `KINGFISHR_EXPERIMENTAL=1`. They were verified against synthetic
signals only — never off-air. See [docs/EXPERIMENTAL.md](docs/EXPERIMENTAL.md).

## Licence and third-party programs

MIT, the same licence as BabelFishR (see `../LICENSE`). SDR++ and DSD-neo
are separate programs that KingFishR starts and talks to; neither is
bundled in this repository. Their licences are recorded with the code that
pins their interfaces (`kingfishr/receiver/contract.py`: DSD-neo
GPL-3.0-or-later, portions ISC; mbelib-neo GPL-2.0-or-later).
