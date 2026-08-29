"""Result type for experimental signalling decoders.

Deliberately separate from :mod:`babelfishr.models` so that nothing in the
supported receive pipeline depends on the experimental package.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict


@dataclasses.dataclass
class DecodeResult:
    """Output of one signalling decoder run over a clip's audio."""

    decoder: str
    label: str
    confidence: float = 0.0
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)
    offset: float = 0.0
    duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)
