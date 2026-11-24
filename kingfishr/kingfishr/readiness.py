"""KingFishR's field-check rows: DSD-neo, SDR input and the SDR receiver.

Moved from ``babelfishr/readiness.py``; added to the report through the
``readiness_checks`` hook.
"""

from __future__ import annotations

from babelfishr.readiness import Check, CheckStatus


def checks(config):
    return [_dsd_check(config), _sdr_check(config), _receiver_check(config)]


def _dsd_check(config) -> Check:
    from .analysis.dsd import DsdNeoAnalyser

    analyser = DsdNeoAnalyser.from_config(config)
    if not analyser.configured:
        return Check("DSD-neo", CheckStatus.SKIP, "not configured (optional)")
    if not analyser.available():
        return Check(
            "DSD-neo", CheckStatus.WARN, analyser.unavailable_reason(),
            remedy="Install dsd-neo and set analysis.dsd_path, or leave it "
                   "unset - BabelFishR works without it.")
    return Check("DSD-neo", CheckStatus.PASS,
                 f"{analyser.executable} ({analyser.version()})",
                 data={"path": analyser.executable, "version": analyser.version()})


def _sdr_check(config) -> Check:
    from .sources import sdr_status

    status = sdr_status(config)
    if not status["configured"]:
        return Check("SDR input", CheckStatus.SKIP,
                     "not configured (optional; the audio path is the default)")
    if not status["available"]:
        return Check("SDR input", CheckStatus.WARN, status["reason"])
    return Check("SDR input", CheckStatus.PASS, status["detail"])


def _receiver_check(config) -> Check:
    from .receiver import receiver_status

    try:
        status = receiver_status(config)
    except Exception as exc:  # noqa: BLE001
        return Check("SDR receiver (SDR++ / DSD-neo)", CheckStatus.WARN,
                     f"could not be checked: {exc}")
    if not config.input_is_receiver and not status["sdrpp"] and not status["sdrpp_running"]:
        return Check("SDR receiver (SDR++ / DSD-neo)", CheckStatus.SKIP,
                     "not installed and not selected (optional; the audio path is the default)")
    detail = (f"SDR++ {'running' if status['sdrpp_running'] else 'installed'}; "
              f"DSD-neo {status['dsd_neo_version'] or 'not installed'}; "
              f"RTL-SDR on USB: "
              + {True: "yes", False: "no", None: "unknown here"}[status["rtl_sdr_present"]])
    if status["problems"]:
        return Check("SDR receiver (SDR++ / DSD-neo)", CheckStatus.WARN,
                     detail + " - " + " ".join(status["problems"]))
    return Check("SDR receiver (SDR++ / DSD-neo)", CheckStatus.PASS, detail)
