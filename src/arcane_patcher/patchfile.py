"""Patch files: everything a build applies, as one versioned, signed file (arcane-patch-<version>.zip).

The patcher program is a generic applier and stays the same between releases; each release ships a patch file:

  patch.json            format, version, min_patcher, the client builds it supports (game library build-id -> name),
                        the variants, the offline runtime's pinned downloads, the content set's hashes, the
                        server file, the source it was made from, the sha256 of every
                        other file, and an Ed25519 signature over all of it (TRUSTED_KEYS: the release keys)
  rules/<build-id>.json the byte edits for that client build: game library sets, dex edits (rules.py)
  manifest/<variant>.xml the components and permissions a variant adds to the game's manifest
  kit/                  the setup screens' dex files, the offline interpreter executable, native Python modules
  server/<name>.zip     the server release the offline variant embeds (optional)

A file whose signature is missing, made with an unknown key or wrong is "unofficial": it is refused unless the user
allows unofficial patch files (it adds code to their app). A file whose contents do not match patch.json is refused
always. Without --patch, the newest patch file next to the program or in the user's data folder is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from arcane_patcher import __version__

FORMAT = 2
# Release keys (Ed25519, raw public key, base64). Patch files signed by one of these are official.
TRUSTED_KEYS = {"release-2026": "gw7LHRn5H/fi5+4fp7ZXd09CSoTVwHZqp3LOQcN6n7g="}
NAME = re.compile(r"arcane-patch-(\d+(?:\.\d+)*)(?:[-+][\w.]+)?\.zip")


class PatchFileError(SystemExit):
    """A patch file problem, reported as a clear message."""


def version_tuple(v: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+)*", v or ""):
        raise PatchFileError(f"bad version {v!r}")
    return tuple(int(x) for x in v.split("."))


def canonical(info: dict) -> bytes:
    """The signed bytes: patch.json without its signature, sorted keys, compact."""
    body = {k: v for k, v in info.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _verify(info: dict, keys: dict[str, str]) -> tuple[str, str | None]:
    """("official" | "unsigned" | "unknown key" | "bad signature", key id)."""
    sig = info.get("signature")
    if not sig:
        return "unsigned", None
    key_id = sig.get("key")
    if key_id not in keys:
        return "unknown key", key_id
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[key_id])).verify(
            base64.b64decode(sig.get("ed25519", "")), canonical(info))
    except (InvalidSignature, ValueError):
        return "bad signature", key_id
    return "official", key_id


@dataclass
class PatchFile:
    path: Path
    info: dict
    trust: str  # official / unsigned / unknown key / bad signature
    key_id: str | None
    _cache: dict[str, bytes] = field(default_factory=dict, repr=False)

    @classmethod
    def open(cls, path: Path, keys: dict[str, str] | None = None) -> PatchFile:
        """Read and check a patch file: its format and that every file matches patch.json (PatchFileError
        otherwise). The signature is checked too; whether an unofficial one may be used is the caller's call."""
        path = Path(path)
        if not path.is_file():
            raise PatchFileError(f"patch file {path}: not found")
        try:
            with zipfile.ZipFile(path) as z:
                info = json.loads(z.read("patch.json"))
                names = {n for n in z.namelist() if not n.endswith("/")}
                files = info.get("files", {})
                fmt = info.get("format")
                if fmt != FORMAT:
                    newer = isinstance(fmt, int) and fmt > FORMAT
                    raise PatchFileError(f"patch file {path.name}: format {fmt!r}, this patcher reads format {FORMAT}; "
                                         + ("get a newer patcher" if newer else "get a newer patch file (the current "
                                                                                 "release's arcane-patch-<version>.zip)"))
                if names - {"patch.json"} != set(files):
                    raise PatchFileError(f"patch file {path.name}: its files do not match its list (damaged?)")
                cache = {}
                for name, sha in files.items():
                    data = z.read(name)
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise PatchFileError(f"patch file {path.name}: {name} is damaged (sha256 differs)")
                    cache[name] = data
        except (zipfile.BadZipFile, KeyError, ValueError) as e:
            raise PatchFileError(f"patch file {path}: not a patch file ({e})") from None
        trust, key_id = _verify(info, TRUSTED_KEYS if keys is None else keys)
        return cls(path, info, trust, key_id, cache)

    # --- contents ---

    @property
    def version(self) -> str:
        return str(self.info.get("version", ""))

    @property
    def official(self) -> bool:
        return self.trust == "official"

    def compatible(self) -> bool:
        return version_tuple(__version__) >= version_tuple(str(self.info.get("min_patcher", "0")))

    @property
    def builds(self) -> dict[str, str]:
        """build-id (hex) -> client build name."""
        return dict(self.info.get("builds", {}))

    @property
    def variants(self) -> dict[str, dict]:
        return dict(self.info.get("variants", {}))

    @property
    def runtime(self) -> dict:
        return dict(self.info.get("runtime", {}))

    @property
    def content(self) -> dict:
        """The client release's content set the builds use: its name and the sha256 of its gamedata and localization
        files (the phone checks the player's imported data against them)."""
        return dict(self.info.get("content", {}))

    @property
    def server(self) -> str | None:
        """The name of the embedded server release in the file (None when it has none)."""
        return self.info.get("server") or None

    def read(self, name: str) -> bytes:
        if name not in self._cache:
            raise PatchFileError(f"patch file {self.path.name}: no {name}")
        return self._cache[name]

    def rules(self, build_id: bytes) -> dict:
        name = f"rules/{build_id.hex()}.json"
        if build_id.hex() not in self.builds or name not in self._cache:
            known = ", ".join(f"{n} (build-id {b})" for b, n in self.builds.items()) or "none"
            raise PatchFileError(f"patch file {self.path.name} has no rules for build-id {build_id.hex() or 'none'}; "
                                 f"it has rules for: {known}")
        return json.loads(self._cache[name])

    def kit(self, name: str) -> bytes:
        return self.read(f"kit/{name}")

    def version_code(self) -> int:
        return version_code(self.version)


