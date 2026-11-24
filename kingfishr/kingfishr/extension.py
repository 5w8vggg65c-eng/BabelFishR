"""KingFishR's implementations of the BabelFishR extension hooks.

Each hook imports its SDR/DSD code only when called, so registering the
extension loads nothing heavy.
"""

from __future__ import annotations

RECEIVER_TEXT = "SDR receiver  —  SDR++ with the RTL-SDR (opens the receiver window)"


class KingFishRExtension:
    name = "kingfishr"

    # -- the SDR receiver as a named input -----------------------------
    def receiver_status(self, config) -> dict:
        from .receiver import receiver_status

        status = dict(receiver_status(config))
        running = bool(status.get("sdrpp_running"))
        # Listed when SDR++ is installed or running - never invented on a
        # machine without it (the input panel also lists it when chosen).
        status["offered"] = bool(status.get("sdrpp") or running)
        status["label"] = "SDR receiver (SDR++)"
        status["summary"] = (f"SDR receiver (SDR++ "
                             f"{'running' if running else 'will be started'})")
        return status

    def receiver_input_text(self) -> str:
        return RECEIVER_TEXT

    def receiver_controller(self, app):
        from .receiver import ReceiverController

        return ReceiverController(
            app.config,
            on_status=lambda kind, message: app.events.publish(
                "audio-status", {"kind": kind, "message": message}))

    # -- checks, preparation, command line -----------------------------
    def readiness_checks(self, config):
        from .readiness import checks

        return checks(config)

    def preparation_checks(self, config, result, say) -> None:
        from .preparation import dsd_line

        dsd_line(config, result, say)

    def cli_commands(self, sub) -> None:
        from .digital import cmd_analyze

        p = sub.add_parser("analyze", help="run digital post-processing on a recording")
        p.add_argument("transmission", help="transmission id")
        p.add_argument("--protocol", help="force a protocol, e.g. DMR")
        p.set_defaults(func=cmd_analyze)

    # -- the window ------------------------------------------------------
    def window_built(self, window) -> None:
        from .ui.receiver_window import install

        install(window)

    def bubble_menu(self, bubble, menu) -> None:
        from .ui.bubble import add_analysis_actions

        add_analysis_actions(bubble, menu)

    def audio_status(self, window, kind: str, message: str) -> None:
        from .ui.receiver_window import audio_status

        audio_status(window, kind, message)


EXTENSION = KingFishRExtension()
