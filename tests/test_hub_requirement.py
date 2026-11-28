"""The Hugging Face client the model download needs is declared.

prepare_model passes tqdm_class (real byte progress) and an exact revision
(the pinned commit) to hf_hub_download and snapshot_download. faster-whisper
alone accepts huggingface_hub>=0.21, whose hf_hub_download has no
tqdm_class; the progress adapter was checked against 2.4.0 and 2.5.0. The
asr extra declares that range and the macOS lock stays inside it.
"""

from __future__ import annotations

import inspect
import pathlib
import re

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _asr_requirements():
    text = (ROOT / "pyproject.toml").read_text()
    block = re.search(r"^asr = \[(.*?)\]", text, re.M | re.S).group(1)
    return {Requirement(r).name: Requirement(r)
            for r in re.findall(r'"([^"]+)"', block)}


def test_the_asr_extra_declares_the_hub_client_range():
    hub = _asr_requirements()["huggingface-hub"]
    assert Version("2.4.0") in hub.specifier
    assert Version("2.5.0") in hub.specifier
    assert Version("0.36.0") not in hub.specifier      # no tqdm_class there
    assert Version("3.0.0") not in hub.specifier


def test_the_macos_lock_pins_a_version_inside_the_declared_range():
    lock = (ROOT / "packaging" / "requirements-macos-arm64.txt").read_text()
    pinned = Version(re.search(r"^huggingface-hub==(\S+)", lock, re.M).group(1))
    assert pinned in _asr_requirements()["huggingface-hub"].specifier


def test_the_installed_client_takes_tqdm_class_and_revision():
    import huggingface_hub

    for func in (huggingface_hub.hf_hub_download, huggingface_hub.snapshot_download):
        params = inspect.signature(func).parameters
        assert "tqdm_class" in params and "revision" in params
    assert Version(huggingface_hub.__version__) in \
        _asr_requirements()["huggingface-hub"].specifier
