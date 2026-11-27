# SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
# Copyright (C) the arcane-patcher contributors
# What this module writes into a client build is covered by the additional permission (GPL section 7) in
# LICENSES/AdditionRef-arcane-client-linking.txt; it grants no rights to the game client itself.
"""Repack the user's own copy of the v29 Android client into one of two variants, with a patch file's rules.

Input: the v29.0 client (build 1587910) as a single APK, or a split bundle (.apkm / .xapk / any zip holding base.apk +
split_*.apk). Output: OUT.apk for a single APK (its armeabi-v7a libraries are dropped: only arm64 is patched, so a
device must run the arm64 code), or a directory of re-signed splits for a bundle (install with
`adb install-multiple OUT/*.apk`). Nothing is written unless the whole build succeeds.

This module is a generic applier: what a build changes comes from the patch file (patchfile.py), so a new release
needs a new patch file, not a new patcher. The patch file holds, per client build (the ELF build-id of
lib/arm64-v8a/libil2cpp.so), the rules: game library byte edits in named sets and dex byte edits, each checked
against its original bytes before anything is written; per variant: its package suffix, label, the loopback port its
bootstrap config points at, its rule sets, its minimum Android version and its manifest fragment; and the kit (the
setup screens, the offline runtime's native parts) and the offline variant's server.

Variants (the patch file's "variants"):
  offline  the game server runs on the phone; the setup screen imports the game data once, starts the server and
           opens the game. Package: input + ".offline", label "Offline". The server runs on the device's own CPU:
           the APK carries a server runtime for arm64 (phones) and one for x86_64 (PC emulators, which run the arm64
           game through ARM translation that does not cover the server process it starts); the launcher picks one.
  multi    the server is chosen on the phone at every launch (typed, scanned from a server's join page QR code, or
           opened as the link arcane://connect?url=...); the game talks to a loopback proxy that forwards to it.
           Package: input + ".multiplayer", label "Multiplayer".

Every build also:
- patches the `bootstrap_config` TextAsset in place (same byte length, padded with JSON whitespace): the variant's
  loopback port on the phone;
- replaces res/xml/network_security_config.xml with one that trusts no certificate authority and permits plain
  HTTP only to 127.0.0.1 (NSC_XML): the client and its third-party SDKs complete no connection to another host;
- removes the third-party services from the manifest and turns their collection off (the rules' "manifest"
  section), and applies the variant's own rule sets (the publisher's services not waited for, analytics upload off);
- applies the option sets (OPTION_SETS) that are on: guest login, ad rewards, no rate prompt, store timers, the
  consent dialog answered automatically;
- keeps the APK's own launcher icon; sets the launcher label (--label) and the package (--package);
- sets android:versionCode from the patch file's version (patchfile.version_code).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from arcane_patcher import apksign
from arcane_patcher.signing import DEBUG_ALIAS, KeystoreSigner, key_from_env


class PatchError(SystemExit):
    """A build problem, reported as a clear message (nothing has been written)."""


Variant = Literal["offline", "multi"]

BOOTSTRAP_MARKER = b'{\n    "client_version"'
NSC_ENTRY = "res/xml/network_security_config.xml"
# The app's network security config: the client reaches only this phone. Plain HTTP only to 127.0.0.1 (the setup
# screens' server or proxy); no certificate authority is trusted, so no TLS connection of the client or its
# third-party SDKs (analytics, ads, attribution, the publisher's services) completes. The multiplayer proxy reaches
# the chosen game server with its own trust store (ServerUrl.tls); the offline server runs in its own process.
NSC_XML = """<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors />
    </base-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="false">127.0.0.1</domain>
        <domain includeSubdomains="false">localhost</domain>
    </domain-config>
