"""SDR++: where it is, how it is configured for us, and how we talk to it."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from .contract import SDRPP

log = logging.getLogger(__name__)


def default_root() -> pathlib.Path:
    """Where SDR++ keeps its config files (its --root default)."""
    home = pathlib.Path(os.environ.get("HOME") or pathlib.Path.home())
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "sdrpp"
    return home / ".config" / "sdrpp"


def find_sdrpp(configured: str = "") -> Optional[str]:
    """The SDR++ executable, or None. A configured path wins; then the
    macOS application; then PATH."""
    if configured:
        path = pathlib.Path(configured).expanduser()
        if path.is_dir() and path.suffix == ".app":
            path = path / SDRPP.macos_executable
        if path.is_file():
            return str(path)
        found = shutil.which(configured)
        if found:
            return found
        return None
    app = pathlib.Path(SDRPP.macos_app) / SDRPP.macos_executable
    if app.is_file():
        return str(app)
    return shutil.which("sdrpp")


class SdrppConfigurator:
    """Edit exactly the SDR++ settings BabelFishR needs; leave the rest.

    Three JSON files under the SDR++ root, each backed up once (``*.before-
    babelfishr.json``) before the first change. Only the keys named in the
    contract are written, and only when their value differs. SDR++ reads
    these files at start, so this has to run before SDR++ is launched; an
    SDR++ already running keeps its in-memory settings (reported, not
    forced).
    """

    def __init__(self, root: Optional[pathlib.Path] = None):
        self.root = pathlib.Path(root) if root is not None else default_root()

    def _load(self, name: str) -> Dict[str, Any]:
        path = self.root / name
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"SDR++ settings file {path} is unreadable: {exc}") from exc

    def _store(self, name: str, data: Dict[str, Any], original: Dict[str, Any]) -> None:
        path = self.root / name
        self.root.mkdir(parents=True, exist_ok=True)
        backup = self.root / f"{path.stem}.before-babelfishr.json"
        if path.is_file() and not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(json.dumps(data, indent=4), encoding="utf-8")
        log.info("SDR++ %s updated by BabelFishR (backup %s)", name, backup.name)

    def ensure(self, *, audio_host: str, audio_port: int, sample_rate: int,
               rigctl_host: str, rigctl_port: int) -> List[str]:
        """Bring the three files to what monitoring needs. Returns the list of
        changes made (empty when nothing had to change)."""
        changes: List[str] = []

        core = self._load(SDRPP.config_file)
        core_before = json.loads(json.dumps(core))
        instances = core.setdefault("moduleInstances", {})
        for instance, module in ((SDRPP.audio_instance, SDRPP.audio_module),
                                 (SDRPP.rigctl_instance, SDRPP.rigctl_module),
                                 (SDRPP.rtl_source_instance, SDRPP.rtl_source_module)):
            entry = instances.setdefault(instance, {})
            if entry.get("module") != module or entry.get("enabled") is not True:
                entry["module"], entry["enabled"] = module, True
                changes.append(f"{SDRPP.config_file}: module {instance!r} enabled")
        stream = core.setdefault("streams", {}).setdefault(SDRPP.radio_stream, {})
        if stream.get("sink") != SDRPP.audio_sink_name:
            stream["sink"] = SDRPP.audio_sink_name
            changes.append(f"{SDRPP.config_file}: stream {SDRPP.radio_stream!r} "
                           f"sink -> {SDRPP.audio_sink_name}")
        if not core.get("source"):
            # Only when no source has been chosen at all: an operator's own
            # choice of source is theirs.
            core["source"] = SDRPP.rtl_source_name
            changes.append(f"{SDRPP.config_file}: source -> {SDRPP.rtl_source_name}")
        if core != core_before:
            self._store(SDRPP.config_file, core, core_before)

        sink = self._load(SDRPP.audio_config_file)
        sink_before = json.loads(json.dumps(sink))
        entry = sink.setdefault(SDRPP.radio_stream, {})
        wanted = {"hostname": audio_host, "port": int(audio_port),
                  "protocol": SDRPP.protocol_tcp, "sampleRate": float(sample_rate),
                  "stereo": False, "listening": True}
        for key, value in wanted.items():
            if entry.get(key) != value:
                entry[key] = value
                changes.append(f"{SDRPP.audio_config_file}: {key} -> {value!r}")
        if sink != sink_before:
            self._store(SDRPP.audio_config_file, sink, sink_before)

        rig = self._load(SDRPP.rigctl_config_file)
        rig_before = json.loads(json.dumps(rig))
        entry = rig.setdefault(SDRPP.rigctl_instance, {})
        wanted = {"host": rigctl_host, "port": int(rigctl_port), "tuning": True,
                  "autoStart": True, "vfo": SDRPP.radio_stream}
        for key, value in wanted.items():
            if entry.get(key) != value:
                entry[key] = value
                changes.append(f"{SDRPP.rigctl_config_file}: {key} -> {value!r}")
        if rig != rig_before:
            self._store(SDRPP.rigctl_config_file, rig, rig_before)
        return changes


class RigctlError(RuntimeError):
    pass


class RigctlClient:
    """One TCP line connection to SDR++'s rigctl server. Not thread-safe:
    the controller serialises its use."""

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self._sock: Optional[socket.socket] = None
        self._buffer = b""

    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(b"q\n")
            except OSError:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _line(self) -> str:
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RigctlError("SDR++ closed the rigctl connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def _command(self, text: str, lines: int = 1) -> List[str]:
        if self._sock is None:
            raise RigctlError("not connected to SDR++")
        try:
            self._sock.sendall((text + "\n").encode("ascii"))
            return [self._line() for _ in range(lines)]
        except (OSError, socket.timeout) as exc:
            raise RigctlError(f"rigctl {text!r} failed: {exc}") from exc

    @staticmethod
    def _ok(reply: str, command: str) -> None:
        if reply != "RPRT 0":
            raise RigctlError(f"SDR++ refused {command!r}: {reply or 'no reply'}")

    def set_frequency(self, hz: float) -> None:
        self._ok(self._command(f"F {int(round(hz))}")[0], "F")

    def frequency(self) -> float:
        reply = self._command("f")[0]
        try:
            return float(reply)
        except ValueError as exc:
            raise RigctlError(f"unexpected frequency reply {reply!r}") from exc

    def set_mode(self, mode: str, bandwidth_hz: int = -1) -> None:
        mode = mode.upper()
        if mode not in SDRPP.rigctl_modes:
            raise RigctlError(f"SDR++ has no mode {mode!r}; one of {SDRPP.rigctl_modes}")
        self._ok(self._command(f"M {mode} {int(bandwidth_hz)}")[0], "M")

    def mode(self) -> tuple:
        mode, bandwidth = self._command("m", lines=2)
        try:
            return mode, int(bandwidth)
        except ValueError:
            return mode, -1

    def start(self) -> None:
        self._command("\\start")

    def stop(self) -> None:
        self._command("\\stop")


def rigctl_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class SdrppProcess:
    """SDR++ as a process BabelFishR started - and only then may stop."""

    def __init__(self, executable: str, root: Optional[pathlib.Path] = None,
                 autostart: bool = True):
        self.executable = executable
        self.root = root
        self.autostart = autostart
        self.process: Optional[subprocess.Popen] = None

    def launch(self) -> None:
        command = [self.executable]
        if self.root is not None:
            command += ["--root", str(self.root)]
        if self.autostart:
            command.append("--autostart")
        self.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        log.info("launched SDR++: %s (pid %s)", " ".join(command), self.process.pid)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def terminate(self, timeout: float = 5.0) -> None:
        if not self.alive:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=timeout)


def rtl_sdr_present() -> Optional[bool]:
    """Is an RTL2832U receiver on the USB bus? True/False, or None when this
    host offers no way to tell (then nothing is claimed)."""
    try:
        if platform.system() == "Darwin":
            result = subprocess.run(["system_profiler", "SPUSBDataType", "-json"],
                                    capture_output=True, text=True, timeout=15)
            text = result.stdout.lower()
            return ("0x0bda" in text and ("0x2838" in text or "0x2832" in text)) or "rtl2838" in text
        usb = pathlib.Path("/sys/bus/usb/devices")
        if usb.is_dir():
            for device in usb.iterdir():
                vendor, product = device / "idVendor", device / "idProduct"
                if vendor.is_file() and vendor.read_text().strip() == "0bda" and \
                        product.is_file() and product.read_text().strip() in ("2838", "2832"):
                    return True
            return False
    except Exception:  # noqa: BLE001 - detection is advisory
        log.debug("USB probe failed", exc_info=True)
    return None
