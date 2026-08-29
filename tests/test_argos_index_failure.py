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


def install_fake_fetch(monkeypatch, *, raises=None, writes=None):
    """Replace BabelFishR's own HTTPS fetch, counting the requests."""
    calls = {"fetch": 0}

    def fake_fetch(url=None, destination=None, timeout=30.0):
        calls["fetch"] += 1
        if raises is not None:
            raise raises
        if writes is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(writes, encoding="utf-8")
        return destination

    monkeypatch.setattr(argos, "fetch_package_index", fake_fetch)
    return calls


def install_fake_argos(monkeypatch, tmp_path, *, update_raises=None,
                       writes_index=False, packages=(), patch_fetch=True):
    """A stand-in for argostranslate, with every index attempt counted.

    ``calls["attempts"]`` is the number that matters: BabelFishR now makes the
    HTTPS request itself and falls back to upstream only if that fails, so
    "one attempt per run" means one of *ours*, not one of theirs.
    """
    calls = {"update": 0, "available": 0, "fetch": 0, "attempts": 0}
    index = tmp_path / "index.json"

    if patch_fetch:
        def fake_fetch(url=None, destination=None, timeout=30.0):
            calls["fetch"] += 1
            calls["attempts"] += 1
            if update_raises is not None:
                raise update_raises
            if writes_index:
                index.write_text('[{"from_code": "es", "to_code": "en"}]',
                                 encoding="utf-8")
                return index
            # No index written and no error: upstream's swallow behaviour.
            return index

        monkeypatch.setattr(argos, "fetch_package_index", fake_fetch)

    def update_package_index():
        calls["update"] += 1
        calls["attempts"] += 1
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

    # Bounded: our own request once, then upstream once as a fallback. Not
    # hundreds, and never get_available_packages() with no index on disk,
    # which is the call that recurses.
    assert calls["fetch"] == 1
    assert calls["update"] <= 1
    assert calls["attempts"] <= 2
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
    assert calls["attempts"] <= 2


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

    # The number that matters: it does not scale with the number of pairs.
    assert calls["fetch"] == 1, (
        "five requested language pairs must produce one network request")
    assert calls["attempts"] <= 2, "our request, plus one fallback, and no more"


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

    assert calls["fetch"] == 1, "one request, not one per language"
    assert calls["attempts"] <= 2, "our request plus one fallback, not ten"

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
    assert calls["attempts"] == 1, "a good index is not re-fetched per call"
    assert len(argos.available_packages()) == 1


# ---- what real argostranslate actually does ----------------------------
#
# The fakes above make update_package_index() raise. Real argostranslate does
# not: it catches its own network errors, logs them somewhere BabelFishR never
# sees, and returns normally having written nothing. That is worse than
# raising, because the only thing left to report is "the index is missing",
# which tells an operator nothing about why.
#
# So BabelFishR makes the request itself and keeps the exception.

def install_silently_failing_argos(monkeypatch, tmp_path):
    """update_package_index() swallows the error and returns. No index."""
    calls, index = install_fake_argos(monkeypatch, tmp_path, patch_fetch=False)

    def swallow():
        calls["update"] += 1
        # Exactly upstream's shape: caught internally, nothing written,
        # nothing raised.
        return None

    sys.modules["argostranslate.package"].update_package_index = swallow
    return calls, index


def test_a_silently_swallowed_error_still_surfaces_the_real_cause(monkeypatch,
                                                                  tmp_path):
    """The case the Mac actually hit."""
    calls, _ = install_silently_failing_argos(monkeypatch, tmp_path)
    original = _certificate_error()
    fetches = install_fake_fetch(monkeypatch, raises=original)
    assert fetches["fetch"] == 0

    with pytest.raises(argos.PackageIndexUnavailable) as raised:
        argos.refresh_package_index()

    message = str(raised.value)
    assert "SSLCertVerificationError" in message
    assert "unable to get local issuer certificate" in message
    assert raised.value.__cause__ is original
    # And emphatically not the useless fallback wording.
    assert "the index was not written" not in message

    assert fetches["fetch"] == 1
    assert calls["available"] == 0


def test_our_own_fetch_is_tried_before_upstream(monkeypatch, tmp_path):
    calls, index = install_fake_argos(monkeypatch, tmp_path, patch_fetch=False)
    fetches = install_fake_fetch(
        monkeypatch, writes='[{"from_code": "es", "to_code": "en"}]')

    assert argos.refresh_package_index() == index
    assert fetches["fetch"] == 1
    assert calls["update"] == 0, (
        "upstream is the fallback; it swallows its errors so nothing is "
        "learned from calling it first")


