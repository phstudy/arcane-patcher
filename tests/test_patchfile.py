"""Patch files (patchfile.py) and making them (release.py): integrity, signatures and the unofficial switch,
compatibility, the versionCode, finding the default one, deterministic output. No network, no real time."""

import json
import os
import zipfile
from pathlib import Path

import pytest
from conftest import fake_kit, keys_for_tests, make_patch_file

from arcane_patcher import __version__, patch, patchfile, release


def _open(path, keys=None):
    return patchfile.PatchFile.open(path, keys=keys_for_tests() if keys is None else keys)


def _rewrite(src, dst, change):
    """dst: src with change(name, data) -> data applied to every member (patch.json included)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for info in zin.infolist():
            zout.writestr(info, change(info.filename, zin.read(info)))
    return dst


def test_contents_and_trust(tmp_path):
    pf = _open(make_patch_file(tmp_path))
    assert pf.official and pf.trust == "official" and pf.key_id == "test" and pf.version == "1.2.3"
    assert pf.compatible() and pf.info["min_patcher"] == __version__
    assert set(pf.variants) == {"offline", "multi"} and pf.content["client"] == "v29"
    assert pf.kit("offline.dex") == b"kit:offline.dex"
    rules = pf.rules(bytes.fromhex(next(iter(pf.builds))))
    assert rules["client"] == "v29.0 build 1587910" and "local_battles" in rules["lib"]
    with pytest.raises(SystemExit, match="has no rules for build-id 00ff.*v29.0 build 1587910"):
        pf.rules(b"\x00\xff")
    # the release key does not know the test key; an unsigned file
    assert patchfile.PatchFile.open(pf.path).trust == "unknown key"
    assert _open(make_patch_file(tmp_path / "u", sign=False)).trust == "unsigned"


def test_tampering_is_caught(tmp_path):
    good = make_patch_file(tmp_path)

    def edit_json(name, data):
        if name != "patch.json":
            return data
        info = json.loads(data)
        info["variants"]["multi"]["label"] = "Changed"
        return json.dumps(info).encode()

    assert _open(_rewrite(good, tmp_path / "arcane-patch-2.zip", edit_json)).trust == "bad signature"
    damaged = _rewrite(good, tmp_path / "arcane-patch-3.zip",
                       lambda n, d: d + b" " if n.startswith("rules/") else d)
    with pytest.raises(SystemExit, match="rules/.* is damaged"):
        _open(damaged)
    extra = _rewrite(good, tmp_path / "arcane-patch-4.zip", lambda n, d: d)
    with zipfile.ZipFile(extra, "a") as z:
        z.writestr("kit/extra.dex", b"x")
    with pytest.raises(SystemExit, match="do not match its list"):
        _open(extra)
    newer = _rewrite(good, tmp_path / "arcane-patch-5.zip",
                     lambda n, d: json.dumps({**json.loads(d), "format": 3}).encode() if n == "patch.json" else d)
    with pytest.raises(SystemExit, match="format 3.*get a newer patcher"):
        _open(newer)
    older = _rewrite(good, tmp_path / "arcane-patch-6.zip",
                     lambda n, d: json.dumps({**json.loads(d), "format": 1}).encode() if n == "patch.json" else d)
    with pytest.raises(SystemExit, match="format 1.*get a newer patch file"):
        _open(older)
    (tmp_path / "x.zip").write_bytes(b"no zip")
    with pytest.raises(SystemExit, match="not a patch file"):
        _open(tmp_path / "x.zip")


def test_unofficial_needs_the_switch(tmp_path, capsys):
    unsigned = make_patch_file(tmp_path, sign=False)
    with pytest.raises(SystemExit, match="unofficial \\(unsigned\\).*--allow-unofficial"):
        patch.open_patch_file(unsigned)
    pf = patch.open_patch_file(unsigned, allow_unofficial=True)
    assert pf.trust == "unsigned" and "WARNING: unofficial patch file" in capsys.readouterr().out
    newer = make_patch_file(tmp_path / "n", edit=lambda info, files: info.update(min_patcher="99.0"))
    with pytest.raises(SystemExit, match="needs patcher 99.0 or newer"):
        patch.open_patch_file(newer, allow_unofficial=True)


def test_version_code_grows_with_the_patch_version():
    v = patchfile.version_code
    assert v("1.0.0") == 2_101_000_000 and v("1.2.3") == 2_101_002_003 and v("1") == v("1.0") == v("1.0.0")
    versions = ["0.1.0", "0.1.1", "0.2.0", "0.10.0", "1.0.0", "1.0.999", "1.999.0", "2.0.0", "46.999.999"]
    codes = [v(x) for x in versions]
    assert codes == sorted(codes) and len(set(codes)) == len(codes)
    assert codes[0] > 2_026_112_200  # above the date-based codes of earlier builds
    assert codes[0] > patch.ORIGINAL_VERSION_CODE and codes[-1] < 1 << 31
    for bad in ("47.0.0", "1.1000.0", "1.0.1000"):
        with pytest.raises(SystemExit, match="too large"):
            v(bad)
    with pytest.raises(SystemExit, match="bad version"):
        v("1.x")


def test_default_is_the_newest_by_version(tmp_path):
    a, b = tmp_path / "next-to-program", tmp_path / "data"
    a.mkdir(), b.mkdir()
    assert patchfile.find_default([a, b, tmp_path / "missing"]) is None
    for d, name in ((a, "arcane-patch-1.2.0.zip"), (a, "arcane-patch-1.10.0.zip"), (b, "arcane-patch-1.9.9.zip"),
                    (b, "arcane-patch-notes.txt"), (a, "other-2.0.0.zip")):
        (d / name).write_bytes(b"")
    os.utime(a / "arcane-patch-1.2.0.zip", (2_000_000_000, 2_000_000_000))  # newer file, older version
    assert patchfile.find_default([a, b]) == a / "arcane-patch-1.10.0.zip"
    assert len(patchfile.search_dirs()) == 2


def test_release_is_deterministic_and_checks_the_kit(tmp_path):
    first = make_patch_file(tmp_path / "a")
    second = make_patch_file(tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()
    stale = fake_kit(tmp_path / "stale")
    (stale / "offline.dex").write_bytes(b"other")
    with pytest.raises(SystemExit, match="kit file damaged: offline.dex"):
        release.contents(kit_dir=stale)
    with zipfile.ZipFile(first) as z:
        names = z.namelist()
        info = json.loads(z.read("patch.json"))
    assert names[0] == "patch.json" and set(names[1:]) == set(info["files"])
    assert {n.split("/")[0] for n in info["files"]} == {"rules", "manifest", "kit"} and info["server"] is None


def test_release_command(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    monkeypatch.setattr(release.kit, "KIT_DIR", fake_kit(tmp_path / "kit"))
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    (tmp_path / "key.pem").write_bytes(pem)
    monkeypatch.setattr(release, "KEY_ID", "test")
    assert release.main(["--version", "0.3.0", "--out", str(tmp_path / "out"), "--key", str(tmp_path / "key.pem")]) == 0
    assert _open(tmp_path / "out" / "arcane-patch-0.3.0.zip").official
    monkeypatch.setenv(release.KEY_ENV, pem.decode())
    assert release.main(["--version", "0.3.1", "--out", str(tmp_path / "out")]) == 0
    assert _open(tmp_path / "out" / "arcane-patch-0.3.1.zip").official
    monkeypatch.delenv(release.KEY_ENV)
    with pytest.raises(SystemExit, match="no signing key"):
        release.main(["--version", "0.3.2", "--out", str(tmp_path / "out")])
    assert release.main(["--version", "0.3.2", "--out", str(tmp_path / "out"), "--unsigned"]) == 0
    assert _open(tmp_path / "out" / "arcane-patch-0.3.2.zip").trust == "unsigned"
    with pytest.raises(SystemExit, match="too large"):
        release.main(["--version", "99.0.0", "--out", str(tmp_path / "out"), "--unsigned"])


def test_program_dir_is_the_folder_holding_the_macos_app(tmp_path):
    exe = tmp_path / "arcane-patcher-macos-arm64" / "arcane-patcher.app" / "Contents" / "MacOS" / "arcane-patcher"
    assert patchfile.program_dir(exe) == tmp_path / "arcane-patcher-macos-arm64"
    plain = tmp_path / "arcane-patcher-linux-x64" / "arcane-patcher"
    assert patchfile.program_dir(plain) == tmp_path / "arcane-patcher-linux-x64"


def test_a_folder_that_cannot_be_listed_is_skipped(tmp_path, monkeypatch):
    """macOS runs a downloaded app from a read-only copy whose folder cannot be listed (Errno 34); the window must
    still open."""
    good, bad = tmp_path / "data", tmp_path / "translocated"
    good.mkdir(), bad.mkdir()
    (good / "arcane-patch-1.0.0.zip").write_bytes(b"")
    real = Path.iterdir

    def iterdir(self):
        if self == bad:
            raise OSError(34, "Result too large")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    assert patchfile.find_default([bad, good]) == good / "arcane-patch-1.0.0.zip"


def test_frozen_program_looks_inside_itself_first(tmp_path, monkeypatch):
    monkeypatch.setattr(patchfile.sys, "frozen", True, raising=False)
    monkeypatch.setattr(patchfile.sys, "_MEIPASS", str(tmp_path / "Frameworks"), raising=False)
    monkeypatch.setattr(patchfile.sys, "executable", str(tmp_path / "x.app" / "Contents" / "MacOS" / "arcane-patcher"))
    dirs = patchfile.search_dirs()
    assert dirs[0] == tmp_path / "Frameworks" / "patch" and dirs[1] == tmp_path
