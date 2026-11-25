"""Argos translation must not reach the network for a pack's own stanza files.

argostranslate 1.11 builds ``stanza.Pipeline(lang, dir=<pack>/stanza,
processors="tokenize")`` without a download_method. stanza's default fetches
resources_<version>.json from raw.githubusercontent.com on every
construction, even when the pack holds its own resources.json, so with no
network every translation through such a pack failed (seen on the operator's
Mac with the de-en 1.3, es-en 1.9 and fr-en 1.9 packs).
``keep_stanza_local`` pins the splitter to the pack's files.

Two layers: fake ``argostranslate.sbd`` / ``stanza`` modules check the wiring
on every host; the real stanza is exercised, with all network refused, where
it is installed (the macOS build installs it with argostranslate).
"""

from __future__ import annotations

import enum
import json
import socket
import sys
import types

import pytest

from babelfishr.providers import argos as argos_module
from babelfishr.providers.base import EngineError


# ---- wiring, with stand-in modules (every host) --------------------------------------

class _DownloadMethod(enum.Enum):
    NONE = 1
    REUSE_RESOURCES = 2
    DOWNLOAD_RESOURCES = 3


@pytest.fixture
def fake_stanza(monkeypatch):
    """Stand-in argostranslate.sbd and stanza.pipeline.core, recording each
    Pipeline construction the way argostranslate's StanzaSentencizer makes it."""
    calls = []

    def Pipeline(*args, **kwargs):
        calls.append(kwargs)
        return object()

    stanza = types.ModuleType("stanza")
    stanza.Pipeline = Pipeline
    stanza.__path__ = []
    pipeline_pkg = types.ModuleType("stanza.pipeline")
    pipeline_pkg.__path__ = []
    core = types.ModuleType("stanza.pipeline.core")
    core.DownloadMethod = _DownloadMethod
    argos = types.ModuleType("argostranslate")
    argos.__path__ = []
    sbd = types.ModuleType("argostranslate.sbd")
    sbd.stanza = stanza
    for name, module in (("stanza", stanza), ("stanza.pipeline", pipeline_pkg),
                         ("stanza.pipeline.core", core), ("argostranslate", argos),
                         ("argostranslate.sbd", sbd)):
        monkeypatch.setitem(sys.modules, name, module)
    return types.SimpleNamespace(sbd=sbd, stanza=stanza, calls=calls)


def _build_like_argos(sbd, directory="/pack/stanza"):
    # argostranslate/sbd.py StanzaSentencizer.lazy_pipeline, verbatim arguments
    return sbd.stanza.Pipeline(lang="de", dir=directory, processors="tokenize",
                               use_gpu=False, logging_level="WARNING")


def test_argos_stanza_pipelines_are_told_to_download_nothing(fake_stanza):
    _build_like_argos(fake_stanza.sbd)
    assert fake_stanza.calls[-1].get("download_method") is None   # stanza's default: fetch

    assert argos_module.keep_stanza_local() is True
    _build_like_argos(fake_stanza.sbd)
    made = fake_stanza.calls[-1]
    assert made["download_method"] is _DownloadMethod.NONE
    assert made["dir"] == "/pack/stanza" and made["processors"] == "tokenize"
    # stanza itself is untouched for anything else in the process
    assert sys.modules["stanza"].Pipeline is fake_stanza.stanza.Pipeline


def test_an_explicit_download_method_is_respected_and_pinning_is_idempotent(fake_stanza):
    assert argos_module.keep_stanza_local() and argos_module.keep_stanza_local()
    fake_stanza.sbd.stanza.Pipeline(lang="fr", dir="/p", processors="tokenize",
                                    download_method=_DownloadMethod.REUSE_RESOURCES)
    assert fake_stanza.calls[-1]["download_method"] is _DownloadMethod.REUSE_RESOURCES
    _build_like_argos(fake_stanza.sbd)
    assert len(fake_stanza.calls) == 2            # one construction each, no double wrap


def test_without_stanza_there_is_nothing_to_pin(monkeypatch):
    monkeypatch.setitem(sys.modules, "argostranslate.sbd", None)   # import fails
    assert argos_module.keep_stanza_local() is False


def _engine_with_fake_translate(monkeypatch, fake_stanza, translate_fn):
    translate_mod = types.ModuleType("argostranslate.translate")
    translate_mod.translate = translate_fn
    monkeypatch.setitem(sys.modules, "argostranslate.translate", translate_mod)
    engine = argos_module.ArgosTranslateEngine(target_language="en")
    monkeypatch.setattr(engine, "supports_pair", lambda s, t: True)
    return engine