</network-security-config>
"""
# v1 signature files of the input (dropped; the output is signed again)
SIGNATURE_FILE = re.compile(r"META-INF/[^/]+\.(SF|MF|RSA|EC|[D]SA)")


# --- game library ----------------------------------------------------------------------------------------------

GAME_LIB = "lib/arm64-v8a/libil2cpp.so"
CONTENT_ASSET = "assets/arcane_content/content.json"
NOTICES_ASSET = "assets/arcane_notices/THIRD_PARTY_NOTICES.txt"
RULES_FORMAT = 2  # rules/<build-id>.json: edits carry the original bytes as length + sha256


def elf_build_id(so: bytes) -> bytes | None:
    """GNU build-id from the ELF64 note segments."""
    phoff, phentsize, phnum = struct.unpack_from("<Q", so, 0x20)[0], *struct.unpack_from("<HH", so, 0x36)
    for i in range(phnum):
        p_type, _, p_offset, _, _, p_filesz = struct.unpack_from("<IIQQQQ", so, phoff + i * phentsize)
        if p_type != 4:  # PT_NOTE
            continue
        off, end = p_offset, p_offset + p_filesz
        while off + 12 <= end:
            namesz, descsz, ntype = struct.unpack_from("<III", so, off)
            name_end = off + 12 + ((namesz + 3) & ~3)
            if ntype == 3 and so[off + 12:off + 12 + namesz].rstrip(b"\0") == b"GNU":
                return so[name_end:name_end + descsz]
            off = name_end + ((descsz + 3) & ~3)
    return None


def apply_edits(data: bytes, edits: list[dict], what: str) -> bytes:
    """`data` with the byte edits of a rules set (offset, length, original_sha256, new: hex). The rules hold no bytes
    of the game: every site's current bytes must hash to its original_sha256, else PatchError listing every site that
    does not (nothing is changed)."""
    out = bytearray(data)
    problems = []
    for e in edits:
        offset, length, new = int(e["offset"]), int(e["length"]), bytes.fromhex(e["new"])
        if len(new) != length:
            problems.append(f"{e.get('note', '')} @ {offset:#x}: edit and original differ in length")
        elif offset + length > len(out) or hashlib.sha256(out[offset:offset + length]).hexdigest() != e["original_sha256"]:
            problems.append(f"{e.get('note', '')} @ {offset:#x}: the {length} bytes there are not the expected ones")
        else:
            out[offset:offset + length] = new
    if problems:
        raise PatchError(f"{what}: the file does not match the build the patch file's rules were made for:\n  "
                         + "\n  ".join(problems))
    return bytes(out)


# --- host tools, bootstrap config, repacking ----------------------------------------------------------------------


def compile_nsc() -> bytes:
    """NSC_XML as binary XML, as aapt2 compiles res/xml files (it only references framework attrs)."""
    from arcane_patcher import axml

    return axml.compile_xml(NSC_XML)


def patch_bootstrap(data: bytes, server: str, log_level: str | None) -> bytes:
    start = data.index(BOOTSTRAP_MARKER)
    (length,) = struct.unpack_from("<i", data, start - 4)
    text = data[start : start + length].decode()
    cfg = json.loads(text)
    server = server.rstrip("/") + "/"
    cfg["service_url"] = server
    cfg["server_maintenance_url"] = server + "status/maintenance.json"
    if log_level:
        cfg["default_log_level"] = log_level
        cfg["log_levels"] = []
    new = json.dumps(cfg, separators=(",", ":"))
    if len(new) > length:
        raise PatchError(f"patched bootstrap_config is {len(new)} bytes, original only {length}; use a shorter server URL")
    new = new[:-1] + " " * (length - len(new)) + "}"
    print("bootstrap_config ->", new.rstrip("} ").strip() + "}")
    return data[:start] + new.encode() + data[start + length :]


def is_bootstrap(name: str, data: bytes) -> bool:
    """The bootstrap_config TextAsset lives in one of the serialized files under assets/bin/Data (its
    hashed file name differs per client version), stored uncompressed."""
    return name.startswith("assets/bin/Data/") and "/Managed/" not in name and BOOTSTRAP_MARKER in data


# The options a build takes, as rule sets of the patch file: option -> (rules section, set name), in the order they
# are applied (after the variant's own sets).
OPTION_SETS = {"guest_login": ("lib", "guest_login"), "ad_rewards": ("lib", "ad_rewards"),
               "skip_rate_prompt": ("lib", "rate_prompt"), "store_timers": ("lib", "store_timers"),
               "skip_consent": ("dex", "consent")}


@dataclass(frozen=True)
class Plan:
    """The rule sets a build applies: game library sets (in order) and dex sets."""

    lib: tuple[str, ...] = ()
    dex: tuple[str, ...] = ()

    def lib_edits(self, rules: dict) -> list[tuple[str, list[dict]]]:
        return [(rules["lib"][k].get("title", k), rules["lib"][k]["edits"]) for k in self.lib]


def plan_for(variant_sets: list[str], options: dict[str, bool], variant_dex_sets: list[str] = ()) -> Plan:
    lib, dex = list(variant_sets), list(variant_dex_sets)
    for option, (section, name) in OPTION_SETS.items():
        if options.get(option, False):
            (lib if section == "lib" else dex).append(name)
    return Plan(tuple(lib), tuple(dex))


def check_plan(plan: Plan, rules: dict) -> None:
    missing = [f"lib/{k}" for k in plan.lib if k not in rules.get("lib", {})]
    missing += [f"dex/{k}" for k in plan.dex if k not in rules.get("dex", {})]
    if missing:
        raise PatchError(f"the patch file's rules for {rules.get('client', '?')} have no {', '.join(missing)}; turn the "
                         "option off or use a patch file that has them")


def patch_game_lib(so: bytes, plan: Plan, rules: dict) -> bytes:
    for title, edits in plan.lib_edits(rules):
        so = apply_edits(so, edits, f"{GAME_LIB} ({title})")
        print(f"libil2cpp.so: {title} patched ({len(edits)} sites)")
    return so


ADDED_ENTRY_TIME = (2024, 2, 27, 0, 0, 0)


def repack(src: Path, dst: Path, signer, tmp: Path, patch: bool, server: str, log_level: str | None,
           nsc: bytes, plan: Plan, rules: dict, extra: dict[str, bytes] | None = None, drop: tuple[str, ...] = (),
           version_code: int | None = None, min_sdk: int = 21) -> bool:
    """Copy `src` to `dst` without its old signature (patching base resources if `patch`, the game library with the
    rule sets of `plan`), align and sign with `signer` (v2, plus v1 below `min_sdk` 24; apksign.py). `extra`: entries to replace or add (name -> bytes; `.zip` entries are
    stored, others deflated). `drop`: name prefixes of entries left out. Returns True if the bootstrap config was
    found and patched. `version_code`: android:versionCode of its manifest."""
    unsigned = tmp / f"{dst.name}.unsigned"
    patched = False
    extra = dict(extra or {})
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(unsigned, "w") as zout:
        for info in zin.infolist():
            if SIGNATURE_FILE.fullmatch(info.filename) or info.filename.startswith(drop):
                continue
            data = zin.read(info)
            if info.filename in extra:
                data = extra.pop(info.filename)
            elif patch and not patched and info.file_size < 1 << 20 and is_bootstrap(info.filename, data):
                data = patch_bootstrap(data, server, log_level)
                patched = True
            elif patch and info.filename == NSC_ENTRY:
                data = nsc
            elif info.filename == GAME_LIB and plan.lib:
                data = patch_game_lib(data, plan, rules)
            if info.filename == "AndroidManifest.xml" and version_code is not None:
                data = axml_set_version_code(data, version_code)
            zout.writestr(info, data, compress_type=info.compress_type)
        for name, data in extra.items():
            # a fixed timestamp: the same inputs give the same APK
            added = zipfile.ZipInfo(name, date_time=ADDED_ENTRY_TIME)
            added.external_attr = 0o600 << 16
            zout.writestr(added, data, compress_type=zipfile.ZIP_STORED if name.endswith(".zip") else zipfile.ZIP_DEFLATED)
    dst.parent.mkdir(parents=True, exist_ok=True)
    apksign.zipalign(unsigned, dst)
    signer.sign(dst, min_sdk)
    return patched


# --- manifest rebuild ---------------------------------------------------------------------------------------------


def manifest_text(apk: Path) -> tuple[str, bytes]:
    """(the APK's AndroidManifest.xml as text, its resources.arsc): axml.decode, app references by name."""
    from arcane_patcher import axml

    with zipfile.ZipFile(apk) as z:
        mf, arsc = z.read("AndroidManifest.xml"), z.read("resources.arsc")
    try:
        return axml.decode(mf, arsc), arsc
    except axml.AxmlError as e:
        raise PatchError(f"{apk.name}: AndroidManifest.xml: {e}") from None


def manifest_package(text: str) -> str:
    m = re.search(r'<manifest\b[^>]*\bpackage="([^"]+)"', text)
    if not m:
        raise PatchError("the manifest names no package")
    return m.group(1)


def rebuild_manifest(text: str, arsc: bytes, edit, min_sdk: int | None = None) -> bytes:
    """Binary AndroidManifest.xml of `text` after `edit(text) -> text` (and android:minSdkVersion of <uses-sdk>).
    Only the manifest changes: resources.arsc and the resource files stay the original ones (resource ids are kept;
    `@type/name` references resolve against `arsc`)."""
    from arcane_patcher import axml

    text = edit(text)
    if min_sdk is not None:
        text, n = re.subn(r'(<uses-sdk\b[^>]*\sandroid:minSdkVersion=)"[^"]*"', rf'\g<1>"{min_sdk}"', text, count=1)
        if n != 1:
            raise PatchError("android:minSdkVersion not found in the manifest's <uses-sdk>")
    try:
        return axml.encode(text, axml.arsc_resolver(arsc))
    except axml.AxmlError as e:
        raise PatchError(f"rebuilding AndroidManifest.xml: {e}") from None


# --- third-party services: removed from the manifest (every variant) -------------------------------------------
#
# The rules' "manifest" section lists, for the client build, the components, meta-data and permissions of
# third-party services (analytics, crash reporting, push messaging, advertising, attribution) to remove by name
# prefix, and meta-data to add that turn their collection off where code still reaches them.
STRIP_TAGS = ("activity", "activity-alias", "service", "receiver", "provider", "meta-data", "uses-permission",
              "uses-permission-sdk-23", "permission")


def strip_manifest(text: str, section: dict) -> str:
    """Decoded manifest without the elements whose android:name starts with one of section["remove"] (an element with
    a body goes with its body), with section["meta_data"] (name -> value) set in <application>."""
    prefixes = tuple(section.get("remove", ()))
    if prefixes:
        tags = "|".join(re.escape(t) for t in STRIP_TAGS)
        element = re.compile(rf'[ \t]*<({tags})\b((?:\s+[\w:.-]+="[^"]*")*)\s*(/>|>.*?</\1\s*>)[ \t]*\n?', re.S)

        def drop(m: re.Match) -> str:
            name = re.search(r'\sandroid:name="([^"]*)"', m.group(2))
            if name and name.group(1).startswith(prefixes):
                return ""
            if m.group(3) == "/>":
                return m.group(0)
            body_at = m.start(3) - m.start(0) + 1  # a kept element: its children are checked too
            whole = m.group(0)
            return whole[:body_at] + element.sub(drop, whole[body_at:])

        text = element.sub(drop, text)
    meta = section.get("meta_data", {})
    if meta:
        for name in meta:
            text = re.sub(rf'[ \t]*<meta-data\b[^>]*android:name="{re.escape(name)}"[^>]*/>[ \t]*\n?', "", text)
        added = "".join(f'<meta-data android:name="{_xml_attr(n)}" android:value="{_xml_attr(v)}"/>'
                        for n, v in meta.items())
        if text.count("</application>") != 1:
            raise PatchError("<application> not found exactly once")
        text = text.replace("</application>", added + "</application>", 1)
    return text


# --- rebranding: package and label (every variant) ----------------------------------------------------------------
#
# A different package name lets the build sit next to another one on the same phone. The manifests change (base +
# every split) and the resource table's package name (code looks resources up by name with getPackageName());
# the code keeps its classes, so every component name is made absolute first. Everything else that must be unique
# per installed package is moved to the new prefix: provider authorities (initialization providers refuse to start
# or clash otherwise) and the app's own permissions (which the code builds as getPackageName() + "...").
COMPONENT_TAGS = {"application", "activity", "activity-alias", "service", "receiver", "provider", "instrumentation"}
CLASS_ATTRS = {"name", "targetActivity", "backupAgent", "manageSpaceActivity"}
PERMISSION_TAGS = {"permission", "permission-group", "permission-tree", "uses-permission", "uses-permission-sdk-23"}
PERMISSION_ATTRS = {"permission", "readPermission", "writePermission", "permissionGroup"}
START_TAG = re.compile(r'<([\w:-]+)((?:\s+[\w:.-]+="[^"]*")*)(\s*/?)>')
ATTR = re.compile(r'(\s+)android:([\w]+)="([^"]*)"')


def _xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _set_label(start_tag: str, label: str) -> str:
    if re.search(r'\sandroid:label="[^"]*"', start_tag):
        return re.sub(r'(\sandroid:label=)"[^"]*"', lambda m: f'{m.group(1)}"{_xml_attr(label)}"', start_tag, count=1)
    return re.sub(r"^<([\w-]+)", lambda m: f'<{m.group(1)} android:label="{_xml_attr(label)}"', start_tag, count=1)


def rebrand_manifest(text: str, package: str | None = None, label: str | None = None) -> str:
    """Decoded manifest with a new package (absolute component names, authorities and own permissions moved to
    it) and/or a literal app label (on <application>, launcher activities and activities that showed the app's
    label)."""
    old = manifest_package(text)
    app = re.search(r"<application\b[^>]*>", text)
    app_label = re.search(r'\sandroid:label="([^"]*)"', app.group(0)) if app else None
    app_label = app_label.group(1) if app_label else None

    def repkg(v: str) -> str:
        return package + v[len(old):] if v == old or v.startswith(old + ".") else v

    def tag(m: re.Match) -> str:
        name, start = m.group(1), m.group(0)
        if package:
            def attr(a: re.Match) -> str:
                key, v = a.group(2), a.group(3)
                if name == "manifest" and key == "package":
                    return a.group(0)
                if name in COMPONENT_TAGS and key in CLASS_ATTRS and v:
                    v = old + v if v.startswith(".") else v if "." in v else f"{old}.{v}"
                elif key == "taskAffinity":
                    v = repkg(v)
                elif name == "provider" and key == "authorities":
                    v = ";".join(repkg(x) for x in v.split(";"))
                elif (name in PERMISSION_TAGS and key == "name") or key in PERMISSION_ATTRS:
                    v = repkg(v)
                return f'{a.group(1)}android:{key}="{v}"'
            start = f"<{name}" + ATTR.sub(attr, start[len(name) + 1:])
            if name == "manifest":
                start = re.sub(r'(\spackage=)"[^"]*"', lambda a: f'{a.group(1)}"{package}"', start, count=1)
        if label and (name == "application" or (name in ("activity", "activity-alias") and app_label is not None
                                                 and f'android:label="{app_label}"' in start)):
            start = _set_label(start, label)
        return start

    text = START_TAG.sub(tag, text)
    if label:
        def launcher(m: re.Match) -> str:
            if "android.intent.category.LAUNCHER" not in m.group(0):
                return m.group(0)
            start = START_TAG.match(m.group(0))
            return _set_label(start.group(0), label) + m.group(0)[start.end():]
        text = re.sub(r"<(activity|activity-alias)\b[^>]*[^/]>.*?</\1>", launcher, text, flags=re.S)
    return text


def axml_strings(data: bytes) -> list[str]:
    """The string pool of a binary XML file (AndroidManifest.xml)."""
    if struct.unpack_from("<HH", data, 0) != (0x0003, 8):
        raise PatchError("not a binary XML file")
    pool = 8
    typ, hsize, size, count, styles, flags, strings_start, styles_start = struct.unpack_from("<HHIIIIII", data, pool)
    if typ != 0x0001:
        raise PatchError("binary XML: string pool not found")
    utf8 = bool(flags & 0x100)
    offsets = struct.unpack_from(f"<{count}I", data, pool + hsize)
    base = pool + strings_start

    def decode(off: int) -> str:
        p = base + off
        if utf8:
            p += 2 if data[p] & 0x80 else 1  # utf-16 length
            n = data[p]
            if n & 0x80:
                n, p = ((n & 0x7F) << 8) | data[p + 1], p + 2
            else:
                p += 1
            return data[p:p + n].decode("utf-8")
        n = struct.unpack_from("<H", data, p)[0]
        p += 2
        if n & 0x8000:
            n, p = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0], p + 2
        return data[p:p + 2 * n].decode("utf-16-le")

    return [decode(o) for o in offsets]


