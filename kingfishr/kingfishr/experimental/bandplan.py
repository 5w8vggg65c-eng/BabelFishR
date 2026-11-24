"""Band plans and channel tables.

BabelFishR records every transmission against *the band the operator is
observing*.  A :class:`Band` describes a slice of spectrum, its default
modulation and channel step, and (where the service is channelised) a table of
named channels.  This lets a recording be filed as ``GMRS/CH16-462.5750`` rather
than an anonymous WAV file, and lets the SDR scanner know where to look.

Frequencies are in Hz.  Channel tables cover the common licence-free and
public-safety-adjacent services; add your own in ``babelfishr.toml``.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, List, Optional

MHZ = 1_000_000.0
KHZ = 1_000.0


@dataclasses.dataclass(frozen=True)
class Channel:
    number: str
    frequency_hz: float
    name: str = ""
    input_hz: Optional[float] = None
    """Repeater input (what a handheld transmits on), when different."""
    bandwidth_hz: float = 12_500.0
    modulation: str = "nfm"
    notes: str = ""

    @property
    def label(self) -> str:
        # Numeric channels read as "CH16"; named ones (WX1, R16, CALL) stand alone.
        base = f"CH{self.number}" if self.number.isdigit() else self.number
        return f"{base} {self.name}".strip()

    def to_dict(self) -> Dict[str, object]:
        d = dataclasses.asdict(self)
        d["label"] = self.label
        return d


@dataclasses.dataclass(frozen=True)
class Band:
    id: str
    name: str
    low_hz: float
    high_hz: float
    modulation: str = "nfm"
    step_hz: float = 12_500.0
    channels: List[Channel] = dataclasses.field(default_factory=list)
    region: str = "US"
    notes: str = ""

    def contains(self, freq_hz: float) -> bool:
        return self.low_hz <= freq_hz <= self.high_hz

    def channel_for(self, freq_hz: float, tolerance_hz: float = 2_500.0) -> Optional[Channel]:
        """Nearest channel to *freq_hz*, or ``None`` if nothing is close enough."""
        best: Optional[Channel] = None
        best_delta = tolerance_hz
        for ch in self.channels:
            for f in (ch.frequency_hz, ch.input_hz):
                if f is None:
                    continue
                delta = abs(f - freq_hz)
                # Strict improvement keeps the first (simplex/output) match
                # ahead of a repeater pair sharing the same output frequency.
                if delta < best_delta:
                    best, best_delta = ch, delta
        return best

    def channel_by_number(self, number: str) -> Optional[Channel]:
        number = str(number).strip().upper().lstrip("CH").strip()
        for ch in self.channels:
            if ch.number.upper() == number or ch.name.upper() == number:
                return ch
        return None

    def to_dict(self) -> Dict[str, object]:
        d = dataclasses.asdict(self)
        d["channels"] = [c.to_dict() for c in self.channels]
        return d


def _gmrs_channels() -> List[Channel]:
    # Channels 1-7 shared FRS/GMRS interstitial, 8-14 FRS-only low power
    # (GMRS 467 MHz interstitials), 15-22 GMRS main + FRS, RPT15-22 repeater
    # pairs with a +5 MHz input offset.
    simplex_1_7 = [
        462.5625, 462.5875, 462.6125, 462.6375, 462.6625, 462.6875, 462.7125,
    ]
    low_power_8_14 = [
        467.5625, 467.5875, 467.6125, 467.6375, 467.6625, 467.6875, 467.7125,
    ]
    main_15_22 = [
        462.5500, 462.5750, 462.6000, 462.6250, 462.6500, 462.6750, 462.7000, 462.7250,
    ]
    out: List[Channel] = []
    for i, f in enumerate(simplex_1_7, start=1):
        out.append(Channel(str(i), f * MHZ, "FRS/GMRS interstitial", bandwidth_hz=12_500.0))
    for i, f in enumerate(low_power_8_14, start=8):
        out.append(Channel(str(i), f * MHZ, "FRS low power", bandwidth_hz=12_500.0,
                           notes="FRS only, 0.5 W"))
    for i, f in enumerate(main_15_22, start=15):
        out.append(Channel(str(i), f * MHZ, "GMRS main", bandwidth_hz=20_000.0))
    for i, f in enumerate(main_15_22, start=15):
        out.append(Channel(f"R{i}", f * MHZ, "GMRS repeater",
                           input_hz=(f + 5.0) * MHZ, bandwidth_hz=20_000.0,
                           notes="Repeater output; handhelds transmit +5 MHz"))
    return out


def _murs_channels() -> List[Channel]:
    freqs = [
        ("1", 151.820, "MURS 1"),
        ("2", 151.880, "MURS 2"),
        ("3", 151.940, "MURS 3"),
        ("4", 154.570, "MURS 4 / Blue Dot"),
        ("5", 154.600, "MURS 5 / Green Dot"),
    ]
    out = []
    for num, f, name in freqs:
        bw = 11_250.0 if f < 154.0 else 20_000.0
        out.append(Channel(num, f * MHZ, name, bandwidth_hz=bw))
    return out


def _pmr446_channels() -> List[Channel]:
    out = []
    for i in range(8):
        out.append(Channel(str(i + 1), (446.00625 + i * 0.0125) * MHZ, "PMR446 analogue"))
    for i in range(8):
        out.append(Channel(str(i + 9), (446.10625 + i * 0.0125) * MHZ, "PMR446 analogue"))
    return out


def _marine_channels() -> List[Channel]:
    rows = [
        ("06", 156.300, "Intership safety"),
        ("09", 156.450, "Boater calling"),
        ("13", 156.650, "Bridge to bridge"),
        ("16", 156.800, "Distress / calling"),
        ("22A", 157.100, "USCG liaison"),
        ("68", 156.425, "Non-commercial"),
        ("69", 156.475, "Non-commercial"),
        ("70", 156.525, "DSC digital selective calling"),
        ("71", 156.575, "Non-commercial"),
        ("72", 156.625, "Non-commercial intership"),
        ("78A", 156.925, "Non-commercial"),
    ]
    return [Channel(n, f * MHZ, name, bandwidth_hz=25_000.0) for n, f, name in rows]


def _wx_channels() -> List[Channel]:
    freqs = [162.400, 162.425, 162.450, 162.475, 162.500, 162.525, 162.550]
    return [Channel(f"WX{i+1}", f * MHZ, "NOAA weather radio", bandwidth_hz=25_000.0)
            for i, f in enumerate(freqs)]


BUILTIN_BANDS: List[Band] = [
    Band(
        id="gmrs", name="GMRS / FRS (US 462/467 MHz)",
        low_hz=462.5 * MHZ, high_hz=467.8 * MHZ,
        modulation="nfm", step_hz=12_500.0, channels=_gmrs_channels(),
        notes="GMRS requires an FCC licence to transmit; receiving is unlicensed.",
    ),
    Band(
        id="murs", name="MURS (US 151/154 MHz)",
        low_hz=151.8 * MHZ, high_hz=154.65 * MHZ,
        modulation="nfm", step_hz=12_500.0, channels=_murs_channels(),
    ),
    Band(
        id="pmr446", name="PMR446 (EU 446 MHz)", region="EU",
        low_hz=446.0 * MHZ, high_hz=446.2 * MHZ,
        modulation="nfm", step_hz=12_500.0, channels=_pmr446_channels(),
    ),
    Band(
        id="marine-vhf", name="Marine VHF",
        low_hz=156.0 * MHZ, high_hz=162.025 * MHZ,
        modulation="nfm", step_hz=25_000.0, channels=_marine_channels(),
    ),
    Band(
        id="noaa-wx", name="NOAA Weather Radio",
        low_hz=162.4 * MHZ, high_hz=162.55 * MHZ,
        modulation="nfm", step_hz=25_000.0, channels=_wx_channels(),
    ),
    Band(
        id="ham-2m", name="Amateur 2 m (VHF)",
        low_hz=144.0 * MHZ, high_hz=148.0 * MHZ,
        modulation="nfm", step_hz=15_000.0,
        channels=[
            Channel("CALL", 146.520 * MHZ, "National simplex calling", bandwidth_hz=15_000.0),
            Channel("APRS", 144.390 * MHZ, "APRS (North America)", bandwidth_hz=15_000.0,
                    notes="AFSK1200 packet"),
            Channel("APRS-EU", 144.800 * MHZ, "APRS (Europe/IARU R1)", bandwidth_hz=15_000.0),
            Channel("SSTV", 145.500 * MHZ, "SSTV / calling", bandwidth_hz=15_000.0),
        ],
    ),
    Band(
        id="ham-70cm", name="Amateur 70 cm (UHF)",
        low_hz=420.0 * MHZ, high_hz=450.0 * MHZ,
        modulation="nfm", step_hz=12_500.0,
        channels=[
            Channel("CALL", 446.000 * MHZ, "National simplex calling"),
            Channel("DSTAR", 445.000 * MHZ, "D-STAR simplex", notes="GMSK 4800"),
        ],
    ),
    Band(
        id="ham-1.25m", name="Amateur 1.25 m (220 MHz)",
        low_hz=222.0 * MHZ, high_hz=225.0 * MHZ, step_hz=20_000.0,
        channels=[Channel("CALL", 223.500 * MHZ, "National simplex calling")],
    ),
    Band(
        id="airband", name="VHF Airband (AM)",
        low_hz=118.0 * MHZ, high_hz=136.975 * MHZ,
        modulation="am", step_hz=25_000.0,
        channels=[
            Channel("EMG", 121.500 * MHZ, "International air distress",
                    bandwidth_hz=25_000.0, modulation="am"),
            Channel("UNI", 122.800 * MHZ, "Unicom / CTAF",
                    bandwidth_hz=25_000.0, modulation="am"),
        ],
    ),
    Band(
        id="pager-vhf", name="VHF paging (POCSAG/FLEX)",
        low_hz=137.0 * MHZ, high_hz=174.0 * MHZ, step_hz=12_500.0,
        notes="POCSAG paging is commonly found near 138-174 MHz depending on region.",
    ),
    Band(
        id="pager-uhf", name="UHF paging (POCSAG/FLEX)",
        low_hz=440.0 * MHZ, high_hz=470.0 * MHZ, step_hz=12_500.0,
    ),
    Band(
        id="cb", name="Citizens Band (27 MHz)",
        low_hz=26.965 * MHZ, high_hz=27.405 * MHZ,
        modulation="am", step_hz=10_000.0,
        channels=[
            Channel("9", 27.065 * MHZ, "Emergency", modulation="am", bandwidth_hz=10_000.0),
            Channel("19", 27.185 * MHZ, "Highway", modulation="am", bandwidth_hz=10_000.0),
        ],
    ),
    Band(
        id="hf-ssb", name="HF (3-30 MHz, SSB/data)",
        low_hz=3.0 * MHZ, high_hz=30.0 * MHZ,
        modulation="usb", step_hz=1_000.0,
        notes="Wide catch-all for HF voice and data; use --frequency to label recordings.",
    ),
    Band(
        id="wideband", name="Unlabelled / wideband capture",
        low_hz=0.0, high_hz=6_000.0 * MHZ, modulation="nfm", step_hz=12_500.0,
        notes="Fallback used when the operator has not selected a band plan.",
    ),
]

_BY_ID: Dict[str, Band] = {b.id: b for b in BUILTIN_BANDS}


def all_bands() -> List[Band]:
    return list(_BY_ID.values())


def get_band(band_id: str) -> Band:
    key = (band_id or "wideband").strip().lower()
    if key not in _BY_ID:
        raise KeyError(f"unknown band {band_id!r}; known: {', '.join(sorted(_BY_ID))}")
    return _BY_ID[key]


def register_band(band: Band) -> None:
    """Add or override a band plan (used by user config)."""
    _BY_ID[band.id] = band


def find_band(freq_hz: float) -> Optional[Band]:
    """Most specific built-in band containing *freq_hz*."""
    matches = [b for b in _BY_ID.values() if b.id != "wideband" and b.contains(freq_hz)]
    if not matches:
        return _BY_ID.get("wideband")
    return min(matches, key=lambda b: b.high_hz - b.low_hz)


def describe(freq_hz: float) -> str:
    """``462.575 MHz (GMRS CH16 GMRS main)`` style label for logs."""
    band = find_band(freq_hz)
    text = f"{freq_hz / MHZ:.4f} MHz"
    if band is None:
        return text
    ch = band.channel_for(freq_hz)
    if ch is not None:
        return f"{text} ({band.id} {ch.label})"
    return f"{text} ({band.id})"


def channel_frequencies(band: Band) -> Iterable[float]:
    """Frequencies a scanner should visit for *band*."""
    if band.channels:
        seen = set()
        for ch in band.channels:
            for f in (ch.frequency_hz, ch.input_hz):
                if f is not None and f not in seen:
                    seen.add(f)
                    yield f
        return
    f = band.low_hz
    while f <= band.high_hz:
        yield f
        f += band.step_hz
