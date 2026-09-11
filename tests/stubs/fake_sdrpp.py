#!/usr/bin/env python3
"""A stand-in for SDR++ at BabelFishR's integration boundary.

It reproduces the two interfaces BabelFishR uses, as read from SDR++'s
sources (misc_modules/rigctl_server/src/main.cpp, sink_modules/network_sink/
src/main.cpp): a rigctl line server (F/f/M/m/\\start/\\stop/q, "RPRT 0")
and a TCP network audio sink that listens and streams raw int16 mono PCM to
the one client it accepts. It reads the ports from the config files
BabelFishR writes under --root, exactly as SDR++ would, so the configurator
is exercised for real. It proves NOTHING about SDR++ itself or about radio
reception: the audio it streams is the WAV named by FAKE_SDRPP_WAV.

Environment: FAKE_SDRPP_WAV (48 kHz mono WAV to stream; silence if unset),
FAKE_SDRPP_LOG (file that receives one line per rigctl command and event),
FAKE_SDRPP_LOOP=1 (loop the WAV), FAKE_SDRPP_REFUSE_TUNE=1 (reply RPRT 1 to F).
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


def load_settings(root):
    root = pathlib.Path(root)
    core = json.loads((root / "config.json").read_text()) if (root / "config.json").exists() else {}
    sink = json.loads((root / "network_sink_config.json").read_text()) \
        if (root / "network_sink_config.json").exists() else {}
    rig = json.loads((root / "rigctl_server_config.json").read_text()) \
        if (root / "rigctl_server_config.json").exists() else {}
    stream = sink.get("Radio", {})
    server = rig.get("Rigctl Server", {})
    return {
        "modules": core.get("moduleInstances", {}),
        "sink_selected": core.get("streams", {}).get("Radio", {}).get("sink"),
        "audio_host": stream.get("hostname", "localhost"),
        "audio_port": int(stream.get("port", 7355)),
        "audio_protocol": stream.get("protocol", 1),
        "audio_rate": int(stream.get("sampleRate", 48000)),
        "audio_listening": bool(stream.get("listening", False)),
        "rig_host": server.get("host", "localhost"),
        "rig_port": int(server.get("port", 4532)),
        "rig_autostart": bool(server.get("autoStart", False)),
        "rig_vfo": server.get("vfo", ""),
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
                        state.playing = True
                        reply = "RPRT 0\n"
                    elif parts[0] == "\\stop":
                        state.playing = False
                        reply = "RPRT 0\n"
                    elif parts[0] in ("q", "\\quit"):
                        return
                    else:
                        reply = "RPRT 1\n"
                    conn.sendall(reply.encode("ascii"))

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


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
    while True:
        conn, _ = srv.accept()
        log("audio client connected")
        position = 0
        try:
            while True:
                if state.playing and pcm and position < len(pcm):
                    data = pcm[position:position + piece]
                    position += piece
                    if position >= len(pcm) and loop:
                        position = 0
                elif state.playing:
                    data = silence
                else:
                    time.sleep(0.02)
                    continue
                conn.sendall(data)
                time.sleep(0.02)
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
    if settings["rig_autostart"]:
        threading.Thread(target=rigctl_server, args=(settings["rig_host"], settings["rig_port"], state),
                         daemon=True).start()
    else:
        log("rigctl not autostarted (config says autoStart false)")
    while True:
        time.sleep(0.5)


if __name__ == "__main__":
    main()