def version_code(version: str) -> int:
    """android:versionCode of a build made with patch file `version`: 2100000000 + major * 1000000 + minor * 1000 +
    patch. It grows with the patch version, so a result installs over the one made with an older patch file; it is
    above the date-based codes of earlier patcher builds (2026112200 and similar)."""
    parts = version_tuple(version) + (0, 0, 0)
    major, minor, patch = parts[:3]
    if major > 46 or minor > 999 or patch > 999:  # 46.999.999 -> 2146999999, below 2^31
        raise PatchFileError(f"patch version {version}: too large for a versionCode (major up to 46, minor and patch "
                             "up to 999)")
    return 2_100_000_000 + major * 1_000_000 + minor * 1_000 + patch


def program_dir(executable: Path) -> Path:
    """The folder the download unpacked: the executable's, or on macOS the one holding its .app bundle
    (<folder>/arcane-patcher.app/Contents/MacOS/arcane-patcher)."""
    parent = executable.parent
    if parent.name == "MacOS" and parent.parent.name == "Contents" and parent.parent.parent.suffix == ".app":
        return parent.parent.parent.parent
    return parent


def search_dirs() -> list[Path]:
    """Where patch files are looked for: inside the program (the macOS app), next to the program (the download ships
    the current one there), and the user's data folder. In a source checkout: dist/ of the checkout."""
    if getattr(sys, "frozen", False):
        # the macOS app carries the release's patch file inside (signed with it): macOS runs a downloaded app
        # from a read-only copy of the app alone, so the file next to it in the download is out of reach
        dirs = [Path(getattr(sys, "_MEIPASS", "")) / "patch", program_dir(Path(sys.executable).resolve())]
    else:
        dirs = [Path(__file__).resolve().parents[2] / "dist"]
    if sys.platform == "win32":
        data = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "arcane-patcher"
    elif sys.platform == "darwin":
        data = Path.home() / "Library/Application Support/arcane-patcher"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "arcane-patcher"
    return [*dirs, data]


def find_default(dirs: list[Path] | None = None) -> Path | None:
    """The newest arcane-patch-<version>.zip in `dirs` (by the version in its name), or None."""
    found = []
    for d in dirs if dirs is not None else search_dirs():
        try:
            entries = list(d.iterdir()) if d.is_dir() else []
        except OSError:  # a folder macOS will not list (the read-only copy it runs a downloaded app from)
            continue
        for f in entries:
            if m := NAME.fullmatch(f.name):
                found.append((version_tuple(m.group(1)), f.stat().st_mtime, f))
    return max(found)[2] if found else None
