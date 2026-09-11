"""One authority over the receiver: finds the parts, configures SDR++,
starts or attaches to it, tunes it, hands the pipeline a source, and
releases everything in order."""

from __future__ import annotations

import logging
import pathlib
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..analysis.dsd import DsdNeoAnalyser, resolve_preset
from .contract import DSD_NEO, SDRPP
from .sdrpp import (RigctlClient, RigctlError, SdrppConfigurator, SdrppProcess,
                    default_root, find_sdrpp, rigctl_reachable, rtl_sdr_present)
from .stream import DecodedVoiceSource, PcmTcpSource, TuningState

log = logging.getLogger(__name__)


class ReceiverError(RuntimeError):
    """The receiver could not be used. Monitoring does not start, and no
    other input is substituted."""


class ReceiverUnavailable(ReceiverError):
    pass


def receiver_status(config) -> Dict[str, Any]:
    """What is installed and what is reachable, for the window and Field
    Check. Never raises; never claims what it did not observe."""
    receiver = getattr(config, "receiver", None)
    sdrpp = find_sdrpp(getattr(receiver, "sdrpp_path", "") or "")
    analyser = DsdNeoAnalyser.from_config(config)
    dsd = analyser.resolve_executable()
    running = rigctl_reachable(receiver.rigctl_host, receiver.rigctl_port) if receiver else False
    usb = rtl_sdr_present()
    problems: List[str] = []
    if not sdrpp and not running:
        problems.append("SDR++ is not installed (expected /Applications/SDR++.app) "
                        "and is not running.")
    if receiver is not None and receiver.digital and not dsd:
        problems.append("DSD-neo is not installed, so digital voice cannot be decoded; "
                        "analog reception still works.")
    if usb is False:
        problems.append("No RTL-SDR receiver is on the USB bus.")
    return {"sdrpp": sdrpp or "", "sdrpp_running": running, "dsd_neo": dsd or "",
            "dsd_neo_version": analyser.version() if dsd else "",
            "rtl_sdr_present": usb, "root": str(default_root()),
            "available": bool((sdrpp or running) and (not receiver or not receiver.digital or dsd)),
            "problems": problems}


