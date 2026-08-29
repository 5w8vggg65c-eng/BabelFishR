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
