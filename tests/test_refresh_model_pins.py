"""scripts/refresh_model_pins.py against a stand-in for the Hub's API.

The real pins can only come from huggingface.co; this proves the script
reads the commit, the LFS sha256 and the small files' Git blob ids
correctly, refuses inconsistent answers, and writes a model_pins.py the
application accepts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import pytest

from pinning import git_blob

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO = "Systran/faster-whisper-tiny"
COMMIT = "b" * 40
SMALL = {"config.json": b'{"a": 1}', "tokenizer.json": b'{"t": 2}'}
WEIGHTS = b"w" * 4096


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location(
        "refresh_model_pins", ROOT / "scripts" / "refresh_model_pins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Api:
    def __init__(self, blob_override=None):
        self.urls = []
        self.blob_override = blob_override or {}

    def json(self, url):
        self.urls.append(url)
        base = f"https://huggingface.co/api/models/{REPO}"
        if url == f"{base}/revision/main":
            return {"sha": COMMIT}
        if url == f"{base}/tree/{COMMIT}":
            entries = [{"type": "file", "path": name, "size": len(data),
                        "oid": self.blob_override.get(name, git_blob(data))}
                       for name, data in SMALL.items()]
            entries.append({"type": "file", "path": "model.bin", "size": len(WEIGHTS),
                            "oid": git_blob(b"pointer"),
                            "lfs": {"oid": hashlib.sha256(WEIGHTS).hexdigest(),
                                    "size": len(WEIGHTS), "pointerSize": 130}})
            entries.append({"type": "file", "path": "README.md", "size": 3, "oid": "x"})
            return entries
        raise AssertionError(url)

    def bytes(self, url):
        self.urls.append(url)
        prefix = f"https://huggingface.co/{REPO}/resolve/{COMMIT}/"
        assert url.startswith(prefix), url
        return SMALL[url[len(prefix):]]


def test_it_pins_the_resolved_commit_and_every_files_sha256(script, tmp_path):
    from babelfishr.model_integrity import pin_for

    api = Api()
    output = tmp_path / "model_pins.py"
    pins = script.refresh(["tiny"], output=output, get_json=api.json,
                          get_bytes=api.bytes,
                          current={"tiny": {"repo": REPO, "revision": None, "files": {}}})
    entry = pins["tiny"]
    assert entry["revision"] == COMMIT
    assert set(entry["files"]) == {"config.json", "tokenizer.json", "model.bin"}
    assert entry["files"]["model.bin"]["sha256"] == hashlib.sha256(WEIGHTS).hexdigest()
    assert entry["files"]["config.json"]["sha256"] == \
        hashlib.sha256(SMALL["config.json"]).hexdigest()
    # The large file is never downloaded; every file URL names the commit.
    assert not any(url.endswith("/model.bin") for url in api.urls)
    namespace = {}
    exec(output.read_text(), namespace)
    assert namespace["PINS"] == pins
    assert pin_for("tiny", namespace["PINS"]).revision == COMMIT
    assert "refreshed" in namespace["__doc__"]


def test_an_inconsistent_answer_writes_nothing(script, tmp_path):
    output = tmp_path / "model_pins.py"
    api = Api(blob_override={"config.json": "0" * 40})
    with pytest.raises(script.RefreshError, match="Git blob id"):
        script.refresh(["tiny"], output=output, get_json=api.json, get_bytes=api.bytes,
                       current={"tiny": {"repo": REPO, "revision": None, "files": {}}})
    assert not output.exists()


def test_check_reports_unpinned_models(script):
    from pinning import pin_entry

    good = pin_entry(REPO, dict(SMALL, **{"model.bin": WEIGHTS}), COMMIT)
    assert script.unpinned({"tiny": good}, ["tiny"]) == {}
    problems = script.unpinned({"tiny": {"repo": REPO, "revision": None, "files": {}}},
                               ["tiny", "base"])
    assert set(problems) == {"tiny", "base"}


def test_a_redirected_repository_is_pinned_where_it_is_served(script, tmp_path):
    """Asked for the old id, the Hub answers for the new one (its "id"): the
    pin names the served repository, keeps the old id as an alias, and with
    --revision pinned reads digests at exactly the pinned commit."""
    old, new = "mobiuslabsgmbh/faster-whisper-large-v3-turbo", REPO
    api = Api()
    real_json = api.json

    def json_(url):
        url = url.replace(f"/api/models/{old}/", f"/api/models/{new}/")
        if url == f"https://huggingface.co/api/models/{new}/revision/{COMMIT}":
            api.urls.append(url)
            return {"sha": COMMIT, "id": new}
        return real_json(url)

    pins = script.refresh(["large-v3-turbo"], revision="pinned",
                          output=tmp_path / "model_pins.py", get_json=json_,
                          get_bytes=api.bytes,
                          current={"large-v3-turbo": {"repo": old, "revision": COMMIT,
                                                      "files": {}}})
    entry = pins["large-v3-turbo"]
    assert entry["repo"] == new and entry["repo_aliases"] == [old]
    assert entry["revision"] == COMMIT
    assert any(f"/tree/{COMMIT}" in url and new in url for url in api.urls)


def test_a_different_commit_than_the_pinned_one_is_refused(script, tmp_path):
    api = Api()
    with pytest.raises(script.RefreshError, match="asked for commit"):
        script.refresh(["tiny"], revision="pinned", output=tmp_path / "p.py",
                       get_json=lambda url: {"sha": COMMIT},
                       get_bytes=api.bytes,
                       current={"tiny": {"repo": REPO, "revision": "c" * 40, "files": {}}})
