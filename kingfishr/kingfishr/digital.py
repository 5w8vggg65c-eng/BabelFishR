"""Digital analysis of stored recordings with DSD-neo (KingFishR).

Moved from BabelFishR: ``BabelFishRApp.analyser`` / ``analyze_digital``,
``babelfishr.ui.workers.analysis_job`` and the ``analyze`` command. The
recording is never changed; the attempt is recorded on the transmission.
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

from babelfishr.app import ProcessingBusy


def analyser(config):
    """The configured DSD-neo analyser, or None when unavailable."""
    from .analysis import DsdNeoAnalyser

    engine = DsdNeoAnalyser.from_config(config)
    return engine if engine.available() else None

def analyze_digital(app, tx_id: str, protocol: str = "",
                    timeout: Optional[float] = None):
    """Run digital analysis over a recording. Returns the attempt, or None.

    Never raises on a missing tool or a failed decode: the outcome is
    recorded on the transmission and the recording is untouched.
    """
    from .analysis.base import AnalysisRequest
    from .analysis.dsd import DsdNeoAnalyser
    from .decoded_metadata import apply_decoded_metadata

    try:
        # Registered for the whole run, result save included: this is a
        # store user shutdown has to wait for. Refused once Quit is
        # pending - returns None rather than start work that could not
        # be saved.
        with app.activity("digital analysis"):
            tx = app.store.get_transmission(tx_id)
            if tx is None:
                return None
            engine = DsdNeoAnalyser.from_config(app.config)
            attempt = engine.analyse(AnalysisRequest(
                transmission=tx, protocol=protocol,
                timeout=timeout or app.config.analysis.timeout))
            tx.analysis_attempts.append(attempt)
            apply_decoded_metadata(tx, attempt)
            app.store.save_transmission(tx)
            app.events.publish("updated", tx)
            return attempt
    except ProcessingBusy:
        return None


def analysis_job(app, transmission_id: str, protocol: str = "",
                 report: Callable[[str], None] = lambda text: None):
    """Digital analysis off the GUI thread; dsd-neo can run for seconds."""
    report("Running digital analysis...")
    return analyze_digital(app, transmission_id, protocol=protocol)


def cmd_analyze(args) -> int:
    """Run digital post-processing over a stored recording."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.config import Config
    from babelfishr.storage import Store

    config = Config.load(args.config)
    app = BabelFishRApp(config=config, store=Store(config.database))
    attempt = analyze_digital(app, args.transmission, protocol=args.protocol or "")
    if attempt is None:
        print(f"No such transmission: {args.transmission}", file=sys.stderr)
        return 1
    print(f"engine    : {attempt.engine} {attempt.engine_version}")
    print(f"input     : {attempt.input_path}"
          + (" (derived copy)" if attempt.input_is_derived else " (original)"))
    print(f"command   : {' '.join(attempt.command) or '-'}")
    print(f"runtime   : {attempt.runtime_seconds:.2f}s  exit={attempt.exit_status}")
    print(f"outcome   : {attempt.outcome.label}")
    if attempt.protocol:
        print(f"protocol  : {attempt.protocol}")
    if attempt.metadata:
        print(f"metadata  : {attempt.metadata}")
    if attempt.decoded_audio:
        print(f"decoded   : {attempt.decoded_audio}")
    if attempt.error:
        print(f"error     : {attempt.error}")
    print("\nThe original recording is unchanged.")
    return 0 if attempt.outcome.is_success else 2
