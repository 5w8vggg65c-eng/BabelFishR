"""KingFishR: BabelFishR's offline capture, transcription and translation,
plus the specialised SDR features (SDR++ receiver, DSD-neo digital voice,
experimental signalling decoders).

Nothing here runs unless KingFishR is started from its own entry point
(``kingfishr``, ``kingfishr-gui`` or ``python -m kingfishr``), which calls
:func:`activate`. BabelFishR never imports this package.

:func:`activate` sets KingFishR's product identity - its own settings,
database, recordings, models and logs under a ``KingFishR`` data folder, and
``KINGFISHR_*`` environment variables - and registers the hooks. It does not
copy, move or open BabelFishR's data.
"""

from __future__ import annotations

from babelfishr.extensions import Product

PRODUCT = Product(name="KingFishR", env_prefix="KINGFISHR", config_stem="kingfishr")


def activate() -> None:
    """Become KingFishR: product identity first, then the hooks."""
    from babelfishr import extensions

    from .extension import EXTENSION

    extensions.set_product(PRODUCT)
    extensions.register(EXTENSION)


__all__ = ["PRODUCT", "activate"]