def axml_replace_strings(data: bytes, mapping: dict[str, str]) -> bytes:
    """Binary XML (e.g. a split's AndroidManifest.xml) with the string-pool entries equal to a key of `mapping`
    replaced (the pool is re-encoded; everything else refers to strings by index and is kept as is)."""
    strings = axml_strings(data)
    pool = 8
    typ, hsize, size, count, styles, flags, strings_start, styles_start = struct.unpack_from("<HHIIIIII", data, pool)
    utf8 = bool(flags & 0x100)

    def encode(s: str) -> bytes:
        if utf8:
            b = s.encode("utf-8")
            def ln(n: int) -> bytes:
                return bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])
            return ln(len(s.encode("utf-16-le")) // 2) + ln(len(b)) + b + b"\0"
        u = s.encode("utf-16-le")
        n = len(u) // 2
        head = struct.pack("<H", n) if n < 0x8000 else struct.pack("<HH", 0x8000 | (n >> 16), n & 0xFFFF)
        return head + u + b"\0\0"

    if not any(s in mapping for s in strings):
        return data
    blob, new_offsets = bytearray(), []
    for s in strings:
        new_offsets.append(len(blob))
        blob += encode(mapping.get(s, s))
    blob += b"\0" * (-len(blob) % 4)
    style_offsets = data[pool + hsize + 4 * count:pool + hsize + 4 * (count + styles)]
    style_data = data[pool + styles_start:pool + size] if styles else b""
    new_strings_start = hsize + 4 * (count + styles)
    new_styles_start = new_strings_start + len(blob) if styles else 0
    body = struct.pack(f"<{count}I", *new_offsets) + style_offsets + bytes(blob) + style_data
    new_size = hsize + len(body)
    header = struct.pack("<HHIIIIII", typ, hsize, new_size, count, styles, flags, new_strings_start, new_styles_start)
    header += data[pool + 28:pool + hsize]
    rest = data[pool + size:]
    out = struct.pack("<HHI", 0x0003, 8, 8 + new_size + len(rest)) + header + body + rest
    return out

def axml_version_code(data: bytes) -> int:
    """android:versionCode of a binary AndroidManifest.xml."""
    off, value = _version_code_attr(data)
    return value


def axml_set_version_code(data: bytes, code: int) -> bytes:
    """Binary AndroidManifest.xml with android:versionCode set to `code` (in place: the attribute's int value)."""
    if not ORIGINAL_VERSION_CODE < code < 1 << 31:
        raise PatchError(f"versionCode {code} must be above {ORIGINAL_VERSION_CODE} (the original's) and fit an int")
    off, _ = _version_code_attr(data)
    out = bytearray(data)
    struct.pack_into("<I", out, off, code)
    return bytes(out)


def _version_code_attr(data: bytes) -> tuple[int, int]:
    """(offset of the value, value) of the <manifest> element's android:versionCode attribute."""
    strings = axml_strings(data)
    pos = 8
    res_ids: list[int] = []
    while pos < len(data):
        typ, hsize, size = struct.unpack_from("<HHI", data, pos)
        if typ == 0x0180:  # resource map: string index -> attribute resource id
            res_ids = list(struct.unpack_from(f"<{(size - hsize) // 4}I", data, pos + hsize))
        elif typ == 0x0102:  # start element
            name = strings[struct.unpack_from("<I", data, pos + 20)[0]]
            if name != "manifest":
                break
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, pos + 24)
            for i in range(attr_count):
                a = pos + 16 + attr_start + i * attr_size
                name_idx, raw = struct.unpack_from("<Ii", data, a + 4)
                _, _, dtype, value = struct.unpack_from("<HBBI", data, a + 12)
                if (name_idx < len(res_ids) and res_ids[name_idx] == ANDROID_VERSION_CODE_ATTR) or strings[name_idx] == "versionCode":
                    if dtype != _TYPE_INT_DEC or raw != -1:
                        raise PatchError(f"android:versionCode is not a plain int (type {dtype:#x}, raw {raw})")
                    return a + 16, value
            break
        pos += size
    raise PatchError("android:versionCode not found in the manifest")


