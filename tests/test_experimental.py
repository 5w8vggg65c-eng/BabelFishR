"""The experimental decoders stay isolated, gated and out of the receive path."""

from __future__ import annotations

import importlib
import pathlib

import numpy as np
import pytest

from babelfishr.experimental import ExperimentalDisabled, enabled, load_decoders


def test_decoders_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BABELFISHR_EXPERIMENTAL", raising=False)
    assert not enabled()
    with pytest.raises(ExperimentalDisabled):
        load_decoders()


def test_decoders_load_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("BABELFISHR_EXPERIMENTAL", "1")
    registry = load_decoders()
    assert {"ctcss", "dtmf", "dcs", "afsk1200", "pocsag"} <= set(registry)


def test_config_flag_also_enables(monkeypatch):
    monkeypatch.delenv("BABELFISHR_EXPERIMENTAL", raising=False)

    class Cfg:
        experimental = True

    assert enabled(Cfg())


def test_receive_pipeline_does_not_import_experimental_code():
    """A decoder must never end up on the supported path by accident."""
    for module_name in ("babelfishr.app", "babelfishr.pipeline",
                        "babelfishr.detect", "babelfishr.storage",
                        "babelfishr.providers"):
        module = importlib.import_module(module_name)
        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        assert "experimental" not in source.replace(
            "# experimental", ""), f"{module_name} references experimental code"


@pytest.mark.parametrize("decoder_id,synth,expected", [
    ("dtmf", lambda m: m.synthesize("1234"), "1234"),
    ("ctcss", lambda m: m.synthesize(100.0, 3.0), "100.0"),
    ("dcs", lambda m: m.synthesize("023", 3.0), "023"),
])
def test_preserved_decoders_still_work(monkeypatch, decoder_id, synth, expected):
    """Isolation must not have broken the research code we kept."""
    monkeypatch.setenv("BABELFISHR_EXPERIMENTAL", "1")
    registry = load_decoders()
    module = importlib.import_module(
        f"babelfishr.experimental.decoders.{decoder_id.replace('-', '_')}")
    audio = synth(module)
    results = registry[decoder_id].decode(audio, 8000)
    assert results, f"{decoder_id} produced no decode"
    assert expected in results[0].label


def test_every_module_in_the_package_imports(monkeypatch):
    """Nothing may become unimportable unnoticed.

    The experimental package is not exercised by the receive-path tests, so a
    stale relative import left by a file move sat there silently until a
    package-wide import check found it.
    """
    import importlib
    import os
    import pkgutil

    monkeypatch.setenv("BABELFISHR_EXPERIMENTAL", "1")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import babelfishr

    failures = []
    for module in pkgutil.walk_packages(babelfishr.__path__, "babelfishr."):
        try:
            importlib.import_module(module.name)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{module.name}: {type(exc).__name__}: {exc}")
    assert not failures, "unimportable modules: " + "; ".join(failures)


def test_quarantined_sdr_demodulator_still_functions(monkeypatch):
    """The module that was broken: prove it works, not merely that it imports."""
    import numpy as np

    monkeypatch.setenv("BABELFISHR_EXPERIMENTAL", "1")
    from babelfishr.experimental.fm import channelise
    from babelfishr.experimental.goertzel import peak_frequency

    sample_rate = 240_000
    t = np.arange(int(sample_rate * 0.05)) / sample_rate
    # 1 kHz tone, FM-modulated onto a carrier 50 kHz off centre.
    iq = np.exp(1j * 2 * np.pi * (50_000 * t
                                  + 5_000 * np.cumsum(np.sin(2 * np.pi * 1000 * t))
                                  / sample_rate))
    audio = channelise(iq, sample_rate, 50_000, 12_500, 16_000)
    recovered, _ = peak_frequency(audio[200:-200], 16_000)
    assert 900 < recovered < 1100
