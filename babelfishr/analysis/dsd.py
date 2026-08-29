"""DSD-neo integration: optional local digital-voice post-processing.

What this is
------------
DSD-neo is an external, locally installed decoder. BabelFishR treats it as an
analysis *provider* over a recording that already exists, which has three
consequences worth stating plainly:

* **It does not need an SDR.** The input is the transmission WAV that was
  captured from the radio's accessory output like any other.
* **It cannot damage anything.** The original WAV is opened read-only; when
  DSD needs a different format, a derived copy is written alongside it.
* **A failure means "no usable decode from this input", not "the recording was
  lost".** That distinction is the whole point of running analysis after
  capture rather than instead of it.

An important honest caveat
--------------------------
Ordinary speaker or accessory audio is usually the *wrong* input for a digital
decoder. It is band-limited, de-emphasised, AGC'd, and if the radio decoded the
traffic itself then what arrives is already analogue voice. Discriminator-tap
or SDR baseband audio is what DSD really wants. So a negative result here says
very little, and the UI must not present it as "this was not digital".

BabelFishR does not attempt to defeat encryption. When DSD reports encrypted
traffic, that is recorded as an outcome and nothing further is attempted.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..models import (AnalysisArtifact, AnalysisAttempt, AnalysisOutcome,
                      Transmission, utcnow)
from .base import AnalysisEngine, AnalysisRequest

log = logging.getLogger(__name__)

#: DSD family decoders conventionally want 48 kHz mono.
DSD_SAMPLE_RATE = 48_000

#: Protocol names DSD-neo commonly reports, mapped to a canonical label.
PROTOCOL_PATTERNS: List[Tuple[str, str]] = [
    (r"\bDMR\b", "DMR"),
    (r"\bP25(?:\s*Phase\s*1)?\b", "P25 Phase 1"),
    (r"\bP25\s*Phase\s*2\b", "P25 Phase 2"),
    (r"\bD-?STAR\b", "D-STAR"),
    (r"\bNXDN\s*(?:48|96)?\b", "NXDN"),
    (r"\bYSF\b|\bFusion\b", "System Fusion"),
    (r"\bdPMR\b", "dPMR"),
    (r"\bProVoice\b", "ProVoice"),
    (r"\bX2-?TDMA\b", "X2-TDMA"),
]

_ENCRYPTED = re.compile(r"encrypt|\bENC\b|scrambl|\bKEY ID\b", re.IGNORECASE)
_SYNC = re.compile(r"sync|frame|voice|slot", re.IGNORECASE)


@dataclasses.dataclass
class DsdConfig:
    executable: str = ""
    extra_args: List[str] = dataclasses.field(default_factory=list)
    timeout: float = 120.0
    auto_analyse_suspected: bool = True


class DsdNeoAnalyser(AnalysisEngine):
    """Runs a locally installed dsd-neo binary over a recording."""

    id = "dsd-neo"
    name = "DSD-neo"

    def __init__(self, executable: str = "", extra_args: Optional[List[str]] = None,
                 timeout: float = 120.0):
        self.executable = executable or ""
        self.extra_args = list(extra_args or [])
        self.timeout = timeout
        self._version: Optional[str] = None

    @classmethod
    def from_config(cls, config) -> "DsdNeoAnalyser":
        analysis = getattr(config, "analysis", None)
        return cls(
            executable=getattr(analysis, "dsd_path", "") or "",
            extra_args=list(getattr(analysis, "dsd_args", []) or []),
            timeout=float(getattr(analysis, "timeout", 120.0)),
        )

    # -- availability ----------------------------------------------------
    @property
    def configured(self) -> bool:
        """Whether the operator asked for DSD at all. It is entirely optional."""
        return bool(self.executable) or bool(self.resolve_executable())

    def resolve_executable(self) -> Optional[str]:
        if self.executable:
            path = pathlib.Path(self.executable).expanduser()
            if path.is_file():
                return str(path)
            found = shutil.which(self.executable)
            return found
        for candidate in ("dsd-neo", "dsdneo", "dsd"):
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def available(self) -> bool:
        return self.resolve_executable() is not None

    def unavailable_reason(self) -> str:
        if self.available():
            return ""
        if self.executable:
            return (f"DSD-neo was configured as {self.executable!r} but no such "
                    f"executable was found.")
        return ("DSD-neo is not installed or not on PATH. It is optional: "
                "BabelFishR records, transcribes and translates without it.")

    def version(self) -> str:
        """The exact version, captured so a decode can be reproduced later."""
        if self._version is not None:
            return self._version
        executable = self.resolve_executable()
        if executable is None:
            self._version = "not installed"
            return self._version
        for flag in ("--version", "-v", "-h"):
            try:
                result = subprocess.run([executable, flag], capture_output=True,
                                        text=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            text = f"{result.stdout}\n{result.stderr}"
            match = re.search(r"(\d+\.\d+(?:\.\d+)?[\w.-]*)", text)
            if match:
                self._version = match.group(1)
                return self._version
            if text.strip():
                self._version = text.strip().splitlines()[0][:80]
                return self._version
        self._version = "unknown"
        return self._version

    def supported_protocols(self) -> List[str]:
        return [label for _, label in PROTOCOL_PATTERNS]

    # -- analysis --------------------------------------------------------
    def analyse(self, request: AnalysisRequest) -> AnalysisAttempt:
        """Analyse a recording. Never raises; never touches the original."""
        tx = request.transmission
        attempt = AnalysisAttempt(
            transmission_id=tx.id, engine=self.id, engine_version=self.version(),
            requested_protocol=request.protocol,
            options=dict(request.options),
            attempt_number=len(tx.analysis_attempts) + 1,
        )
        started = time.monotonic()

        executable = self.resolve_executable()
        if executable is None:
            return self._finish(attempt, started, AnalysisOutcome.ANALYSIS_FAILED,
                                error=self.unavailable_reason())
        if not tx.audio_path or not pathlib.Path(tx.audio_path).exists():
            return self._finish(attempt, started, AnalysisOutcome.ANALYSIS_FAILED,
                                error="the recording is missing from disk")

        try:
            input_path, derived = self._prepare_input(tx, request.output_dir)
        except Exception as exc:  # noqa: BLE001
            return self._finish(attempt, started, AnalysisOutcome.ANALYSIS_FAILED,
                                error=f"could not prepare input: {exc}")
        attempt.input_path = input_path
        attempt.input_is_derived = derived
        if derived:
            attempt.artifacts.append(AnalysisArtifact(
                kind="derived-input", path=input_path,
                description="Resampled copy for DSD; the original is untouched."))

        # Per-attempt output path: reruns must not inherit a previous
        # attempt's decoded file, which would report a stale success.
        source = pathlib.Path(tx.audio_path)
        output_dir = pathlib.Path(request.output_dir or source.parent)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_wav = str(output_dir / f"{source.stem}.{attempt.id}.decoded.wav")
        pathlib.Path(output_wav).unlink(missing_ok=True)
        command = self._build_command(executable, input_path, output_wav,
                                      request.protocol)
        attempt.command = command

        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=request.timeout or self.timeout,
                                    check=False)
        except subprocess.TimeoutExpired:
            return self._finish(attempt, started, AnalysisOutcome.ANALYSIS_FAILED,
                                error=f"dsd-neo timed out after {request.timeout}s")
        except (OSError, subprocess.SubprocessError) as exc:
            return self._finish(attempt, started, AnalysisOutcome.ANALYSIS_FAILED,
                                error=f"could not run dsd-neo: {exc}")

        attempt.exit_status = result.returncode
        attempt.stdout = (result.stdout or "")[-20_000:]
        attempt.stderr = (result.stderr or "")[-20_000:]

        outcome, protocol, metadata = self._interpret(
            attempt.stdout, attempt.stderr, result.returncode, output_wav)
        attempt.protocol = protocol
        attempt.metadata = metadata

        decoded = pathlib.Path(output_wav)
        if decoded.exists() and decoded.stat().st_size > 44 and result.returncode == 0:
            attempt.artifacts.append(AnalysisArtifact(
                kind="decoded-audio", path=str(decoded),
                description="Voice decoded by DSD-neo"))
            if outcome in (AnalysisOutcome.PROTOCOL_IDENTIFIED,
                           AnalysisOutcome.PROTOCOL_CANDIDATE,
                           AnalysisOutcome.NO_RESULT):
                outcome = AnalysisOutcome.VOICE_DECODED
        elif decoded.exists():
            decoded.unlink(missing_ok=True)  # empty stub, not a real decode

        return self._finish(attempt, started, outcome)

    def _prepare_input(self, tx: Transmission,
                       output_dir: Optional[str]) -> Tuple[str, bool]:
        """Return a path DSD can read, deriving a copy only if necessary.

        The original recording is opened read-only and never rewritten: if a
        conversion is needed the result goes to a separate file with its own
        provenance.
        """
        from ..audio.wavefile import read_wav, write_wav

        original = pathlib.Path(tx.audio_path)
        samples, rate = read_wav(str(original))
        if rate == DSD_SAMPLE_RATE:
            return (str(original), False)

        from ..dsp.resample import resample

        target_dir = pathlib.Path(output_dir) if output_dir else original.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        derived = target_dir / f"{original.stem}.dsd48k.wav"
        write_wav(str(derived), resample(samples, rate, DSD_SAMPLE_RATE),
                  DSD_SAMPLE_RATE, bit_depth=16)
        return (str(derived), True)

    def _build_command(self, executable: str, input_path: str, output_path: str,
                       protocol: str) -> List[str]:
        command = [executable, "-i", input_path, "-w", output_path]
        if protocol:
            flag = {
                "DMR": "-fr", "P25 Phase 1": "-f1", "P25 Phase 2": "-f2",
                "D-STAR": "-fd", "NXDN": "-fn", "System Fusion": "-fy",
                "dPMR": "-fm", "X2-TDMA": "-fx",
            }.get(protocol)
            if flag:
                command.append(flag)
        command.extend(self.extra_args)
        return command

    def _interpret(self, stdout: str, stderr: str, exit_status: int,
                   output_wav: str) -> Tuple[AnalysisOutcome, str, Dict[str, object]]:
        """Map DSD-neo output onto the outcome taxonomy.

        Parsing is deliberately conservative: unrecognised output becomes
        NO_RESULT rather than an invented protocol identification.
        """
        text = f"{stdout}\n{stderr}"
        metadata: Dict[str, object] = {}

        protocols = [label for pattern, label in PROTOCOL_PATTERNS
                     if re.search(pattern, text)]
        if protocols:
            metadata["protocols_mentioned"] = protocols
        protocol = protocols[0] if protocols else ""

        for key, pattern in (("talkgroup", r"(?:TG|Talkgroup)[:\s]+(\d+)"),
                             ("source_id", r"(?:SRC|Source)[:\s]+(\d+)"),
                             ("color_code", r"(?:CC|Color Code)[:\s]+(\d+)"),
                             ("nac", r"NAC[:\s]+([0-9A-Fa-f]+)")):
            match = re.search(pattern, text)
            if match:
                metadata[key] = match.group(1)

        if _ENCRYPTED.search(text):
            return (AnalysisOutcome.ENCRYPTED_OR_UNSUPPORTED, protocol, metadata)
        if exit_status != 0 and not protocol:
            return (AnalysisOutcome.ANALYSIS_FAILED, protocol, metadata)
        if protocol and _SYNC.search(text):
            if metadata:
                return (AnalysisOutcome.PROTOCOL_IDENTIFIED, protocol, metadata)
            return (AnalysisOutcome.PROTOCOL_CANDIDATE, protocol, metadata)
        if protocol:
            return (AnalysisOutcome.PROTOCOL_CANDIDATE, protocol, metadata)
        if metadata:
            return (AnalysisOutcome.METADATA_ONLY, protocol, metadata)
        return (AnalysisOutcome.NO_RESULT, protocol, metadata)

    def _finish(self, attempt: AnalysisAttempt, started: float,
                outcome: AnalysisOutcome, error: str = "") -> AnalysisAttempt:
        attempt.outcome = outcome
        attempt.error = error
        attempt.finished_at = utcnow()
        attempt.runtime_seconds = round(time.monotonic() - started, 3)
        return attempt
