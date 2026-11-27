"""The offline variant's embedded server: the Python runtime, the server release and the Java launcher.

The offline variant is the game APK with the game server inside, run on the phone at 127.0.0.1. patch.py calls
`build_runtime()` for what it injects:
  base:  classesN.dex (the patch file's kit: SetupActivity launcher, ServerProvider, OfflineServer),
         assets/arcane_offline/payload.zip (CPython stdlib + pure-Python deps + lz4 + protobuf + the server release
         as its project root: app/src/arcane_server, app/content, app/proto), assets/arcane_offline/build_id,
         (assets/arcane_content/content.json, the client release it serves: patch.py, every variant)
         assets/arcane_offline/runtimes (OfflineServer's table of the server runtimes, RUNTIMES_ASSET)
  arm64: lib/arm64-v8a/: one server runtime per kit.SERVER_ABIS entry, each under its own names (ServerAbi.lib):
         libarcanepython.so (interpreter executable), libpython3.14.so, lib*_python.so for arm64 (phones),
         libarcanepython_x86_64.so, libpython3.14_x86_64.so, ... for x86_64 (PC emulators; see GAME_ABI_LIB_DIR)
The server inside is the released server, unchanged (`python -m arcane_server`); OfflineServer only sets its settings
(ARCANE_HOST, ARCANE_PORT, ARCANE_DATA_DIR, ARCANE_CLIENT, ARCANE_DB, ARCANE_GAME_SPEED, ARCANE_CLUB_WAR_CLUBS,
ARCANE_INBOX_REWARD_MAIL_ONLY, ARCANE_ACCOUNT_PICKER, ARCANE_CLIENT_PACKAGE, ...). It runs its content checks itself
on its first start with the imported game data.

Everything comes from the patch file (patchfile.py): the server release (server/<name>.zip; for development
`--emulator-zip PATH` or `--server-dir PATH` replace it), the kit (the launcher's dex, the interpreter executable, lz4
and protobuf built for Android, per server ABI), and the runtime's pinned downloads (its "runtime": CPython for Android
per server ABI and the server's Python packages, each checked against its sha256 and cached in the user's cache folder,
build/cache/ in a checkout, or ARCANE_CACHE). The payload's Python files are precompiled with the runtime's CPython version: the running
interpreter when it is that version (the release downloads are), else `uv python find`. emulator.lock and read_lock()
are for making a release (release.py --pinned-server).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import ssl
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import truststore

from arcane_patcher import kit
from arcane_patcher.patch import PatchError

FROZEN = bool(getattr(sys, "frozen", False))  # running as the packaged download (PyInstaller)
# the repository checkout; in the packaged download, the folder its data files are unpacked to
ROOT = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parents[2]
LOCK = ROOT / "emulator.lock"
OFFLINE_SRC = Path(__file__).resolve().parent / "android" / "offline"
SERVER_PACKAGE = "arcane_server"
SERVER_TREE = (f"src/{SERVER_PACKAGE}", "content", "proto")  # a release zip / checkout: its PROJECT_ROOT layout


def cache_dir() -> Path:
    """Downloads: ARCANE_CACHE, else build/cache in a checkout, else the user's cache folder (packaged download)."""
    if os.environ.get("ARCANE_CACHE"):
        return Path(os.environ["ARCANE_CACHE"])
    if not FROZEN:
        return ROOT / "build" / "cache"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "arcane-patcher" / "cache"
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/arcane-patcher"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "arcane-patcher"


PY_VER = "3.14"  # the CPython the patcher precompiles for when the patch file names none (its "runtime" "python")


def native_libs(py_ver: str) -> list[str]:
    return [f"libpython{py_ver}.so", "libcrypto_python.so", "libssl_python.so", "libsqlite3_python.so"]

# stdlib parts the server never imports
STDLIB_DROP = {"test", "idlelib", "tkinter", "turtledemo", "ensurepip", "pydoc_data", "_pyrepl", "__phello__",
               "venv", "lib2to3", "turtle.py", "doctest.py", "pydoc.py",
               "build-details.json"}  # (CPython's build description: differs per ABI, read by nothing at run time)
