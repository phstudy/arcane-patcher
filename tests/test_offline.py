"""offline.py: the server embedded in the offline variant comes from the patch file (release.py packs the release
pinned in emulator.lock, sha256-checked), or from --emulator-zip / --server-dir; without one the build stops before
anything is built. No network: downloads are local files."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from arcane_patcher import offline, patch


def _release_zip(path: Path, marker: str = "1") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("src/arcane_server/__init__.py", "")
        z.writestr("src/arcane_server/__main__.py", f"# release {marker}\n")
        z.writestr("src/arcane_server/__pycache__/x.cpython-314.pyc", b"stale")
        z.writestr("content/clients/v29.json", "{}")
        z.writestr("content/.content_validated.json", "{}")
        z.writestr("proto/serverproto.proto", 'syntax = "proto3";\n')
    return path


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("ARCANE_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def _pf_with_server(tmp_path, rel: Path):
    from conftest import make_patch_file, keys_for_tests

    from arcane_patcher import patchfile

    return patchfile.PatchFile.open(make_patch_file(tmp_path / "pf", server_zip=rel), keys=keys_for_tests())


def test_offline_uses_the_patch_files_server(tmp_path, monkeypatch):
    rel = _release_zip(tmp_path / "arcane-server-0.1.0.zip")
    pf = _pf_with_server(tmp_path, rel)
    assert pf.server == "server/arcane-server-0.1.0.zip"
    rel.unlink()  # it comes from the patch file

    def no_download(*a, **k):
        raise AssertionError("downloaded")

    monkeypatch.setattr(offline, "fetch", no_download)
    src = offline.server_source(tmp_path / "w1", pf=pf)
    assert (src / "src/arcane_server/__main__.py").read_text() == "# release 1\n"
    assert (src / "content/clients/v29.json").is_file() and (src / "proto/serverproto.proto").is_file()
    assert not (src / "src/arcane_server/__pycache__").exists() and not (src / "content/.content_validated.json").exists()


def test_patch_file_without_a_server_says_so(tmp_path, patch_file):
    assert patch_file.server is None
    with pytest.raises(SystemExit, match="holds no server release.*--emulator-zip PATH.*--server-dir PATH"):
        offline.server_source(tmp_path / "w", pf=patch_file)


def test_offline_build_without_a_server_stops_before_building(tmp_path, monkeypatch, patch_file):
    monkeypatch.setattr(patch, "read_inputs", lambda apk: patch.Inputs(Path(apk), []))
    monkeypatch.setattr(patch, "build_rules", lambda inputs, pf: (b"", {"client": "x", "lib": {"local_battles": {},
                                                                       "publisher_services": {}},
                                                                       "dex": {"analytics_upload": {}}}))
    monkeypatch.setattr(patch, "input_package", lambda inputs: "com.example.game")

    def no_build(*a, **k):
        raise AssertionError("the build started")

    monkeypatch.setattr(patch, "compile_nsc", no_build)
    monkeypatch.setattr(patch, "KeystoreSigner", lambda key: object())  # the keystore is signing's test
    monkeypatch.setattr(patch, "key_from_env", lambda *a: None)
    out = tmp_path / "out" / "offline.apk"
    with pytest.raises(SystemExit, match="holds no server release"):
        patch.patch(tmp_path / "game.apk", out, "offline", tmp_path / "k", None, env={}, patch_file=patch_file,
                    skip_consent=False, guest_login=False, ad_rewards=False, skip_rate_prompt=False,
                    store_timers=False)
    assert not out.parent.exists()


def test_release_server_pin(tmp_path, cache):
    """release.py --pinned-server: emulator.lock's pin, sha256-checked (a mismatch is an error, nothing cached)."""
    rel = _release_zip(tmp_path / "release.zip")
    sha = hashlib.sha256(rel.read_bytes()).hexdigest()
    lock = tmp_path / "emulator.lock"
    lock.write_text(json.dumps({"version": "0.1.0", "url": rel.as_uri(), "sha256": sha}))
    pin = offline.read_lock(lock)
    assert offline.fetch(pin.url, pin.sha256).read_bytes() == rel.read_bytes()
    other = _release_zip(tmp_path / "other.zip", "2")
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        offline.fetch(other.as_uri(), sha, "other-0.2.0.zip")
    assert not any("0.2.0" in p.name for p in cache.iterdir())


def test_repository_lock_has_the_pin_fields():
    pin = offline.read_lock()
    assert set(json.loads(offline.LOCK.read_text())) == {"version", "url", "sha256"}
    assert (pin.url == "") == (pin.sha256 == "")


def test_server_dir_override(tmp_path, monkeypatch):
    """--server-dir: a server checkout (src/arcane_server, content/, proto/) is used as is; nothing is downloaded."""
    def no_download(*a, **k):
        raise AssertionError("downloaded")

    monkeypatch.setattr(offline, "fetch", no_download)
    checkout = tmp_path / "server"
    (checkout / "src/arcane_server").mkdir(parents=True)
    (checkout / "src/arcane_server/__init__.py").write_text("")
    (checkout / "src/arcane_server/__main__.py").write_text("# checkout\n")
    (checkout / "content").mkdir()
    (checkout / "content/a.json").write_text("{}")
    (checkout / "proto").mkdir()
    src = offline.server_source(tmp_path / "w", server_dir=checkout)
    assert (src / "src/arcane_server/__main__.py").read_text() == "# checkout\n" and (src / "content/a.json").is_file()
    # a release zip given as --emulator-zip (development: not checked against the lock)
    rel = _release_zip(tmp_path / "rel.zip")
    assert (offline.server_source(tmp_path / "w3", emulator_zip=rel) / "src/arcane_server").is_dir()
    # the zip's layout without src/ is not a release
    flat = tmp_path / "flat"
    (flat / "arcane_server").mkdir(parents=True)
    (flat / "arcane_server/__main__.py").write_text("")
    (flat / "content").mkdir()
    (flat / "proto").mkdir()
    with pytest.raises(SystemExit, match="not a server release"):
        offline.server_source(tmp_path / "w5", server_dir=flat)
    with pytest.raises(SystemExit, match="not a server release"):
        offline.server_source(tmp_path / "w4", server_dir=tmp_path / "w")


def test_payload_pins_every_download():
    """Every download of the offline runtime and the kit is pinned by sha256; the payload's Python packages are pure
    wheels."""
    from arcane_patcher import kit

    rt = kit.runtime()
    pins = [(rt["cpython_android"]["url"], rt["cpython_android"]["sha256"]),
            *((w["url"], w["sha256"]) for w in rt["wheels"]), kit.LZ4_SDIST, kit.PROTOBUF_SDIST, kit.ZXING_CORE]
    for url, sha in pins:
        assert url.startswith("https://") and len(sha) == 64 and int(sha, 16) >= 0
    assert all(w["url"].endswith("-none-any.whl") for w in rt["wheels"])
    assert f"/python-{rt['python']}." in rt["cpython_android"]["url"]
    assert offline.device_root("com.example.game.offline") == "/data/user/0/com.example.game.offline/files/arcane_offline/root"
