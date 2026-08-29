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

#: Automatic hunting rotates through the candidate profiles; upstream documents
#: a full rotation as roughly six seconds at 48 kHz. A shorter recording can
#: therefore finish before the right profile is tried, which is why a narrower
#: preset is offered and why a negative auto result means very little.
AUTO_ROTATION_SECONDS = 6.0

AUTO_FLAG = "-fa"


@dataclasses.dataclass(frozen=True)
class Preset:
    """One decoder profile, mapped to its documented dsd-neo flag."""

    id: str
    label: str
    flag: str
    note: str = ""

    def describe(self) -> str:
        return f"{self.label} ({self.flag})" + (f" - {self.note}" if self.note else "")


#: Flags per https://github.com/arancormonk/dsd-neo/blob/main/docs/cli.md
#: Kept faithful to upstream, including the distinctions an earlier version
#: collapsed: DMR dual-slot (-fs) vs single-slot mono (-fr), and NXDN48 (-fi)
#: vs NXDN96 (-fn).
PRESETS: List[Preset] = [
    Preset("auto", "Automatic (hunt all profiles)", AUTO_FLAG,
           f"a full rotation takes about {AUTO_ROTATION_SECONDS:.0f}s at 48 kHz"),
    Preset("dmr-dual", "DMR simplex, dual slot", "-fs",
           "BS/MS dual-slot decoder"),
    Preset("dmr-mono", "DMR simplex, single slot (mono)", "-fr",
           "BS/MS single-slot mono decoder"),
    Preset("p25p1", "P25 Phase 1", "-f1"),
    Preset("p25p2", "P25 Phase 2", "-f2", "6000 sps"),
    Preset("dstar", "D-STAR", "-fd"),
    Preset("nxdn48", "NXDN48", "-fi", "6.25 kHz"),
    Preset("nxdn96", "NXDN96", "-fn", "12.5 kHz"),
    Preset("x2tdma", "X2-TDMA", "-fx"),
    Preset("ysf", "System Fusion (YSF)", "-fy"),
    Preset("m17", "M17", "-fz"),
    Preset("provoice", "ProVoice", "-fp"),
    Preset("dpmr", "dPMR", "-fm"),
    Preset("edacs", "EDACS / ProVoice", "-fh"),
    Preset("edacs-esk", "EDACS / ProVoice with ESK 0xA0", "-fH"),
    Preset("edacs-ea", "EDACS EA / ProVoice", "-fe"),
    Preset("edacs-ea-esk", "EDACS EA / ProVoice with ESK 0xA0", "-fE"),
]

PRESETS_BY_ID: Dict[str, Preset] = {preset.id: preset for preset in PRESETS}

#: Accept a human label or an id, so the UI and the CLI can both be friendly.
_PRESET_ALIASES: Dict[str, str] = {
    "": "auto", "auto": "auto", "unknown": "auto",
    "dmr": "dmr-dual", "dmr simplex": "dmr-dual", "dmr dual": "dmr-dual",
    "dmr mono": "dmr-mono", "dmr single": "dmr-mono",
    "p25": "p25p1", "p25 phase 1": "p25p1", "p25 phase 2": "p25p2",
    "d-star": "dstar", "dstar": "dstar",
    "nxdn": "nxdn96", "nxdn48": "nxdn48", "nxdn96": "nxdn96",
    "x2-tdma": "x2tdma", "system fusion": "ysf", "fusion": "ysf", "ysf": "ysf",
    "m17": "m17", "provoice": "provoice", "dpmr": "dpmr", "edacs": "edacs",
}


def resolve_preset(name: str) -> Preset:
    """Map an id, label or alias onto a documented preset. Unknown -> auto."""
    key = (name or "").strip().lower()
    if key in PRESETS_BY_ID:
        return PRESETS_BY_ID[key]
    if key in _PRESET_ALIASES:
        return PRESETS_BY_ID[_PRESET_ALIASES[key]]
    return PRESETS_BY_ID["auto"]


#: Protocol recognition in decoder output. ORDER MATTERS: the Phase 2 pattern
#: must be tried before the generic P25 pattern, or "P25 Phase 2" matches the
#: Phase 1 rule first and a Phase 2 decode is mislabelled as Phase 1.
PROTOCOL_PATTERNS: List[Tuple[str, str]] = [
    (r"\bP25\s*(?:Phase\s*)?2\b|\bP25p2\b", "P25 Phase 2"),
    (r"\bP25\s*(?:Phase\s*)?1\b|\bP25p1\b|\bP25\b", "P25 Phase 1"),
    (r"\bNXDN\s*96\b", "NXDN96"),
    (r"\bNXDN\s*48\b", "NXDN48"),
    (r"\bNXDN\b", "NXDN"),
    (r"\bDMR\b", "DMR"),
    (r"\bD-?STAR\b", "D-STAR"),
    (r"\bYSF\b|\bFusion\b", "System Fusion"),
    (r"\bM17\b", "M17"),
    (r"\bdPMR\b", "dPMR"),
    (r"\bProVoice\b", "ProVoice"),
    (r"\bEDACS\b", "EDACS"),
    (r"\bX2-?TDMA\b", "X2-TDMA"),
]

_ENCRYPTED = re.compile(r"encrypt|\bENC\b|scrambl|\bKEY ID\b", re.IGNORECASE)
_SYNC = re.compile(r"sync|frame|voice|slot", re.IGNORECASE)
_VOICE = re.compile(r"\bvoice\b", re.IGNORECASE)

