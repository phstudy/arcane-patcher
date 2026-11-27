"""The kit: the parts of every build that do not depend on the user's APK, built once by a maintainer or CI
(`python -m arcane_patcher.kitbuild`, which needs the Android SDK, NDK and a JDK) and shipped in the patch file
(release.py packs it as kit/), so a user's build needs none of those tools:

  offline.dex          the offline variant's setup screen, server provider and launcher (android/offline/java)
  multiplayer.dex      the multiplayer variant's setup screen, QR scanner and loopback proxy (android/multiplayer/java,
                       with ZXing core dexed in)
  <abi>/libarcanepython.so   the offline variant's interpreter executable (android/offline/native), per server ABI
  <abi>/site-native.zip      lz4 and protobuf (with its upb C extension) built for Android on that ABI: the offline
                             payload's site-packages entries that are not pure Python (their file names carry the
                             ABI, so every ABI's modules share one site-packages)
  NOTICES.txt          the licenses of the third-party code above (written into every built APK)

kit.json records each file's sha256 and `inputs`, a hash of everything the kit is built from (the Java and native
sources under android/, the pinned downloads, the NDK version, the minimum SDKs). A kit that does not match the
sources it ships with is refused by release.py (`problems`), so a stale kit cannot slip into a patch file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent / "kit"
ANDROID_DIR = Path(__file__).resolve().parent / "android"
KIT_FORMAT = 2


@dataclass(frozen=True)
class ServerAbi:
    """A CPU the offline variant's built-in server runs on. Every offline APK carries a server runtime for each, and
    the launcher (OfflineServer) picks the one for the device's CPU. The game itself is always the arm64 build: an
    x86_64 PC emulator runs it through its ARM translation, but that covers only the game's own process, not the
    server process it starts, so such an emulator needs the server built for x86_64."""

    name: str
    triple: str  # the NDK compiler's target and CPython's extension-module tag
    device_abis: tuple[str, ...]  # the Android ABIs (Build.SUPPORTED_ABIS) this runtime runs on natively
    lib_suffix: str  # its files in the APK's lib folder: libpython3.14<suffix>.so, ...

    def lib(self, name: str) -> str:
        """The APK file name of this runtime's library `name` (lib*.so: the package manager extracts only those)."""
        return name.removesuffix(".so") + self.lib_suffix + ".so"


# The one list of server ABIs: the kit (kitbuild), the runtime pins (patchdata/runtime.json "cpython_android"), the
# APK's runtimes (offline.build_runtime) and the launcher's table of them (offline.RUNTIMES_ASSET) all follow it; a
# test checks the pins and the kit cover exactly these. The first is the one used on a device whose ABIs none lists.
SERVER_ABIS = {a.name: a for a in (
    ServerAbi("arm64", "aarch64-linux-android", ("arm64-v8a", "armeabi-v7a", "armeabi"), ""),
    ServerAbi("x86_64", "x86_64-linux-android", ("x86_64", "x86"), "_x86_64"),
)}
SERVER_FILES = ("libarcanepython.so", "site-native.zip")  # built once per server ABI


def server_file(abi: str, name: str) -> str:
    """The kit file `name` (one of SERVER_FILES) for server ABI `abi`."""
    return f"{abi}/{name}"


FILES = ("offline.dex", "multiplayer.dex", "NOTICES.txt",
         *(server_file(abi, name) for abi in SERVER_ABIS for name in SERVER_FILES))


class KitError(SystemExit):
    """The kit is missing or does not match the sources, reported as a clear message."""


# The kit's pinned downloads (url, sha256); CPython for Android (one per server ABI) comes from the patch file's runtime
# pins (patchdata/runtime.json), so the kit's headers and libpython match the interpreter the offline variant ships.
LZ4_SDIST = ("https://files.pythonhosted.org/packages/57/51/f1b86d93029f418033dddf9b9f79c8d2641e7454080478ee2aab5123173e/"
             "lz4-4.4.5.tar.gz", "5f0b9e53c1e82e88c10d7c180069363980136b9d7a8306c4dca4f760d60c39f0")
