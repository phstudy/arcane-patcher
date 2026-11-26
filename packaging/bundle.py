"""The download for one platform: arcane-patcher-<platform>.zip, one folder holding the program, the release's patch
file next to it (where the program looks for it) and the program's third-party notices.

The program is the one-file executable (Windows, Linux; its executable bit is kept: a bare file downloaded by a
browser loses it) or, on macOS, the arcane-patcher.app bundle, zipped with ditto so its symlinks and signature
survive.

    python packaging/bundle.py --program dist/arcane-patcher --platform linux-x64 --patch dist/arcane-patch-0.3.3.zip \\
        --notices build/THIRD_PARTY_NOTICES.txt --out dist/arcane-patcher-linux-x64.zip
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

EXECUTABLE = 0o755
REGULAR = 0o644
PROGRAMS = {"arcane-patcher", "arcane-patcher.exe"}


def stage(program: Path, platform: str, patch: Path, notices: Path, dest: Path) -> Path:
    """dest/arcane-patcher-<platform>/ with the program, the patch file and the notices."""
    folder = dest / f"arcane-patcher-{platform}"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    if program.is_dir():  # the macOS .app
        shutil.copytree(program, folder / program.name, symlinks=True)
    else:
        name = "arcane-patcher" + (".exe" if program.suffix == ".exe" else "")
        shutil.copyfile(program, folder / name)
        os.chmod(folder / name, EXECUTABLE)
    shutil.copyfile(patch, folder / patch.name)
    shutil.copyfile(notices, folder / "THIRD_PARTY_NOTICES.txt")
    return folder


def zip_folder(folder: Path, out: Path) -> Path:
    """`out` holding `folder` as its one top folder: ditto on macOS (symlinks, extended attributes, a stapled
    ticket), else with each file's unix mode."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    if shutil.which("ditto"):
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(folder), str(out)], check=True)
        return out
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                info = zipfile.ZipInfo.from_file(path, path.relative_to(folder.parent).as_posix())
                # the program by name: Windows reports every file as executable
                mode = EXECUTABLE if path.name in PROGRAMS else REGULAR
                info.external_attr = (stat.S_IFREG | mode) << 16  # read by unzip and the macOS Archive Utility
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, path.read_bytes())
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Write the download zip for one platform.")
    ap.add_argument("--program", type=Path, required=True, help="the executable, or the macOS .app")
    ap.add_argument("--platform", required=True)
    ap.add_argument("--patch", type=Path, required=True)
    ap.add_argument("--notices", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stage-only", action="store_true", help="only lay out the folder (to staple before zipping)")
    a = ap.parse_args()
    folder = stage(a.program, a.platform, a.patch, a.notices, a.out.parent / "stage")
    print(folder if a.stage_only else zip_folder(folder, a.out))


if __name__ == "__main__":
    main()
