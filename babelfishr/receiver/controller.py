"""One authority over the receiver: finds the parts, configures SDR++,
starts or attaches to it, tunes it, hands the pipeline a source, watches
the tuning while the source runs, and releases everything in order -
never on the GUI thread's time."""

from __future__ import annotations

import ipaddress
import logging
import pathlib
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..analysis.dsd import DsdNeoAnalyser, resolve_preset
from .contract import DSD_NEO, SDRPP
from .sdrpp import (RigctlClient, RigctlError, SdrppConfigurator, SdrppProcess,
                    default_root, find_sdrpp, rigctl_reachable, rtl_sdr_present,
                    sdrpp_processes)
from .stream import DecodedVoiceSource, PcmTcpSource, TuningState

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ReceiverError(RuntimeError):
    """The receiver could not be used. Monitoring does not start, and no
    other input is substituted."""


class ReceiverUnavailable(ReceiverError):
    pass


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Discovery:
    """What is installed and what is on the bus - found off the GUI thread.

    The cheap facts (paths on disk) are read at once; the slow ones (the
    USB probe, dsd-neo's version) run on a background thread and are cached
    for a while. A snapshot never blocks and says what it does not yet
    know.
    """

    TTL = 30.0

    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._slow: Dict[str, Any] = {"rtl_sdr_present": None, "dsd_neo_version": "",
                                      "probed_at": None, "probing": False}
        self._thread: Optional[threading.Thread] = None

    def snapshot(self, refresh: bool = True) -> Dict[str, Any]:
        receiver = getattr(self.config, "receiver", None)
        sdrpp = find_sdrpp(getattr(receiver, "sdrpp_path", "") or "")
        analyser = DsdNeoAnalyser.from_config(self.config)
        dsd = analyser.resolve_executable()
        with self._lock:
            slow = dict(self._slow)
            stale = slow["probed_at"] is None or time.monotonic() - slow["probed_at"] > self.TTL
            if refresh and stale and not slow["probing"]:
                self._slow["probing"] = True
                self._thread = threading.Thread(target=self._probe, args=(dsd,),
                                                daemon=True, name="receiver discovery")
                self._thread.start()
        return {"sdrpp": sdrpp or "", "dsd_neo": dsd or "",
                "dsd_neo_version": slow["dsd_neo_version"] if dsd else "",
                "rtl_sdr_present": slow["rtl_sdr_present"],
                "probed": slow["probed_at"] is not None, "probing": slow["probing"]}

    def _probe(self, dsd: str) -> None:
        usb = rtl_sdr_present()
        version = ""
        if dsd:
            try:
                version = DsdNeoAnalyser(executable=dsd).version()
            except Exception:  # noqa: BLE001
                version = "unknown"
        with self._lock:
            self._slow.update({"rtl_sdr_present": usb, "dsd_neo_version": version,
                               "probed_at": time.monotonic(), "probing": False})

    def wait(self, timeout: float = 20.0) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


def receiver_status(config, discovery: Optional[Discovery] = None,
                    controller: Optional["ReceiverController"] = None) -> Dict[str, Any]:
    """For the window and Field Check. Never raises; never claims what it did
    not observe; never blocks on a probe. Four different things, kept apart:
    software found, USB receiver detected, control connected, audio
    actually receiving."""
    receiver = getattr(config, "receiver", None)
    found = (discovery or Discovery(config)).snapshot()
    control = controller is not None and controller.control_connected
    running = control or (bool(receiver) and rigctl_reachable(receiver.rigctl_host,
                                                               receiver.rigctl_port, timeout=0.2))
    receiving = controller is not None and controller.audio_receiving
    problems: List[str] = []
    if not found["sdrpp"] and not running:
        problems.append("SDR++ is not installed (expected /Applications/SDR++.app) "
                        "and is not running.")
    if receiver is not None and receiver.digital and not found["dsd_neo"]:
        problems.append("DSD-neo is not installed, so digital voice cannot be decoded; "
                        "analog reception still works.")
    if found["rtl_sdr_present"] is False:
        problems.append("No RTL-SDR receiver is on the USB bus.")
    if receiver is not None:
        for host in (receiver.rigctl_host, receiver.audio_host):
            if not _is_loopback(host) and not receiver.allow_remote_receiver:
                problems.append(f"The receiver address {host!r} is not this computer; only "
                                f"a local SDR++ is used unless allow_remote_receiver is set.")
    return {"sdrpp": found["sdrpp"], "sdrpp_running": running, "control_connected": control,
            "audio_receiving": receiving, "dsd_neo": found["dsd_neo"],
            "dsd_neo_version": found["dsd_neo_version"],
            "rtl_sdr_present": found["rtl_sdr_present"], "usb_probed": found["probed"],
            "root": str(default_root()),
            "available": bool((found["sdrpp"] or running)
                              and (not receiver or not receiver.digital or found["dsd_neo"])
                              and not any("not this computer" in p for p in problems)),
            "problems": problems}