# protobuf with its upb C extension (google._upb._message): the pure-Python fallback decodes ~50x slower, which made
# the phone's server start take 90 s and slowed every RPC.
PROTOBUF_SDIST = ("https://files.pythonhosted.org/packages/d9/89/5b8517baa72f84a67b8a307ba953c91057af618bf40bf676f3c03551f8f0/"
                  "protobuf-7.36.2.tar.gz", "497d0463ff3316681da6c0b9e8d06cb465d61abce00b613ab42226175644d1bb")
# "Scan QR code" decodes with ZXing core (Apache-2.0, plain Java: no Google Play services needed), dexed into
# multiplayer.dex
ZXING_CORE = ("https://repo1.maven.org/maven2/com/google/zxing/core/3.5.4/core-3.5.4.jar",
              "71de5d89341b5fcf5dd89da7f44e84d825d0e084cdf3ec77c9abe26b0f0ceb13")
OFFLINE_MIN_SDK = 24  # CPython's Android minimum
MULTIPLAYER_MIN_SDK = 24  # the multiplayer variant's minimum (Android 7: its network security config)
RUNTIME_JSON = Path(__file__).resolve().parents[2] / "patchdata" / "runtime.json"


def runtime() -> dict:
    """The offline runtime's pins (patchdata/runtime.json, which release.py packs into the patch file)."""
    return json.loads(RUNTIME_JSON.read_text())


def pins() -> dict[str, object]:
    """The non-source inputs of the kit (changing one makes the kit stale)."""
    from arcane_patcher import offline, sdktools

    rt = runtime()
    return {"format": KIT_FORMAT, "zxing": ZXING_CORE, "multiplayer_min_sdk": MULTIPLAYER_MIN_SDK,
            "python_android": rt["cpython_android"], "python": rt["python"], "lz4": LZ4_SDIST,
            "protobuf": PROTOBUF_SDIST, "offline_min_sdk": OFFLINE_MIN_SDK, "ndk": sdktools.NDK_VERSION,
            # the interpreter executables load these by name (kitbuild.build_launcher)
            "runtime_libs": {a.name: [a.lib(so) for so in offline.native_libs(rt["python"])] for a in SERVER_ABIS.values()}}


def inputs_hash(android_dir: Path = ANDROID_DIR) -> str:
    h = hashlib.sha256(json.dumps(pins(), sort_keys=True).encode())
    for f in sorted(p for p in android_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts
                    and not p.name.startswith(".")):
        h.update(f.relative_to(android_dir).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def problems(kit_dir: Path = KIT_DIR) -> list[str]:
    """What is wrong with the kit (empty when it is complete, intact and built from the current sources)."""
    manifest = kit_dir / "kit.json"
    if not manifest.is_file():
        return [f"the kit is missing ({kit_dir})"]
    try:
        info = json.loads(manifest.read_text())
    except ValueError as e:
        return [f"{manifest}: unreadable ({e})"]
    out = []
    if info.get("format") != KIT_FORMAT or info.get("inputs") != inputs_hash():
        out.append("the kit was built from other sources than these")
    for name in FILES:
        f = kit_dir / name
        if not f.is_file():
            out.append(f"kit file missing: {name}")
        elif hashlib.sha256(f.read_bytes()).hexdigest() != info.get("files", {}).get(name):
            out.append(f"kit file damaged: {name}")
    return out


def read(name: str, kit_dir: Path = KIT_DIR) -> bytes:
    if name not in FILES:
        raise KeyError(name)
    return (kit_dir / name).read_bytes()


def write_manifest(kit_dir: Path, extra: dict[str, object] | None = None) -> None:
    info = {"format": KIT_FORMAT, "inputs": inputs_hash(),
            "files": {n: hashlib.sha256((kit_dir / n).read_bytes()).hexdigest() for n in FILES}, **(extra or {})}
    (kit_dir / "kit.json").write_text(json.dumps(info, indent=1, sort_keys=True) + "\n")
