"""Android binary XML (AndroidManifest.xml, res/xml/*.xml) to text and back, in pure Python.

decode(data) -> text         binary XML to the text dialect below
encode(text, resolve) -> data
                             text (the dialect, or plain hand-written XML) to binary XML as aapt2 links it: string
                             pool, resource map for the android attribute names, namespace chunks, attributes sorted by
                             resource id, values typed by the attribute's declared formats
compile_xml(text) -> data    a resource XML file (res/xml/...) as aapt2 compiles it (UTF-8 string pool)
arsc_resource_id(arsc, type, entry) -> id
                             a resource of the APK's resources.arsc by name; arsc_resolver(arsc) wraps it as the
                             `resolve` callback of encode

The android attribute ids, formats and enum / flag values, and the framework's public resource ids, come from
android_attrs.json, generated from the Android SDK by tools/gen_android_attrs.py.

Text dialect (what decode writes; encode reads it and plain XML)
----------------------------------------------------------------
Laid out like apktool's decoded manifest, so text edits written for that work unchanged:
`<?xml version="1.0" encoding="utf-8" standalone="no"?>` and the root element on line 1, the namespace declarations
(`xmlns:android="..."`) on the element that declared them, all attributes of an element on one line as
` prefix:name="value"`, an element without children closed with `/>`, children indented by four spaces. Every start
and end tag sits on the line number the binary file records for it (blank lines fill the gaps), so a round trip keeps
the line numbers; `<uses-sdk>` stays in the text (apktool moves it out). Comments are not kept (aapt2 drops them).

Attribute values (XML-escaped; `\n`, `\r` and `\t` as character references):
  text                     a string. A string that would read as something else (`true`, `12`, `@...`, `%...`) or
                           that starts with a backslash is written with one more backslash in front: `\true`.
  \text                    always the string `text`.
  @0x7f120070              a resource reference by id; `?0x01010036` an attribute reference by id.
  @null, @empty            the null reference (reference 0) and the empty value (null type, data 1).
  @type/name               a resource of the app by name, resolved through encode's `resolve` callback, which gets
                           the reference as written ("@string/app_name"); decode writes it when given the APK's
                           resources.arsc (else the id form). `@android:type/name` (and `?android:attr/name`,
                           `?android:name`): a public framework resource from the table (decode names them always).
  true, 12, 0x10, #ff0000, 1.5, 5dp, 50%, name, a|b
                           a typed value read as aapt2 reads it for the attribute's declared formats: color, boolean,
                           integer (decimal: int_dec; 0x...: int_hex), float / dimension / fraction, enum symbol
                           (int_dec), flag symbols joined by "|" (int_hex); an attribute without a declaration (no
                           namespace, as `package`) is read as any format. decode writes this form only when encode
                           reads it back to the identical type and data.
  %type:data               any other typed value, exactly: `type` one of TYPE_NAMES (or 0xNN), `data` an int
                           (decimal or 0x...), e.g. `%int_dec:33`, `%int_boolean:0x00000001`, `%dimension:0x00000501`.

What the dialect does not keep: comments, the raw-value string of typed attributes (aapt2 writes none; strings keep
theirs), end-tag line numbers of `/>` elements, and string-pool order. decode refuses what it could not write back
the same: an android attribute whose resource id differs from the table's or that the table lacks, a resource id on
an attribute of another namespace.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from xml.parsers import expat

ANDROID_NS = "http://schemas.android.com/apk/res/android"
TOOLS_NS = "http://schemas.android.com/tools"  # design-time attributes; aapt2 leaves them out
ATTRS_JSON = Path(__file__).resolve().parent / "android_attrs.json"
XML_DECLARATION = '<?xml version="1.0" encoding="utf-8" standalone="no"?>'


class AxmlError(ValueError):
    """Binary XML that cannot be decoded, or text that cannot be encoded (with the reason)."""


# --- chunk and value constants (ResourceTypes.h) ------------------------------------------------------------------

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_XML = 0x0003
XML_START_NAMESPACE = 0x0100
XML_END_NAMESPACE = 0x0101
XML_START_ELEMENT = 0x0102
XML_END_ELEMENT = 0x0103
XML_CDATA = 0x0104
XML_RESOURCE_MAP = 0x0180
TABLE_PACKAGE = 0x0200
TABLE_TYPE = 0x0201
TABLE_TYPE_SPEC = 0x0202
UTF8_FLAG = 0x100
NO_INDEX = 0xFFFFFFFF
ID_ATTR = 0x010100D0  # android:id (the element's idIndex)

T_NULL, T_REFERENCE, T_ATTRIBUTE, T_STRING, T_FLOAT, T_DIMENSION, T_FRACTION = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
T_INT_DEC, T_INT_HEX, T_INT_BOOLEAN = 0x10, 0x11, 0x12
T_ARGB8, T_RGB8, T_ARGB4, T_RGB4 = 0x1C, 0x1D, 0x1E, 0x1F
TYPE_NAMES = {
    0x00: "null", 0x01: "reference", 0x02: "attribute", 0x03: "string", 0x04: "float", 0x05: "dimension",
    0x06: "fraction", 0x07: "dynamic_reference", 0x08: "dynamic_attribute", 0x10: "int_dec", 0x11: "int_hex",
    0x12: "int_boolean", 0x1C: "color_argb8", 0x1D: "color_rgb8", 0x1E: "color_argb4", 0x1F: "color_rgb4",
}
TYPE_IDS = {v: k for k, v in TYPE_NAMES.items()}

# attribute formats (ResTable_map::TYPE_*), as attrs.xml names them
FORMATS = {"reference": 0x1, "string": 0x2, "integer": 0x4, "boolean": 0x8, "color": 0x10, "float": 0x20,
           "dimension": 0x40, "fraction": 0x80, "enum": 0x10000, "flags": 0x20000}
ANY_FORMAT = 0xFFFF  # every primitive format (no enum / flags)
_FORMAT_OF_TYPE = {T_FLOAT: FORMATS["float"], T_DIMENSION: FORMATS["dimension"], T_FRACTION: FORMATS["fraction"]}

DIMENSION_UNITS = {"px": 0, "dip": 1, "dp": 1, "sp": 2, "pt": 3, "in": 4, "mm": 5}
FRACTION_UNITS = {"%": 0, "%p": 1}
_UNIT_NAMES = {T_DIMENSION: ["px", "dp", "sp", "pt", "in", "mm"], T_FRACTION: ["%", "%p"]}
_RADIX_SCALE = (1.0, 1.0 / (1 << 7), 1.0 / (1 << 15), 1.0 / (1 << 23))  # COMPLEX_RADIX_23p0, 16p7, 8p15, 0p23


# --- the android attribute table --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Attr:
    name: str
    id: int
    formats: int  # FORMATS bits
    enum: dict[str, int] = field(default_factory=dict)
    flags: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Table:
    attrs: dict[str, Attr]
    by_id: dict[int, Attr]
    resources: dict[str, dict[str, int]]  # public framework resources: type -> name -> id
    names: dict[int, str]  # id -> "android:type/name" (attributes and public resources)


def format_mask(formats: str) -> int:
    """FORMATS bits of a "string|integer" style format list."""
    return 0 if not formats else sum(FORMATS[f] for f in formats.split("|"))


@cache
def table() -> Table:
    raw = json.loads(ATTRS_JSON.read_text())
    attrs = {name: Attr(name, int(a["id"], 16), format_mask(a["format"]), a.get("enum", {}), a.get("flags", {}))
             for name, a in raw["attrs"].items()}
    resources = {t: {n: int(i, 16) for n, i in entries.items()} for t, entries in raw["resources"].items()}
    names = {i: f"android:{t}/{n}" for t, entries in resources.items() for n, i in entries.items()}
    names.update({a.id: f"android:attr/{a.name}" for a in attrs.values()})
    return Table(attrs, {a.id: a for a in attrs.values()}, resources, names)


# --- string pools -----------------------------------------------------------------------------------------------

def read_pool(data: bytes, off: int) -> list[str]:
    """The strings of the string pool chunk at `off` (styles are ignored)."""
    typ, hsize, _size, count, _styles, flags, strings_start = struct.unpack_from("<HHIIIII", data, off)
    if typ != RES_STRING_POOL:
        raise AxmlError(f"string pool expected at {off:#x}")
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from(f"<{count}I", data, off + hsize)
    base = off + strings_start
    out = []
    for o in offsets:
        p = base + o
        if utf8:
            p += 2 if data[p] & 0x80 else 1  # length in UTF-16 units (unused)
            n = data[p]
            if n & 0x80:
                n, p = ((n & 0x7F) << 8) | data[p + 1], p + 2
            else:
                p += 1
            out.append(data[p:p + n].decode("utf-8", "surrogatepass"))
        else:
            n = struct.unpack_from("<H", data, p)[0]
            p += 2
            if n & 0x8000:
                n, p = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", data, p)[0], p + 2
            out.append(data[p:p + 2 * n].decode("utf-16-le", "surrogatepass"))
    return out


def build_pool(strings: list[str], utf8: bool) -> bytes:
    """A string pool chunk holding `strings` in order."""
    blob, offsets = bytearray(), []
    for s in strings:
        offsets.append(len(blob))
        u16 = len(s.encode("utf-16-le", "surrogatepass")) // 2
        if utf8:
            b = s.encode("utf-8", "surrogatepass")
            if u16 > 0x7FFF or len(b) > 0x7FFF:
                raise AxmlError(f"string too long for a UTF-8 pool ({len(b)} bytes)")
            for n in (u16, len(b)):
                blob += bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])
            blob += b + b"\0"
        else:
            blob += struct.pack("<H", u16) if u16 < 0x8000 else struct.pack("<HH", 0x8000 | (u16 >> 16), u16 & 0xFFFF)
            blob += s.encode("utf-16-le", "surrogatepass") + b"\0\0"
    blob += b"\0" * (-len(blob) % 4)
    hsize = 28
    strings_start = hsize + 4 * len(strings)
    size = strings_start + len(blob)
    return (struct.pack("<HHIIIIII", RES_STRING_POOL, hsize, size, len(strings), 0, UTF8_FLAG if utf8 else 0,
                        strings_start, 0)
            + struct.pack(f"<{len(strings)}I", *offsets) + bytes(blob))


# --- values: text -> (type, data) -------------------------------------------------------------------------------

def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _f32_bits(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


def _complex(f: float, unit: int) -> int:
    """floatToComplex of the platform: a dimension / fraction value in the 24-bit mantissa + radix encoding."""
    neg = f < 0
    if neg:
        f = -f
    bits = int(_f32(_f32(f * (1 << 23)) + 0.5))
    if bits & 0x7FFFFF == 0:
        radix, shift = 0, 23
    elif bits & ~0x7FFFFF == 0:
        radix, shift = 3, 0
    elif bits & ~0x7FFFFFFF == 0:
        radix, shift = 2, 8
    elif bits & ~0x7FFFFFFFFF == 0:
        radix, shift = 1, 16
    else:
        radix, shift = 0, 23
    mantissa = (bits >> shift) & 0xFFFFFF
    if neg:
        mantissa = -mantissa & 0xFFFFFF
    return unit | radix << 4 | mantissa << 8


_INT = re.compile(r"(-?)(0x[0-9a-fA-F]+|[0-9]+)")
_FLOAT = re.compile(r"([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)([a-z%]*)")
_COLOR = re.compile(r"#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_HEX_REF = re.compile(r"([@?])0x([0-9a-fA-F]{8})")
_NAME_REF = re.compile(r"([@?])\*?(?:([\w.]+):)?(?:([\w-]+)/)?([\w.$-]+)")
_TOKEN = re.compile(r"%(\w+):(-?(?:0x[0-9a-fA-F]+|[0-9]+))")


def _parse_int(s: str) -> tuple[int, int] | None:
    m = _INT.fullmatch(s)
    if not m:
        return None
    v = int(m.group(2), 0)
    if m.group(2).startswith("0x"):
        if v > 0xFFFFFFFF:
            return None
        return T_INT_HEX, (-v if m.group(1) else v) & 0xFFFFFFFF
    v = -v if m.group(1) else v
    if not -(1 << 31) <= v < 1 << 31:
        return None
    return T_INT_DEC, v & 0xFFFFFFFF


def _parse_float(s: str) -> tuple[int, int] | None:
    m = _FLOAT.fullmatch(s)
    if not m:
        return None
    f, unit = _f32(float(m.group(1))), m.group(2)
    if not unit:
        return T_FLOAT, _f32_bits(f)
    if unit in DIMENSION_UNITS:
        return T_DIMENSION, _complex(f, DIMENSION_UNITS[unit])
    if unit in FRACTION_UNITS:
        return T_FRACTION, _complex(_f32(f * _f32(1.0 / 100)), FRACTION_UNITS[unit])
    return None


def _parse_color(s: str) -> tuple[int, int] | None:
    m = _COLOR.fullmatch(s)
    if not m:
        return None
    h = m.group(1)
    if len(h) <= 4:
        nib = [int(c, 16) * 0x11 for c in h]
        a, (r, g, b) = (nib[0], nib[1:]) if len(h) == 4 else (0xFF, nib)
        return (T_ARGB4 if len(h) == 4 else T_RGB4), a << 24 | r << 16 | g << 8 | b
    v = int(h, 16)
    return (T_ARGB8, v) if len(h) == 8 else (T_RGB8, 0xFF000000 | v)


def _parse_reference(s: str, resolve: Callable[[str], int] | None) -> tuple[int, int]:
    m = _HEX_REF.fullmatch(s)
    if m:
        return (T_REFERENCE if m.group(1) == "@" else T_ATTRIBUTE), int(m.group(2), 16)
    m = _NAME_REF.fullmatch(s)
    if not m or (m.group(1) == "@" and not m.group(3)):
        raise AxmlError(f"not a resource reference: {s!r} (write a string starting with @ or ? as \\{s})")
    kind, package, rtype, name = m.groups()
    typ = T_REFERENCE if kind == "@" else T_ATTRIBUTE
    rtype = rtype or "attr"
    if package == "android":
        t = table()
        rid = t.attrs[name].id if rtype == "attr" and name in t.attrs else t.resources.get(rtype, {}).get(name)
        if rid is None:
            raise AxmlError(f"unknown public framework resource {s!r}")
        return typ, rid
    if rtype.startswith("+"):
        raise AxmlError(f"new ids are not supported: {s!r}")
    if resolve is None:
        raise AxmlError(f"app resource {s!r} needs a resolver (encode(..., resolve=arsc_resolver(arsc)))")
    return typ, resolve(s)


def _parse_token(s: str) -> tuple[int, int]:
    m = _TOKEN.fullmatch(s)
    if not m:
        raise AxmlError(f"bad typed value {s!r} (expected %type:data, e.g. %int_dec:12)")
    name, data = m.groups()
    typ = TYPE_IDS.get(name)
    if typ is None:
        if not re.fullmatch(r"0x[0-9a-fA-F]{1,2}", name):
            raise AxmlError(f"unknown value type {name!r} in {s!r}")
        typ = int(name, 16)
    if typ == T_STRING:
        raise AxmlError(f"{s!r}: write strings as text")
    return typ, int(data, 0) & 0xFFFFFFFF


def compile_value(attr: Attr | None, value: str, resolve: Callable[[str], int] | None = None,
                  where: str = "") -> tuple[int, int | str]:
    """(type, data) of an attribute value; data is the string itself for T_STRING. `attr` None = an attribute
    without a declaration (read as any format). Follows aapt2: null / empty, reference, then by the declared
    formats color, boolean, integer, float / dimension / fraction, enum, flags, and string last."""
    if value.startswith("\\"):
        return T_STRING, value[1:]
    if value.startswith("%"):
        return _parse_token(value)
    s = value.strip()
    if s == "@null":
        return T_REFERENCE, 0
    if s == "@empty":
        return T_NULL, 1
    if s.startswith(("@", "?")):
        return _parse_reference(s, resolve)
    mask = attr.formats if attr else ANY_FORMAT
    if mask & FORMATS["color"] and (v := _parse_color(s)):
        return v
    if mask & FORMATS["boolean"] and s in ("true", "TRUE", "True", "false", "FALSE", "False"):
        return T_INT_BOOLEAN, 0xFFFFFFFF if s[0] in "tT" else 0
    if mask & FORMATS["integer"] and (v := _parse_int(s)):
        return v
    if mask & (FORMATS["float"] | FORMATS["dimension"] | FORMATS["fraction"]) and (v := _parse_float(s)) \
            and mask & _FORMAT_OF_TYPE[v[0]]:
        return v
    if attr and mask & FORMATS["enum"] and s in attr.enum:
        return T_INT_DEC, attr.enum[s] & 0xFFFFFFFF
    if attr and mask & FORMATS["flags"] and s:
        parts = [p.strip() for p in s.split("|")]
        if all(p in attr.flags for p in parts):
            v = 0
            for p in parts:
                v |= attr.flags[p]
            return T_INT_HEX, v & 0xFFFFFFFF
    if mask & FORMATS["string"]:
        return T_STRING, value
    allowed = "|".join(n for n, b in FORMATS.items() if mask & b)
    raise AxmlError(f"{where}android:{attr.name}=\"{value}\": not a valid {allowed} value"
                    + (f" (enum: {', '.join(attr.enum)})" if attr.enum else "")
                    + (f" (flags: {', '.join(attr.flags)})" if attr.flags else ""))


# --- values: (type, data) -> text -------------------------------------------------------------------------------

def _short_float(f: float, ok: Callable[[str], bool]) -> str | None:
    plain = [f"{f:.{d}f}".rstrip("0").rstrip(".") if d else f"{f:.0f}" for d in range(13)]
    for s in plain + [f"{f:.{p}g}" for p in range(1, 12)]:
        for c in (s, s + ".0") if "." not in s and "e" not in s else (s,):
            if ok(c):
                return c
    return None


def _flag_names(attr: Attr, data: int) -> str | None:
    if data == 0:
        zero = [n for n, v in attr.flags.items() if v == 0]
        return zero[0] if zero else None
    names, covered = [], 0
    # widest masks first (a combined flag like "keyboard|keyboardHidden" groups stay readable), then by value
    for n, v in sorted(attr.flags.items(), key=lambda kv: (-kv[1].bit_count(), kv[1])):
        if v and v & data == v and v & ~covered:
            names.append(n)
            covered |= v
    return "|".join(names) if covered == data else None


def value_text(attr: Attr | None, typ: int, data: int, string: str | None = None,
               arsc_names: ArscNames | None = None) -> str:
    """The dialect's text for a typed value (the shortest form that compile_value reads back identically). A
    reference is named when the framework table (public resources) or `arsc_names` (the app's) knows its id."""
    resolve = arsc_names.resolve if arsc_names else None

    def same(c: str) -> bool:
        try:
            return compile_value(attr, c, resolve) == (typ, string if typ == T_STRING else data)
        except AxmlError:
            return False

    if typ == T_STRING:
        return string if same(string) and not string.startswith("\\") else "\\" + string
    candidates: list[str | None] = []
    if typ in (T_REFERENCE, T_ATTRIBUTE):
        kind = "@" if typ == T_REFERENCE else "?"
        if typ == T_REFERENCE and data == 0:
            candidates.append("@null")
        named = table().names.get(data) or (arsc_names.names.get(data) if arsc_names else None)
        if named:
            candidates.append(kind + named)
        candidates.append(f"{kind}0x{data:08x}")
    elif typ == T_NULL and data == 1:
        candidates.append("@empty")
    elif typ == T_INT_BOOLEAN:
        candidates.append({0: "false", 0xFFFFFFFF: "true"}.get(data))
    elif typ == T_INT_DEC:
        signed = data - (1 << 32) if data & 0x80000000 else data
        if attr:
            candidates += [n for n, v in attr.enum.items() if v & 0xFFFFFFFF == data][:1]
        candidates.append(str(signed))
    elif typ == T_INT_HEX:
        if attr and attr.flags:
            candidates.append(_flag_names(attr, data))
        candidates.append(f"0x{data:08x}")
    elif typ in (T_ARGB8, T_RGB8):
        candidates.append(f"#{data:08x}" if typ == T_ARGB8 else f"#{data & 0xFFFFFF:06x}")
    elif typ in (T_ARGB4, T_RGB4):
        nib = "".join(f"{(data >> s) & 0xF:x}" for s in (24, 16, 8, 0))
        candidates.append(f"#{nib}" if typ == T_ARGB4 else f"#{nib[1:]}")
    elif typ == T_FLOAT:
        candidates.append(_short_float(struct.unpack("<f", struct.pack("<I", data))[0], same))
    elif typ in (T_DIMENSION, T_FRACTION):
        units = _UNIT_NAMES[typ]
        if (data & 0xF) < len(units):
            mantissa = (data >> 8) - (1 << 24) if data & 0x80000000 else data >> 8
            f = mantissa * _RADIX_SCALE[(data >> 4) & 3] * (100 if typ == T_FRACTION else 1)
            unit = units[data & 0xF]
            num = _short_float(f, lambda c: same(c + unit))
            candidates.append(num + unit if num is not None else None)
    for c in candidates:
        if c is not None and same(c):
            return c
    name = TYPE_NAMES.get(typ, f"0x{typ:02x}")
    return f"%{name}:{data - (1 << 32) if typ == T_INT_DEC and data & 0x80000000 else data}" if typ == T_INT_DEC \
        else f"%{name}:0x{data:08x}"


def _escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;"))


def _escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- decode -----------------------------------------------------------------------------------------------------

def _chunks(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int]]:
    """(type, header size, size, offset) of the chunks in data[start:end]."""
    off = start
    while off + 8 <= end:
        typ, hsize, size = struct.unpack_from("<HHI", data, off)
        if size < 8 or off + size > end:
            raise AxmlError(f"bad chunk at {off:#x}")
        yield typ, hsize, size, off
        off += size


