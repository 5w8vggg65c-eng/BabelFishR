"""What the upstream components actually do - read from their sources.

Every value here was taken from the named file at the named revision, not
from memory, and the behaviour marked *observed* was exercised against the
real dsd-neo binary built from that revision. Nothing here has run against
SDR++ itself in the development environment (no receiver, no display); the
SDR++ facts are source facts until a Mac with SDR++ confirms them.
"""

from __future__ import annotations

import dataclasses
from typing import Tuple


@dataclasses.dataclass(frozen=True)
class SdrppContract:
    """SDR++ (AlexandreRouma/SDRPlusPlus, master as inspected 2026-09-11)."""

    #: rigctl_server module, misc_modules/rigctl_server/src/main.cpp:
    #: config keyed by module instance name in <root>/rigctl_server_config.json.
    rigctl_default_host: str = "localhost"
    rigctl_default_port: int = 4532
    rigctl_config_file: str = "rigctl_server_config.json"
    rigctl_module: str = "rigctl_server"
    rigctl_instance: str = "Rigctl Server"
    #: Commands: "F <hz>" -> "RPRT 0"; "f" -> "<hz>\n"; "M <mode> <bw>" ->
    #: "RPRT 0" (bw 0 or -1 = default); "m" -> "<mode>\n<bw>\n"; "\start",
    #: "\stop" set the play state; "q" closes. Unknown -> "RPRT 1".
    rigctl_modes: Tuple[str, ...] = ("FM", "WFM", "AM", "DSB", "USB", "CW", "LSB", "RAW")
    #: "F" tunes the VFO named in config "vfo" (we set "Radio"); it does not
    #: check whether the SDR is started.

    #: network_sink module, sink_modules/network_sink/src/main.cpp: config
    #: keyed by *stream* name in <root>/network_sink_config.json. TCP mode
    #: listens on hostname:port and accepts ONE client at a time; samples are
    #: int16 little-endian (volk_32f_s32f_convert_16i, scale 32768), mono
    #: unless "stereo", raw PCM with no framing, at "sampleRate" (double).
    audio_default_port: int = 7355
    audio_config_file: str = "network_sink_config.json"
    audio_module: str = "network_sink"
    audio_instance: str = "Network Sink"
    audio_sink_name: str = "Network"          # what the stream's "sink" selects
    protocol_tcp: int = 0                     # SINK_MODE_TCP
    protocol_udp: int = 1                     # SINK_MODE_UDP (upstream default)
    sample_format: str = "s16le"
    default_sample_rate: float = 48000.0

    #: core/src/core.cpp defConfig: "moduleInstances"[name] = {"module", "enabled"};
    #: "streams"["Radio"] = {"muted", "sink", "volume"}; "source" = "".
    #: core/src/command_args.cpp: --root default "$HOME/Library/Application
    #: Support/sdrpp" (macOS bundle), "$HOME/.config/sdrpp" (Linux);
    #: --autostart starts the SDR after loading.
    config_file: str = "config.json"
    radio_stream: str = "Radio"
    rtl_source_module: str = "rtl_sdr_source"
    rtl_source_instance: str = "RTL-SDR Source"
    rtl_source_name: str = "RTL-SDR"
    macos_app: str = "/Applications/SDR++.app"
    macos_executable: str = "Contents/MacOS/sdrpp"


@dataclasses.dataclass(frozen=True)
class DsdNeoContract:
    """dsd-neo (arancormonk/dsd-neo 630a123e2d7dd52d66b8444314049c50d904a41e,
    project version 2.9.0; docs/cli.md, docs/network-audio.md; behaviour
    observed on the binary built from that revision here)."""

    #: "-i tcp[:host:port]" - dsd-neo is the CLIENT; it connects to a PCM
    #: producer (SDR++'s TCP network sink). s16le, mono, "-s" Hz (48000).
    input_sample_rate: int = 48000
    #: Observed: connection refused -> exits 0 promptly; producer closing the
    #: connection -> dsd-neo keeps running and must be terminated.
    #: "-o -": decoded audio on stdout. Observed: s16le, 8000 Hz, 2 channels
    #: interleaved - left = TDMA slot 1, right = slot 2 (channels differ).
    output_sample_rate: int = 8000
    output_channels: int = 2
    #: "-w <file>": the same decoded audio to a WAV. Observed: 8000 Hz stereo;
    #: receiver noise produced a 44-byte (empty) file, i.e. nothing decoded.
    #: "-P -7 <dir>": per-call WAVs named <stamp>_<proto>_CC_<n>__TGT_<n>_SRC_<n>.wav.
    #: "-V <0|1|2|3>": TDMA voice synthesis, 3 = both slots (default).
    #: Decoder events go to STDERR, e.g.
    #: "06:13:40 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1".
    sync_line: str = r"Sync:\s+\+?(?P<proto>[A-Z0-9]+)\s+(?P<slots>.*?)\|"
    #: Observed decodes from the project's own IQ fixtures, both through
    #: "--iq-replay" and through FM-discriminated 48 kHz audio (the shape of
    #: SDR++'s network sink): DMR (-fs), P25 Phase 1 C4FM (-f1). Receiver
    #: noise (noise_floor.iq, -fa): no audio. CQPSK/LSM P25 was not tried
    #: through audio; it is expected to need the IQ path.
    demonstrated_baseband: Tuple[str, ...] = ("dmr-dual", "p25p1")
    #: License: GPL-3.0-or-later (portions ISC); mbelib-neo GPL-2.0-or-later.
    license: str = "GPL-3.0-or-later"


SDRPP = SdrppContract()
DSD_NEO = DsdNeoContract()