class ReceiverController:
    """Owns the receiver session for the application. One per app."""

    def __init__(self, config, on_status: Optional[Callable[[str, str], None]] = None):
        self.config = config
        self.on_status = on_status
        self.tuning = TuningState()
        self._lock = threading.RLock()
        self._rigctl: Optional[RigctlClient] = None
        self._process: Optional[SdrppProcess] = None
        self.source = None
        self.configuration_changes: List[str] = []
        self.notes: List[str] = []

    # -- status -------------------------------------------------------------
    @property
    def receiver(self):
        return self.config.receiver

    def status(self) -> Dict[str, Any]:
        state = receiver_status(self.config)
        state.update({"tuning": {"requested_hz": self.tuning.requested_hz,
                                 "confirmed_hz": self.tuning.confirmed_hz,
                                 "mode": self.tuning.mode},
                      "launched_by_us": bool(self._process and self._process.alive),
                      "source": getattr(self.source, "name", "")})
        return state

    def _report(self, kind: str, message: str) -> None:
        log.info("receiver: %s %s", kind, message)
        if self.on_status is not None:
            try:
                self.on_status(kind, message)
            except Exception:  # noqa: BLE001
                log.exception("receiver status callback failed")

    # -- SDR++ ------------------------------------------------------------------
    def ensure_sdrpp(self, timeout: Optional[float] = None) -> None:
        """SDR++ configured for us and answering on rigctl - attached if it is
        already running, launched by us if not. Raises ReceiverUnavailable."""
        receiver = self.receiver
        timeout = receiver.connect_timeout_s if timeout is None else timeout
        with self._lock:
            if rigctl_reachable(receiver.rigctl_host, receiver.rigctl_port):
                # Already running (perhaps the operator opened it). One owner of
                # the USB receiver: we attach, we do not start a second one -
                # and its files are not rewritten under a running program.
                self.notes.append("attached to a running SDR++")
                return
            executable = find_sdrpp(receiver.sdrpp_path)
            if not executable:
                raise ReceiverUnavailable(
                    "SDR++ is not installed. Install SDR++ (the receiver window) "
                    "and try again. BabelFishR will not record from anything else "
                    "in its place.")
            root = pathlib.Path(receiver.sdrpp_root).expanduser() if receiver.sdrpp_root \
                else default_root()
            self.configuration_changes = SdrppConfigurator(root).ensure(
                audio_host=receiver.audio_host, audio_port=receiver.audio_port,
                sample_rate=receiver.sample_rate, rigctl_host=receiver.rigctl_host,
                rigctl_port=receiver.rigctl_port)
            if receiver.launch_sdrpp:
                self._process = SdrppProcess(executable, root=root if receiver.sdrpp_root else None,
                                             autostart=True)
                self._process.launch()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if rigctl_reachable(receiver.rigctl_host, receiver.rigctl_port):
                    return
                if self._process is not None and not self._process.alive:
                    raise ReceiverUnavailable(
                        f"SDR++ exited right after starting (code "
                        f"{self._process.process.returncode}). Open SDR++ yourself "
                        f"to see what it reports, then try again.")
                time.sleep(0.2)
            raise ReceiverUnavailable(
                f"SDR++ did not answer on its rigctl port {receiver.rigctl_host}:"
                f"{receiver.rigctl_port} within {timeout:.0f}s. If SDR++ is open, "
                f"enable its Rigctl Server module (BabelFishR sets this up when it "
                f"starts SDR++ itself).")

    def _rig(self) -> RigctlClient:
        if self._rigctl is None or not self._rigctl.connected:
            self._rigctl = RigctlClient(self.receiver.rigctl_host, self.receiver.rigctl_port)
            self._rigctl.connect()
        return self._rigctl

    # -- tuning -----------------------------------------------------------------
    def tune(self, frequency_hz: Optional[float] = None, mode: Optional[str] = None,
             bandwidth_hz: Optional[int] = None) -> Dict[str, Any]:
        """Ask SDR++ to tune, then read back what it did. The read-back is the
        confirmed value; the request is kept separately. A source that is
        streaming drops what it buffered under the previous tuning."""
        receiver = self.receiver
        frequency_hz = receiver.frequency_hz if frequency_hz is None else frequency_hz
        mode = (receiver.mode if mode is None else mode) or ""
        bandwidth_hz = receiver.bandwidth_hz if bandwidth_hz is None else bandwidth_hz
        with self._lock:
            rig = self._rig()
            try:
                if frequency_hz:
                    self.tuning.requested_hz = float(frequency_hz)
                    rig.set_frequency(frequency_hz)
                if mode:
                    rig.set_mode(mode, bandwidth_hz if bandwidth_hz else -1)
                confirmed = rig.frequency()
                actual_mode, actual_bw = rig.mode()
            except RigctlError as exc:
                self._rigctl = None
                raise ReceiverError(str(exc)) from exc
            from ..models import utcnow

            self.tuning.confirmed_hz = confirmed
            self.tuning.mode = actual_mode
            self.tuning.bandwidth_hz = actual_bw if actual_bw > 0 else None
            self.tuning.confirmed_at = utcnow()
            if self.source is not None:
                self.source.flush()
            self._report("tuned", f"SDR++ reports {confirmed / 1e6:.4f} MHz {actual_mode}")
            return {"requested_hz": self.tuning.requested_hz, "confirmed_hz": confirmed,
                    "mode": actual_mode, "bandwidth_hz": actual_bw}

    def start_radio(self) -> None:
        with self._lock:
            try:
                self._rig().start()
            except RigctlError as exc:
                self._rigctl = None
                raise ReceiverError(str(exc)) from exc

    # -- the source -------------------------------------------------------------
    def open_source(self):
        """Everything up to a started-able AudioSource for the pipeline:
        SDR++ answering, tuned as configured, radio started, and either the
        analog stream or dsd-neo's decoded stream. Raises ReceiverError; never
        returns a different input."""
        receiver = self.receiver
        self.ensure_sdrpp()
        self.tune()
        self.start_radio()
        if receiver.digital:
            analyser = DsdNeoAnalyser.from_config(self.config)
            executable = analyser.resolve_executable()
            if not executable:
                raise ReceiverUnavailable(analyser.unavailable_reason())
            preset = resolve_preset(receiver.digital_protocol)
            self.source = DecodedVoiceSource(
                executable, receiver.audio_host, receiver.audio_port, self.tuning,
                protocol_flag=preset.flag, slot=receiver.digital_slot,
                input_sample_rate=receiver.sample_rate, on_status=self.on_status,
                extra_args=list(self.config.analysis.dsd_args or []))
        else:
            self.source = PcmTcpSource(
                receiver.audio_host, receiver.audio_port, self.tuning,
                sample_rate=receiver.sample_rate, on_status=self.on_status,
                connect_timeout=receiver.connect_timeout_s)
        return self.source

    def release_source(self) -> None:
        """Monitoring stopped: our consumers go; SDR++ keeps running so the
        operator's receiver window stays as it is."""
        with self._lock:
            source, self.source = self.source, None
        if source is not None and source.running:
            source.stop()

    def shutdown(self) -> None:
        """Application exit: release the source, close rigctl, and stop the
        SDR++ we started (never one the operator started)."""
        self.release_source()
        with self._lock:
            if self._rigctl is not None:
                self._rigctl.close()
                self._rigctl = None
            if self._process is not None:
                self._process.terminate()
                self._process = None