def decode(data: bytes, arsc: bytes | None = None) -> str:
    """Binary XML to the text dialect (module docstring). With the APK's resources.arsc, references to the app's
    resources are written by name ("@string/app_name", as apktool writes them; encode then needs
    arsc_resolver(arsc)); without it as ids ("@0x7f120070")."""
    arsc_names = ArscNames.of(arsc) if arsc is not None else None
    if len(data) < 8 or struct.unpack_from("<HH", data, 0) != (RES_XML, 8):
        raise AxmlError("not a binary XML file")
    t = table()
    strings: list[str] = []
    res_map: list[int] = []
    events: list[tuple] = []
    for typ, hsize, size, off in _chunks(data, 8, min(len(data), struct.unpack_from("<I", data, 4)[0])):
        if typ == RES_STRING_POOL:
            strings = read_pool(data, off)
        elif typ == XML_RESOURCE_MAP:
            res_map = list(struct.unpack_from(f"<{(size - hsize) // 4}I", data, off + hsize))
        elif typ in (XML_START_NAMESPACE, XML_END_NAMESPACE, XML_START_ELEMENT, XML_END_ELEMENT, XML_CDATA):
            line = struct.unpack_from("<I", data, off + 8)[0]
            body = off + hsize
            if typ in (XML_START_NAMESPACE, XML_END_NAMESPACE):
                prefix, uri = struct.unpack_from("<II", data, body)
                events.append((typ, line, strings[prefix] if prefix != NO_INDEX else "", strings[uri]))
            elif typ == XML_END_ELEMENT:
                ns, name = struct.unpack_from("<II", data, body)
                events.append((typ, line, ns, name))
            elif typ == XML_CDATA:
                events.append((typ, line, strings[struct.unpack_from("<I", data, body)[0]]))
            else:
                ns, name, a_start, a_size, count = struct.unpack_from("<IIHHH", data, body)
                attrs = []
                for i in range(count):
                    a = body + a_start + i * a_size
                    a_ns, a_name, _raw, _vsize, _res0, vtype, vdata = struct.unpack_from("<IIIHBBI", data, a)
                    rid = res_map[a_name] if a_name < len(res_map) else 0
                    attrs.append((a_ns, a_name, rid, vtype, vdata))
                events.append((typ, line, ns, name, attrs))

    def s(i: int) -> str:
        return strings[i] if i != NO_INDEX else ""

    out = [XML_DECLARATION]
    cur = 1
    depth = 0
    prefixes: dict[str, list[str]] = {}
    pending_ns: list[tuple[str, str]] = []

    after_text = False  # the tag after text follows it directly (whitespace there would become text)

    def place(line: int) -> None:
        nonlocal cur, after_text
        if line > cur and not after_text:
            out.append("\n" * (line - cur) + "    " * depth)
            cur = line
        after_text = False

    def qname(ns: int, name: str) -> str:
        if ns == NO_INDEX:
            return name
        uri = s(ns)
        if not prefixes.get(uri):
            raise AxmlError(f"namespace {uri!r} of {name!r} has no prefix")
        return f"{prefixes[uri][-1]}:{name}"

    for i, ev in enumerate(events):
        typ, line = ev[0], ev[1]
        if typ == XML_START_NAMESPACE:
            prefixes.setdefault(ev[3], []).append(ev[2])
            pending_ns.append((ev[2], ev[3]))
        elif typ == XML_END_NAMESPACE:
            if prefixes.get(ev[3]):
                prefixes[ev[3]].pop()
        elif typ == XML_START_ELEMENT:
            _, _, ns, name, attrs = ev
            tag = qname(ns, s(name))
            parts = [f"<{tag}"] + [f' xmlns:{p}="{_escape(u)}"' if p else f' xmlns="{_escape(u)}"'
                                   for p, u in pending_ns]
            pending_ns = []
            for a_ns, a_name, rid, vtype, vdata in attrs:
                name_s = s(a_name)
                uri = s(a_ns)
                attr = None
                if uri == ANDROID_NS:
                    attr = t.attrs.get(name_s) or (t.by_id.get(rid) if not name_s else None)
                    if attr is None:
                        raise AxmlError(f"<{tag}> android:{name_s} ({rid:#010x}): not in the attribute table")
                    if rid != attr.id:
                        raise AxmlError(f"<{tag}> android:{attr.name}: resource id {rid:#010x}, the table has "
                                        f"{attr.id:#010x}")
                    name_s = attr.name
                elif rid:
                    raise AxmlError(f"<{tag}> {uri}:{name_s}: attributes of other namespaces with resource ids are "
                                    "not supported")
                if vtype == T_STRING:
                    text = value_text(attr, vtype, vdata, s(vdata), arsc_names)
                else:
                    text = value_text(attr, vtype, vdata, None, arsc_names)
                parts.append(f' {qname(a_ns, name_s)}="{_escape(text)}"')
            place(line)
            leaf = i + 1 < len(events) and events[i + 1][0] == XML_END_ELEMENT
            out.append("".join(parts) + ("/>" if leaf else ">"))
            if not leaf:
                depth += 1
        elif typ == XML_END_ELEMENT:
            _, _, ns, name = ev
            if i > 0 and events[i - 1][0] == XML_START_ELEMENT:
                continue  # closed with "/>"
            depth -= 1
            # on its own line when the next tag's line leaves room (aapt2 records the start tag's line here)
            nxt = next((e[1] for e in events[i + 1:] if e[0] in (XML_START_ELEMENT, XML_CDATA)), None)
            if line > cur or nxt is None or nxt > cur + 1:
                place(max(line, cur + 1))
            out.append(f"</{qname(ns, s(name))}>")
        elif typ == XML_CDATA and ev[2].strip():
            place(line)
            out.append(_escape_text(ev[2]))
            cur += ev[2].count("\n")
            after_text = True
    return "".join(out) + "\n"