def arsc_rename_package(data: bytes, old: str, new: str) -> bytes:
    """resources.arsc with its package `old` renamed to `new` (ResTable_package.name: fixed 128 UTF-16 units, so
    in place; resource ids unchanged). Needed because code looks resources up by name with getPackageName()
    (Unity: getIdentifier("game_view_content_description", "string", getPackageName()) -> 0 -> crash)."""
    if len(new) > 127:
        raise PatchError(f"package name {new!r} too long for resources.arsc")
    typ, hsize, size = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0002:
        raise PatchError("resources.arsc: not a resource table")
    out = bytearray(data)
    off, renamed = hsize, 0
    while off < size:
        ctype, _, csize = struct.unpack_from("<HHI", data, off)
        if ctype == 0x0200:  # RES_TABLE_PACKAGE_TYPE: header, u32 id, char16 name[128]
            name = data[off + 12:off + 12 + 256].decode("utf-16-le").split("\0", 1)[0]
            if name == old:
                out[off + 12:off + 12 + 256] = new.encode("utf-16-le").ljust(256, b"\0")
                renamed += 1
        off += csize
    if not renamed:
        raise PatchError(f"resources.arsc: package {old!r} not found")
    return bytes(out)


# --- versionCode (every build) ------------------------------------------------------------------------------------
#
# Every build's Android versionCode follows its patch file's version (patchfile.version_code), so a build made with a
# newer patch file installs as an update over an older one (data kept), and a rebuild with the same one installs over
# itself. versionName stays the game's ("29.0"). Base and every split get the same code (Android requires it).
# --version-code N sets one.

