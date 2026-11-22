"""Build the kit (kit.py) for maintainers and CI: `python -m arcane_patcher.kitbuild [--out DIR]`.

Needs the Android SDK (build-tools with d8, a platform android.jar), the Android NDK sdktools.NDK_VERSION, a JDK 17+
and network on the first run (the pinned downloads: CPython for Android for its headers and libpython, the lz4 and
protobuf sources, ZXing core). Writes the kit files and kit.json into src/arcane_patcher/kit/ by default.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

from arcane_patcher import kit, offline, sdktools

PY_VER = kit.runtime()["python"]
SO_SUFFIX = f".cpython-{PY_VER.replace('.', '')}-aarch64-linux-android.so"


def run(*cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


_PREFIX_MAP: list[str] = []  # -ffile-prefix-map for the current build's temporary folder (set by build)


def clang(*args) -> None:
    # the temporary folder's path would end up in assertion messages: map it to a fixed name (reproducible kit, no
    # local paths in a release)
    run(sdktools.ndk_tool(f"aarch64-linux-android{kit.OFFLINE_MIN_SDK}-clang"), "-O2", "-Wl,-z,max-page-size=16384",
        *_PREFIX_MAP, *args)


def build_dex(tmp: Path, java_src: Path, min_sdk: int, jars: list[Path] = ()) -> bytes:
    """classes.dex of every .java under `java_src` and android/common/java (the shared content import and download
    serving), and of the libraries `jars` (compiled against, and dexed in)."""
    work = tmp / f"dex-{java_src.parent.name}"
    classes = work / "classes"
    sources = sorted([*java_src.rglob("*.java"), *(kit.ANDROID_DIR / "common" / "java").rglob("*.java")])
    jar = sdktools.android_jar()
    run(sdktools.javac(), "-nowarn", "--release", "11", "-cp", os.pathsep.join(map(str, [jar, *jars])), "-d", classes,
        *sources)
    dex_out = work / "dex"
    dex_out.mkdir(parents=True)
    bt = sdktools.find_build_tools()
    run(sdktools.tool(bt, "d8"), "--release", "--min-api", min_sdk, "--lib", jar, "--output", dex_out,
        *sorted(classes.rglob("*.class")), *jars)
    if (dex_out / "classes2.dex").exists():
        raise sdktools.ToolError("build_dex: more than one dex file")
    return (dex_out / "classes.dex").read_bytes()


def build_launcher(prefix: Path, tmp: Path) -> bytes:
    out = tmp / "libarcanepython.so"
    clang("-fPIE", "-pie", "-I", prefix / "include" / f"python{PY_VER}",
          kit.ANDROID_DIR / "offline" / "native" / "arcanepython.c", "-L", prefix / "lib", f"-lpython{PY_VER}",
          # find libpython next to itself: the ARM translation of x86_64 devices/emulators ignores LD_LIBRARY_PATH
          "-Wl,-rpath,$ORIGIN", "-o", out)
    return out.read_bytes()


def build_lz4(prefix: Path, tmp: Path, site: Path) -> None:
    sd = offline.fetch(*kit.LZ4_SDIST)
    with tarfile.open(sd) as t:
        t.extractall(tmp / "lz4", filter="data")
    src = next((tmp / "lz4").iterdir())
    shutil.copytree(src / "lz4", site / "lz4", ignore=shutil.ignore_patterns("*.c", "frame", "stream"))
    inc = ["-I", src / "lz4libs", "-I", prefix / "include" / f"python{PY_VER}"]
    link = ["-shared", "-fPIC", "-L", prefix / "lib", f"-lpython{PY_VER}"]
    clang(*inc, src / "lz4/_version.c", src / "lz4libs/lz4.c", *link, "-o", site / "lz4" / f"_version{SO_SUFFIX}")
    clang(*inc, src / "lz4/block/_block.c", src / "lz4libs/lz4.c", src / "lz4libs/lz4hc.c", *link,
          "-o", site / "lz4" / "block" / f"_block{SO_SUFFIX}")


def build_protobuf(prefix: Path, tmp: Path, site: Path) -> None:
    """protobuf with the upb backend: the sdist's Python package plus google/_upb/_message (the sources and flags of
    its setup.py: every .c except the fasttable decoder)."""
    sd = offline.fetch(*kit.PROTOBUF_SDIST)
    with tarfile.open(sd) as t:
        t.extractall(tmp / "protobuf", filter="data")
    src = next((tmp / "protobuf").iterdir())
    shutil.copytree(src / "google", site / "google", ignore=shutil.ignore_patterns("*.c", "*.h"), dirs_exist_ok=True)
    sources = [f for pattern in ("google/protobuf/*.c", "python/*.c", "upb/**/*.c", "utf8_range/*.c")
               for f in sorted(src.glob(pattern)) if "decode_fast" not in f.name]
    (site / "google" / "_upb").mkdir(exist_ok=True)
    clang("-fvisibility=hidden", "-std=gnu11", "-I", src, "-I", src / "utf8_range",
          "-I", prefix / "include" / f"python{PY_VER}", *sources,
          "-shared", "-fPIC", "-L", prefix / "lib", f"-lpython{PY_VER}", "-o", site / "google" / "_upb" / f"_message{SO_SUFFIX}")


def _sdist_text(sdist: tuple[str, str], member_suffix: str) -> str:
    with tarfile.open(offline.fetch(*sdist)) as t:
        [m] = [m for m in t.getmembers() if m.name.endswith(member_suffix)]
        return t.extractfile(m).read().decode()


def notices() -> bytes:
    """THIRD_PARTY_NOTICES for what the kit puts into a built APK: ZXing core (multiplayer.dex), python-lz4 with the
    LZ4 library and protobuf with utf8_range (site-native.zip of the offline runtime). The license texts come from the
    pinned sources, else from android/licenses/ (texts the sources point to without carrying them)."""
    lic = kit.ANDROID_DIR / "licenses"
    lz4_c = _sdist_text(kit.LZ4_SDIST, "lz4libs/lz4.c")
    lz4_header = lz4_c[:lz4_c.index("*/") + 2]
    parts = [
        ("ZXing core 3.5.4 (in the multiplayer setup screen's code)", "Apache License 2.0",
         "Copyright the ZXing authors\n\n" + (lic / "Apache-2.0.txt").read_text()),
        ("python-lz4 4.4.5 (offline runtime)", "BSD 3-Clause License", _sdist_text(kit.LZ4_SDIST, "-4.4.5/LICENSE")),
        ("LZ4 library (offline runtime, inside python-lz4)", "BSD 2-Clause License", lz4_header),
        ("protobuf 7.36.2 with its upb extension (offline runtime)", "BSD 3-Clause License",
         _sdist_text(kit.PROTOBUF_SDIST, "-7.36.2/LICENSE")),
        ("utf8_range (offline runtime, inside protobuf)", "MIT License", (lic / "utf8_range-MIT.txt").read_text()),
    ]
    out = ["Third-party software in the parts arcane-patcher adds to a built APK.",
           "The offline variant's Python runtime (assets/arcane_offline/payload.zip) also carries CPython's LICENSE.txt "
           "(python/lib/python3.*/LICENSE.txt) and each Python package's license in its .dist-info folder.", ""]
    for name, license_name, text in parts:
        out += ["=" * 100, f"{name}: {license_name}", "=" * 100, text.strip(), ""]
    return ("\n".join(out) + "\n").encode()


def zip_tree(root: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(root.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                z.writestr(zipfile.ZipInfo(f.relative_to(root).as_posix(), date_time=(2024, 2, 27, 0, 0, 0)),
                           f.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def build(out: Path = kit.KIT_DIR) -> Path:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        _PREFIX_MAP[:] = [f"-ffile-prefix-map={tmp}=/kit", f"-ffile-prefix-map={tmp.resolve()}=/kit"]
        prefix = offline.python_prefix(tmp, kit.runtime())
        site = tmp / "site"
        site.mkdir()
        build_lz4(prefix, tmp, site)
        build_protobuf(prefix, tmp, site)
        files = {
            "offline.dex": build_dex(tmp, kit.ANDROID_DIR / "offline" / "java", kit.OFFLINE_MIN_SDK),
            "multiplayer.dex": build_dex(tmp, kit.ANDROID_DIR / "multiplayer" / "java", kit.MULTIPLAYER_MIN_SDK,
                                         [offline.fetch(*kit.ZXING_CORE)]),
            "libarcanepython.so": build_launcher(prefix, tmp),
            "site-native.zip": zip_tree(site),
            "NOTICES.txt": notices(),
        }
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    kit.write_manifest(out)
    if problems := kit.problems(out):
        raise sdktools.ToolError("the new kit does not check out: " + "; ".join(problems))
    for name in kit.FILES:
        print(f"kit: {name} ({(out / name).stat().st_size // 1024} KB)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m arcane_patcher.kitbuild", description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=kit.KIT_DIR, help=f"where to write the kit (default {kit.KIT_DIR})")
    build(ap.parse_args(argv).out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