#: A decoded WAV must contain this much non-silent audio to count as a decode.
MIN_DECODED_SECONDS = 0.20
MIN_DECODED_RMS = 1e-3


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
        return [preset.id for preset in PRESETS]

    def presets(self) -> List[Preset]:
        return list(PRESETS)

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

        preset = resolve_preset(request.protocol)
        attempt.options.setdefault("preset", preset.id)
        attempt.options.setdefault("flag", preset.flag)
        try:
            input_path, derived = self._prepare_input(tx, request.output_dir,
                                                      attempt.id)
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
        command = self._build_command(executable, input_path, output_wav, preset)
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

        text_output = f"{attempt.stdout}\n{attempt.stderr}"
        outcome, protocol, metadata = self._interpret(
            attempt.stdout, attempt.stderr, result.returncode, output_wav)
        attempt.protocol = protocol
        attempt.metadata = metadata

        decoded = pathlib.Path(output_wav)
        if decoded.exists() and result.returncode == 0:
            usable, detail = self._decoded_audio_is_usable(decoded)
            attempt.metadata["decoded_audio"] = detail
            if usable:
                attempt.artifacts.append(AnalysisArtifact(
                    kind="decoded-audio", path=str(decoded),
                    description="Voice decoded by DSD-neo"))
                # Only claim a voice decode when the audio is real AND the
                # decoder said something corroborating. A file that merely
                # exceeds its 44-byte header is not evidence of a decode.
                if _VOICE.search(text_output) or outcome in (
                        AnalysisOutcome.PROTOCOL_IDENTIFIED,
                        AnalysisOutcome.PROTOCOL_CANDIDATE):
                    outcome = AnalysisOutcome.VOICE_DECODED
                elif outcome is AnalysisOutcome.NO_RESULT:
                    outcome = AnalysisOutcome.PROTOCOL_CANDIDATE
            else:
                decoded.unlink(missing_ok=True)
                if outcome is AnalysisOutcome.NO_RESULT:
                    attempt.metadata.setdefault(
                        "note", f"decoder wrote no usable audio ({detail})")
        elif decoded.exists():
            decoded.unlink(missing_ok=True)

        if (preset.id == "auto" and not outcome.is_success
                and self._input_duration(input_path) < AUTO_ROTATION_SECONDS):
            attempt.metadata["auto_hunt_warning"] = (
                f"Automatic hunting rotates through profiles in about "
                f"{AUTO_ROTATION_SECONDS:.0f}s; this recording is shorter, so "
                f"the correct profile may never have been tried. Try a "
                f"specific preset.")

        return self._finish(attempt, started, outcome)

    def _decoded_audio_is_usable(self, path: pathlib.Path) -> Tuple[bool, str]:
        """Is this a real decode, or an empty file the decoder happened to open?

        Requires meaningful duration and non-silent content. The previous
        44-byte header check declared VOICE_DECODED for any file dsd-neo
        created, including silent placeholders.
        """
        import numpy as np

        from ..audio.wavefile import read_wav

        try:
            samples, rate = read_wav(str(path))
        except Exception as exc:  # noqa: BLE001
            return (False, f"unreadable: {exc}")
        if rate <= 0 or samples.size == 0:
            return (False, "empty")
        duration = samples.size / float(rate)
        if duration < MIN_DECODED_SECONDS:
            return (False, f"only {duration:.2f}s")
        rms = float(np.sqrt(np.mean(np.square(samples))))
        if rms < MIN_DECODED_RMS:
            return (False, f"silent ({duration:.2f}s, rms {rms:.2e})")
        return (True, f"{duration:.2f}s, rms {rms:.3f}")

    def _input_duration(self, path: str) -> float:
        from ..audio.wavefile import wav_duration

        try:
            return wav_duration(path)
        except Exception:  # noqa: BLE001
            return 0.0

    def _prepare_input(self, tx: Transmission, output_dir: Optional[str],
                       attempt_id: str) -> Tuple[str, bool]:
        """Return a path DSD can read, deriving a copy only if necessary.

        The original recording is opened read-only and never rewritten: if a
        conversion is needed the result goes to a separate file named for this
        attempt, so a rerun or a concurrent analysis of the same recording
        cannot read or overwrite another attempt's input.
        """
        from ..audio.wavefile import read_wav, write_wav

        original = pathlib.Path(tx.audio_path)
        samples, rate = read_wav(str(original))
        if rate == DSD_SAMPLE_RATE:
            return (str(original), False)

        from ..dsp.resample import resample

        target_dir = pathlib.Path(output_dir) if output_dir else original.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        derived = target_dir / f"{original.stem}.{attempt_id}.dsd48k.wav"
        write_wav(str(derived), resample(samples, rate, DSD_SAMPLE_RATE),
                  DSD_SAMPLE_RATE, bit_depth=16)
        return (str(derived), True)

    def _build_command(self, executable: str, input_path: str, output_path: str,
                       preset: Preset) -> List[str]:
        """Build the dsd-neo invocation.

        The profile flag is always explicit - including ``-fa`` when the
        protocol is unknown. Relying on dsd-neo's default rather than naming
        the automatic profile leaves the behaviour dependent on a default that
        may change.
        """
        command = [executable, "-i", input_path, "-w", output_path,
                   "-s", str(DSD_SAMPLE_RATE), preset.flag]
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