# --- encode -----------------------------------------------------------------------------------------------------

@dataclass
class _Node:
    kind: int
    line: int
    a: str = ""  # namespace uri (elements) / prefix (namespaces) / text
    b: str = ""  # name (elements) / uri (namespaces)
    attrs: list[tuple[str, str, int, int, int | str]] = field(default_factory=list)  # ns, name, id, type, data


def _parse_text(text: str, resolve: Callable[[str], int] | None) -> list[_Node]:
    t = table()
    nodes: list[_Node] = []
    p = expat.ParserCreate("utf-8", namespace_separator=" ")
    p.ordered_attributes = True
    p.buffer_text = True
    text_buf: list[str] = []
    text_line = [0]
    open_lines: list[int] = []

    def split(qn: str) -> tuple[str, str]:
        return tuple(qn.split(" ", 1)) if " " in qn else ("", qn)

    def flush() -> None:
        if text_buf:
            txt = "".join(text_buf)
            if txt.strip():
                nodes.append(_Node(XML_CDATA, text_line[0], txt))
            text_buf.clear()

    def start(qn: str, attrs: list[str]) -> None:
        flush()
        ns, name = split(qn)
        where = f"line {p.CurrentLineNumber} <{name}> "
        out = []
        for k, v in zip(attrs[::2], attrs[1::2]):
            a_ns, a_name = split(k)
            if a_ns == TOOLS_NS:
                continue
            attr = None
            if a_ns == ANDROID_NS:
                attr = t.attrs.get(a_name)
                if attr is None:
                    raise AxmlError(f"{where}android:{a_name}: unknown android attribute")
            try:
                typ, data = compile_value(attr, v, resolve, where)
            except AxmlError as e:
                raise AxmlError(str(e) if str(e).startswith("line ") else f"{where}{a_name}=\"{v}\": {e}") from None
            out.append((a_ns, a_name, attr.id if attr else 0, typ, data))
        nodes.append(_Node(XML_START_ELEMENT, p.CurrentLineNumber, ns, name, out))
        open_lines.append(p.CurrentLineNumber)

    def end(qn: str) -> None:
        flush()
        ns, name = split(qn)
        # aapt2 gives an end tag the line of its start tag
        nodes.append(_Node(XML_END_ELEMENT, open_lines.pop(), ns, name))

    def chars(data: str) -> None:
        if not text_buf:
            text_line[0] = p.CurrentLineNumber
        text_buf.append(data)

    p.StartElementHandler = start
    p.EndElementHandler = end
    p.CharacterDataHandler = chars
    p.StartNamespaceDeclHandler = lambda prefix, uri: nodes.append(
        _Node(XML_START_NAMESPACE, p.CurrentLineNumber, prefix or "", uri))
    p.EndNamespaceDeclHandler = lambda prefix: nodes.append(_Node(XML_END_NAMESPACE, p.CurrentLineNumber, prefix or ""))
    try:
        p.Parse(text.encode("utf-8"), True)
    except expat.ExpatError as e:
        raise AxmlError(f"not well-formed XML: {e}") from None
    # an end-namespace event names only the prefix: pair it with its start
    open_ns: list[_Node] = []
    for n in nodes:
        if n.kind == XML_START_NAMESPACE:
            open_ns.append(n)
        elif n.kind == XML_END_NAMESPACE:
            for j in range(len(open_ns) - 1, -1, -1):
                if open_ns[j].a == n.a:
                    n.b, n.line = open_ns[j].b, open_ns[j].line
                    open_ns.pop(j)
                    break
    return nodes


