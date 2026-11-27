"""offline.py: the server embedded in the offline variant comes from the patch file (release.py packs the release
pinned in emulator.lock, sha256-checked), or from --emulator-zip / --server-dir; without one the build stops before
anything is built. No network: downloads are local files."""

import hashlib
import json
import ssl
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
    pins = [*((p["url"], p["sha256"]) for p in rt["cpython_android"].values()),
            *((w["url"], w["sha256"]) for w in rt["wheels"]), kit.LZ4_SDIST, kit.PROTOBUF_SDIST, kit.ZXING_CORE]
    for url, sha in pins:
        assert url.startswith("https://") and len(sha) == 64 and int(sha, 16) >= 0
    assert all(w["url"].endswith("-none-any.whl") for w in rt["wheels"])
    for pin in rt["cpython_android"].values():
        assert f"/python-{rt['python']}." in pin["url"]
    assert offline.device_root("com.example.game.offline") == "/data/user/0/com.example.game.offline/files/arcane_offline/root"


def test_downloads_trust_the_operating_systems_certificates():
    """The packaged Python carries no CA bundle: a fresh Mac refused every HTTPS download until the system's trust
    store was used."""
    import truststore

    from arcane_patcher import offline
    ctx = offline.tls_context()
    assert isinstance(ctx, truststore.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


# --- the server runtimes (kit.SERVER_ABIS) -----------------------------------------------------------------------


def test_every_server_abi_has_its_cpython_pin_and_kit_files():
    """kit.SERVER_ABIS is the one list: the runtime pins one CPython for Android per entry (python.org's build for that
    CPU, the same version for all), the kit holds the interpreter and the native modules per entry, and each entry's
    files in the APK have their own names (arm64 keeps the plain ones)."""
    from arcane_patcher import kit

    rt = kit.runtime()
    assert set(rt["cpython_android"]) == set(kit.SERVER_ABIS) and list(kit.SERVER_ABIS)[0] == "arm64"
    for name, abi in kit.SERVER_ABIS.items():
        url = rt["cpython_android"][name]["url"]
        assert url.startswith("https://www.python.org/ftp/python/") and url.endswith(f"-{abi.triple}.tar.gz")
        for f in kit.SERVER_FILES:
            assert kit.server_file(name, f) in kit.FILES
    assert len({p["url"].split("/")[-2] for p in rt["cpython_android"].values()}) == 1  # one CPython version
    names = [a.lib(so) for a in kit.SERVER_ABIS.values() for so in [offline.LAUNCHER, *offline.native_libs("3.14")]]
    assert len(set(names)) == len(names) and all(n.startswith("lib") and n.endswith(".so") for n in names)
    assert kit.SERVER_ABIS["arm64"].lib("libpython3.14.so") == "libpython3.14.so"
    assert kit.SERVER_ABIS["x86_64"].lib("libpython3.14.so") == "libpython3.14_x86_64.so"
    device_abis = [d for a in kit.SERVER_ABIS.values() for d in a.device_abis]
    assert len(set(device_abis)) == len(device_abis)


def test_the_interpreter_loads_its_own_runtimes_libraries():
    """Each interpreter executable is built with its own runtime's library names (libpython first)."""
    from arcane_patcher import kit, kitbuild

    assert kitbuild.launcher_libs(kit.SERVER_ABIS["x86_64"]) == (
        '"libpython3.14_x86_64.so","libcrypto_python_x86_64.so","libssl_python_x86_64.so","libsqlite3_python_x86_64.so"')
    assert kitbuild.launcher_libs(kit.SERVER_ABIS["arm64"]).startswith('"libpython3.14.so","libcrypto_python.so"')
    src = (kit.ANDROID_DIR / "offline" / "native" / "arcanepython.c").read_text()
    assert "ARCANE_LIBS" in src and "Py_BytesMain" in src and "#include <Python.h>" not in src


def test_runtimes_table_lists_every_runtime_for_the_launcher():
    from arcane_patcher import kit

    lines = offline.runtimes_table().decode().splitlines()
    assert lines == ["arm64 libarcanepython.so arm64-v8a,armeabi-v7a,armeabi",
                     "x86_64 libarcanepython_x86_64.so x86_64,x86"]
    assert [line.split()[0] for line in lines] == list(kit.SERVER_ABIS)


def test_server_runtimes_of_every_abi_go_into_the_games_abi_folder(tmp_path, monkeypatch, patch_file):
    """Every offline APK carries each server runtime in lib/arm64-v8a/ (the only ABI folder of the game, which the
    package manager extracts to the folder OfflineServer execs from), each under its own names; the payload is one."""
    from arcane_patcher import kit

    seen = {}

    def prefix(tmp, runtime, abi):
        d = tmp_path / f"prefix-{abi}"
        (d / "lib").mkdir(parents=True, exist_ok=True)
        for so in offline.native_libs("3.14"):
            (d / "lib" / so).write_bytes(f"{abi}:{so}".encode())
        return d

    monkeypatch.setattr(offline, "python_prefix", prefix)
    monkeypatch.setattr(offline, "build_payload", lambda prefixes, *a: seen.update(abis=list(prefixes)) or b"payload")
    base, native = offline.build_runtime(tmp_path / "w", "com.example.game.offline", tmp_path, patch_file)
    assert seen["abis"] == list(kit.SERVER_ABIS)
    assert base[offline.RUNTIMES_ASSET] == offline.runtimes_table()
    assert set(native) == {f"lib/arm64-v8a/{a.lib(so)}" for a in kit.SERVER_ABIS.values()
                           for so in [offline.LAUNCHER, *offline.native_libs("3.14")]}
    assert native["lib/arm64-v8a/libarcanepython.so"] == b"kit:arm64/libarcanepython.so"
    assert native["lib/arm64-v8a/libarcanepython_x86_64.so"] == b"kit:x86_64/libarcanepython.so"
    assert native["lib/arm64-v8a/libpython3.14.so"] == b"arm64:libpython3.14.so"
    assert native["lib/arm64-v8a/libssl_python_x86_64.so"] == b"x86_64:libssl_python.so"


def test_missing_pin_is_refused():
    from arcane_patcher import kit

    assert offline.cpython_pin(kit.runtime(), "x86_64")["url"].endswith("x86_64-linux-android.tar.gz")
    with pytest.raises(SystemExit, match="no CPython for the x86_64 server; get the current patch file"):
        offline.cpython_pin({"cpython_android": {"arm64": {"url": "u", "sha256": "s"}}}, "x86_64")


def _prefix(root: Path, triple: str, extra: dict[str, bytes] | None = None) -> Path:
    """A stand-in CPython for Android prefix: shared pure Python, extension modules named for `triple`."""
    lib = root / "lib" / "python3.14"
    files = {"os.py": b"shared", "json/__init__.py": b"shared json",
             f"lib-dynload/_ssl.cpython-314-{triple}.so": triple.encode(),
             f"_sysconfigdata__android_{triple}.py": triple.encode(),
             "build-details.json": triple.encode(), f"config-3.14-{triple}/Makefile": b"x", **(extra or {})}
    for name, data in files.items():
        (lib / name).parent.mkdir(parents=True, exist_ok=True)
        (lib / name).write_bytes(data)
    return root


def _payload(tmp_path, monkeypatch, patch_file, prefixes):
    import io

    real_kit = patch_file.kit

    def kit_file(name):
        if name.endswith("site-native.zip"):
            abi = name.split("/")[0]
            triple = {"arm64": "aarch64-linux-android", "x86_64": "x86_64-linux-android"}[abi]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("lz4/__init__.py", "shared lz4")
                z.writestr(f"lz4/_version.cpython-314-{triple}.so", abi)
            return buf.getvalue()
        return real_kit(name)

    monkeypatch.setattr(patch_file, "kit", kit_file)
    monkeypatch.setattr(offline, "compile_tree", lambda *a, **k: None)
    server = tmp_path / "server"
    for d in offline.SERVER_TREE:
        (server / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(patch_file.info, "runtime", {**patch_file.runtime, "wheels": []})
    payload = offline.build_payload(prefixes, tmp_path / "w", "com.example.game.offline", server, patch_file)
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_payload_is_shared_with_every_abis_native_modules(tmp_path, monkeypatch, patch_file):
    """One Python home for every runtime: the pure Python once, each ABI's extension modules and lz4 / protobuf next
    to each other (their names carry the ABI, so each interpreter imports only its own)."""
    prefixes = {"arm64": _prefix(tmp_path / "a", "aarch64-linux-android"),
                "x86_64": _prefix(tmp_path / "x", "x86_64-linux-android")}
    files = _payload(tmp_path, monkeypatch, patch_file, prefixes)
    lib = "python/lib/python3.14/"
    assert files[lib + "os.py"] == b"shared" and files[lib + "site-packages/lz4/__init__.py"] == b"shared lz4"
    for abi, triple in (("arm64", "aarch64-linux-android"), ("x86_64", "x86_64-linux-android")):
        assert files[f"{lib}lib-dynload/_ssl.cpython-314-{triple}.so"] == triple.encode()
        assert files[f"{lib}_sysconfigdata__android_{triple}.py"] == triple.encode()
        assert files[f"{lib}site-packages/lz4/_version.cpython-314-{triple}.so"] == abi.encode()
    assert not any("build-details" in n or "/config-3.14" in n for n in files)


def test_payload_refuses_runtimes_whose_shared_files_differ(tmp_path, monkeypatch, patch_file):
    prefixes = {"arm64": _prefix(tmp_path / "a", "aarch64-linux-android"),
                "x86_64": _prefix(tmp_path / "x", "x86_64-linux-android", {"os.py": b"other"})}
    with pytest.raises(SystemExit, match="os.py differs between the server ABIs' runtimes"):
        _payload(tmp_path, monkeypatch, patch_file, prefixes)
