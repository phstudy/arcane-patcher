"""THIRD_PARTY_NOTICES for the packaged executable: the licenses of everything PyInstaller puts into it.

    python packaging/notices.py --out build/THIRD_PARTY_NOTICES.txt     (in the environment that runs PyInstaller)

Collected from that environment: CPython's LICENSE.txt, Tcl and Tk (the window; their license.terms, kept in
packaging/licenses/ because not every Python build ships them), and the license files of every installed
distribution the executable bundles (arcane-patcher's dependencies: truststore; cryptography with the OpenSSL and Rust code it
links, cffi, pycparser) plus PyInstaller's bootloader license (GPL with its exception for the bundled program). The
same environment (uv sync --locked) gives the same file.
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLED = ("cryptography", "cffi", "pycparser", "truststore")  # the runtime dependencies of arcane-patcher (uv.lock)
NOTES = {
    "cryptography": "cryptography's compiled extension statically links OpenSSL (Apache License 2.0) and Rust crates "
                    "(MIT and / or Apache License 2.0, as listed in cryptography's Cargo.lock).",
}


def _section(title: str, text: str) -> list[str]:
    return ["=" * 100, title, "=" * 100, text.strip(), ""]


def _python_license() -> str:
    for p in (Path(sys.base_prefix) / "LICENSE.txt",
              Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt",
              Path(sys.base_prefix) / "Lib" / "LICENSE.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    import sysconfig

    p = Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"
    if p.is_file():
        return p.read_text(encoding="utf-8", errors="replace")
    raise SystemExit("CPython's LICENSE.txt not found in this Python")


def _dist_licenses(name: str) -> str:
    dist = metadata.distribution(name)
    files = [f for f in dist.files or () if "licen" in f.name.lower() or "copying" in f.name.lower()
             or "/licenses/" in str(f)]
    if not files:
        raise SystemExit(f"{name}: no license file in its installed metadata")
    texts = []
    for f in sorted(files, key=str):
        p = Path(dist.locate_file(f))
        if p.is_file():
            texts.append(f"--- {f}\n{p.read_text(encoding='utf-8', errors='replace').strip()}")
    return "\n\n".join(texts)


def notices() -> str:
    out = ["Third-party software in this arcane-patcher executable (arcane-patcher itself: GNU GPL version 3, see "
           "LICENSE).", ""]
    v = sys.version.split()[0]
    out += _section(f"CPython {v}: Python Software Foundation License", _python_license())
    out += _section("Tcl: Tcl/Tk license (BSD-style)", (HERE / "licenses" / "tcl-license.terms").read_text())
    out += _section("Tk: Tcl/Tk license (BSD-style)", (HERE / "licenses" / "tk-license.terms").read_text())
    for name in BUNDLED:
        dist = metadata.distribution(name)
        lic = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
        note = NOTES.get(name, "")
        out += _section(f"{name} {dist.version}: {lic}".rstrip(": "), (note + "\n\n" if note else "") + _dist_licenses(name))
    pi = metadata.distribution("pyinstaller")
    out += _section(f"PyInstaller {pi.version} bootloader: GPL 2.0 or later with the bootloader exception "
                    "(the bundled program may be under any license)", _dist_licenses("pyinstaller"))
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(notices(), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