def _attr_order(a: tuple) -> tuple:
    """aapt2's attribute order: attributes with a resource id by id, then the others by namespace and name."""
    ns, name, rid = a[0], a[1], a[2]
    return (0, rid, "", "") if rid else (1, 0, ns, name)


def encode(text: str, resolve: Callable[[str], int] | None = None, *, utf8: bool = False) -> bytes:
    """Text XML (the dialect of the module docstring, or plain XML) to binary XML as aapt2 links it. `resolve`
    turns an app resource reference ("@string/app_name") into its id (arsc_resolver). The string pool is UTF-16 by
    default (as aapt2 writes AndroidManifest.xml); `utf8` for resource XML files."""
    nodes = _parse_text(text, resolve)
    # string pool: attribute names with resource ids first, in id order (the resource map covers them), then the rest
    # string pool as aapt2 orders it: attribute names with resource ids first, by id (the resource map covers
    # them), then the other strings sorted
    named = sorted({(a[2], a[1]) for n in nodes if n.kind == XML_START_ELEMENT for a in n.attrs if a[2]})
    used: set[str] = set()

    def collect(sv: str | None, rid: int = 0) -> int:
        if sv is not None and not rid:
            used.add(sv)
        return 0

    _body(nodes, collect)
    pool = [name for _, name in named] + sorted(used)
    index = {(rid, name): i for i, (rid, name) in enumerate(named)}
    index.update({(0, sv): i for i, sv in enumerate(sorted(used), len(named))})

    def ref(sv: str | None, rid: int = 0) -> int:
        return NO_INDEX if sv is None else index[(rid, sv)]

    body = _body(nodes, ref)
    pool_chunk = build_pool(pool, utf8)
    res_map = struct.pack(f"<HHI{len(named)}I", XML_RESOURCE_MAP, 8, 8 + 4 * len(named), *(r for r, _ in named))
    size = 8 + len(pool_chunk) + len(res_map) + len(body)
    return struct.pack("<HHI", RES_XML, 8, size) + pool_chunk + res_map + body