ORIGINAL_VERSION_CODE = 1587910  # v29.0
ANDROID_VERSION_CODE_ATTR = 0x0101021B  # android:versionCode
_TYPE_INT_DEC = 0x10


# --- setup-screen launchers (offline, multiplayer) ----------------------------------------------------------------

EMBED_ABI_SPLIT = "split_config.arm64_v8a.apk"
# A single APK's 32-bit libraries are left out: only the arm64 game library is patched (and the embedded server's
# runtimes go into the arm64 folder, offline.GAME_ABI_LIB_DIR), so the device must run the arm64 code.
SINGLE_APK_DROP = ("lib/armeabi-v7a/",)
LAUNCHER_FILTER = re.compile(
    r'\s*<intent-filter>\s*<action android:name="android.intent.action.MAIN"/>\s*'
    r'<category android:name="android.intent.category.LAUNCHER"/>\s*</intent-filter>')
ACTIVITY_BLOCK = re.compile(r'<activity\b((?:\s+[\w:.-]+="[^"]*")*)\s*>(.*?)</activity>', re.S)
# The setup screens read the game's activity from this <meta-data> of <application> (android/*/SetupActivity).
GAME_ACTIVITY_META = "arcane.game_activity"


def _absolute(name: str, package: str) -> str:
    return package + name if name.startswith(".") else name if "." in name else f"{package}.{name}"


def launcher_activity(text: str) -> str:
    """The class name (absolute) of the decoded manifest's activity with the MAIN / LAUNCHER intent filter."""
    found = []
    for m in ACTIVITY_BLOCK.finditer(text):
        body = m.group(2)
        if ("android.intent.action.MAIN" in body and "android.intent.category.LAUNCHER" in body
                and LAUNCHER_FILTER.search(body)):
            name = re.search(r'\sandroid:name="([^"]+)"', m.group(1))
            if name:
                found.append(_absolute(name.group(1), manifest_package(text)))
    if len(found) != 1:
        raise PatchError(f"expected one launcher activity (MAIN / LAUNCHER) in the manifest, found {found or 'none'}")
    return found[0]


def fragment_parts(fragment: str, package: str, game_activity: str) -> tuple[str, str]:
    """(manifest-level part, application part) of a patch file's manifest fragment, its placeholders filled in."""
    text = re.sub(r"<!--.*?-->", "", fragment, flags=re.S)  # its comments are for its readers
    text = text.replace("{package}", package).replace("{game_activity}", game_activity)
    parts = []
    for tag in ("manifest", "application"):
        found = re.findall(rf"<{tag}>(.*?)</{tag}>", text, re.S)
        if len(found) != 1:
            raise PatchError(f"the patch file's manifest fragment has {len(found)} <{tag}> parts, expected one")
        parts.append(found[0])
    return parts[0], parts[1]