class ReceiverShutdown:
    """The receiver's part of Quit, on its own thread: our source is asked to
    end, dsd-neo is waited for, rigctl closes, and an SDR++ we started is
    stopped. The application waits for :attr:`settled` and reads
    :attr:`error`; a failure is reported, never silently counted as done."""

    def __init__(self, work: Callable[[], None]):
        self._work = work
        self.error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="receiver shutdown")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._work()
        except BaseException as exc:  # noqa: BLE001 - recorded for the owner
            self.error = exc
            log.exception("receiver shutdown failed")

    @property
    def settled(self) -> bool:
        return not self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> bool:
        self._thread.join(timeout)
        return self.settled


class ReceiverController:
    """Owns the receiver session for the application. One per app."""

    POLL_SECONDS = 1.0

    def __init__(self, config, on_status: Optional[Callable[[str, str], None]] = None):
        self.config = config
        self.on_status = on_status
        self.tuning = TuningState()
        self.discovery = Discovery(config)
        self._lock = threading.RLock()
        self._rigctl: Optional[RigctlClient] = None
        self._process: Optional[SdrppProcess] = None
        self.source = None
        self.configuration_changes: List[str] = []
        self.notes: List[str] = []
        self.pending_tune = False
        self._poller: Optional[threading.Thread] = None
        self._polling = False
        self.shutdown_handle: Optional[ReceiverShutdown] = None

    # -- status -------------------------------------------------------------
    @property
    def receiver(self):
        return self.config.receiver

    @property
    def control_connected(self) -> bool:
        return self._rigctl is not None and self._rigctl.connected

    @property
    def audio_receiving(self) -> bool:
        return self.source is not None and self.source.receiving

    def status(self) -> Dict[str, Any]:
        state = receiver_status(self.config, self.discovery, self)
        state.update({"tuning": {"requested_hz": self.tuning.requested_hz,
                                 "confirmed_hz": self.tuning.confirmed_hz,
                                 "mode": self.tuning.mode,
                                 "confirmed_at": self.tuning.confirmed_at,
                                 "radio_start_requested": self.tuning.play_requested,
                                 "radio_start_confirmed": None},
                      "launched_by_us": bool(self._process and self._process.alive),
                      "source": getattr(self.source, "name", ""),
                      "pending_tune": self.pending_tune})
        return state

    def _report(self, kind: str, message: str) -> None:
        log.info("receiver: %s %s", kind, message)
        if self.on_status is not None:
            try:
                self.on_status(kind, message)
            except Exception:  # noqa: BLE001
                log.exception("receiver status callback failed")

    def _check_local(self) -> None:
        receiver = self.receiver
        for host in (receiver.rigctl_host, receiver.audio_host):
            if not _is_loopback(host) and not receiver.allow_remote_receiver:
                raise ReceiverError(
                    f"The receiver address {host!r} is not this computer. BabelFishR "
                    f"only connects to a local SDR++ (127.0.0.1) unless "
                    f"allow_remote_receiver is set in its settings file.")

    # -- SDR++ ------------------------------------------------------------------
    def ensure_sdrpp(self, timeout: Optional[float] = None) -> None:
        """SDR++ configured for us and answering on rigctl - attached if it is
        already running, launched by us if not. Raises ReceiverUnavailable."""
        receiver = self.receiver
        self._check_local()
        timeout = receiver.connect_timeout_s if timeout is None else timeout
        with self._lock:
            if self.control_connected or rigctl_reachable(receiver.rigctl_host, receiver.rigctl_port):
                # Already running (perhaps the operator opened it). One owner of
                # the USB receiver: we attach, we do not start a second one -
                # and its files are not rewritten under a running program.
                if "attached to a running SDR++" not in self.notes:
                    self.notes.append("attached to a running SDR++")
                return
            if self._process is not None and self._process.alive:
                raise ReceiverUnavailable(
                    "The SDR++ that BabelFishR started is running but has not "
                    "answered on rigctl yet. Give it a moment, then try again.")
            root = pathlib.Path(receiver.sdrpp_root).expanduser() if receiver.sdrpp_root \
                else default_root()
            running = sdrpp_processes(find_sdrpp(receiver.sdrpp_path) or "", str(root))
            if running:
                # A closed rigctl port does not mean SDR++ is absent. Starting
                # another would put two programs on one USB receiver, and
                # rewriting a running program's files would be undone or
                # ignored. Say what is needed instead.
                raise ReceiverUnavailable(
                    f"SDR++ is already running (process {running[0]}) but its Rigctl "
                    f"Server is not listening on {receiver.rigctl_host}:"
                    f"{receiver.rigctl_port}. In SDR++, open Module Manager, add "
                    f"'Rigctl Server' if it is missing, tick Auto start and press Start "
                    f"in its panel - or quit SDR++ and use Receiver > Open receiver "
                    f"window so BabelFishR starts it configured.")
            executable = find_sdrpp(receiver.sdrpp_path)
            if not executable:
                raise ReceiverUnavailable(
                    "SDR++ is not installed. Install SDR++ (the receiver window) "
                    "and try again. BabelFishR will not record from anything else "
                    "in its place.")
            self.configuration_changes = SdrppConfigurator(root).ensure(
                audio_host=receiver.audio_host, audio_port=receiver.audio_port,
                sample_rate=receiver.sample_rate, rigctl_host=receiver.rigctl_host,
                rigctl_port=receiver.rigctl_port)
            if not receiver.launch_sdrpp:
                raise ReceiverUnavailable(
                    "SDR++ is installed but not running, and BabelFishR is set not to "
                    "start it. Open SDR++ yourself, then try again.")
            self._process = SdrppProcess(executable, root=root if receiver.sdrpp_root else None,
                                         autostart=True)
            self._process.launch()
            # Freshly started by us with a frequency the operator chose in
            # Tune receiver: that choice is applied once SDR++ answers.
            self.pending_tune = self.pending_tune or bool(receiver.frequency_hz)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if rigctl_reachable(receiver.rigctl_host, receiver.rigctl_port):
                    return
                if not self._process.alive:
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
        # One connection, held: upstream serves one rigctl client at a time,
        # so a second connection would wait behind this one.
        if self._rigctl is None or not self._rigctl.connected:
            self._rigctl = RigctlClient(self.receiver.rigctl_host, self.receiver.rigctl_port)
            self._rigctl.connect()
        return self._rigctl

    # -- tuning -----------------------------------------------------------------
    def read_back(self) -> Dict[str, Any]:
        """What SDR++ says it is tuned to, right now - the receiver window's
        own setting, whoever made it. Updates the confirmed state; marks a
        boundary on the running source when it changed."""
        with self._lock:
            rig = self._rig()
            try:
                confirmed = rig.frequency()
                mode, bandwidth = rig.mode()
            except RigctlError as exc:
                self._rigctl = None
                raise ReceiverError(str(exc)) from exc
            return self._confirm(confirmed, mode, bandwidth)

    def _confirm(self, confirmed: float, mode: str, bandwidth: int) -> Dict[str, Any]:
        from ..models import utcnow

        changed = (self.tuning.confirmed_hz is not None
                   and (confirmed != self.tuning.confirmed_hz or mode != self.tuning.mode))
        if changed and self.source is not None and self.source.running:
            # The boundary carries the tuning that is ending; the source
            # drops what it buffered under it before the new values apply.
            self.source.retuned("retune")
        self.tuning.confirmed_hz = confirmed
        self.tuning.mode = mode
        self.tuning.bandwidth_hz = bandwidth if bandwidth > 0 else None
        self.tuning.confirmed_at = utcnow()
        if changed:
            self.tuning.epoch += 1
            self._report("tuned", f"SDR++ reports {confirmed / 1e6:.4f} MHz {mode}")
        return {"confirmed_hz": confirmed, "mode": mode, "bandwidth_hz": bandwidth,
                "changed": changed}

    def tune(self, frequency_hz: Optional[float] = None, mode: Optional[str] = None,
             bandwidth_hz: Optional[int] = None) -> Dict[str, Any]:
        """The operator asked (Tune receiver): ask SDR++, then read back. The
        read-back is the confirmed value; the request is kept separately. A
        streaming source gets a boundary and drops what it buffered under
        the previous tuning."""
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
            result = self._confirm(confirmed, actual_mode, actual_bw)
            if not result["changed"] and self.source is not None and self.source.running \
                    and frequency_hz and abs(confirmed - float(frequency_hz)) < 0.5:
                pass                              # same tuning: nothing to cut
            self.pending_tune = False
            self._report("tuned", f"SDR++ reports {confirmed / 1e6:.4f} MHz {actual_mode}")
            result["requested_hz"] = self.tuning.requested_hz
            return result

    def request_start(self) -> None:
        """\\start: upstream writes no reply, so this is a request, not a
        confirmation. Whether audio then arrives is what confirms it."""
        with self._lock:
            try:
                self._rig().start()
            except RigctlError as exc:
                self._rigctl = None
                raise ReceiverError(str(exc)) from exc
            self.tuning.play_requested = True

    start_radio = request_start

    # -- watching the receiver while a source runs -----------------------------
    def _poll(self) -> None:
        receiver_lost = None
        while self._polling:
            time.sleep(self.POLL_SECONDS)
            if not self._polling:
                break
            try:
                self.read_back()
            except ReceiverError as exc:
                receiver_lost = str(exc)
                break
        if receiver_lost and self._polling:
            source = self.source
            if source is not None and source.running:
                source.end(f"SDR++ stopped answering ({receiver_lost}); the stream is over. "
                           f"Nothing else is recorded in its place.")
        self._polling = False

    def _start_polling(self) -> None:
        self._polling = True
        self._poller = threading.Thread(target=self._poll, daemon=True, name="receiver poll")
        self._poller.start()

    def _stop_polling(self) -> None:
        self._polling = False

    # -- the source -------------------------------------------------------------
    def open_source(self):
        """Everything up to a start-able AudioSource for the pipeline:
        SDR++ answering, its *current* tuning read back (an operator's choice
        in the SDR++ window is respected), a pending Tune-receiver request
        applied, the radio asked to start, and either the analog stream or
        dsd-neo's decoded stream. Raises ReceiverError; never returns a
        different input."""
        receiver = self.receiver
        self.ensure_sdrpp()
        if self.pending_tune:
            self.tune()
        else:
            self.read_back()
        self.request_start()
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
        self._start_polling()
        return self.source

    def release_source(self) -> None:
        """Monitoring stopped. Nothing is waited for here: the capture's own
        stopper thread stops the source (one owner of that stop); a source
        that never reached a capture is stopped directly, which is cheap.
        SDR++ keeps running so the operator's receiver window stays as it is."""
        self._stop_polling()
        with self._lock:
            source, self.source = self.source, None
        if source is not None and source.running and getattr(source, "_reader", None) is None:
            source.stop()

    def settle_source(self, source, timeout: float = 5.0) -> bool:
        """Wait for a stopped source's process to end. For the capture
        stopper and the shutdown thread, never the GUI thread."""
        if hasattr(source, "settle"):
            return source.settle(timeout)
        return True

    def begin_shutdown(self) -> ReceiverShutdown:
        """Application exit. Returns the handle the application waits for."""
        if self.shutdown_handle is not None and not self.shutdown_handle.settled:
            return self.shutdown_handle
        self._stop_polling()
        with self._lock:
            source, self.source = self.source, None
            rigctl, self._rigctl = self._rigctl, None
            process, self._process = self._process, None

        def work() -> None:
            # What does not end is kept, so the next begin_shutdown (the
            # application retries a failed close) tries it again rather than
            # forgetting it.
            if source is not None:
                if source.running:
                    source.stop()
                if not self.settle_source(source):
                    with self._lock:
                        self.source = source
                    raise RuntimeError("dsd-neo did not end when asked")
            if rigctl is not None:
                rigctl.close()
            if process is not None:
                process.terminate()
                if process.alive:
                    with self._lock:
                        self._process = process
                    raise RuntimeError("the SDR++ that BabelFishR started did not end")

        self.shutdown_handle = ReceiverShutdown(work)
        return self.shutdown_handle

    def shutdown(self, timeout: float = 15.0) -> bool:
        """Blocking form, for the command line and tests."""
        handle = self.begin_shutdown()
        handle.join(timeout)
        if handle.error is not None:
            raise handle.error
        return handle.settled
