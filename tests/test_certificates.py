"""The CA bundle the frozen application needs to make any HTTPS request.

A PyInstaller bundle carries its own OpenSSL but not the system trust store,
and macOS keeps its roots in the Keychain rather than a PEM file OpenSSL can
read. Without a bundled CA file, every download fails with "unable to get local
issuer certificate" - which is what happened on a real Mac.
"""

from __future__ import annotations

import pathlib
import ssl

import pytest

from babelfishr import certificates

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(certificates, "_CONFIGURED", None)
    original = ssl._create_default_https_context  # noqa: SLF001
    yield
    ssl._create_default_https_context = original  # noqa: SLF001


def test_certifi_provides_a_readable_bundle():
    bundle = certificates.certifi_bundle()
    if bundle is None:
        pytest.skip("certifi is not installed in this environment")
    assert bundle.is_file()
    assert bundle.stat().st_size > 0
    assert "BEGIN CERTIFICATE" in bundle.read_text(encoding="utf-8",
                                                   errors="replace")


def test_configuring_sets_every_variable_the_http_stacks_read(monkeypatch,
                                                              tmp_path):
    fake = tmp_path / "roots.pem"
    fake.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(certificates, "certifi_bundle", lambda: fake)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    applied = certificates.configure_certificates(force=True)

    assert applied == str(fake)
    import os

    for name in certificates.CERT_ENV_VARS:
        assert os.environ[name] == str(fake)


def test_the_default_https_context_uses_the_bundle_and_still_verifies(
        monkeypatch, tmp_path):
    """The lever that actually fixes Argos: urllib builds its context here."""
    bundle = certificates.certifi_bundle()
    if bundle is None:
        pytest.skip("certifi is not installed in this environment")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    certificates.configure_certificates(force=True)
    context = ssl._create_default_https_context()  # noqa: SLF001

    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.get_ca_certs(), "the context loaded no roots"


def test_verification_is_never_disabled(monkeypatch, tmp_path):
    """Turning verification off would swap a visible failure for a silent one."""
    source = pathlib.Path(certificates.__file__).read_text(encoding="utf-8")
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "_create_unverified_context" not in source


def test_an_administrator_bundle_is_not_overridden(monkeypatch, tmp_path):
    """A corporate trust store already in SSL_CERT_FILE wins."""
    corporate = tmp_path / "corporate.pem"
    corporate.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(corporate))
    monkeypatch.setattr(certificates, "certifi_bundle",
                        lambda: tmp_path / "certifi.pem")

    assert certificates.resolve_bundle() == corporate


def test_a_missing_bundle_is_reported_rather_than_pretended(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(certificates, "certifi_bundle", lambda: None)

    assert certificates.configure_certificates(force=True) is None
    assert "NONE FOUND" in certificates.describe()


def test_bootstrap_applies_the_bundle_before_anything_imports_argos(tmp_path,
                                                                    monkeypatch):
    from babelfishr.modes import bootstrap_environment

    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    monkeypatch.setattr(certificates, "_CONFIGURED", None)
    applied = bootstrap_environment()

    assert "ARGOS_PACKAGES_DIR" in applied
    if certificates.certifi_bundle() is not None or \
            certificates.resolve_bundle() is not None:
        assert "SSL_CERT_FILE" in applied


def test_the_packaging_spec_ships_certifi_data():
    """An implicit hook is not good enough for the thing that broke."""
    spec = pathlib.Path("packaging/babelfishr.spec").read_text(encoding="utf-8")
    assert "certifi" in spec
    assert "certifi.where()" in spec
    # And the build must refuse rather than quietly produce a bundle that
    # cannot download anything.
    assert "SystemExit" in spec
