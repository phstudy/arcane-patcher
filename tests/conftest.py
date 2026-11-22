"""Shared test settings. Tests that need the user's v29 APK (single APK or split bundle) run only when the
environment variable ARCANE_TEST_APK names it; nothing here uses the network or the real time.

Patch files for tests are made from the repository's patchdata/ with a stand-in kit and signed with a test key
(keys_for_tests), so the release key is never needed."""

import json
import os
from pathlib import Path

import pytest

APK = Path(os.environ["ARCANE_TEST_APK"]) if os.environ.get("ARCANE_TEST_APK") else None
needs_apk = pytest.mark.skipif(APK is None or not APK.is_file(), reason="ARCANE_TEST_APK not set")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "patchdata"
VARIANTS = json.loads((DATA / "variants.json").read_text())
# the manifest fragment of each variant kind (the setup screen's launcher, its components)
FRAGMENTS = {v["kind"]: (DATA / v["manifest"]).read_text() for v in VARIANTS.values()}


def _test_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def keys_for_tests() -> dict[str, str]:
    import base64

    from cryptography.hazmat.primitives import serialization

    raw = _test_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"test": base64.b64encode(raw).decode()}


def fake_kit(dir: Path) -> Path:
    """A kit whose files are placeholders, with a kit.json that matches the current sources."""
    from arcane_patcher import kit

    dir.mkdir(parents=True, exist_ok=True)
    for name in kit.FILES:
        (dir / name).write_bytes(f"kit:{name}".encode())
    kit.write_manifest(dir)
    return dir


def make_patch_file(dir: Path, version: str = "1.2.3", sign: bool = True, kit_dir: Path | None = None,
                    server_zip: Path | None = None, edit=None) -> Path:
    """dir/arcane-patch-<version>.zip from patchdata/ (the stand-in kit unless `kit_dir`), signed with the test key
    unless `sign` is False. `edit(info, files)` may change the contents before signing."""
    from arcane_patcher import release

    info, files = release.contents(server_zip, kit_dir=kit_dir or fake_kit(dir / "kit"))
    if edit:
        edit(info, files)
    return release.write(dir / f"arcane-patch-{version}.zip", version, info, files,
                         _test_key() if sign else None, "test",
                         {"repository": "https://example.invalid/arcane-patcher", "tag": f"v{version}", "commit": "",
                          "license": "GPL-3.0-only"})


@pytest.fixture
def patch_file(tmp_path):
    """An opened, official (test key) patch file of the repository's patchdata/."""
    from arcane_patcher import patchfile

    return patchfile.PatchFile.open(make_patch_file(tmp_path / "pf"), keys=keys_for_tests())
