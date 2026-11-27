"""What a build needs, checked before it starts: `arcane-patcher doctor` prints it, every `build` runs it first and
stops on a blocking problem, and the window shows it next to its Build button. One list (`checks`): each requirement
says whether it is met, what was found, and how to get it when it is not. The patch file comes first: found, its
signature (official, or unofficial and allowed), compatible with this patcher; then, with an APK, whether the patch
file has rules for its game library's build-id and every rule set of the build applies to it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    blocking: bool = True  # a failed non-blocking check is a notice (e.g. downloads still to come)

    @property
    def status(self) -> str:
        return "ok" if self.ok else ("missing" if self.blocking else "note")


def _patch_file(patch_file: Path | None, allow_unofficial: bool) -> tuple[list[Check], object]:
    """(the patch file's checks: found, signature, compatible; the opened PatchFile or None)."""
    from arcane_patcher import patchfile

    get = ("download the current arcane-patch-<version>.zip from the project's releases and put it next to the "
           "patcher (or give --patch PATH)")
    path = Path(patch_file) if patch_file is not None else patchfile.find_default()
    if path is None:
        return [Check("patch file", False, "none found in " + ", ".join(map(str, patchfile.search_dirs())), get)], None
    try:
        pf = patchfile.PatchFile.open(path)
    except SystemExit as e:
        return [Check("patch file", False, str(e), get)], None
    out = [Check("patch file", True, f"{path} (version {pf.version}, " + ", ".join(
        f"{n} build-id {b[:12]}" for b, n in pf.builds.items()) + ")")]
    if pf.official:
        out.append(Check("signature", True, f"official (release key {pf.key_id})"))
    elif allow_unofficial:
        out.append(Check("signature", False, f"UNOFFICIAL ({pf.trust}): allowed by --allow-unofficial; it adds code "
                         "to your app, use it only if you trust its source", blocking=False))
    else:
        out.append(Check("signature", False, f"unofficial ({pf.trust}): not signed by the project's release key",
                         "use an official patch file, or allow unofficial patch files (--allow-unofficial) if you "
                         "trust its source"))
    if pf.compatible():
        out.append(Check("compatible", True, f"made for patcher {pf.info.get('min_patcher')} or newer "
                         f"(this is {patchfile.__version__})"))
    else:
        out.append(Check("compatible", False, f"needs patcher {pf.info.get('min_patcher')} or newer (this is "
                         f"{patchfile.__version__})", "download the current patcher"))
    return out, pf


def _python(pf) -> Check:
    from arcane_patcher import offline

    ver = pf.runtime.get("python", offline.PY_VER)
    if sys.version_info[:2] == tuple(int(x) for x in ver.split(".")):
        return Check(f"Python {ver}", True, "the patcher itself runs on it")
    try:
        path = offline.host_python(ver)
    except SystemExit:
        return Check(f"Python {ver}", False,
                     f"the offline variant precompiles its server with CPython {ver}; this patcher runs on "
                     f"{sys.version.split()[0]} and none was found",
                     f"use a release download of the patcher, or install it: uv python install {ver}")
    return Check(f"Python {ver}", True, path)


def _server(pf, emulator_zip: Path | None, server_dir: Path | None) -> Check:
    if server_dir is not None:
        ok = Path(server_dir).is_dir()
        return Check("server", ok, f"--server-dir {server_dir}" + ("" if ok else ": not a directory"),
                     "" if ok else "give the folder of a server checkout")
    if emulator_zip is not None:
        ok = Path(emulator_zip).is_file()
        return Check("server", ok, f"--emulator-zip {emulator_zip}" + ("" if ok else ": not found"),
                     "" if ok else "give the path of a server release zip")
    if pf.server:
        return Check("server", True, f"{Path(pf.server).name} (in the patch file)")
    return Check("server", False, "the patch file holds no server release",
                 "build the offline variant with --emulator-zip PATH (a server release zip)")


def _downloads(pf) -> Check:
    from arcane_patcher import kit, offline

    try:  # CPython for Android for every server runtime the APK carries, and the server's Python packages
        items = [*(offline.cpython_pin(pf.runtime, abi) for abi in kit.SERVER_ABIS), *pf.runtime.get("wheels", [])]
    except SystemExit as e:
        return Check("downloads", False, str(e), "download the current patcher with its patch file")
    pending = [i["url"] for i in items if offline.cached(i["url"], i["sha256"]) is None]
    if not pending:
        return Check("downloads", True, f"everything is in the cache ({offline.cache_dir()})", blocking=False)
    return Check("downloads", False, f"{len(pending)} pinned files are downloaded on the first build "
                 f"(checked against their sha256, then kept in {offline.cache_dir()})",
                 "the first build needs an internet connection", blocking=False)


def _apk(apk: Path, variant: str, pf, options: dict[str, bool]) -> list[Check]:
    from arcane_patcher import patch

    hint = "give your own copy of the v29.0 client (build 1587910) as an .apk or a split bundle (.apkm / .xapk)"
    try:
        inputs = patch.read_inputs(Path(apk))
        package = patch.input_package(inputs)
    except SystemExit as e:
        return [Check("APK", False, str(e), hint)]
    out = [Check("APK", True, f"{Path(apk).name}: package {package}, " + (
        f"split bundle ({len(inputs.members)} APKs)" if inputs.bundle else "single APK"))]
    if pf is None:
        return out
    try:
        build_id, rules = patch.build_rules(inputs, pf)
    except SystemExit as e:
        return [*out, Check("rules", False, str(e), hint + ", or a patch file with rules for this build")]
    spec = patch.variant_spec(pf, variant) if variant in pf.variants else None
    if spec is None:
        return [*out, Check("rules", True, f"{rules.get('client', '?')} (build-id {build_id.hex()[:12]})")]
    try:
        if spec.kind == "offline" and inputs.bundle and patch.EMBED_ABI_SPLIT not in inputs.members:
            raise patch.PatchError(f"the offline variant needs the arm64 split ({patch.EMBED_ABI_SPLIT})")
        plan = patch.options_plan(spec, **options)
        patch.check_inputs(inputs, plan, rules)
    except SystemExit as e:
        return [*out, Check("rules", False, str(e), hint)]
    return [*out, Check("rules", True, f"{rules.get('client', '?')} (build-id {build_id.hex()[:12]}): "
                        f"{len(plan.lib) + len(plan.dex)} rule sets apply")]


def _keystore(keystore: Path, alias: str, env: dict[str, str]) -> Check:
    from arcane_patcher import signing

    hint = ("create one with `arcane-patcher keystore new --out my.keystore` (or the window's Create button) and keep "
            "it: updates must be signed with the same key")
    try:
        key = signing.key_from_env(Path(keystore), alias, env)
        signing.load_key(key)
    except SystemExit as e:
        return Check("keystore", False, str(e), hint)
    return Check("keystore", True, f"{Path(keystore).name}: key {alias!r}")


def _output(out: Path) -> Check:
    parent = Path(out).resolve().parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    ok = os.access(parent, os.W_OK)
    return Check("output", ok, f"{out}" + ("" if ok else f": {parent} is not writable"),
                 "" if ok else "choose another output location")


def checks(variant: str | None = None, apk: Path | None = None, keystore: Path | None = None,
           ks_alias: str = "androiddebugkey", out: Path | None = None, emulator_zip: Path | None = None,
           server_dir: Path | None = None, options: dict[str, bool] | None = None, patch_file: Path | None = None,
           allow_unofficial: bool = False, env: dict[str, str] | None = None) -> list[Check]:
    """Every requirement of a build with these options (the ones given; a check without its input is skipped).
    `options`: the build options (patch.options_plan's keyword arguments; default all on)."""
    env = os.environ if env is None else env
    found, pf = _patch_file(patch_file, allow_unofficial)
    if pf is not None and variant is not None and variant not in pf.variants:
        found.append(Check("variant", False, f"the patch file has no variant {variant!r}",
                           "use a patch file that has it"))
    elif pf is not None and variant is not None and pf.variants[variant].get("kind") == "offline":
        found.append(_python(pf))
        found.append(_server(pf, emulator_zip, server_dir))
        found.append(_downloads(pf))
    if apk is not None:
        found += _apk(apk, variant or "multi", pf, options or {})
    if keystore is not None:
        found.append(_keystore(keystore, ks_alias, env))
    if out is not None:
        found.append(_output(out))
    return found


def blocking(found: list[Check]) -> list[Check]:
    return [c for c in found if not c.ok and c.blocking]


def report(found: list[Check]) -> str:
    lines = []
    for c in found:
        lines.append(f"[{c.status:>7}] {c.name}: {c.detail}")
        if not c.ok and c.hint:
            lines.append(f"          -> {c.hint}")
    return "\n".join(lines)
