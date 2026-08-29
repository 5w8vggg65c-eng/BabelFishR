"""A CA bundle the frozen application can actually find.

The defect this exists for: on a real Apple Silicon Mac, the packaged app could
not reach the Argos package index at all. Every route failed, and the first
error in the log was

    SSLCertVerificationError: certificate verify failed:
    unable to get local issuer certificate

A PyInstaller bundle carries its own Python and its own OpenSSL, but it does
not carry the system's trust store, and macOS keeps its roots in the Keychain
rather than in a PEM file that OpenSSL knows how to read. So OpenSSL falls back
to a compiled-in default path that does not exist inside the bundle, finds no
roots, and rejects every certificate - including perfectly valid ones.

The fix is to ship a bundle of roots (certifi) and point every HTTPS path at
it before any library opens a connection.

Verification is never disabled here. Turning it off would swap a visible
failure for an invisible one, on an application whose entire premise is that
the operator can trust what it tells them.
"""

from __future__ import annotations

import logging
import os
import pathlib
import ssl
from typing import Optional

log = logging.getLogger(__name__)

#: Environment variables consulted by OpenSSL (SSL_CERT_FILE) and by the
#: requests/urllib3 stack (the other two). All three are set, because the code
#: that eventually opens the socket is not ours.
CERT_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

_CONFIGURED: Optional[str] = None


def certifi_bundle() -> Optional[pathlib.Path]:
    """The CA bundle shipped by certifi, if it is installed and present."""
    try:
        import certifi
    except Exception as exc:  # noqa: BLE001 - optional dependency
        log.debug("certifi is not available: %s", exc)
        return None
    try:
        path = pathlib.Path(certifi.where())
    except Exception as exc:  # noqa: BLE001
        log.debug("certifi.where() failed: %s", exc)
        return None
    return path if path.is_file() else None


def resolve_bundle() -> Optional[pathlib.Path]:
    """The CA bundle to use, in order of preference.

    An operator or administrator who has already set ``SSL_CERT_FILE`` to a
    corporate trust store wins: overriding it would break exactly the
    environments that are most deliberate about certificates.
    """
    existing = os.environ.get("SSL_CERT_FILE")
    if existing:
        path = pathlib.Path(existing)
        if path.is_file():
            return path
        log.warning("SSL_CERT_FILE points at %s, which does not exist", path)
    return certifi_bundle()


def configure_certificates(force: bool = False) -> Optional[str]:
    """Make a CA bundle available to every HTTPS client in this process.

    Called from :func:`babelfishr.modes.bootstrap_environment`, which every
    entry point runs before any library that might open a connection is
    imported. Returns the bundle path that was applied, or ``None`` when no
    bundle could be found - in which case HTTPS is left exactly as the
    interpreter configured it, and any failure surfaces normally.
    """
    global _CONFIGURED
    if _CONFIGURED is not None and not force:
        return _CONFIGURED

    bundle = resolve_bundle()
    if bundle is None:
        log.warning(
            "no CA certificate bundle found; HTTPS requests will use whatever "
            "trust store this interpreter was built with. In a frozen "
            "application that is usually nothing, and every download will "
            "fail with 'unable to get local issuer certificate'.")
        return None

    for name in CERT_ENV_VARS:
        os.environ[name] = str(bundle)

    # The environment variables cover OpenSSL's default paths and the requests
    # stack. They do NOT cover code that builds its own context - and
    # http.client, which is what urllib and therefore Argos ends up using,
    # calls ssl._create_default_https_context() when no context is passed in.
    # Binding that to our bundle is what actually fixes Argos.
    def _context(*args, **kwargs):
        kwargs.pop("cafile", None)
        kwargs.pop("capath", None)
        kwargs.pop("cadata", None)
        context = ssl.create_default_context(*args, cafile=str(bundle), **kwargs)
        # Belt and braces: these are the defaults, restated so that a future
        # edit cannot quietly weaken them.
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    ssl._create_default_https_context = _context  # noqa: SLF001
    _CONFIGURED = str(bundle)
    log.info("HTTPS trust store: %s", bundle)
    return _CONFIGURED


def configured_bundle() -> Optional[str]:
    """What :func:`configure_certificates` applied, if anything."""
    return _CONFIGURED


def describe() -> str:
    """One line for a diagnostic report or a build log."""
    bundle = _CONFIGURED or resolve_bundle()
    if bundle is None:
        return "CA bundle: NONE FOUND - HTTPS downloads will fail"
    applied = "applied" if _CONFIGURED else "found but not yet applied"
    return f"CA bundle: {bundle} ({applied})"


__all__ = ["CERT_ENV_VARS", "certifi_bundle", "configure_certificates",
           "configured_bundle", "describe", "resolve_bundle"]