def launcher_manifest(text: str, fragment: str) -> str:
    """Decoded manifest with the launcher moved from the game's launcher activity (read from the manifest) to the
    setup activity of the variant's manifest fragment, which also brings its other components (in <application>)
    and manifest-level elements (before <application>); the fragment names the game activity in a meta-data."""
    game = launcher_activity(text)
    package = manifest_package(text)
    uses, app = fragment_parts(fragment, package, game)
    for m in ACTIVITY_BLOCK.finditer(text):
        name = re.search(r'\sandroid:name="([^"]+)"', m.group(1))
        if name and _absolute(name.group(1), package) == game:
            text = text[:m.start(2)] + LAUNCHER_FILTER.sub("", m.group(2), count=1) + text[m.end(2):]
            break
    if text.count("</application>") != 1 or len(re.findall(r"<application\b", text)) != 1:
        raise PatchError("<application> not found exactly once")
    text = text.replace("</application>", app + "</application>", 1)
    if uses:
        at = re.search(r"<application\b", text).start()
        text = text[:at] + uses + text[at:]
    return text


def next_dex_name(apk: Path) -> str:
    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())
    i = 2
    while f"classes{i}.dex" in names:
        i += 1
    return f"classes{i}.dex"


def loopback_port(server: str) -> int:
    url = urlsplit(server)
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
        raise PatchError(f"{server}: a setup-screen build needs http://127.0.0.1:PORT")
    return url.port


def multiplayer_entries(base_apk: Path, server: str, pf) -> dict[str, bytes]:
    """Base / single APK entries of the multiplayer variant: the setup screen + loopback proxy dex (the patch file's
    kit), the proxy's port."""
    port = loopback_port(server)
    return {next_dex_name(base_apk): pf.kit("multiplayer.dex"), "assets/arcane_multiplayer/port": str(port).encode()}


def offline_entries(base_apk: Path, tmp: Path, server: str, package: str, server_src: Path,
                    pf) -> dict[str, dict[str, bytes]]:
    """Entries to add / replace per split for the offline variant (`package`: the installed package name)."""
    from arcane_patcher import offline

    port = loopback_port(server)
    base, arm64 = offline.build_runtime(tmp, package, server_src, pf)
    base[next_dex_name(base_apk)] = base.pop("classes.dex")
    base["assets/arcane_offline/port"] = str(port).encode()
    return {"base.apk": base, EMBED_ABI_SPLIT: arm64}


def dex_entries(base_apk: Path, plan: Plan, rules: dict) -> dict[str, bytes]:
    """{dex name: dex} for the dex rule sets of `plan`: byte edits, then the dex header's checksum and signature."""
    from arcane_patcher import dexpatch

    out: dict[str, bytes] = {}
    with zipfile.ZipFile(base_apk) as z:
        for name in plan.dex:
            rule = rules["dex"][name]
            for entry, edits in sorted(rule["files"].items()):
                dex = out.get(entry) or z.read(entry)
                out[entry] = apply_edits(dex, edits, f"{entry} ({rule.get('title', name)})")
            print(f"{', '.join(sorted(rule['files']))}: {rule.get('title', name)}")
    return {entry: dexpatch.Dex(bytearray(dex)).finish() for entry, dex in out.items()}


# --- variants ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VariantSpec:
    """A variant as the patch file describes it (patch.json "variants")."""

    kind: str  # offline (the server on the phone) / multiplayer (the server chosen on the phone)
    package_suffix: str  # appended to the input package
    label: str  # default launcher label
    loopback: str  # the server URL baked into the bootstrap config (a port on the phone)
    lib_sets: tuple[str, ...]  # game library rule sets this variant always applies
    dex_sets: tuple[str, ...]  # dex rule sets this variant always applies
    min_sdk: int  # the result's minimum Android API level (signing: v1 below 24)
    raise_min_sdk: bool  # set android:minSdkVersion to min_sdk
    manifest: str  # the manifest fragment in the patch file

    @classmethod
    def of(cls, d: dict) -> VariantSpec:
        return cls(d["kind"], d["package_suffix"], d["label"], d["loopback"], tuple(d.get("lib_sets", ())),
                   tuple(d.get("dex_sets", ())), int(d["min_sdk"]), bool(d.get("raise_min_sdk")), d["manifest"])


VARIANT_NAMES = ("offline", "multi")  # the command line's names (the patch file describes each)


def variant_spec(pf, variant: str) -> VariantSpec:
    if variant not in pf.variants:
        raise PatchError(f"the patch file has no variant {variant!r} (it has {', '.join(pf.variants) or 'none'})")
    return VariantSpec.of(pf.variants[variant])


def output_package(input_package: str, spec: VariantSpec, override: str | None = None) -> str:
    """The installed package of a variant: --package if given, else the input's plus the variant's suffix."""
    package = override or input_package + spec.package_suffix
    if not re.fullmatch(r"[A-Za-z]\w*(\.[A-Za-z]\w*)+", package):
        raise PatchError(f"{package!r} is not a valid package name")
    return package


@dataclass(frozen=True)
class Inputs:
    """The input APK as found by preflight: single APK or bundle members, the game library's container."""
    apk: Path
    members: list[str]  # split bundle: the .apk members (base.apk, split_*.apk); single APK: []

    @property
    def bundle(self) -> bool:
        return bool(self.members)


def read_inputs(apk: Path) -> Inputs:
    if not apk.is_file():
        raise PatchError(f"{apk}: not found")
    try:
        with zipfile.ZipFile(apk) as z:
            members = [n for n in z.namelist() if n.endswith(".apk") and "/" not in n]
            names = set(z.namelist())
    except zipfile.BadZipFile:
        raise PatchError(f"{apk}: not an APK or split bundle (not a zip file)") from None
    if "base.apk" in members:
        return Inputs(apk, members)
    if "AndroidManifest.xml" not in names:
        raise PatchError(f"{apk}: neither an APK (no AndroidManifest.xml) nor a split bundle (no base.apk)")
    return Inputs(apk, [])


def read_game_lib(inputs: Inputs) -> bytes | None:
    """The arm64 game library of the input (the single APK, or the bundle split that holds it)."""
    with zipfile.ZipFile(inputs.apk) as z:
        if not inputs.bundle:
            return z.read(GAME_LIB) if GAME_LIB in z.namelist() else None
        for m in sorted(inputs.members):
            try:
                with z.open(m) as f, zipfile.ZipFile(f) as split:
                    if GAME_LIB in split.namelist():
                        return split.read(GAME_LIB)
            except zipfile.BadZipFile:
                raise PatchError(f"{inputs.apk}: {m} is not an APK") from None
    return None


