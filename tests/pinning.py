"""Test pins: a model's pinned identity made from fixture bytes.

The shipped model_pins.PINS holds the real Hub identities (or, until they
are refreshed, none - and then every install fails closed). Tests that
install fixture weights pin exactly those bytes, through the same table the
application reads, for the duration of one test.
"""

from __future__ import annotations

import hashlib

#: A legitimate-looking but fixture-only commit (never a real Hub revision).
REVISION = "a1" * 20


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def pin_entry(repo: str, files: dict, revision: str = REVISION) -> dict:
    return {"repo": repo, "revision": revision,
            "files": {name: {"sha256": sha256(data), "size": len(data),
                             "git_oid": git_blob(data)}
                      for name, data in files.items()}}


def pin(monkeypatch, model: str, files: dict, repo: str = None,
        revision: str = REVISION) -> dict:
    """Pin *model* to *files* ({name: bytes}) for this test."""
    from babelfishr import model_pins

    repo = repo or model_pins.PINS.get(model, {}).get("repo") or f"test/{model}"
    entry = pin_entry(repo, files, revision)
    monkeypatch.setitem(model_pins.PINS, model, entry)
    return entry


#: What fakes that write b"synthetic" into every asset install.
SYNTHETIC = {"config.json": b"synthetic", "model.bin": b"synthetic",
             "tokenizer.json": b"synthetic"}


def pin_synthetic(monkeypatch, *models: str) -> None:
    """Pin each model (default: every supported size) to SYNTHETIC."""
    from babelfishr.providers.whisper_local import MODEL_SIZES

    for model in models or MODEL_SIZES:
        pin(monkeypatch, model, SYNTHETIC)
