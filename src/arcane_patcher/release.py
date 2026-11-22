"""Make a release's patch file (patchfile.py), for maintainers and CI:

    python -m arcane_patcher.release --version 1.0.0 [--out dist] [--server-zip ZIP | --pinned-server] [--key PEM]

It packs patchdata/ (rules written by rulesgen.py, manifest fragments, variants, the offline runtime's pins), the kit
(kit.py; refused unless it was built from the current sources), and the server release the offline variant embeds:
--server-zip, or with --pinned-server the release pinned in emulator.lock (downloaded, sha256-checked). It signs the
result with the Ed25519 key in the PEM file --key or in the environment variable ARCANE_PATCH_SIGNING_KEY (the PEM
text, e.g. from CI secrets); --unsigned leaves the signature out (development: the patcher then needs
--allow-unofficial). The file is written deterministically: the same inputs give the same bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from arcane_patcher import __version__, kit, patchfile

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "patchdata"
KEY_ENV = "ARCANE_PATCH_SIGNING_KEY"
KEY_ID = "release-2026"
SOURCE_REPOSITORY = "https://github.com/phstudy/arcane-patcher"


class ReleaseError(SystemExit):
    pass


def _server(server_zip: Path | None, pinned: bool) -> tuple[str, bytes] | None:
    from arcane_patcher import offline

    if server_zip is not None:
        data = Path(server_zip).read_bytes()
        return f"server/{Path(server_zip).name}", data
    if pinned:
        pin = offline.read_lock()
        if not pin.url or not pin.sha256:
            raise ReleaseError("emulator.lock pins no server release; give --server-zip")
        path = offline.fetch(pin.url, pin.sha256, f"arcane-server-{pin.version or pin.sha256[:12]}.zip")
        return f"server/arcane-server-{pin.version or pin.sha256[:12]}.zip", path.read_bytes()
    return None


def source(version: str | None = None) -> dict:
    """Where the Corresponding Source of what a patch file adds to an APK is (GPL-3.0 section 6(d)): the repository
    and the tag / commit it was made from (CI: GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_REF_NAME / GITHUB_SHA;
    else the checkout's HEAD)."""
    env = os.environ
    repo = (f"{env['GITHUB_SERVER_URL']}/{env['GITHUB_REPOSITORY']}" if env.get("GITHUB_REPOSITORY")
            else SOURCE_REPOSITORY)
    commit = env.get("GITHUB_SHA", "")
    if not commit:
        import subprocess

        try:
            commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True,
                                    text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            commit = ""
    ref = env.get("GITHUB_REF_NAME") if env.get("GITHUB_REF_TYPE") == "tag" else (f"v{version}" if version else "")
    return {"repository": repo, "tag": ref, "commit": commit, "license": "GPL-3.0-only"}


def contents(server_zip: Path | None = None, pinned_server: bool = False, data: Path = DATA,
             kit_dir: Path | None = None) -> tuple[dict, dict[str, bytes]]:
    """(patch.json without version and signature, files) from patchdata/, the kit (default kit.KIT_DIR) and the
    server."""
    kit_dir = kit_dir or kit.KIT_DIR
    if problems := kit.problems(kit_dir):
        raise ReleaseError("kit: " + "; ".join(problems) + " (python -m arcane_patcher.kitbuild)")
    files: dict[str, bytes] = {}
    builds = {}
    for f in sorted((data / "rules").glob("*.json")):
        rules = json.loads(f.read_text())
        builds[rules["build_id"]] = rules["client"]
        files[f"rules/{f.name}"] = f.read_bytes()
    if not builds:
        raise ReleaseError(f"no rules in {data / 'rules'} (python -m arcane_patcher.rulesgen --apk APK)")
    variants = json.loads((data / "variants.json").read_text())
    for v in variants.values():
        files[v["manifest"]] = (data / v["manifest"]).read_bytes()
    for name in kit.FILES:
        files[f"kit/{name}"] = kit.read(name, kit_dir)
    server = _server(server_zip, pinned_server)
    if server:
        files[server[0]] = server[1]
    info = {"format": patchfile.FORMAT, "min_patcher": __version__, "builds": builds, "variants": variants,
            "runtime": json.loads((data / "runtime.json").read_text()),
            "content": json.loads((data / "content.json").read_text()), "server": server[0] if server else None}
    return info, files


def load_key(pem: bytes):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(pem, None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseError("the signing key must be an Ed25519 private key (PEM)")
    return key


def write(out: Path, version: str, info: dict, files: dict[str, bytes], key=None, key_id: str = KEY_ID,
          src: dict | None = None) -> Path:
    patchfile.version_code(version)  # checks the version fits a versionCode
    info = {**info, "version": version, "source": src if src is not None else source(version), "files": {n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())}}
    if key is not None:
        info["signature"] = {"key": key_id, "ed25519": base64.b64encode(key.sign(patchfile.canonical(info))).decode()}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        def put(name: str, data: bytes) -> None:
            # zips (the server) are stored; everything else deflated
            kind = zipfile.ZIP_STORED if name.endswith(".zip") else zipfile.ZIP_DEFLATED
            z.writestr(zipfile.ZipInfo(name, date_time=(2024, 2, 27, 0, 0, 0)), data, compress_type=kind)

        put("patch.json", json.dumps(info, indent=1, sort_keys=True).encode() + b"\n")
        for name in sorted(files):
            put(name, files[name])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(buf.getvalue())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m arcane_patcher.release", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", required=True, help="the patch file's version (e.g. 1.0.0)")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="output directory (default dist/)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--server-zip", type=Path, help="the server release zip the offline variant embeds")
    src.add_argument("--pinned-server", action="store_true", help="the server release pinned in emulator.lock")
    key = ap.add_mutually_exclusive_group()
    key.add_argument("--key", type=Path, help=f"Ed25519 private key (PEM); default: the environment variable {KEY_ENV}")
    key.add_argument("--unsigned", action="store_true", help="no signature (development)")
    a = ap.parse_args(argv)
    signing_key = None
    if not a.unsigned:
        pem = a.key.read_bytes() if a.key else os.environ.get(KEY_ENV, "").encode()
        if not pem:
            raise ReleaseError(f"no signing key: give --key PEM, set {KEY_ENV}, or --unsigned")
        signing_key = load_key(pem)
    info, files = contents(a.server_zip, a.pinned_server)
    out = write(a.out / f"arcane-patch-{a.version}.zip", a.version, info, files, signing_key, KEY_ID)
    print(f"wrote {out} ({out.stat().st_size >> 10} KB, {'signed' if signing_key else 'unsigned'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