def build_rules(inputs: Inputs, pf) -> tuple[bytes, dict]:
    """(the game library's build-id, the patch file's rules for it); PatchError naming the build-id when the patch
    file has none."""
    so = read_game_lib(inputs)
    if so is None:
        raise PatchError(f"{inputs.apk}: no {GAME_LIB} (the patches need the arm64 build)")
    build_id = elf_build_id(so) or b""
    try:
        rules = pf.rules(build_id)
    except SystemExit as e:
        raise PatchError(f"{GAME_LIB}: {e} Use the supported APK.") from None
    if rules.get("format") != RULES_FORMAT:
        raise PatchError(f"the patch file's rules are format {rules.get('format')!r}, this patcher reads format "
                         f"{RULES_FORMAT}")
    return build_id, rules


def check_inputs(inputs: Inputs, plan: Plan, rules: dict) -> None:
    """Apply the rule sets of `plan` to the input in memory (nothing is written): PatchError naming every site that
    does not hold its original bytes. The same checks the build runs, for doctor."""
    check_plan(plan, rules)
    so = read_game_lib(inputs)
    for title, edits in plan.lib_edits(rules):
        apply_edits(so, edits, f"{GAME_LIB} ({title})")
    if plan.dex:
        names = sorted({entry for k in plan.dex for entry in rules["dex"][k]["files"]})
        with zipfile.ZipFile(inputs.apk) as z:
            if inputs.bundle:
                with z.open("base.apk") as f, zipfile.ZipFile(f) as base:
                    dexes = {n: base.read(n) for n in names}
            else:
                dexes = {n: z.read(n) for n in names}
        for k in plan.dex:
            rule = rules["dex"][k]
            for entry, edits in sorted(rule["files"].items()):
                dexes[entry] = apply_edits(dexes[entry], edits, f"{entry} ({rule.get('title', k)})")


def options_plan(spec: VariantSpec, guest_login: bool = True, ad_rewards: bool = True, skip_rate_prompt: bool = True,
                 store_timers: bool = True, skip_consent: bool = True) -> Plan:
    return plan_for(list(spec.lib_sets), {"guest_login": guest_login, "ad_rewards": ad_rewards,
                                          "skip_rate_prompt": skip_rate_prompt, "store_timers": store_timers,
                                          "skip_consent": skip_consent}, list(spec.dex_sets))


def customize(tmp: Path, base_apk: Path, splits: dict[str, Path], spec: VariantSpec, server: str, package: str,
              label: str, plan: Plan, rules: dict, server_src: Path | None, pf) -> dict[str, dict[str, bytes]]:
    """Entries to add / replace per output APK ("base.apk" = the base / single APK)."""
    extra: dict[str, dict[str, bytes]] = {"base.apk": {}}
    extra["base.apk"].update(dex_entries(base_apk, plan, rules))
    # the content set the setup screen imports and checks (both variants): the client release and its files' hashes
    extra["base.apk"][CONTENT_ASSET] = (json.dumps(pf.content, indent=1, sort_keys=True) + "\n").encode()
    extra["base.apk"][NOTICES_ASSET] = pf.kit("NOTICES.txt")  # the licenses of the third-party code the build adds
    text, arsc = manifest_text(base_apk)
    old = manifest_package(text)
    if spec.kind == "offline":
        for name, entries in offline_entries(base_apk, tmp, server, package, server_src, pf).items():
            extra.setdefault(name, {}).update(entries)
    elif spec.kind == "multiplayer":
        extra["base.apk"].update(multiplayer_entries(base_apk, server, pf))
    else:
        raise PatchError(f"unknown variant kind {spec.kind!r} in the patch file (a newer patcher may know it)")
    fragment = pf.read(spec.manifest).decode()

    def edit(text: str) -> str:
        text = strip_manifest(text, rules.get("manifest", {}))
        return rebrand_manifest(launcher_manifest(text, fragment), package if package != old else None, label)

    extra["base.apk"]["AndroidManifest.xml"] = rebuild_manifest(text, arsc, edit,
                                                                spec.min_sdk if spec.raise_min_sdk else None)
    if package != old:
        print(f"package {old} -> {package}")
        with zipfile.ZipFile(base_apk) as z:
            extra["base.apk"]["resources.arsc"] = arsc_rename_package(z.read("resources.arsc"), old, package)
        for name, path in splits.items():
            with zipfile.ZipFile(path) as z:
                mf = z.read("AndroidManifest.xml")
            extra.setdefault(name, {})["AndroidManifest.xml"] = axml_replace_strings(mf, {old: package})
    return extra


def input_package(inputs: Inputs) -> str:
    """The input's package name, from the binary manifest of the single / base APK."""
    with zipfile.ZipFile(inputs.apk) as z:
        if inputs.bundle:
            with z.open("base.apk") as f, zipfile.ZipFile(f) as base:
                mf = base.read("AndroidManifest.xml")
        else:
            mf = z.read("AndroidManifest.xml")
    return axml_package(mf)


def axml_package(data: bytes) -> str:
    """The package attribute of a binary AndroidManifest.xml (<manifest package=...>)."""
    strings = axml_strings(data)
    pos = 8
    while pos < len(data):
        typ, hsize, size = struct.unpack_from("<HHI", data, pos)
        if typ == 0x0102:  # start element
            if strings[struct.unpack_from("<I", data, pos + 20)[0]] != "manifest":
                break
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, pos + 24)
            for i in range(attr_count):
                a = pos + 16 + attr_start + i * attr_size
                name_idx, raw = struct.unpack_from("<Ii", data, a + 4)
                if strings[name_idx] == "package" and raw >= 0:
                    return strings[raw]
            break
        pos += size
    raise PatchError("the manifest names no package")


