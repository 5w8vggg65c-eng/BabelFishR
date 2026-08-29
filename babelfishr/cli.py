"""Command-line interface: diagnostics, replay, headless monitoring, export.

The diagnostic commands exist because the first thing that goes wrong with a
radio-to-computer audio chain is always the audio chain, and it is far easier
to debug from a terminal than from a GUI.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time
from typing import List, Optional

from . import __version__


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")


# ---- commands ---------------------------------------------------------
def cmd_devices(args) -> int:
    from .audio.devices import backend_status, list_input_devices

    print(backend_status())
    devices = list_input_devices()
    if not devices:
        print("\nNo audio input devices found.")
        print("On macOS, check System Settings > Privacy & Security > Microphone")
        print("and make sure your terminal (or BabelFishR) is allowed.")
        return 1
    print(f"\n{len(devices)} input device(s):\n")
    for device in devices:
        print("  " + device.describe())
    print("\nUse the index or a name fragment with --device.")
    return 0


def cmd_level(args) -> int:
    """Live level meter in the terminal - the fastest way to check the chain."""
    from .audio.meter import LevelMeter
    from .audio.source import LiveAudioSource

    source = LiveAudioSource(device=args.device, sample_rate=args.rate,
                             block_size=args.block, reconnect=False)
    try:
        source.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open the input: {exc}", file=sys.stderr)
        return 1

    meter = LevelMeter()
    print(f"Monitoring {source.name}. Press Ctrl+C to stop.\n")
    deadline = time.monotonic() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            block = source.read(timeout=1.0)
            if block is None:
                continue
            reading = meter.update(block)
            filled = int(reading.rms_fraction * 44)
            bar = "#" * filled + "-" * (44 - filled)
            flag = "  CLIP!" if reading.clipped else ""
            sys.stdout.write(
                f"\r[{bar}] rms {reading.rms_dbfs:6.1f} dBFS  "
                f"peak {reading.peak_hold_dbfs:6.1f}{flag}   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
    print(f"\n\nTotal clipped samples: {meter.clip_count}")
    return 0


def cmd_calibrate(args) -> int:
    from .audio.meter import calibrate
    from .audio.source import LiveAudioSource

    source = LiveAudioSource(device=args.device, sample_rate=args.rate,
                             block_size=args.block, reconnect=False)
    try:
        source.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open the input: {exc}", file=sys.stderr)
        return 1
    print(f"Listening to {source.name} for {args.seconds:.0f}s. "
          "Keep the channel idle (nobody talking).\n")
    try:
        result = calibrate(source, seconds=args.seconds)
    finally:
        source.stop()
    print(result.summary())
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 2


def cmd_test_record(args) -> int:
    """Record a short clip and report what was captured."""
    import numpy as np

    from .audio.meter import LevelMeter
    from .audio.source import LiveAudioSource
    from .audio.wavefile import write_wav
    from .dsp.filters import dbfs, rms

    source = LiveAudioSource(device=args.device, sample_rate=args.rate,
                             block_size=args.block, reconnect=False)
    try:
        source.start()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not open the input: {exc}", file=sys.stderr)
        return 1

    print(f"Recording {args.seconds:.0f}s from {source.name}...")
    print("Key up a radio, or speak into the audio chain, now.")
    meter = LevelMeter()
    chunks: List[np.ndarray] = []
    collected = 0.0
    started = time.monotonic()
    try:
        while collected < args.seconds:
            if time.monotonic() - started > args.seconds * 3 + 5:
                break
            block = source.read(timeout=1.0)
            if block is None:
                continue
            meter.update(block)
            chunks.append(block.samples)
            collected += block.duration
    finally:
        source.stop()

    if not chunks:
        print("\nNo audio was captured at all.", file=sys.stderr)
        print("Check the device selection and macOS microphone permission.",
              file=sys.stderr)
        return 1

    audio = np.concatenate(chunks)
    path = write_wav(args.output, audio, source.sample_rate, args.bit_depth)
    peak = dbfs(float(np.max(np.abs(audio))))
    print(f"\nWrote {path}")
    print(f"  duration     : {audio.size / source.sample_rate:.1f}s")
    print(f"  peak         : {peak:.1f} dBFS")
    print(f"  rms          : {dbfs(rms(audio)):.1f} dBFS")
    print(f"  clipped      : {meter.clip_count} samples")
    if peak < -60:
        print("\n  ! Essentially silent. The radio audio is not reaching this input.")
        return 2
    if meter.clip_count:
        print("\n  ! Clipping detected. Reduce the radio volume or interface gain.")
        return 2
    print("\n  Levels look usable. Play the file back to confirm it sounds right.")
    return 0


def cmd_engines(args) -> int:
    from .config import Config
    from .providers import (transcription_engine_status,
                            translation_engine_status)

    config = Config.load(args.config)
    print("Transcription engines:")
    for status in transcription_engine_status(config):
        mark = "OK " if status.available else "-- "
        note = " (PLACEHOLDER, not real output)" if status.is_placeholder else ""
        print(f"  [{mark}] {status.name}{note}")
        if status.reason:
            for line in status.reason.splitlines():
                print(f"          {line}")
    print("\nTranslation engines:")
    for status in translation_engine_status(config):
        mark = "OK " if status.available else "-- "
        note = " (PLACEHOLDER, not real output)" if status.is_placeholder else ""
        print(f"  [{mark}] {status.name}{note}")
        print(f"          privacy: {status.privacy}")
        if status.reason:
            for line in status.reason.splitlines():
                print(f"          {line}")
    return 0


def cmd_doctor(args) -> int:
    """Everything a new install needs checked, in one place."""
    import platform

    from .audio.devices import backend_available, backend_status, list_input_devices
    from .config import Config
    from .providers import (transcription_engine_status,
                            translation_engine_status)
    from .providers.credentials import describe_source, keychain_available

    config = Config.load(args.config)
    problems = 0

    print(f"BabelFishR {__version__}")
    print(f"  python      : {platform.python_version()} on {platform.platform()}")
    print(f"  config      : {config.source_path or 'defaults (no file)'}")
    print(f"  database    : {pathlib.Path(config.database).resolve()}")
    print(f"  recordings  : {pathlib.Path(config.recording.directory).resolve()}")

    print("\nAudio backend")
    print(f"  {backend_status()}")
    if not backend_available():
        problems += 1
        print("  ! Install the audio extra: pip install 'babelfishr[audio]'")
    else:
        devices = list_input_devices()
        print(f"  {len(devices)} input device(s)")
        for device in devices:
            print("    " + device.describe())
        if not devices:
            problems += 1
            print("  ! No input devices. On macOS check microphone permission.")

    print("\nGUI")
    try:
        import PySide6  # noqa: F401

        print(f"  PySide6 {PySide6.__version__}")
    except Exception:  # noqa: BLE001
        problems += 1
        print("  ! PySide6 not installed: pip install 'babelfishr[gui]'")

    print("\nEngines")
    real_asr = [s for s in transcription_engine_status(config)
                if s.available and not s.is_placeholder]
    real_mt = [s for s in translation_engine_status(config)
               if s.available and not s.is_placeholder]
    print(f"  real transcription engines: "
          f"{', '.join(s.name for s in real_asr) or 'NONE (mock only)'}")
    print(f"  real translation engines  : "
          f"{', '.join(s.name for s in real_mt) or 'NONE (mock only)'}")
    if not real_asr:
        problems += 1
        print("  ! Install a real ASR engine: pip install 'babelfishr[asr]'")
    if not real_mt:
        problems += 1
        print("  ! Install a real translation engine: "
              "pip install 'babelfishr[translate]' (offline) or "
              "'babelfishr[cloud]' (Claude API)")

    print("\nCredentials")
    print(f"  macOS Keychain available: {keychain_available()}")
    print(f"  ANTHROPIC_API_KEY: {describe_source('ANTHROPIC_API_KEY')}")

    print(f"\n{problems} problem(s) found." if problems else "\nNo problems found.")
    return 1 if problems else 0


def cmd_replay(args) -> int:
    """Run a WAV file through exactly the same pipeline as live audio."""
    from .app import BabelFishRApp
    from .config import Config
    from .storage import Store

    config = Config.load(args.config)
    if args.database:
        config.database = args.database
    if args.recordings:
        config.recording.directory = args.recordings
    if args.target_language:
        config.translate.target_language = args.target_language
    if args.asr:
        config.asr.engine = args.asr
    if args.translate:
        config.translate.engine = args.translate

    app = BabelFishRApp(config=config, store=Store(config.database))
    summary = app.select_engines()
    for warning in summary.warnings:
        print(f"! {warning}", file=sys.stderr)
    for notice in summary.privacy_notices:
        print(f"* {notice}", file=sys.stderr)

    app.start_session(replay_path=args.path, realtime_replay=args.realtime,
                      profile_id=args.profile, name=args.name or "replay",
                      source_language=args.source_language,
                      source_language_mode=("specified" if args.source_language
                                            else "automatic"))
    count = app.run_replay()
    transmissions = app.transmissions()
    app.stop_session()

    print(f"\n{count} transmission(s) detected in {args.path}\n")
    for tx in transmissions:
        stamp = tx.started_at.astimezone().strftime("%H:%M:%S")
        meta = f"{stamp}  {tx.duration:5.2f}s  [{tx.state.value}]"
        if tx.source_language:
            meta += f"  {tx.source_language}"
        print(meta)
        if tx.display_transcript:
            print(f"    original  : {tx.display_transcript}")
        if tx.display_translation:
            print(f"    translated: {tx.display_translation}")
        if tx.error:
            print(f"    ERROR ({tx.error.stage}): {tx.error.message}")
        if tx.audio_path:
            print(f"    audio     : {tx.audio_path}")
    if args.json:
        print(json.dumps([t.to_dict() for t in transmissions], indent=2,
                         ensure_ascii=False))
    app.close()
    return 0


def cmd_listen(args) -> int:
    """Headless live monitoring, printing each transmission as it completes."""
    from .app import BabelFishRApp
    from .config import Config
    from .storage import Store

    config = Config.load(args.config)
    if args.device:
        config.audio.device = args.device
    if args.target_language:
        config.translate.target_language = args.target_language

    app = BabelFishRApp(config=config, store=Store(config.database))
    summary = app.select_engines()
    for warning in summary.warnings:
        print(f"! {warning}", file=sys.stderr)
    for notice in summary.privacy_notices:
        print(f"* {notice}", file=sys.stderr)

    def on_event(event) -> None:
        if event.kind == "updated":
            tx = event.payload
            if tx.state.value in ("complete", "failed"):
                stamp = tx.started_at.astimezone().strftime("%H:%M:%S")
                print(f"\n[{stamp}] {tx.duration:.1f}s "
                      f"{tx.source_language or '??'}")
                if tx.display_transcript:
                    print(f"  {tx.display_transcript}")
                if tx.display_translation:
                    print(f"  -> {tx.display_translation}")
                if tx.error:
                    print(f"  ERROR ({tx.error.stage}): {tx.error.message}")
        elif event.kind == "audio-status":
            print(f"[audio] {event.payload.get('kind')}: "
                  f"{event.payload.get('message')}", file=sys.stderr)

    app.events.subscribe(on_event)
    try:
        app.start_session(device=args.device, profile_id=args.profile,
                          name=args.name or "listen")
        app.begin_capture()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not start monitoring: {exc}", file=sys.stderr)
        return 1

    print("Monitoring. Press Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        app.stop_session()
        app.close()
    return 0


def cmd_search(args) -> int:
    from .config import Config
    from .storage import Store

    config = Config.load(args.config)
    store = Store(config.database)
    results = store.search(
        args.query or "", session_id=args.session, language=args.language,
        tag=args.tag, channel=args.channel, bookmarked=args.bookmarked or None,
        limit=args.limit)
    print(f"{len(results)} match(es)\n")
    for tx in results:
        stamp = tx.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp}  {tx.duration:5.1f}s  {tx.source_language or '??'}  "
              f"{tx.channel_name}")
        if tx.display_transcript:
            print(f"    {tx.display_transcript}")
        if tx.display_translation:
            print(f"    -> {tx.display_translation}")
    if args.json:
        print(json.dumps([t.to_dict() for t in results], indent=2,
                         ensure_ascii=False))
    return 0


def cmd_sessions(args) -> int:
    from .config import Config
    from .storage import Store

    store = Store(Config.load(args.config).database)
    sessions = store.list_sessions(limit=args.limit)
    if not sessions:
        print("No sessions recorded yet.")
        return 0
    for session in sessions:
        count = len(store.list_transmissions(session_id=session.id, limit=100_000))
        state = "open" if session.is_open else "closed"
        print(f"{session.id}  {session.started_at.astimezone():%Y-%m-%d %H:%M}  "
              f"{count:4d} tx  [{state}]  {session.name or ''} "
              f"{session.profile_label}")
    return 0


def cmd_export(args) -> int:
    from . import export as export_module
    from .config import Config
    from .storage import Store

    config = Config.load(args.config)
    store = Store(config.database)
    session_id = args.session
    if not session_id:
        sessions = store.list_sessions(limit=1)
        if not sessions:
            print("No sessions to export.", file=sys.stderr)
            return 1
        session_id = sessions[0].id

    if args.format == "bundle":
        path = export_module.export_session(
            store, session_id, args.output or f"babelfishr_{session_id}",
            include_audio=not args.no_audio, archive=args.zip)
        print(f"Exported bundle to {path}")
        return 0

    session = store.get_session(session_id)
    transmissions = store.list_transmissions(session_id=session_id, limit=100_000)
    if args.format == "json":
        text = export_module.to_json(transmissions, session)
    elif args.format == "csv":
        text = export_module.to_csv(transmissions)
    else:
        text = export_module.to_markdown(transmissions, session)

    if args.output:
        pathlib.Path(args.output).write_text(text, encoding="utf-8")
        print(f"Exported {len(transmissions)} transmission(s) to {args.output}")
    else:
        print(text)
    return 0


def cmd_profiles(args) -> int:
    from .config import Config
    from .models import RadioProfile
    from .storage import Store

    store = Store(Config.load(args.config).database)
    if args.add:
        profile = RadioProfile(
            name=args.add, channel_name=args.channel or "",
            frequency_mhz=args.frequency, radio_make=args.make or "",
            radio_model=args.model or "", mode=args.mode or "")
        store.save_profile(profile)
        print(f"Added profile {profile.id}: {profile.name} - {profile.label()}")
        return 0
    profiles = store.list_profiles()
    if not profiles:
        print("No radio profiles yet. Add one with:")
        print("  babelfishr profiles --add 'My radio' --channel 'GMRS 16' "
              "--frequency 462.5750")
        return 0
    for profile in profiles:
        print(f"{profile.id}  {profile.name:24s} {profile.label()}")
    print("\nChannel and frequency are operator-supplied labels; BabelFishR "
          "cannot measure them from audio.")
    return 0


def cmd_selftest(args) -> int:
    """Run a synthetic fixture through the entire pipeline."""
    import tempfile

    from .app import BabelFishRApp
    from .config import Config
    from .storage import Store
    from .testing import standard_fixture

    workdir = pathlib.Path(args.workdir or tempfile.mkdtemp(prefix="babelfishr-"))
    workdir.mkdir(parents=True, exist_ok=True)
    fixture = standard_fixture(48_000)
    wav = fixture.write(str(workdir / "selftest.wav"))
    expected = len([t for t in fixture.transmissions
                    if t.kind == "voice" and t.duration > 0.1])

    config = Config()
    config.database = str(workdir / "selftest.sqlite3")
    config.recording.directory = str(workdir / "recordings")
    config.asr.engine = args.asr or "mock"
    config.translate.engine = args.translate or "mock"

    app = BabelFishRApp(config=config, store=Store(config.database))
    summary = app.select_engines()
    print(f"transcription: {summary.transcription}")
    print(f"translation  : {summary.translation}")
    app.start_session(replay_path=wav, name="selftest")
    captured = app.run_replay()
    transmissions = app.transmissions()
    app.stop_session()

    print(f"\nfixture     : {fixture.duration:.1f}s, {expected} voice transmissions")
    print(f"detected    : {captured}")
    recordings = list((workdir / "recordings").rglob("*.wav"))
    print(f"recorded    : {len(recordings)} WAV file(s)")
    transcribed = sum(1 for t in transmissions if t.transcript)
    translated = sum(1 for t in transmissions if t.translation)
    print(f"transcribed : {transcribed}")
    print(f"translated  : {translated}")
    print(f"workdir     : {workdir}")

    ok = (captured == expected and len(recordings) == captured
          and transcribed == captured)
    print("\nSELFTEST PASSED" if ok else "\nSELFTEST FAILED")
    app.close()
    return 0 if ok else 1


def cmd_gui(args) -> int:
    from .ui import run

    return run(config_path=args.config)


def cmd_config(args) -> int:
    from .config import Config

    config = Config.load(args.config)
    if args.write:
        path = config.save(args.write)
        print(f"Wrote {path}")
        return 0
    print(config.dump_toml())
    return 0


# ---- parser -----------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="babelfishr",
        description="Receive, record, transcribe and translate radio traffic.")
    parser.add_argument("--version", action="version", version=f"babelfishr {__version__}")
    parser.add_argument("--config", help="path to a config file")
    parser.add_argument("--log-level", default="WARNING",
                        help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    def audio_args(p):
        p.add_argument("--device", help="device index or name fragment")
        p.add_argument("--rate", type=int, default=48_000)
        p.add_argument("--block", type=int, default=2048)

    p = sub.add_parser("devices", help="list audio input devices")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("level", help="live input level meter")
    audio_args(p)
    p.add_argument("--seconds", type=float, default=0.0,
                   help="stop after N seconds (default: run until Ctrl+C)")
    p.set_defaults(func=cmd_level)

    p = sub.add_parser("calibrate", help="measure an idle channel and suggest a threshold")
    audio_args(p)
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("test-record", help="record a short test clip")
    audio_args(p)
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--output", default="babelfishr-test.wav")
    p.add_argument("--bit-depth", type=int, default=16, choices=(16, 24, 32))
    p.set_defaults(func=cmd_test_record)

    p = sub.add_parser("doctor", help="check the whole install")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("engines", help="show transcription/translation engine status")
    p.set_defaults(func=cmd_engines)

    p = sub.add_parser("replay", help="run a WAV file through the pipeline")
    p.add_argument("path")
    p.add_argument("--realtime", action="store_true",
                   help="play at wall-clock speed instead of as fast as possible")
    p.add_argument("--profile", help="radio profile id for channel metadata")
    p.add_argument("--name", help="session name")
    p.add_argument("--source-language")
    p.add_argument("--target-language")
    p.add_argument("--asr", help="override the transcription engine")
    p.add_argument("--translate", help="override the translation engine")
    p.add_argument("--database")
    p.add_argument("--recordings")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("listen", help="monitor a live input (headless)")
    p.add_argument("--device")
    p.add_argument("--profile")
    p.add_argument("--name")
    p.add_argument("--target-language")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("search", help="search transcripts and translations")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--session")
    p.add_argument("--language")
    p.add_argument("--tag")
    p.add_argument("--channel")
    p.add_argument("--bookmarked", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("sessions", help="list recorded sessions")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("export", help="export a session")
    p.add_argument("--session")
    p.add_argument("--format", choices=("md", "json", "csv", "bundle"), default="md")
    p.add_argument("--output")
    p.add_argument("--zip", action="store_true", help="zip the bundle")
    p.add_argument("--no-audio", action="store_true",
                   help="omit audio files from the bundle")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("profiles", help="list or add radio/channel profiles")
    p.add_argument("--add", metavar="NAME")
    p.add_argument("--channel")
    p.add_argument("--frequency", type=float, metavar="MHZ")
    p.add_argument("--make")
    p.add_argument("--model")
    p.add_argument("--mode")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("selftest", help="run a synthetic fixture through the pipeline")
    p.add_argument("--workdir")
    p.add_argument("--asr")
    p.add_argument("--translate")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("gui", help="launch the desktop application")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("config", help="show or write the configuration")
    p.add_argument("--write", nargs="?", const="babelfishr.toml", metavar="PATH")
    p.set_defaults(func=cmd_config)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        if str(args.log_level).upper() == "DEBUG":
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
