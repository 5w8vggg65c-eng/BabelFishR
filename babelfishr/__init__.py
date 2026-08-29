"""BabelFishR - receive, record, transcribe and translate radio traffic.

Receive-only by design: BabelFishR takes demodulated audio from a radio's
accessory output, detects each transmission, records it, transcribes it and
translates it into the operator's language.  It never transmits, and it never
claims to know anything the audio cannot tell it.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
