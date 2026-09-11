#!/usr/bin/env python3
"""A stand-in for SDR++ at BabelFishR's integration boundary.

It reproduces the two interfaces BabelFishR uses, as read from SDR++'s
sources at 8c9f5ee8fe405775bfcd62c8c8f8c0fc928a64af (misc_modules/
rigctl_server/src/main.cpp, sink_modules/network_sink/src/main.cpp,
core/src/signal_path/sink.cpp): a rigctl line server that serves ONE client
at a time (F/f/M/m/q answer; \\start and \\stop change the play state and
write NOTHING back) and a TCP network audio sink that listens and streams
raw int16 mono PCM to the one client it accepts, only while playing. It
reads the config files BabelFishR writes under --root with the same typed
reads SDR++ performs (a missing or null key is fatal, as nlohmann::json's
typed get would throw), so the configurator is exercised for real. It
proves NOTHING about SDR++ itself or about radio reception: the audio it
streams is the WAV named by FAKE_SDRPP_WAV.

Environment: FAKE_SDRPP_WAV (48 kHz mono WAV to stream; silence if unset),
FAKE_SDRPP_LOG (file that receives one line per rigctl command and event),
FAKE_SDRPP_LOOP=1 (loop the WAV), FAKE_SDRPP_REFUSE_TUNE=1 (reply RPRT 1 to F),
FAKE_SDRPP_DROP_AUDIO_AFTER=<seconds> (close the audio client after that
long while keeping rigctl up, like a sink switched off in the SDR++ window),
FAKE_SDRPP_NO_RIGCTL=1 (run without the rigctl server, whatever the config),
FAKE_SDRPP_AUDIO_STOP_AFTER=<seconds> (close the audio client and stop
listening for good after that long; rigctl stays up), FAKE_SDRPP_TUNE_FILE
(a file whose contents "<hz> [<mode> [<bw>]]" are applied as if the operator
tuned in the SDR++ window, then removed), FAKE_SDRPP_RIGCTL_DELAY=<seconds>
(every rigctl reply is delayed that long).
"""

import json
import os
import pathlib
import signal
import socket
import sys
import threading
import time
import wave


