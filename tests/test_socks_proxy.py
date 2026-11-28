"""The model hub is reachable through a SOCKS proxy.

Preview 11 on a Mac whose network supplied ALL_PROXY=socks5h://... : a
clean Online / Setup install of Tiny through the packaged CLI failed before
any download with "Using SOCKS proxy, but the socksio package is not
installed". huggingface_hub's client (httpx2/httpcore2) honours the system
proxy settings and needs socksio for SOCKS. These tests run the production
client against a local SOCKS5 stand-in: it starts, routes the request
through the proxy with the host name resolved by the proxy (socks5h), keeps
TLS verification on, and a network-restricted mode still sends nothing.
"""

from __future__ import annotations

import os
import pathlib
import re
import socket
import ssl
import struct
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROXY_KEYS = ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
              "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")


class Socks5Stub:
    """A SOCKS5 server that records each CONNECT request and then refuses
    it (reply 0x05: connection refused), so nothing leaves this machine."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = []
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                try:
                    conn.settimeout(5)
                    ver, nmethods = self._read(conn, 2)
                    self._read(conn, nmethods)
                    conn.sendall(b"\x05\x00")                # no auth
                    ver, cmd, _rsv, atyp = self._read(conn, 4)
                    if atyp == 3:                            # domain name
                        host = self._read(conn, self._read(conn, 1)[0]).decode()
                    elif atyp == 1:
                        host = socket.inet_ntoa(self._read(conn, 4))
                    else:
                        host = socket.inet_ntop(socket.AF_INET6, self._read(conn, 16))
                    port = struct.unpack("!H", self._read(conn, 2))[0]
                    self.requests.append((atyp, host, port))
                    conn.sendall(b"\x05\x05\x00\x01" + bytes(6))  # refused
                except Exception:  # noqa: BLE001 - a probe, not a server
                    pass

    def close(self):
        self._stop = True
        self.sock.close()
        self.thread.join(2)


@pytest.fixture
def socks_proxy(monkeypatch):
    stub = Socks5Stub()
    for key in PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ALL_PROXY", f"socks5h://127.0.0.1:{stub.port}")
    from huggingface_hub.utils import _http as hf_http

    hf_http.close_session()
    yield stub
    hf_http.close_session()
    stub.close()


def test_the_production_hub_client_starts_behind_a_socks_proxy(socks_proxy):
    from huggingface_hub.utils import get_session

    client = get_session()          # raised ImportError without socksio
    transport = client._transport_for_url(client._merge_url("https://huggingface.co/"))
    pool = getattr(transport, "_pool", transport)
    assert "socks" in type(pool).__name__.lower()
    # TLS verification stays on through the proxy.
    context = pool._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_a_hub_request_goes_through_the_proxy_by_name(socks_proxy):
    import httpx2
    from huggingface_hub.utils import get_session

    with pytest.raises(httpx2.ProxyError):
        get_session().get("https://huggingface.co/api/models/Systran/faster-whisper-tiny")
    # socks5h: the proxy, not this machine, resolves the name.
    assert socks_proxy.requests == [(3, "huggingface.co", 443)]


@pytest.mark.parametrize("mode", ["field-offline", "record-only"])
def test_restricted_modes_send_nothing_through_the_proxy(socks_proxy, config,
                                                         mode):
    from babelfishr import preparation
    from babelfishr.modes import OfflineViolation, OperatingMode

    with pytest.raises(OfflineViolation):
        preparation.install_asr_model(config, "tiny", mode=OperatingMode(mode))
    assert socks_proxy.requests == []


def test_the_frozen_self_test_builds_the_client_behind_socks(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "app_entry_probe", ROOT / "packaging" / "app_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HTTPS_PROXY", "http://keep.example:3128")
    assert "SOCKS" in module._selftest_socks_client()
    assert os.environ["HTTPS_PROXY"] == "http://keep.example:3128"   # restored
    assert "ALL_PROXY" not in os.environ or os.environ["ALL_PROXY"] != "socks5h://127.0.0.1:9"


def test_socksio_is_declared_locked_and_bundled():
    pyproject = (ROOT / "pyproject.toml").read_text()
    asr = re.search(r"^asr = \[(.*)\]$", pyproject, re.M).group(1)
    assert "socksio" in asr
    lock = (ROOT / "packaging" / "requirements-macos-arm64.txt").read_text()
    entry = re.search(r"^socksio==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})", lock, re.M)
    assert entry, "socksio must be pinned with a sha256 in the macOS lock"
    spec = (ROOT / "packaging" / "babelfishr.spec").read_text()
    assert 'collect_submodules("socksio")' in spec
    entry_py = (ROOT / "packaging" / "app_entry.py").read_text()
    assert "_selftest_socks_client()" in entry_py