def open_patch_file(patch_file, allow_unofficial: bool = False):
    """The PatchFile to build with: `patch_file` (a path or an opened PatchFile), else the newest one found
    (patchfile.find_default). An unofficial file (unsigned, unknown key, bad signature) needs `allow_unofficial`; a
    file made for a newer patcher is refused."""
    from arcane_patcher import patchfile

    if patch_file is None:
        patch_file = patchfile.find_default()
        if patch_file is None:
            raise PatchError("no patch file found (" + ", ".join(map(str, patchfile.search_dirs())) + "); download "
                             "the current arcane-patch-<version>.zip and put it next to the patcher, or give --patch")
    pf = patch_file if isinstance(patch_file, patchfile.PatchFile) else patchfile.PatchFile.open(Path(patch_file))
    if not pf.compatible():
        raise PatchError(f"patch file {pf.path.name} needs patcher {pf.info.get('min_patcher')} or newer (this is "
                         f"{patchfile.__version__}); download the current patcher")
    if not pf.official:
        if not allow_unofficial:
            raise PatchError(f"patch file {pf.path.name} is unofficial ({pf.trust}): it is not signed by the project's "
                             "release key. Use an official patch file, or --allow-unofficial if you trust its source "
                             "(it adds code to your app)")
        print(f"WARNING: unofficial patch file {pf.path.name} ({pf.trust}); use it only if you trust its source")
    return pf


def patch(apk: Path, out: Path, variant: Variant, keystore: Path, label: str | None = None, *,
          ks_alias: str = DEBUG_ALIAS, package: str | None = None, version_code: int | None = None,
          emulator_zip: Path | None = None, server_dir: Path | None = None, log_level: str | None = None,
          guest_login: bool = True, ad_rewards: bool = True, skip_rate_prompt: bool = True, store_timers: bool = True,
          skip_consent: bool = True, patch_file=None, allow_unofficial: bool = False,
          env: dict[str, str] | None = None) -> Path:
    """Build `variant` of the input `apk` into `out` (an .apk for a single APK, a directory of splits for a bundle)
    with the rules of a patch file (`patch_file`, default the newest found), signed with `keystore` (password:
    ARCANE_KS_PASS). Every check that needs only the inputs runs first; the output is written only when the whole
    build succeeded. Returns `out`."""
    from arcane_patcher import offline

    pf = open_patch_file(patch_file, allow_unofficial)
    spec = variant_spec(pf, variant)
    apk, out = Path(apk), Path(out)
    server = spec.loopback
    if spec.kind != "offline" and (emulator_zip or server_dir):
        raise PatchError("--emulator-zip / --server-dir are for the offline variant")
    if emulator_zip and server_dir:
        raise PatchError("--emulator-zip and --server-dir exclude each other")
    label = label or spec.label
    inputs = read_inputs(apk)
    build_id, rules = build_rules(inputs, pf)
    plan = options_plan(spec, guest_login, ad_rewards, skip_rate_prompt, store_timers, skip_consent)
    check_plan(plan, rules)
    if spec.kind == "offline" and inputs.bundle and EMBED_ABI_SPLIT not in inputs.members:
        raise PatchError(f"the offline variant needs the arm64 split ({EMBED_ABI_SPLIT}) in {apk}")
    pkg = output_package(input_package(inputs), spec, package)
    signer = KeystoreSigner(key_from_env(Path(keystore), ks_alias, env))
    if version_code is None:
        version_code = pf.version_code()
    if not ORIGINAL_VERSION_CODE < version_code < 1 << 31:
        raise PatchError(f"versionCode {version_code} must be above {ORIGINAL_VERSION_CODE} (the original's) and fit an int")

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        server_src = offline.server_source(tmp, emulator_zip, server_dir, pf) if spec.kind == "offline" else None
        print(f"{variant}: patch file {pf.version} ({pf.trust}), {rules.get('client', build_id.hex())}, package {pkg}, "
              f"label {label!r}, versionCode {version_code}")
        nsc = compile_nsc()
        common = dict(server=server, log_level=log_level, nsc=nsc, plan=plan, rules=rules, version_code=version_code,
                      min_sdk=spec.min_sdk)
        stage = tmp / "out"
        if not inputs.bundle:
            extra = customize(tmp, apk, {}, spec, server, pkg, label, plan, rules, server_src, pf)
            extra["base.apk"].update(extra.pop(EMBED_ABI_SPLIT, {}))  # the embedded runtime's native libraries
            built = stage / out.name
            if not repack(apk, built, signer, tmp, True, extra=extra["base.apk"], drop=SINGLE_APK_DROP, **common):
                raise PatchError("bootstrap_config not found in the APK")
        else:
            members = inputs.members
            if spec.kind == "offline":
                members = ["base.apk", EMBED_ABI_SPLIT]  # the game runs as arm64; the server runtimes go in its split
            src = tmp / "in"
            with zipfile.ZipFile(apk) as z:
                z.extractall(src, members)
            extra = customize(tmp, src / "base.apk", {n: src / n for n in members if n != "base.apk"}, spec,
                              server, pkg, label, plan, rules, server_src, pf)
            built = stage / "splits"
            for name in sorted(members):
                is_base = name == "base.apk"
                found = repack(src / name, built / name, signer, tmp, is_base, extra=extra.get(name), **common)
                if is_base and not found:
                    raise PatchError("bootstrap_config not found in base.apk")
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.is_dir():
            shutil.rmtree(out)
        elif out.exists():
            out.unlink()
        shutil.move(str(built), str(out))
    if inputs.bundle:
        print(f"wrote splits to {out} - install: adb install-multiple {out}/*.apk")
    else:
        print("wrote", out)
    return out


def _base_apk(inputs: Inputs, tmp: Path) -> Path:
    if not inputs.bundle:
        return inputs.apk
    dst = tmp / "base-check" / "base.apk"
    if not dst.exists():
        with zipfile.ZipFile(inputs.apk) as z:
            z.extract("base.apk", dst.parent)
    return dst