def test_upstream_is_still_used_as_a_fallback(monkeypatch, tmp_path):
    """If our request fails but upstream can reach it, that is a success."""
    calls, index = install_fake_argos(monkeypatch, tmp_path, writes_index=True,
                                      patch_fetch=False)
    fetches = install_fake_fetch(monkeypatch, raises=_certificate_error())

    assert argos.refresh_package_index() == index
    assert fetches["fetch"] == 1
    assert calls["update"] == 1


def test_the_fetch_writes_atomically_and_rejects_a_bad_body(monkeypatch,
                                                            tmp_path):
    """A truncated body must not land on disk looking like a catalogue."""
    import urllib.request

    destination = tmp_path / "index.json"

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: FakeResponse(b"{ truncated"))
    with pytest.raises(Exception):
        argos.fetch_package_index(url="https://example.invalid/index.json",
                                  destination=destination)
    assert not destination.exists(), "a bad body must leave nothing behind"
    assert not list(tmp_path.glob(".index-*")), "no temporary file left over"

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: FakeResponse(
                            b'[{"from_code": "es", "to_code": "en"}]'))
    assert argos.fetch_package_index(
        url="https://example.invalid/index.json",
        destination=destination) == destination
    assert destination.is_file()


def test_the_fetch_never_relaxes_tls_verification():
    import inspect

    source = inspect.getsource(argos.fetch_package_index)
    for forbidden in ("_create_unverified_context", "CERT_NONE",
                      "check_hostname", "verify=False"):
        assert forbidden not in source


# ---- Prepare Again really retries --------------------------------------
def test_prepare_again_makes_a_new_index_request(monkeypatch, tmp_path):
    """First run fails, the operator fixes the network, second run succeeds.

    Each individual run still makes exactly one request: the cache is what
    stops five language pairs becoming five attempts, and it is cleared once
    per run rather than once per pair.
    """
    from babelfishr import preparation
    from babelfishr.config import Config

    calls, index = install_fake_argos(monkeypatch, tmp_path, patch_fetch=False)
    state = {"working": False, "fetches": 0}

    def flaky_fetch(url=None, destination=None, timeout=30.0):
        state["fetches"] += 1
        if not state["working"]:
            raise _certificate_error()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text('[{"from_code": "es", "to_code": "en"}]',
                               encoding="utf-8")
        return destination

    monkeypatch.setattr(argos, "fetch_package_index", flaky_fetch)
    monkeypatch.setattr(preparation, "_prepare_asr",
                        lambda *a, **k: (preparation.ASR_STEP, True, "medium"))
    monkeypatch.setattr(preparation, "_prepare_language",
                        _language_step_using_the_index)

    config = Config()
    config.app_home = str(tmp_path / "home")
    pairs = [("es", "en"), ("de", "en"), ("fr", "en"), ("uk", "en"),
             ("ru", "en")]

    first = preparation.prepare_field(config, asr_model="medium",
                                      language_pairs=pairs)
    assert state["fetches"] == 1, "one request per run, not one per pair"
    assert not first.ok

    # The operator fixes their network and presses Prepare again.
    state["working"] = True
    second = preparation.prepare_field(config, asr_model="medium",
                                       language_pairs=pairs)

    assert state["fetches"] == 2, (
        "a new run must make a new request, not replay the cached failure")
    assert second.ok, second.summary()

    # Still one request for the whole second run, despite five pairs.
    language_steps = [s for s in second.steps if s[0].startswith("Language")]
    assert len(language_steps) == 5
    assert all(ok for _, ok, _ in language_steps)


def _language_step_using_the_index(source, target, say, skip_download):
    """A language step that consults the index, like the real one does."""
    from babelfishr.providers.argos import (PackageIndexUnavailable,
                                            refresh_package_index)
    from babelfishr.preparation import _IndexUnavailable

    try:
        refresh_package_index()
    except PackageIndexUnavailable as exc:
        raise _IndexUnavailable(str(exc)) from exc
    return (f"Language pack {source}->{target}", True, "installed")


def test_the_reset_happens_once_per_run_not_per_pair(monkeypatch, tmp_path):
    """Resetting between pairs would undo the one-attempt guarantee."""
    import inspect

    from babelfishr import preparation

    source = inspect.getsource(preparation.prepare_field)
    assert source.count("reset_package_index_state()") == 1
    before_loop = source.index("reset_package_index_state()")
    loop = source.index("for index, (source, target) in enumerate(pending)")
    assert before_loop < loop, "the reset must precede the language loop"
