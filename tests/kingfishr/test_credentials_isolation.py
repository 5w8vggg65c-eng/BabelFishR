"""KingFishR's credentials are its own: Keychain service "KingFishR" for
reading AND writing, and its own ~/.config/kingfishr/.env. BabelFishR's
Keychain entry and .env file are never read, copied or used as a fallback.

Mock-only: ``security`` is never run and no real secret is touched. A fake
``subprocess.run`` records every Keychain call and answers from an
in-memory store.
"""

from __future__ import annotations

import subprocess
import types

import pytest

from babelfishr import extensions
from babelfishr.providers import credentials

NAME = "ANTHROPIC_API_KEY"


@pytest.fixture
def keychain(monkeypatch, tmp_path):
    """An in-memory Keychain holding only a BabelFishR entry; HOME and the
    working directory are empty temporary folders."""
    store = {("BabelFishR", NAME): "babelfishr-secret-do-not-share"}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        service = argv[argv.index("-s") + 1]
        account = argv[argv.index("-a") + 1]
        if argv[1] == "find-generic-password":
            value = store.get((service, account))
            return types.SimpleNamespace(returncode=0 if value else 44,
                                         stdout=(value or "") + "\n", stderr="")
        if argv[1] == "add-generic-password":
            store[(service, account)] = argv[argv.index("-w") + 1]
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {argv}")

    monkeypatch.setattr(credentials, "keychain_available", lambda: True)
    monkeypatch.setattr(credentials.subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return store, calls


def _services(calls):
    return [argv[argv.index("-s") + 1] for argv in calls]


def test_kingfishr_reads_and_writes_its_own_service(keychain):
    store, calls = keychain
    assert credentials.keychain_service() == "KingFishR"
    # BabelFishR's stored key is not found, and not used as a fallback.
    assert credentials.get_secret(NAME) is None
    assert credentials.get_from_keychain(NAME) is None
    assert credentials.describe_source(NAME) == "not configured"
    assert credentials.store_in_keychain(NAME, "kingfishr-secret")
    assert credentials.get_secret(NAME) == "kingfishr-secret"
    assert set(_services(calls)) == {"KingFishR"}, calls
    # BabelFishR's entry is exactly as it was: not read, copied or replaced.
    assert store[("BabelFishR", NAME)] == "babelfishr-secret-do-not-share"
    assert ("KingFishR", NAME) in store and len(store) == 2


def test_kingfishr_does_not_read_babelfishrs_env_file(keychain, tmp_path):
    bfr_env = tmp_path / "home" / ".config" / "babelfishr" / ".env"
    bfr_env.parent.mkdir(parents=True)
    bfr_env.write_text(f"{NAME}=babelfishr-dotenv-secret\n")
    assert credentials.get_secret(NAME) is None
    own = tmp_path / "home" / ".config" / "kingfishr" / ".env"
    own.parent.mkdir(parents=True)
    own.write_text(f"{NAME}=kingfishr-dotenv-secret\n")
    assert credentials.get_secret(NAME) == "kingfishr-dotenv-secret"


def test_babelfishr_keeps_its_existing_service(keychain):
    store, calls = keychain
    extensions.reset()   # the same process, started as BabelFishR
    assert credentials.keychain_service() == credentials.KEYCHAIN_SERVICE == "BabelFishR"
    assert credentials.get_secret(NAME) == "babelfishr-secret-do-not-share"
    assert set(_services(calls)) == {"BabelFishR"}


def test_kingfishr_does_not_fall_back_to_a_shared_working_directory_env(keychain, tmp_path):
    (tmp_path / ".env").write_text(f"{NAME}=shared-cwd-dotenv-secret\n")
    assert credentials.get_secret(NAME) is None
    assert credentials.describe_source(NAME) == "not configured"


def test_explicit_overrides_still_apply_in_kingfishr(keychain, tmp_path, monkeypatch):
    named = tmp_path / "chosen.env"
    named.write_text(f"{NAME}=explicit-dotenv-secret\n")
    assert credentials.get_secret(NAME, dotenv_path=str(named)) == "explicit-dotenv-secret"
    monkeypatch.setenv(NAME, "process-environment-secret")
    assert credentials.get_secret(NAME) == "process-environment-secret"


def test_babelfishr_still_reads_the_working_directory_env(keychain, tmp_path):
    extensions.reset()
    (tmp_path / ".env").write_text(f"{NAME}=babelfishr-cwd-secret\n")
    assert credentials.get_secret(NAME) == "babelfishr-cwd-secret"