def _body(nodes: list[_Node], ref: Callable[..., int]) -> bytes:
    """The node chunks; `ref(string, resource id)` gives a string's pool index."""
    body = bytearray()
    for n in nodes:
        def head(kind: int, size: int, line: int = n.line) -> bytes:
            return struct.pack("<HHIII", kind, 16, size, line, NO_INDEX)
        if n.kind in (XML_START_NAMESPACE, XML_END_NAMESPACE):
            body += head(n.kind, 24) + struct.pack("<II", ref(n.a) if n.a else NO_INDEX, ref(n.b))
        elif n.kind == XML_END_ELEMENT:
            body += head(n.kind, 24) + struct.pack("<II", ref(n.a or None), ref(n.b))
        elif n.kind == XML_CDATA:
            body += head(n.kind, 28) + struct.pack("<IHBBI", ref(n.a), 0, 0, T_NULL, 0)  # as aapt2: size 0
        else:
            attrs = sorted(n.attrs, key=_attr_order)
            id_index = class_index = style_index = 0
            flat = bytearray()
            for i, (a_ns, a_name, rid, typ, data) in enumerate(attrs, 1):
                if rid == ID_ATTR:
                    id_index = i
                elif not a_ns and a_name == "class":
                    class_index = i
                elif not a_ns and a_name == "style":
                    style_index = i
                if typ == T_STRING:
                    raw = value = ref(data)
                else:
                    raw, value = NO_INDEX, data
                flat += struct.pack("<IIIHBBI", ref(a_ns or None), ref(a_name, rid), raw, 8, 0, typ, value)
            ext = struct.pack("<IIHHHHHH", ref(n.a or None), ref(n.b), 20, 20, len(attrs),
                              id_index, class_index, style_index)
            body += head(n.kind, 16 + len(ext) + len(flat)) + ext + flat
    return bytes(body)


