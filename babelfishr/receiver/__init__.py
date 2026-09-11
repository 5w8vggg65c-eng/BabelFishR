"""SDR reception through existing software: SDR++ receives, DSD-neo decodes.

BabelFishR does not drive the RTL-SDR itself. SDR++ owns the USB receiver and
is the operator's receiver window (band, frequency, mode). It hands the
demodulated audio to BabelFishR over its network sink and takes tuning
commands over its rigctl server. For digital voice the audio goes through
DSD-neo first, and BabelFishR receives the decoded speech.

Contracts pinned from the upstream sources are in :mod:`.contract`.
"""

from .contract import DSD_NEO, SDRPP
from .controller import ReceiverController, ReceiverError, ReceiverUnavailable, receiver_status
from .stream import DecodedVoiceSource, PcmTcpSource

__all__ = ["DSD_NEO", "SDRPP", "ReceiverController", "ReceiverError",
           "ReceiverUnavailable", "receiver_status", "DecodedVoiceSource",
           "PcmTcpSource"]