def test_translate_pins_the_splitter_before_argos_builds_it(monkeypatch, fake_stanza):
    def translate(text, source, target):
        made = _build_like_argos(fake_stanza.sbd)    # what argos does inside translate()
        assert made is not None
        return "and this is dictation number one"

    engine = _engine_with_fake_translate(monkeypatch, fake_stanza, translate)
    result = engine.translate("Et c'est la dictée numéro 1.", "en", source_language="fr")
    assert result.text == "and this is dictation number one"
    assert fake_stanza.calls[-1]["download_method"] is _DownloadMethod.NONE


def test_a_missing_pack_file_is_reported_as_such(monkeypatch, fake_stanza):
    def translate(text, source, target):
        raise FileNotFoundError(2, "Resources file not found at: /pack/stanza/resources.json")

    engine = _engine_with_fake_translate(monkeypatch, fake_stanza, translate)
    with pytest.raises(EngineError) as raised:
        engine.translate("Hallo", "en", source_language="de")
    message = str(raised.value)
    assert "de->en language pack is missing a file" in message
    assert "/pack/stanza/resources.json" in message
    assert "Online / Setup" in message


def test_callsigns_bypass_argos_and_glossary_substitutions(monkeypatch, fake_stanza):
    calls = []

    def translate(text, source, target):
        calls.append(text)
        return {"buenos días": "good morning.", "adelante": "go ahead",
                "¿me recibe?": "Do you copy?"}[text]

    engine = _engine_with_fake_translate(monkeypatch, fake_stanza, translate)
    result = engine.translate("buenos días  KD8XYZ-2, kd8XyZ!  adelante", "en",
                              source_language="es", do_not_translate=["KD8XYZ", "KD8XYZ-2"],
                              glossary={"morning": "day", "KD8XYZ": "wrong"})
    assert result.text == "good day  KD8XYZ-2, kd8XyZ!  go ahead"
    assert calls == ["buenos días", "adelante"]
    assert engine.translate("KD8XYZ", "en", source_language="es",
                            do_not_translate=["KD8XYZ"]).text == "KD8XYZ"
    assert calls == ["buenos días", "adelante"]
    result = engine.translate("buenos días, KD8XYZ, ¿me recibe?", "en",
                              source_language="es", do_not_translate=["KD8XYZ"])
    assert result.text == "good morning, KD8XYZ, Do you copy?"


# ---- the real stanza, network refused (where stanza is installed) ----------------------

@pytest.fixture
def no_network(monkeypatch):
    attempts = []

    def refuse(self, address, *args, **kwargs):
        attempts.append(address)
        raise OSError(101, "network unreachable (test)")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def _real_stanza_pinned():
    pytest.importorskip("stanza")
    pytest.importorskip("argostranslate.sbd")
    import argostranslate.sbd as sbd

    original = sbd.stanza
    assert argos_module.keep_stanza_local()
    return sbd, original


def test_real_stanza_with_no_resources_file_fails_locally_without_a_request(tmp_path, no_network):
    sbd, original = _real_stanza_pinned()
    try:
        with pytest.raises(FileNotFoundError) as raised:
            _build_like_argos(sbd, str(tmp_path / "stanza"))
        assert "resources.json" in str(raised.value)
        assert no_network == [], f"stanza tried the network: {no_network}"
    finally:
        sbd.stanza = original


def test_real_stanza_with_a_local_resources_file_makes_no_request(tmp_path, no_network):
    """A pack directory holding resources.json (but, here, no model weights):
    whatever stanza does with it, it must not reach for the network. The
    positive case - a real pack translating offline - needs a real pack and is
    checked on the Mac."""
    sbd, original = _real_stanza_pinned()
    directory = tmp_path / "stanza"
    directory.mkdir()
    (directory / "resources.json").write_text(json.dumps(
        {"de": {"lang_name": "German", "tokenize": {"gsd": {}},
                "default_processors": {"tokenize": "gsd"}}}))
    try:
        try:
            _build_like_argos(sbd, str(directory))
        except Exception:  # noqa: BLE001 - missing weights may raise; that is local
            pass
        assert no_network == [], f"stanza tried the network: {no_network}"
    finally:
        sbd.stanza = original