def log(text):
    path = os.environ.get("FAKE_SDRPP_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def typed(obj, key, kind, where):
    """SDR++ reads these with nlohmann::json typed gets: a missing key reads
    as null and the conversion throws. Same here: fail, loudly, naming it."""
    if key not in obj or obj[key] is None:
        raise SystemExit(f"fatal: {where}[{key!r}] is missing/null (SDR++ typed read would throw)")
    value = obj[key]
    if kind is float and isinstance(value, int):
        value = float(value)
    if kind is int and isinstance(value, bool):
        raise SystemExit(f"fatal: {where}[{key!r}] is a bool where an int is read")
    if not isinstance(value, kind):
        raise SystemExit(f"fatal: {where}[{key!r}]={value!r} is not {kind.__name__}")
    return value


def load_settings(root):
    root = pathlib.Path(root)
    core = json.loads((root / "config.json").read_text()) if (root / "config.json").exists() else {}
    sink = json.loads((root / "network_sink_config.json").read_text()) \
        if (root / "network_sink_config.json").exists() else {}
    rig = json.loads((root / "rigctl_server_config.json").read_text()) \
        if (root / "rigctl_server_config.json").exists() else {}
    # core.cpp: a string-valued instance is upgraded to {module, enabled}.
    modules = {}
    for name, inst in core.get("moduleInstances", {}).items():
        if isinstance(inst, str):
            inst = {"module": inst, "enabled": True}
        typed(inst, "module", str, f"moduleInstances[{name!r}]")
        typed(inst, "enabled", bool, f"moduleInstances[{name!r}]")
        modules[name] = inst
    # sink.cpp loadStreamConfig: sink, volume, muted - all typed.
    stream_conf = core.get("streams", {}).get("Radio")
    if stream_conf is None:
        raise SystemExit("fatal: streams['Radio'] missing")
    sink_selected = typed(stream_conf, "sink", str, "streams['Radio']")
    typed(stream_conf, "volume", float, "streams['Radio']")
    typed(stream_conf, "muted", bool, "streams['Radio']")
    if "Radio" not in modules or modules["Radio"]["module"] != "radio":
        raise SystemExit("fatal: no 'Radio' module instance - the Radio stream has no VFO")
    # network_sink: defaults inserted only when the stream entry is absent.
    stream = sink.get("Radio")
    if stream is None:
        stream = {"hostname": "localhost", "port": 7355, "protocol": 1,
                  "sampleRate": 48000.0, "stereo": False, "listening": False}
    audio = {k: typed(stream, k, t, "network_sink['Radio']") for k, t in
             (("hostname", str), ("port", int), ("protocol", int), ("sampleRate", float),
              ("stereo", bool), ("listening", bool))}
    # rigctl_server: defaults only when the whole instance is absent; all seven typed.
    server = rig.get("Rigctl Server")
    if server is None:
        server = {"host": "localhost", "port": 4532, "tuning": True, "recording": False,
                  "autoStart": False, "vfo": "", "recorder": ""}
    rigc = {k: typed(server, k, t, "rigctl['Rigctl Server']") for k, t in
            (("host", str), ("port", int), ("tuning", bool), ("recording", bool),
             ("autoStart", bool), ("vfo", str), ("recorder", str))}
    return {
        "modules": modules,
        "sink_selected": sink_selected,
        "audio_host": audio["hostname"], "audio_port": audio["port"],
        "audio_protocol": audio["protocol"], "audio_rate": int(audio["sampleRate"]),
        "audio_listening": audio["listening"],
        "rig_host": rigc["host"], "rig_port": rigc["port"],
        "rig_autostart": rigc["autoStart"], "rig_vfo": rigc["vfo"],
        "rig_tuning": rigc["tuning"],
    }


class State:
    frequency = 100_000_000
    mode = "FM"
    bandwidth = -1
    playing = False


def rigctl_server(host, port, state):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(4)
    log(f"rigctl listening {host}:{port}")
    refuse = os.environ.get("FAKE_SDRPP_REFUSE_TUNE") == "1"
    delay = float(os.environ.get("FAKE_SDRPP_RIGCTL_DELAY") or 0)

    def serve(conn):
        buffer = b""
        with conn:
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    command = line.decode("ascii", "replace").strip()
                    log(f"rigctl {command}")
                    parts = command.split()
                    if not parts:
                        continue
                    if parts[0] in ("F", "\\set_freq") and len(parts) > 1:
                        if refuse:
                            reply = "RPRT 1\n"
                        else:
                            state.frequency = int(parts[1])
                            reply = "RPRT 0\n"
                    elif parts[0] in ("f", "\\get_freq"):
                        reply = f"{state.frequency}\n"
                    elif parts[0] in ("M", "\\set_mode"):
                        if len(parts) > 1 and parts[1] == "?":
                            reply = "FM WFM AM DSB USB CW LSB RAW\n"
                        elif len(parts) > 1 and parts[1] in ("FM", "WFM", "AM", "DSB", "USB", "CW", "LSB", "RAW"):
                            state.mode = parts[1]
                            state.bandwidth = int(parts[2]) if len(parts) > 2 else -1
                            reply = "RPRT 0\n"
                        else:
                            reply = "RPRT 1\n"
                    elif parts[0] in ("m", "\\get_mode"):
                        reply = f"{state.mode}\n{state.bandwidth}\n"
                    elif parts[0] == "\\start":
                        state.playing = True       # setPlayState(true); no reply
                        continue
                    elif parts[0] == "\\stop":
                        state.playing = False      # setPlayState(false); no reply
                        continue
                    elif parts[0] in ("q", "\\quit"):
                        return
                    else:
                        reply = "RPRT 1\n"
                    if delay:
                        time.sleep(delay)          # a slow or busy SDR++ answering late
                    conn.sendall(reply.encode("ascii"))

    # One client at a time, as upstream's clientHandler does: the next
    # connection is accepted only after the current one has ended.
    while True:
        conn, _ = srv.accept()
        serve(conn)


def audio_sink(host, port, rate, state):
    """TCP mode: listen, accept one client at a time, stream int16 mono PCM
    in 20 ms pieces in real time - and only while 'playing'."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    log(f"audio listening {host}:{port} rate {rate}")
    wav_path = os.environ.get("FAKE_SDRPP_WAV")
    pcm = b""
    if wav_path:
        with wave.open(wav_path) as handle:
            assert handle.getnchannels() == 1 and handle.getsampwidth() == 2, "fixture must be mono s16"
            assert handle.getframerate() == rate, f"fixture rate {handle.getframerate()} != sink rate {rate}"
            pcm = handle.readframes(handle.getnframes())
    piece = rate * 2 // 50
    silence = b"\0" * piece
    loop = os.environ.get("FAKE_SDRPP_LOOP") == "1"
    drop_after = float(os.environ.get("FAKE_SDRPP_DROP_AUDIO_AFTER") or 0)
    stop_after = float(os.environ.get("FAKE_SDRPP_AUDIO_STOP_AFTER") or 0)
    while True:
        conn, _ = srv.accept()
        log("audio client connected")
        position = 0
        connected_at = time.time()
        sent_pieces = 0                  # paced by the clock, not by sleep
        try:
            while True:
                if drop_after and time.time() - connected_at > drop_after:
                    log("audio client dropped by the sink (FAKE_SDRPP_DROP_AUDIO_AFTER)")
                    drop_after = 0
                    break
                if stop_after and time.time() - connected_at > stop_after:
                    # The sink goes away for good (the operator removed the
                    # module, say) while SDR++ and its rigctl stay up.
                    log("audio sink stopped for good (FAKE_SDRPP_AUDIO_STOP_AFTER)")
                    conn.close()
                    srv.close()
                    return
                if not state.playing:
                    time.sleep(0.02)
                    connected_at = time.time() - sent_pieces * 0.02
                    continue
                # Real time by the clock: as many 20 ms pieces as the elapsed
                # time calls for, so a machine whose sleep() wakes late still
                # streams at the sink's rate rather than at a fraction of it.
                due = int((time.time() - connected_at) / 0.02)
                if sent_pieces >= due:
                    time.sleep(0.005)
                    continue
                if pcm and position < len(pcm):
                    data = pcm[position:position + piece]
                    position += piece
                    if position >= len(pcm) and loop:
                        position = 0
                else:
                    data = silence
                conn.sendall(data)
                sent_pieces += 1
        except OSError:
            log("audio client gone")
        finally:
            conn.close()


def main():
    root = None
    args = sys.argv[1:]
    if "--root" in args:
        root = args[args.index("--root") + 1]
    if root is None:
        root = os.path.join(os.path.expanduser("~"), ".config", "sdrpp")
    settings = load_settings(root)
    log("settings " + json.dumps(settings, sort_keys=True))
    state = State()
    state.playing = "--autostart" in args
    signal.signal(signal.SIGTERM, lambda *_: (log("terminated"), sys.exit(0)))
    threading.Thread(target=audio_sink, args=(settings["audio_host"], settings["audio_port"],
                                              settings["audio_rate"], state), daemon=True).start()
    if settings["rig_autostart"] and os.environ.get("FAKE_SDRPP_NO_RIGCTL") != "1":
        threading.Thread(target=rigctl_server, args=(settings["rig_host"], settings["rig_port"], state),
                         daemon=True).start()
    else:
        log("rigctl not started (autoStart false or FAKE_SDRPP_NO_RIGCTL)")
    tune_file = os.environ.get("FAKE_SDRPP_TUNE_FILE")
    while True:
        time.sleep(0.1)
        # The operator turning the dial in the SDR++ window: a file naming
        # "<hz> [<mode> [<bw>]]" is applied to the VFO and removed. Not a
        # rigctl command - the window's own tuning, which rigctl "f" reports.
        if tune_file and os.path.exists(tune_file):
            try:
                parts = pathlib.Path(tune_file).read_text().split()
                os.remove(tune_file)
            except OSError:
                continue
            if parts:
                state.frequency = int(float(parts[0]))
                if len(parts) > 1:
                    state.mode = parts[1]
                if len(parts) > 2:
                    state.bandwidth = int(parts[2])
                log(f"gui tune {state.frequency} {state.mode} {state.bandwidth}")


if __name__ == "__main__":
    main()
