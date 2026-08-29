"""What happens when the Argos package index cannot be fetched.

Driven by a real failure on a real Apple Silicon Mac. The frozen app could not
reach the package index at all; the first error in the log was

    SSLCertVerificationError: certificate verify failed:
    unable to get local issuer certificate

and argostranslate then recursed - `get_available_packages()` calls
`update_package_index()` when the index is missing, which comes back into
`get_available_packages()` - until the interpreter raised

    RecursionError: maximum recursion depth exceeded

burying the real cause under hundreds of identical log lines, five times over
because five language pairs were requested.
"""

from __future__ import annotations

import ssl
import sys
import types

import pytest

from babelfishr.providers import argos

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_index_state():
    argos.reset_package_index_state()
    yield
    argos.reset_package_index_state()


def _certificate_error() -> ssl.SSLCertVerificationError:
    """The exact class and wording the Mac produced."""
    return ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "unable to get local issuer certificate (_ssl.c:1000)")


def install_fake_argos(monkeypatch, tmp_path, *, update_raises=None,
                       writes_index=False, packages=()):
    """A stand-in for argostranslate.package with a recorded call count."""
    calls = {"update": 0, "available": 0}
    index = tmp_path / "index.json"

    def update_package_index():
        calls["update"] += 1
        if update_raises is not None:
            raise update_raises
        if writes_index:
            index.write_text('[{"from_code": "es", "to_code": "en"}]',
                             encoding="utf-8")

    def get_available_packages():
        calls["available"] += 1
        if not index.is_file():
            # Faithful to upstream: this is the recursion.
            update_package_index()
            return get_available_packages()
        return list(packages)

    module = types.ModuleType("argostranslate.package")
    module.update_package_index = update_package_index
    module.get_available_packages = get_available_packages
    module.install_from_path = lambda path: None
    monkeypatch.setitem(sys.modules, "argostranslate.package", module)

    settings = types.ModuleType("argostranslate.settings")
    settings.local_package_index = index
    monkeypatch.setitem(sys.modules, "argostranslate.settings", settings)

    # The engine checks the library is importable and asks which routes are
    # already installed before it downloads anything, so those have to exist
    # too - otherwise the test never reaches the index at all.
    translate = types.ModuleType("argostranslate.translate")
    translate.get_installed_languages = lambda: []
    translate.translate = lambda text, *a, **k: text
    monkeypatch.setitem(sys.modules, "argostranslate.translate", translate)

    root = types.ModuleType("argostranslate")
    root.package = module
    root.settings = settings
    root.translate = translate
    monkeypatch.setitem(sys.modules, "argostranslate", root)
    return calls, index


# ---- 1. a failed refresh terminates, and does not recurse --------------
def test_a_failed_index_refresh_terminates_without_recursion(monkeypatch,
                                                             tmp_path):
    calls, _ = install_fake_argos(monkeypatch, tmp_path,
                                  update_raises=_certificate_error())

    with pytest.raises(argos.PackageIndexUnavailable):
        argos.refresh_package_index()

    # One attempt. Not hundreds, and never get_available_packages() with no
    # index on disk, which is the call that recurses.
    assert calls["update"] == 1
    assert calls["available"] == 0


def test_available_packages_never_asks_upstream_without_an_index(monkeypatch,
                                                                 tmp_path):
    calls, _ = install_fake_argos(monkeypatch, tmp_path,
                                  update_raises=_certificate_error())

    with pytest.raises(argos.PackageIndexUnavailable):
        argos.available_packages()
    assert calls["available"] == 0, (
        "get_available_packages() must not be reached without an index; that "
        "is the recursion")


def test_recursion_error_from_upstream_is_contained(monkeypatch, tmp_path):
    """Even if upstream still recurses, it must not escape as RecursionError."""
    calls, _ = install_fake_argos(
        monkeypatch, tmp_path,
        update_raises=RecursionError("maximum recursion depth exceeded"))

    with pytest.raises(argos.PackageIndexUnavailable):
        argos.refresh_package_index()
    assert calls["update"] == 1


# ---- 2. the original exception stays visible ---------------------------
def test_the_certificate_error_survives_in_the_message_and_the_cause(
        monkeypatch, tmp_path):
    original = _certificate_error()
    install_fake_argos(monkeypatch, tmp_path, update_raises=original)

    with pytest.raises(argos.PackageIndexUnavailable) as raised:
        argos.refresh_package_index()

    message = str(raised.value)
    assert "SSLCertVerificationError" in message
    assert "certificate verify failed" in message
    assert "unable to get local issuer certificate" in message
    # And the real exception is still chained, for a traceback or a report.
    assert raised.value.__cause__ is original

    # It must also say what kind of problem this is, since "no language pack"
    # is the wrong conclusion to draw from it.
    assert "certificate verification failure" in message
    assert "not a missing" in message
    assert "CA bundle" in message


def test_a_truncated_index_is_treated_as_missing(monkeypatch, tmp_path):
    """A half-written index is worse than none: upstream thinks it is there."""
    calls, index = install_fake_argos(monkeypatch, tmp_path, writes_index=False)
    index.write_text("", encoding="utf-8")

    with pytest.raises(argos.PackageIndexUnavailable):
        argos.refresh_package_index()
    assert calls["available"] == 0


# ---- 3. one failure, not five ------------------------------------------
def test_one_index_failure_is_not_retried_per_language(monkeypatch, tmp_path):
    calls, _ = install_fake_argos(monkeypatch, tmp_path,
                                  update_raises=_certificate_error())

    for _ in range(5):
        with pytest.raises(argos.PackageIndexUnavailable):
            argos.refresh_package_index()

    assert calls["update"] == 1, (
        "five requested language pairs must produce one network attempt")


def test_preparation_stops_after_the_first_index_failure(monkeypatch, tmp_path):
    """Through prepare_field, with five pairs requested."""
    from babelfishr import preparation
    from babelfishr.config import Config

    calls, _ = install_fake_argos(monkeypatch, tmp_path,
                                  update_raises=_certificate_error())

    config = Config()
    config.app_home = str(tmp_path / "home")
    # Only the language half of preparation is under test here.
    monkeypatch.setattr(preparation, "_prepare_asr",
                        lambda *a, **k: (preparation.ASR_STEP, True, "medium"))

    pairs = [("es", "en"), ("de", "en"), ("fr", "en"), ("uk", "en"),
             ("ru", "en")]
    result = preparation.prepare_field(config, asr_model="medium",
                                       language_pairs=pairs)

    assert calls["update"] == 1, "one attempt, not one per language"

    language_steps = [(n, ok, d) for n, ok, d in result.steps
                      if n.startswith("Language pack")]
    assert len(language_steps) == 5, "every requested pair is still accounted for"
    assert not any(ok for _, ok, _ in language_steps)

    # The first carries the real cause; the rest say plainly that they were
    # not attempted, rather than repeating the same error five times.
    assert "certificate verify failed" in language_steps[0][2]
    for _, _, detail in language_steps[1:]:
        assert "not attempted" in detail
        assert "certificate verify failed" not in detail


# ---- a working index still works ---------------------------------------
def test_a_successful_refresh_returns_the_index_and_is_cached(monkeypatch,
                                                              tmp_path):
    calls, index = install_fake_argos(monkeypatch, tmp_path, writes_index=True,
                                      packages=[object()])

    assert argos.refresh_package_index() == index
    assert argos.refresh_package_index() == index
    assert calls["update"] == 1, "a good index is not re-fetched per call"
    assert len(argos.available_packages()) == 1
