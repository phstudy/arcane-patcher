"""Generate src/arcane_patcher/android_attrs.json: the framework's public attributes and resources.

    uv run python tools/gen_android_attrs.py [--sdk SDK] [--api N]

Reads the resource table (resources.arsc) inside <sdk>/platforms/android-<newest>/android.jar: the same table aapt2
links against. For every public attribute: its id, formats, and enum / flag values (names from the framework's id
entries); for every other public resource (style, drawable, color, ...): its id. When the platform has its resource
sources (data/res/values/public-final.xml, attrs*.xml), every public attribute and style id listed there and every
attribute format declared there is checked against the table first; a mismatch stops the generator.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from arcane_patcher import axml
from arcane_patcher.sdktools import android_sdk

OUT = Path(__file__).resolve().parent.parent / "src/arcane_patcher/android_attrs.json"
ATTR_TYPE, ATTR_MIN, ATTR_MAX, ATTR_L10N = 0x01000000, 0x01000001, 0x01000002, 0x01000003


def platform(sdk: Path, api: int | None) -> Path:
    found = sorted((int(p.name.split("-")[1]), p) for p in (sdk / "platforms").glob("android-*")
                   if p.name.split("-")[1].isdigit() and (p / "android.jar").is_file())
    if api is not None:
        found = [f for f in found if f[0] == api]
    if not found:
        raise SystemExit(f"no platforms/android-*/android.jar under {sdk}")
    return found[-1][1]


def read_table(jar: Path) -> dict:
    with zipfile.ZipFile(jar) as z:
        arsc = z.read("resources.arsc")
    entries: dict[int, axml.ArscEntry] = {}
    for e in axml.arsc_entries(arsc):
        entries.setdefault(e.id, e)  # the first configuration (the default one) of each id
    attrs, resources = {}, {}
    for e in sorted(entries.values(), key=lambda e: e.id):
        if not e.public:
            continue
        # a name can have a finalized id and a staging id (newer platforms): the lowest one is the finalized one
        if e.type != "attr":
            resources.setdefault(e.type, {}).setdefault(e.key, f"0x{e.id:08x}")
            continue
        if e.key in attrs:
            continue
        mask, values = 0, {}
        for name, _vt, data in e.bag or ():
            if name == ATTR_TYPE:
                mask = data
            elif name not in (ATTR_MIN, ATTR_MAX, ATTR_L10N):
                values[entries[name].key] = data
        formats = "|".join(n for n, b in axml.FORMATS.items() if mask & b)
        a: dict = {"id": f"0x{e.id:08x}", "format": formats}
        if values:
            a["flags" if mask & axml.FORMATS["flags"] else "enum"] = values
        attrs[e.key] = a
    return {"attrs": attrs, "resources": resources}


def check_sources(values: Path, data: dict) -> int:
    """Compare the table with the platform's resource sources; returns the number of ids checked."""
    public = values / "public-final.xml"
    checked = 0
    if public.is_file():
        for typ, name, rid in re.findall(r'<public type="(\w+)" name="([\w.]+)" id="(0x[0-9a-f]+)"',
                                         public.read_text()):
            if name.startswith(("removed_", "__removed")) or typ not in ("attr", "style"):
                continue
            have = data["attrs"].get(name, {}).get("id") if typ == "attr" else data["resources"]["style"].get(name)
            if have is None or int(have, 16) != int(rid, 16):
                raise SystemExit(f"{public.name}: {typ}/{name} is {rid}, the table has {have}")
            checked += 1
    for src in sorted(values.glob("attrs*.xml")):
        for name, formats in re.findall(r'<attr name="(\w+)" format="([\w|]+)"', src.read_text()):
            a = data["attrs"].get(name)
            if a and (axml.format_mask(formats) & ~axml.format_mask(a["format"])):
                raise SystemExit(f"{src.name}: attr {name} format {formats}, the table has {a['format']}")
    return checked


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sdk", type=Path, default=None)
    ap.add_argument("--api", type=int, default=None)
    args = ap.parse_args()
    plat = platform(args.sdk or android_sdk(), args.api)
    data = read_table(plat / "android.jar")
    values = plat / "data/res/values"
    checked = check_sources(values, data) if values.is_dir() else 0
    out = {"source": f"{plat.name} android.jar resources.arsc (public entries)", **data}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=False) + "\n")
    print(f"{OUT.name}: {len(data['attrs'])} attrs, "
          f"{sum(len(v) for v in data['resources'].values())} other resources from {plat.name}"
          f"; {checked} ids checked against public-final.xml")


if __name__ == "__main__":
    main()
