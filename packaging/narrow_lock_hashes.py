"""Narrow the lock's hashes to the wheels the release target can install.

uv writes a hash for every file PyPI has for each pinned version: Windows,
Linux, Intel and sdists included. This keeps only the hashes of wheels a
macOS arm64 CPython 3.12 interpreter can install (cp312, abi3 or pure-Python,
for ``arm64`` or ``universal2`` up to the runner's macOS version), so the
lock admits nothing the runner should never download.

A hash is kept only if it appears in BOTH sources: the uv output and PyPI's
JSON API (``https://pypi.org/pypi/<name>/<version>/json``) for that exact
file. A pin left with no hash is an error, never silently dropped.

Standard library only; run with ``python3 -I``. Called by
refresh_macos_lock.sh.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

MACOS = (26, 0)            # the macos-26 release runner
PY = (3, 12)


def compatible(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) not in (5, 6):
        return False
    pythons, abis, platforms = (set(p.split(".")) for p in parts[-3:])
    major, minor = PY

    def python_ok(tag: str) -> bool:
        if tag in (f"py{major}", f"py{major}{minor}"):
            return True
        match = re.fullmatch(r"cp(\d)(\d+)", tag)
        if not match or int(match[1]) != major:
            return False
        version = int(match[2])
        # An abi3 wheel built for an older cp3x runs on 3.12.
        return version == minor or ("abi3" in abis and version <= minor)

    def abi_ok(tag: str) -> bool:
        return tag in ("none", "abi3", f"cp{major}{minor}")

    def platform_ok(tag: str) -> bool:
        if tag == "any":
            return True
        match = re.fullmatch(r"macosx_(\d+)_(\d+)_(arm64|universal2)", tag)
        return bool(match) and (int(match[1]), int(match[2])) <= MACOS

    return (any(python_ok(t) for t in pythons) and any(abi_ok(t) for t in abis)
            and any(platform_ok(t) for t in platforms))


def pypi_digests(name: str, version: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = json.load(response)
    return {f["filename"]: f["digests"]["sha256"] for f in data["urls"]}


def narrow(text: str) -> str:
    out, problems = [], []
    blocks = re.split(r"(?m)^(?=[A-Za-z0-9])", text)
    for block in blocks:
        match = re.match(r"([A-Za-z0-9][\w.-]*)==(\S+) \\\n", block)
        if not match:
            out.append(block)
            continue
        name, version = match.groups()
        uv_hashes = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block))
        keep = sorted({digest for filename, digest in pypi_digests(name, version).items()
                       if compatible(filename) and digest in uv_hashes})
        if not keep:
            problems.append(f"{name}=={version}: no macOS arm64 cp312 wheel on PyPI")
            continue
        comments = "".join(line + "\n" for line in block.splitlines()
                           if line.lstrip().startswith("#"))
        hashes = " \\\n".join(f"    --hash=sha256:{digest}" for digest in keep)
        out.append(f"{name}=={version} \\\n{hashes}\n{comments}")
    if problems:
        sys.exit("\n".join(problems))
    return "".join(out)


def main(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    narrowed = narrow(text)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(narrowed)


if __name__ == "__main__":
    main(sys.argv[1])