DYNLOAD_DROP = re.compile(r"^(_test|_xxtest|xx|_ctypes_test|_remote_debugging)")


def device_root(package: str) -> str:
    """Where OfflineServer unpacks the payload (source paths baked into the precompiled .pyc files)."""
    return f"/data/user/0/{package}/files/arcane_offline/root"


def run(*cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def cached(url: str, sha256: str, name: str | None = None) -> Path | None:
    """The cached copy of `url` if it is there with this sha256 (no download)."""
    dst = cache_dir() / (name or url.rsplit("/", 1)[1])
    return dst if dst.is_file() and sha256_file(dst) == sha256 else None


def tls_context() -> ssl.SSLContext:
    """HTTPS checked against the operating system's own trusted certificates (Keychain, Windows store, the
    distribution's bundle): the Python the download packs carries none of its own, so a fresh computer refused
    every certificate. The downloads are checked against their sha256 as well."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def fetch(url: str, sha256: str, name: str | None = None) -> Path:
    """The file at `url` (http(s) or file:), from the cache when it is there with this sha256, else downloaded;
    PatchError (and nothing cached) when the download's sha256 differs."""
    if hit := cached(url, sha256, name):
        return hit
    dst = cache_dir() / (name or url.rsplit("/", 1)[1])
    print("downloading", url)
    with urllib.request.urlopen(url, context=tls_context()) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise PatchError(f"sha256 mismatch for {url}: got {got}, expected {sha256}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(dst)
    return dst


# --- the server release ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmulatorLock:
    version: str
    url: str
    sha256: str


def read_lock(path: Path | None = None) -> EmulatorLock:
    path = path or LOCK
    try:
        d = json.loads(path.read_text())
        return EmulatorLock(str(d.get("version", "")), str(d.get("url", "")), str(d.get("sha256", "")))
    except (OSError, ValueError, AttributeError) as e:
        raise PatchError(f"{path}: cannot read the server release pin ({e})") from None


def _check_layout(root: Path) -> None:
    """`root` is a server project root: src/arcane_server/ (with __main__.py), content/, proto/."""
    missing = [str(p) for p in (root / "src" / SERVER_PACKAGE / "__main__.py", root / "content", root / "proto")
               if not p.exists()]
    if missing:
        raise PatchError(f"{root} is not a server release (src/{SERVER_PACKAGE}/, content/, proto/): "
                         f"missing {', '.join(missing)}")


def server_source(tmp: Path, emulator_zip: Path | None = None, server_dir: Path | None = None, pf=None) -> Path:
    """A server project root (src/arcane_server/, content/, proto/) with the server to embed: --server-dir,
    --emulator-zip, or the release in the patch file `pf`."""
    if server_dir is not None:
        src_root = Path(server_dir)
        if not src_root.is_dir():
            raise PatchError(f"--server-dir {src_root}: not a directory")
        print(f"server: {src_root} (--server-dir, not the patch file's)")
    else:
        if emulator_zip is not None:
            zpath = Path(emulator_zip)
            if not zpath.is_file():
                raise PatchError(f"--emulator-zip {zpath}: not found")
            print(f"server: {zpath} (--emulator-zip, sha256 {sha256_file(zpath)}, not the patch file's)")
        else:
            if pf is None or not pf.server:
                raise PatchError("the patch file holds no server release; build the offline variant with "
                                 "--emulator-zip PATH (a server release zip) or --server-dir PATH (a server checkout)")
            zpath = tmp / "server.zip"
            tmp.mkdir(parents=True, exist_ok=True)
            zpath.write_bytes(pf.read(pf.server))
            print(f"server: {Path(pf.server).name} (patch file {pf.version})")
        src_root = tmp / "server_zip"
        try:
            with zipfile.ZipFile(zpath) as z:
                bad = [n for n in z.namelist() if n.startswith("/") or ".." in Path(n).parts]
                if bad:
                    raise PatchError(f"{zpath}: unsafe paths in the zip: {bad[:3]}")
                z.extractall(src_root)
        except zipfile.BadZipFile:
            raise PatchError(f"{zpath}: not a zip file") from None
    _check_layout(src_root)
    out = tmp / "server"
    ign = shutil.ignore_patterns("__pycache__", "*.pyc", ".*")  # .*: hidden files (.DS_Store, stamps)
    for d in SERVER_TREE:
        shutil.copytree(src_root / d, out / d, ignore=ign)
    return out


# --- Python runtime + payload --------------------------------------------------------------------------------


# Every server runtime (kit.SERVER_ABIS) ships in the game's ABI folder, whatever its own CPU: the APK holds arm64 code
# only, so the package manager picks arm64-v8a (translated on an x86_64 emulator) and extracts that folder's lib*.so
# files to the app's nativeLibraryDir, the only place an app may exec files from; it does not look at their CPU. Each
# runtime's files have their own names there (ServerAbi.lib), and OfflineServer execs the interpreter of the runtime
# for the device's CPU: on an x86_64 emulator the x86_64 one runs natively, where the ARM translation does not cover a
# process the app starts (the shell read an arm64 executable as a script: "syntax error").
GAME_ABI_LIB_DIR = "lib/arm64-v8a"
# OfflineServer's table of the runtimes, one line each in SERVER_ABIS order (the first is its default):
#   <name> <interpreter file name> <Android ABIs it runs on, comma-separated>
RUNTIMES_ASSET = "assets/arcane_offline/runtimes"
LAUNCHER = "libarcanepython.so"


def runtimes_table() -> bytes:
    return "".join(f"{a.name} {a.lib(LAUNCHER)} {','.join(a.device_abis)}\n" for a in kit.SERVER_ABIS.values()).encode()


def cpython_pin(runtime: dict, abi: str) -> dict:
    """The runtime's pinned CPython for Android download for server ABI `abi`."""
    pin = runtime.get("cpython_android", {}).get(abi)
    if not pin:
        raise PatchError(f"the patch file has no CPython for the {abi} server; get the current patch file")
    return pin


def python_prefix(tmp: Path, runtime: dict, abi: str) -> Path:
    """CPython for Android on server ABI `abi` (headers, stdlib, libpython) from the runtime's pinned download."""
    pin = cpython_pin(runtime, abi)
    tar = fetch(pin["url"], pin["sha256"])
    with tarfile.open(tar) as t:
        t.extractall(tmp / f"cpython-{abi}", filter="data")
    return tmp / f"cpython-{abi}" / "prefix"


def merge_into(dst: Path, src: Path, ignore=None) -> None:
    """Copy the tree `src` into `dst`, where another server ABI's tree may already be: a file both have must be the
    same (pure Python is shared; extension modules and sysconfig data carry their ABI in their names)."""
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if ignore is not None and any(ignore(part) for part in rel.parts) or not f.is_file():
            continue
        out = dst / rel
        if out.exists():
            if out.read_bytes() != f.read_bytes():
                raise PatchError(f"{rel.as_posix()} differs between the server ABIs' runtimes")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, out)


def compile_tree(stage: Path, package: str, py_ver: str = PY_VER) -> None:
    """Precompile stage/python and stage/app/src for CPython `py_ver` (the device never writes bytecode), with the
    source paths the device sees; unchecked-hash: valid whatever the extracted mtimes."""
    dirs = [stage / "python", stage / "app" / "src"]
    if sys.version_info[:2] == tuple(int(x) for x in py_ver.split(".")):
        import compileall
        import py_compile

        for d in dirs:
            ok = compileall.compile_dir(d, quiet=1, stripdir=str(stage), prependdir=device_root(package),
                                        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, workers=1)
            if not ok:
                raise PatchError(f"precompiling {d.name} failed")
        return
    run(host_python(py_ver), "-m", "compileall", "-q", "-j0", "--invalidation-mode", "unchecked-hash",
        "-s", stage, "-p", device_root(package), *dirs)


def host_python(py_ver: str = PY_VER) -> str:
    try:
        return subprocess.run(["uv", "python", "find", py_ver], check=True, capture_output=True,
                              text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise PatchError(f"the offline variant needs CPython {py_ver} on this machine (uv python install {py_ver})") from None


def build_payload(prefixes: dict[str, Path], tmp: Path, package: str, server_src: Path, pf) -> bytes:
    """Python home (stripped stdlib + site-packages) and the server release under app/ (app/src/arcane_server,
    app/content, app/proto: the server's PROJECT_ROOT is app/). One Python home serves every server ABI
    (`prefixes`: each one's CPython for Android): the pure Python once, the extension modules of each (named for
    their ABI: _ssl.cpython-314-x86_64-linux-android.so, which only that ABI's interpreter imports)."""
    py_ver = pf.runtime.get("python", PY_VER)
    stage = tmp / "payload"
    lib = stage / "python" / "lib" / f"python{py_ver}"
    site = lib / "site-packages"
    site.mkdir(parents=True)
    for abi, prefix in prefixes.items():
        merge_into(lib, prefix / "lib" / f"python{py_ver}",
                   lambda n: n in STDLIB_DROP or n == "__pycache__" or n.startswith("config-") or DYNLOAD_DROP.match(n))
    for wheel in pf.runtime.get("wheels", []):
        with zipfile.ZipFile(fetch(wheel["url"], wheel["sha256"])) as z:
            z.extractall(site)
    for abi in prefixes:  # lz4, protobuf (upb) built for Android on each ABI
        native = tmp / f"site-native-{abi}"
        with zipfile.ZipFile(io.BytesIO(pf.kit(kit.server_file(abi, "site-native.zip")))) as z:
            z.extractall(native)
        merge_into(site, native)
    app = stage / "app"
    for d in SERVER_TREE:  # the server's project root: PYTHONPATH=app/src (OfflineServer)
        shutil.copytree(server_src / d, app / d)
    compile_tree(stage, package, py_ver)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f in sorted(stage.rglob("*")):
            if f.is_file():
                info = zipfile.ZipInfo(f.relative_to(stage).as_posix(), date_time=(2024, 2, 27, 0, 0, 0))
                z.writestr(info, f.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def build_runtime(tmp: Path, package: str, server_src: Path, pf) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """(entries for the base / single APK, the server runtimes' native files for GAME_ABI_LIB_DIR: the arm64 split of
    a bundle, else the single APK); the dex is named by the caller (next free index). `pf`: the patch file (kit,
    runtime pins). Every runtime of kit.SERVER_ABIS goes in, under its own file names; OfflineServer picks one."""
    prefixes = {abi: python_prefix(tmp, pf.runtime, abi) for abi in kit.SERVER_ABIS}
    payload = build_payload(prefixes, tmp, package, server_src, pf)
    base = {
        "classes.dex": pf.kit("offline.dex"),
        "assets/arcane_offline/payload.zip": payload,
        "assets/arcane_offline/build_id": hashlib.sha256(payload).hexdigest()[:16].encode(),
        RUNTIMES_ASSET: runtimes_table(),
        # the client release this build serves: the game data zip holds content/<client>/ (DataImport checks it)
    }
    native = {}
    for name, abi in kit.SERVER_ABIS.items():
        native[f"{GAME_ABI_LIB_DIR}/{abi.lib(LAUNCHER)}"] = pf.kit(kit.server_file(name, LAUNCHER))
        for so in native_libs(pf.runtime.get("python", PY_VER)):
            native[f"{GAME_ABI_LIB_DIR}/{abi.lib(so)}"] = (prefixes[name] / "lib" / so).read_bytes()
    print(f"embedded server runtimes ({', '.join(kit.SERVER_ABIS)}): payload {len(payload) >> 20} MB "
          f"(build {base['assets/arcane_offline/build_id'].decode()})")
    return base, native