def compile_xml(text: str, resolve: Callable[[str], int] | None = None) -> bytes:
    """A resource XML file (res/xml/...) compiled as aapt2 does it (UTF-8 string pool)."""
    return encode(text, resolve, utf8=True)


# --- resources.arsc ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ArscEntry:
    id: int
    type: str
    key: str
    public: bool
    value: tuple[int, int] | None  # (type, data) of a simple entry
    bag: list[tuple[int, int, int]] | None  # (name id, type, data) of a complex entry


def arsc_entries(arsc: bytes, with_values: bool = True) -> Iterator[ArscEntry]:
    """Every entry of every type chunk (all configurations, so an id can come more than once), package by package."""
    typ, hsize, size = struct.unpack_from("<HHI", arsc, 0)
    if typ != RES_TABLE:
        raise AxmlError("resources.arsc: not a resource table")
    for ctype, chsize, csize, off in _chunks(arsc, hsize, min(size, len(arsc))):
        if ctype != TABLE_PACKAGE:
            continue
        pkg_id = struct.unpack_from("<I", arsc, off + 8)[0]
        type_strings, _, key_strings = struct.unpack_from("<III", arsc, off + 268)
        type_id_offset = struct.unpack_from("<I", arsc, off + 284)[0] if chsize >= 288 else 0
        types = read_pool(arsc, off + type_strings)
        keys = read_pool(arsc, off + key_strings)
        spec_flags: dict[int, tuple[int, ...]] = {}
        for t, th, ts, toff in _chunks(arsc, off + chsize, off + csize):
            if t == TABLE_TYPE_SPEC:
                tid, count = arsc[toff + 8], struct.unpack_from("<I", arsc, toff + 12)[0]
                spec_flags[tid] = struct.unpack_from(f"<{count}I", arsc, toff + th)
            elif t == TABLE_TYPE:
                tid, tflags = arsc[toff + 8], arsc[toff + 9]
                count, entries_start = struct.unpack_from("<II", arsc, toff + 12)
                type_name = types[tid - 1 - type_id_offset]
                if tflags & 0x01:  # sparse: (entry index, offset / 4) pairs
                    pairs = struct.unpack_from(f"<{2 * count}H", arsc, toff + th)
                    slots = [(pairs[2 * i], pairs[2 * i + 1] * 4) for i in range(count)]
                elif tflags & 0x02:  # 16-bit offsets / 4
                    slots = [(i, o * 4) for i, o in enumerate(struct.unpack_from(f"<{count}H", arsc, toff + th))
                             if o != 0xFFFF]
                else:
                    slots = [(i, o) for i, o in enumerate(struct.unpack_from(f"<{count}I", arsc, toff + th))
                             if o != NO_INDEX]
                flags = spec_flags.get(tid, ())
                for idx, eo in slots:
                    e = toff + entries_start + eo
                    esize, eflags, key = struct.unpack_from("<HHI", arsc, e)
                    value = bag = None
                    if eflags & 0x08:  # compact entry: key in the size field, type in the flags' high byte
                        key, value = esize, (eflags >> 8, key)
                    elif with_values and eflags & 0x01:
                        count_maps = struct.unpack_from("<I", arsc, e + 12)[0]
                        bag = []
                        for m in range(count_maps):
                            name, _vs, _r, vt, vd = struct.unpack_from("<IHBBI", arsc, e + esize + 12 * m)
                            bag.append((name, vt, vd))
                    elif with_values:
                        _vs, _r, vt, vd = struct.unpack_from("<HBBI", arsc, e + esize)
                        value = (vt, vd)
                    public = bool(idx < len(flags) and flags[idx] & 0x40000000)
                    yield ArscEntry(pkg_id << 24 | tid << 16 | idx, type_name, keys[key], public, value, bag)


