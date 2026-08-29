# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the BabelFishR macOS application bundle.

Build (on macOS, from the repository root):

    pip install -e ".[gui,audio,asr,translate]" pyinstaller
    pyinstaller packaging/babelfishr.spec --noconfirm

The result is dist/BabelFishR.app.

Deliberately NOT bundled: the Whisper model and Argos language packs. They are
large, they are chosen by the operator, and - most importantly - they live in
Application Support so that reinstalling or upgrading the app cannot delete
them. `babelfishr prepare-field` puts them there.
"""

import pathlib
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = pathlib.Path(SPECPATH).parent
ICON = ROOT / "packaging" / "BabelFishR.icns"

hidden = collect_submodules("babelfishr")
datas = []

# certifi's cacert.pem, explicitly. Without a CA bundle inside the bundle the
# frozen app has no trust store at all - macOS keeps its roots in the Keychain,
# not in a PEM file OpenSSL can read - and every HTTPS download fails with
# "unable to get local issuer certificate". A real Mac proved that the hard
# way. This is not left to an implicit hook.
try:
    import certifi

    datas += [(certifi.where(), "certifi")]
    hidden += ["certifi"]
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        "certifi is required to build a working bundle: without its CA data "
        "the packaged app cannot complete any HTTPS download. "
        f"(pip install certifi)  [{exc}]")

# Optional engines: include when installed, so a prepared machine can build a
# self-contained app, but never fail the build when they are absent.
for package in ("faster_whisper", "argostranslate", "ctranslate2"):
    try:
        datas += collect_data_files(package)
        hidden += collect_submodules(package)
    except Exception:  # noqa: BLE001 - optional extras
        pass

analysis = Analysis(
    [str(ROOT / "packaging" / "app_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore"],
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data)

exe = EXE(
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name="BabelFishR",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,          # build natively; arm64 on Apple silicon
    codesign_identity=None,    # see docs/MACOS_PACKAGING.md
    entitlements_file=str(ROOT / "packaging" / "entitlements.plist"),
    icon=str(ICON) if ICON.exists() else None,
)

collected = COLLECT(
    exe, analysis.binaries, analysis.datas,
    strip=False, upx=False, name="BabelFishR",
)

app = BUNDLE(
    collected,
    name="BabelFishR.app",
    icon=str(ICON) if ICON.exists() else None,
    bundle_identifier="org.babelfishr.app",
    info_plist={
        "CFBundleName": "BabelFishR",
        "CFBundleDisplayName": "BabelFishR",
        "CFBundleShortVersionString": "0.3.0",
        "CFBundleVersion": "0.3.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # macOS refuses audio input without this string, and shows it verbatim
        # in the permission prompt.
        "NSMicrophoneUsageDescription":
            "BabelFishR records received radio audio from the audio input you "
            "select, so it can transcribe and translate transmissions. It is "
            "receive-only and never transmits.",
        # Follow the system light/dark appearance rather than forcing one.
        "NSRequiresAquaSystemAppearance": False,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "CFBundleDocumentTypes": [],
    },
)
