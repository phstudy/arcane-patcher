"""The per-platform download: the program starts after unzipping (executable bit, intact .app) and the patch file
sits next to it."""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("bundle", Path(__file__).resolve().parents[1] / "packaging" / "bundle.py")
assert _spec is not None and _spec.loader is not None
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)


def _inputs(tmp: Path, program_name: str) -> tuple[Path, Path, Path]:
    program, patch, notices = tmp / program_name, tmp / "arcane-patch-9.9.9.zip", tmp / "notices.txt"
    for f in (program, patch, notices):
        f.write_bytes(f.name.encode())
    return program, patch, notices


def test_zipfile_route_keeps_the_executable_bit_and_the_patch_file(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)  # the Windows / Linux route, also on a Mac
    program, patch, notices = _inputs(tmp_path, "arcane-patcher")
    folder = bundle.stage(program, "linux-x64", patch, notices, tmp_path / "stage")
    out = bundle.zip_folder(folder, tmp_path / "out.zip")
    with zipfile.ZipFile(out) as z:
        modes = {i.filename: stat.S_IMODE(i.external_attr >> 16) for i in z.infolist()}
    assert modes == {
        "arcane-patcher-linux-x64/arcane-patcher": 0o755,
        "arcane-patcher-linux-x64/arcane-patch-9.9.9.zip": 0o644,
        "arcane-patcher-linux-x64/THIRD_PARTY_NOTICES.txt": 0o644,
    }


def test_windows_download_keeps_the_exe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    program, patch, notices = _inputs(tmp_path, "arcane-patcher.exe")
    out = bundle.zip_folder(bundle.stage(program, "windows-x64", patch, notices, tmp_path / "s"), tmp_path / "o.zip")
    with zipfile.ZipFile(out) as z:
        assert "arcane-patcher-windows-x64/arcane-patcher.exe" in z.namelist()


@pytest.mark.skipif(not shutil.which("ditto"), reason="macOS only")
def test_macos_app_keeps_its_symlinks(tmp_path):
    app = tmp_path / "arcane-patcher.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "arcane-patcher").write_bytes(b"x")
    os.symlink("MacOS/arcane-patcher", app / "Contents" / "link")
    _, patch, notices = _inputs(tmp_path, "unused")
    out = bundle.zip_folder(bundle.stage(app, "macos-arm64", patch, notices, tmp_path / "s"), tmp_path / "o.zip")
    dest = tmp_path / "x"
    dest.mkdir()
    import subprocess
    subprocess.run(["ditto", "-x", "-k", str(out), str(dest)], check=True)
    root = dest / "arcane-patcher-macos-arm64"
    assert (root / "arcane-patcher.app" / "Contents" / "link").is_symlink()
    assert (root / "arcane-patch-9.9.9.zip").is_file()