def arsc_resource_id(arsc: bytes, type_name: str, entry: str) -> int:
    """The id of resource `type_name`/`entry` (e.g. "string", "app_name") in a resources.arsc."""
    for e in arsc_entries(arsc, with_values=False):
        if e.type == type_name and e.key == entry:
            return e.id
    raise AxmlError(f"resources.arsc: no resource {type_name}/{entry}")


@dataclass(frozen=True)
class ArscNames:
    """The app resources of a resources.arsc by name and by id."""
    ids: dict[tuple[str, str], int]
    names: dict[int, str]  # id -> "type/name"

    @classmethod
    def of(cls, arsc: bytes) -> ArscNames:
        ids: dict[tuple[str, str], int] = {}
        for e in arsc_entries(arsc, with_values=False):
            ids.setdefault((e.type, e.key), e.id)
        return cls(ids, {i: f"{t}/{n}" for (t, n), i in ids.items()})

    def resolve(self, ref: str) -> int:
        m = _NAME_REF.fullmatch(ref)
        if not m:
            raise AxmlError(f"not a resource reference: {ref!r}")
        key = (m.group(3) or "attr", m.group(4))
        if key not in self.ids:
            raise AxmlError(f"resources.arsc: no resource {key[0]}/{key[1]} (for {ref})")
        return self.ids[key]


def arsc_resolver(arsc: bytes) -> Callable[[str], int]:
    """encode's `resolve` callback for the app resources of `arsc` ("@string/app_name", "@app.pkg:string/x")."""
    return ArscNames.of(arsc).resolve
