#!/usr/bin/env python3
"""Refresh babelfishr/model_pins.py from the real Hugging Face Hub API.

For each supported Whisper model this asks the Hub for

1. the commit a revision (default ``main``) currently points at -
   ``GET /api/models/<repo>/revision/<revision>`` -> ``sha`` (40 hex);
2. the files at exactly that commit - ``GET /api/models/<repo>/tree/<sha>``
   - keeping the ones faster-whisper fetches (whisper_local.ALLOW_PATTERNS);
3. each file's sha256: for an LFS file, the Hub's ``lfs.oid`` (the sha256 of
   the content, with ``lfs.size``); for a small non-LFS file, the file itself
   is fetched from ``/<repo>/resolve/<sha>/<name>``, its Git blob id is
   checked against the tree's ``oid`` and its sha256 computed.

Nothing is typed in by hand and nothing is guessed: a failed request or any
inconsistency stops the script without writing. Then it rewrites
babelfishr/model_pins.py (review the diff and commit it).

Usage (from the repository root, with access to huggingface.co):

    python scripts/refresh_model_pins.py               # every model, from main
    python scripts/refresh_model_pins.py --model small # one model
    python scripts/refresh_model_pins.py --revision pinned
                                    # keep the pinned commits, fill in digests
    python scripts/refresh_model_pins.py --check       # exit 1 if any model
                                                       # is unpinned (release gate)

It honours HTTPS_PROXY and the standard CA variables (urllib). Set HF_TOKEN
to send a token (not needed for these public repositories).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import pathlib
import pprint
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Iterable, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "babelfishr" / "model_pins.py"
ENDPOINT = "https://huggingface.co"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RefreshError(RuntimeError):
    pass


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": "model-pins-refresh"}
    token = os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(_request(url), timeout=60) as response:
        return response.read()


def fetch_json(url: str):
    return json.loads(fetch_bytes(url).decode("utf-8"))


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _quote(repo: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/"))


def pin_repository(repo: str, revision: str = "main", endpoint: str = ENDPOINT,
                   patterns: Iterable[str] = (),
                   get_json: Callable[[str], object] = fetch_json,
                   get_bytes: Callable[[str], bytes] = fetch_bytes,
                   aliases: Iterable[str] = ()) -> dict:
    """One pin entry for *repo* at the commit *revision* resolves to now."""
    base = f"{endpoint.rstrip('/')}/api/models/{_quote(repo)}"
    info = get_json(f"{base}/revision/{urllib.parse.quote(revision, safe='')}")
    commit = str((info or {}).get("sha") or "")
    if not _COMMIT.match(commit):
        raise RefreshError(f"{repo}@{revision}: the Hub returned no commit sha ({commit!r})")
    if _COMMIT.match(revision) and commit != revision:
        raise RefreshError(f"{repo}: asked for commit {revision}, the Hub answered {commit}")
    # A moved repository: the Hub redirects the old id and names the new one.
    # Pin the repository it actually serves; keep the old id as an alias.
    aliases = list(aliases)
    served = str((info or {}).get("id") or (info or {}).get("modelId") or repo)
    if served != repo:
        print(f"  {repo} is served as {served} (redirect); pinning {served}",
              file=sys.stderr)
        aliases.append(repo)
        repo = served
        base = f"{endpoint.rstrip('/')}/api/models/{_quote(repo)}"
    tree = get_json(f"{base}/tree/{commit}")
    if not isinstance(tree, list):
        raise RefreshError(f"{repo}@{commit}: unexpected tree listing")
    files: Dict[str, dict] = {}
    for entry in tree:
        name = entry.get("path", "")
        if entry.get("type") != "file" or not any(
                fnmatch.fnmatch(name, pattern) for pattern in patterns):
            continue
        oid = str(entry.get("oid") or "")
        lfs = entry.get("lfs")
        if lfs:
            digest, size = str(lfs.get("oid") or "").lower(), lfs.get("size")
            if not _SHA256.match(digest) or not isinstance(size, int):
                raise RefreshError(f"{repo}@{commit}/{name}: no LFS sha256/size")
        else:
            data = get_bytes(f"{endpoint.rstrip('/')}/{_quote(repo)}/resolve/{commit}/"
                             f"{urllib.parse.quote(name)}")
            if git_blob_id(data) != oid:
                raise RefreshError(f"{repo}@{commit}/{name}: the content does not match "
                                   f"the tree's Git blob id {oid}")
            if entry.get("size") is not None and len(data) != entry["size"]:
                raise RefreshError(f"{repo}@{commit}/{name}: size mismatch")
            digest, size = hashlib.sha256(data).hexdigest(), len(data)
        files[name] = {"sha256": digest, "size": int(size), "git_oid": oid or None}
    entry = {"repo": repo, "revision": commit, "files": dict(sorted(files.items()))}
    aliases = sorted(set(aliases) - {repo})
    if aliases:
        entry["repo_aliases"] = aliases
    return entry


def render(pins: dict, note: str) -> str:
    """The text of babelfishr/model_pins.py for *pins*."""
    from babelfishr import model_pins

    doc = (model_pins.__doc__ or "").split("State of this file:")[0].rstrip()
    body = pprint.pformat(pins, width=100, sort_dicts=False)
    return (f'"""{doc}\n\nState of this file: {note}\n"""\n\nPINS = {body}\n')


def unpinned(pins: dict, models: Iterable[str]) -> Dict[str, str]:
    """``{model: reason}`` for each model whose pin cannot establish identity."""
    from babelfishr.model_integrity import UnpinnedModel, pin_for

    problems = {}
    for model in models:
        try:
            pin_for(model, pins)
        except UnpinnedModel as exc:
            problems[model] = str(exc)
    return problems


def refresh(models: Iterable[str], revision: str = "main", endpoint: str = ENDPOINT,
            output: pathlib.Path = OUTPUT, get_json=fetch_json, get_bytes=fetch_bytes,
            current: Optional[dict] = None) -> dict:
    from babelfishr.providers.whisper_local import ALLOW_PATTERNS, _repository_id

    if current is None:
        from babelfishr import model_pins

        current = model_pins.PINS
    pins = {name: dict(entry) for name, entry in current.items()}
    for model in models:
        entry = current.get(model) or {}
        repo = entry.get("repo") or _repository_id(model)
        wanted = revision
        if revision == "pinned":       # digests at the commit already pinned
            wanted = str(entry.get("revision") or "")
            if not _COMMIT.match(wanted):
                raise RefreshError(f"{model}: --revision pinned, but no commit is pinned")
        print(f"{model}: {repo}@{wanted} ...", file=sys.stderr)
        pins[model] = pin_repository(repo, wanted, endpoint, ALLOW_PATTERNS,
                                     get_json, get_bytes,
                                     aliases=entry.get("repo_aliases") or ())
        print(f"  -> {pins[model]['revision']} ({len(pins[model]['files'])} files)",
              file=sys.stderr)
    problems = unpinned(pins, models)
    if problems:
        raise RefreshError("refusing to write incomplete pins: " + "; ".join(
            f"{m}: {why}" for m, why in problems.items()))
    note = (f"refreshed {time.strftime('%Y-%m-%d', time.gmtime())} from {endpoint} "
            f"(revision {revision!r} of each repository, resolved to the commits below).")
    output.write_text(render(pins, note), encoding="utf-8")
    return pins


def main(argv=None) -> int:
    sys.path.insert(0, str(ROOT))
    from babelfishr import model_pins
    from babelfishr.providers.whisper_local import MODEL_SIZES

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", action="append", choices=MODEL_SIZES,
                        help="refresh only this model (repeatable)")
    parser.add_argument("--revision", default="main",
                        help="branch, tag or commit to pin (default: main); "
                             "'pinned' keeps each model's pinned commit and only "
                             "reads its file digests")
    parser.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", ENDPOINT))
    parser.add_argument("--check", action="store_true",
                        help="only check that every model is pinned; exit 1 if not")
    args = parser.parse_args(argv)
    models = args.model or list(MODEL_SIZES)
    if args.check:
        problems = unpinned(model_pins.PINS, models)
        for model, why in problems.items():
            print(f"UNPINNED {model}: {why}", file=sys.stderr)
        return 1 if problems else 0
    try:
        refresh(models, args.revision, args.endpoint)
    except (RefreshError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {OUTPUT.relative_to(ROOT)}; review the diff and commit it.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
