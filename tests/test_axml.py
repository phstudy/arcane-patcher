"""Binary XML in pure Python (axml): the text dialect, encode / decode round trips, aapt2 equivalence."""

import re
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import APK, FRAGMENTS, needs_apk

from arcane_patcher import axml, patch

ANDROID = 'xmlns:android="http://schemas.android.com/apk/res/android"'
APP_IDS = {"@string/app_name": 0x7F120070, "@mipmap/app_icon": 0x7F0E0000, "@style/Game": 0x7F130325}

MANIFEST = f"""<?xml version="1.0" encoding="utf-8"?>
<manifest {ANDROID} android:versionCode="7" android:versionName="1.0" package="com.example.game">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="33"/>
    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-feature android:glEsVersion="0x00030001"/>
    <uses-feature android:name="android.hardware.touchscreen" android:required="false"/>
    <application android:icon="@mipmap/app_icon" android:label="@string/app_name" android:name=".App"
        android:theme="@style/Game">
        <activity android:label="@string/app_name" android:name=".Main" android:launchMode="singleTask"
            android:screenOrientation="userLandscape" android:exported="true"
            android:configChanges="orientation|screenSize|keyboardHidden">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
            <meta-data android:name="unityplayer.UnityActivity" android:value="true"/>
        </activity>
        <meta-data android:name="a.version" android:value="12"/>
        <meta-data android:name="a.text" android:value="\\12"/>
        <meta-data android:name="a.float" android:value="1.5"/>
        <meta-data android:name="a.color" android:value="#ff0000"/>
        <provider android:authorities="com.example.game.init" android:name="x.Init" android:initOrder="100"
            android:exported="false"/>
    </application>
</manifest>
"""


def _resolve(ref: str) -> int:
    return APP_IDS[ref]


def _attrs(data: bytes) -> list[tuple[str, str, int, int]]:
    """(element, attribute, type, data) of every attribute (strings as their text), in file order."""
    strings = axml.read_pool(data, 8)
    out = []
    for typ, hsize, size, off in axml._chunks(data, 8, len(data)):
        if typ == axml.XML_START_ELEMENT:
            _, name, a_start, a_size, count = struct.unpack_from("<IIHHH", data, off + hsize)
            for i in range(count):
                _, a_name, _, _, _, vt, vd = struct.unpack_from("<IIIHBBI", data, off + hsize + a_start + i * a_size)
                out.append((strings[name], strings[a_name], vt, strings[vd] if vt == axml.T_STRING else vd))
    return out


# --- values -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("attr, text, expected", [
    ("configChanges", "orientation|screenSize|keyboardHidden", (axml.T_INT_HEX, 0x4A0)),
    ("windowSoftInputMode", "adjustResize", (axml.T_INT_HEX, 0x10)),
    ("launchMode", "singleTask", (axml.T_INT_DEC, 2)),
    ("exported", "true", (axml.T_INT_BOOLEAN, 0xFFFFFFFF)),
    ("required", "false", (axml.T_INT_BOOLEAN, 0)),
    ("minSdkVersion", "21", (axml.T_INT_DEC, 21)),
    ("minSdkVersion", "Tiramisu", (axml.T_STRING, "Tiramisu")),
    ("initOrder", "1000", (axml.T_INT_DEC, 1000)),
    ("priority", "-500", (axml.T_INT_DEC, 0xFFFFFE0C)),
    ("glEsVersion", "0x00030001", (axml.T_INT_HEX, 0x30001)),
    ("theme", "@android:style/Theme.Material.NoActionBar", (axml.T_REFERENCE, 0x0103022E)),
    ("theme", "@android:style/Theme.Translucent.NoTitleBar", (axml.T_REFERENCE, 0x01030010)),
    ("theme", "@0x7f130325", (axml.T_REFERENCE, 0x7F130325)),
    ("label", "@string/app_name", (axml.T_REFERENCE, 0x7F120070)),
    ("label", "Offline & co", (axml.T_STRING, "Offline & co")),
    ("label", "\\@home", (axml.T_STRING, "@home")),
    ("process", ":arcane_restart", (axml.T_STRING, ":arcane_restart")),
    ("value", "12", (axml.T_INT_DEC, 12)),
    ("value", "\\12", (axml.T_STRING, "12")),
    ("value", "1.5", (axml.T_FLOAT, 0x3FC00000)),
    ("value", "5dp", (axml.T_STRING, "5dp")),  # value takes no dimensions
    ("value", "#fff", (axml.T_RGB4, 0xFFFFFFFF)),
    ("value", "#80ff0000", (axml.T_ARGB8, 0x80FF0000)),
    ("value", "%int_boolean:1", (axml.T_INT_BOOLEAN, 1)),
    ("label", "@null", (axml.T_REFERENCE, 0)),
    ("label", "@empty", (axml.T_NULL, 1)),
    ("textSize", "5dp", (axml.T_DIMENSION, 0x501)),
    ("textSize", "1.5sp", (axml.T_DIMENSION, 0x00C00022)),  # measured with aapt2
    ("textSize", "-2.75px", (axml.T_DIMENSION, 0xFEA00020)),
    ("fromXDelta", "33.3%p", (axml.T_FRACTION, 0x2A9FBE31)),
    ("textColor", "?android:attr/textColorPrimary", (axml.T_ATTRIBUTE, 0x01010036)),
])
def test_values_read_as_aapt2_reads_them(attr, text, expected):
    """Hand-written values get aapt2's types for the attribute's formats (flags int_hex, enums int_dec, ...)."""
    assert axml.compile_value(axml.table().attrs[attr], text, _resolve) == expected


def test_undeclared_attributes_read_any_format():
    """Attributes without a declaration (no namespace) read as any format, as aapt2 does (measured with aapt2)."""
    for text, expected in [("true", (axml.T_INT_BOOLEAN, 0xFFFFFFFF)), ("12", (axml.T_INT_DEC, 12)),
                           ("0x10", (axml.T_INT_HEX, 0x10)), ("#fff", (axml.T_RGB4, 0xFFFFFFFF)),
                           ("1.5", (axml.T_FLOAT, 0x3FC00000)), ("5dp", (axml.T_DIMENSION, 0x501)),
                           ("50%", (axml.T_FRACTION, 0x40000030)), (" 7 ", (axml.T_INT_DEC, 7)),
                           ("-3", (axml.T_INT_DEC, 0xFFFFFFFD)), ("TRUE", (axml.T_INT_BOOLEAN, 0xFFFFFFFF)),
                           ("@android:style/Theme", (axml.T_REFERENCE, 0x01030005)), ("abc", (axml.T_STRING, "abc"))]:
        assert axml.compile_value(None, text) == expected, text


@pytest.mark.parametrize("text, message", [
    (f'<manifest {ANDROID}><x android:nosuchattr="1"/></manifest>', "unknown android attribute"),
    (f'<manifest {ANDROID}><x android:launchMode="sometimes"/></manifest>', "not a valid"),
    (f'<manifest {ANDROID}><x android:configChanges="orientation|nope"/></manifest>', "orientation"),
    (f'<manifest {ANDROID}><x android:exported="yes"/></manifest>', "not a valid boolean"),
    (f'<manifest {ANDROID}><x android:label="@string/app_name"/></manifest>', "needs a resolver"),
    (f'<manifest {ANDROID}><x android:theme="@android:style/NoSuchTheme"/></manifest>', "unknown public framework"),
    (f'<manifest {ANDROID}><x android:label="@ not a ref"/></manifest>', "not a resource reference"),
    (f'<manifest {ANDROID}><x android:label="%bogus:1"/></manifest>', "unknown value type"),
    ("<manifest><x></manifest>", "not well-formed"),
])
def test_encode_fails_loudly(text, message):
    with pytest.raises(axml.AxmlError, match=message):
        axml.encode(text)


def test_typed_values_round_trip_exactly():
    """Every typed value comes back as the same type and data: the readable form when it reads back the same,
    else a %type:data token; strings that would read as something else get a backslash."""
    values = ["true", "%int_boolean:0x00000001", "%int_dec:5", "0x00000005", "-7", "12", "\\12", "\\true", "\\\\x",
              "\\%int_dec:1", "\\@x", "\\?x", "1.5", "0.1", "#ff0000", "#80ff0000", "#f00", "#8f00", "@null",
              "@empty", "@0x7f120070", "?0x01010036", "%dimension:0x00000501", "%float:0x7fc00000",
              "%0x1b:0x12345678", "%dynamic_reference:0x7f010001", " padded ", "a\nb\tc", 'q"<&>']
    rows = "\n".join(f'        <meta-data android:name="m{i}" android:value="{axml._escape(v)}" x="{axml._escape(v)}"/>'
                     for i, v in enumerate(values))
    sized = ('        <x android:textSize="1.5sp" android:layout_width="match_parent" android:layout_weight="0.25"'
             ' android:fromXDelta="50%p" android:pivotX="25%" android:textColor="?android:attr/textColorPrimary"/>')
    text = f"<manifest {ANDROID}>\n    <application>\n{rows}\n{sized}\n    </application>\n</manifest>\n"
    data = axml.encode(text)
    decoded = axml.decode(data)
    assert axml.encode(decoded) == data
    assert axml.decode(axml.encode(decoded)) == decoded
    # readable where possible
    assert 'android:value="0.1"' in decoded and 'android:value="#80ff0000"' in decoded
    assert 'android:value="\\12"' in decoded and 'x="\\12"' in decoded
    assert 'android:value="%int_boolean:0x00000001"' in decoded and 'android:value="@null"' in decoded
    assert 'android:textSize="1.5sp"' in decoded and 'android:layout_weight="0.25"' in decoded
    # match_parent and fill_parent share a value: the first name declared is written
    assert 'android:layout_width="fill_parent"' in decoded
    assert 'android:fromXDelta="50%p"' in decoded and 'android:pivotX="25%"' in decoded
    assert 'android:textColor="?android:attr/textColorPrimary"' in decoded
    assert 'android:value="a&#10;b&#9;c"' in decoded and 'android:value="q&quot;&lt;&amp;&gt;"' in decoded


# --- documents ----------------------------------------------------------------------------------------------------

def test_synthetic_manifest_round_trip():
    data = axml.encode(MANIFEST, _resolve)
    text = axml.decode(data)
    assert axml.encode(text) == data  # app references come back as ids
    assert text.startswith('<?xml version="1.0" encoding="utf-8" standalone="no"?>\n<manifest ' + ANDROID)
    # apktool's layout: what patch.py's text edits match
    assert '<uses-sdk android:minSdkVersion="21" android:targetSdkVersion="33"/>' in text
    assert 'android:configChanges="keyboardHidden|orientation|screenSize"' in text
    assert 'android:launchMode="singleTask"' in text and 'android:screenOrientation="userLandscape"' in text
    assert 'android:theme="@0x7f130325"' in text and 'android:label="@0x7f120070"' in text
    assert patch.LAUNCHER_FILTER.search(text)
    assert patch.launcher_activity(text) == "com.example.game.Main"
    assert patch.manifest_package(text) == "com.example.game"
    # line numbers are kept: every start tag on its line of the source
    lines = text.split("\n")
    for i, src in enumerate(MANIFEST.split("\n")[1:], 1):
        tag = re.match(r"\s*<([\w-]+)", src)
        if tag:
            assert lines[i].lstrip().startswith(f"<{tag.group(1)}"), (i, src, lines[i])
    # attributes sorted by resource id (android:theme 0x01010000 .. android:configChanges 0x0101001f), then the rest
    assert [a for e, a, _, _ in _attrs(data) if e == "manifest"] == ["versionCode", "versionName", "package"]
    assert [a for e, a, _, _ in _attrs(data) if e == "activity"] == [
        "label", "name", "exported", "launchMode", "screenOrientation", "configChanges"]
    # string pool: attribute names with ids first, matching the resource map
    strings = axml.read_pool(data, 8)
    pool_size = struct.unpack_from("<I", data, 12)[0]
    typ, _, size = struct.unpack_from("<HHI", data, 8 + pool_size)
    assert typ == axml.XML_RESOURCE_MAP
    ids = struct.unpack_from(f"<{(size - 8) // 4}I", data, 8 + pool_size + 8)
    assert [axml.table().attrs[strings[i]].id for i in range(len(ids))] == list(ids) == sorted(ids)


def test_setup_screen_edits_encode():
    """The elements patch.py adds (setup screen, settings / restart activities, provider, camera features, deep
    link) encode with the right types."""
    for kind in FRAGMENTS:
        text = axml.decode(axml.encode(MANIFEST, _resolve))
        edited = patch.rebrand_manifest(patch.launcher_manifest(text, FRAGMENTS[kind]), "com.example.game.x", "X")
        attrs = _attrs(axml.encode(edited, _resolve))
        assert ("activity", "configChanges", axml.T_INT_HEX, 0x4A0) in attrs
        assert ("activity", "windowSoftInputMode", axml.T_INT_HEX, 0x10) in attrs
        assert ("activity", "theme", axml.T_REFERENCE, 0x0103022E) in attrs
        assert ("activity", "theme", axml.T_REFERENCE, 0x01030010) in attrs  # Theme.Translucent.NoTitleBar
        assert ("activity", "noHistory", axml.T_INT_BOOLEAN, 0xFFFFFFFF) in attrs
        assert ("activity", "process", axml.T_STRING, ":arcane_restart") in attrs
        assert ("provider", "initOrder", axml.T_INT_DEC, 1000) in attrs
        assert ("provider", "authorities", axml.T_STRING, f"com.example.game.x.arcane{kind}") in attrs
        assert ("meta-data", "value", axml.T_STRING, "com.example.game.Main") in attrs
        assert ("activity", "label", axml.T_STRING, "X") in attrs
    multi = _attrs(axml.encode(patch.launcher_manifest(axml.decode(axml.encode(MANIFEST, _resolve)),
                                                       FRAGMENTS["multiplayer"]),
                               _resolve))
    assert ("data", "scheme", axml.T_STRING, "arcane") in multi and ("data", "host", axml.T_STRING, "connect") in multi
    assert ("uses-feature", "required", axml.T_INT_BOOLEAN, 0) in multi
    assert ("uses-permission", "name", axml.T_STRING, "android.permission.CAMERA") in multi
    assert ("activity", "theme", axml.T_REFERENCE, axml.table().resources["style"][
        "Theme.Material.NoActionBar.Fullscreen"]) in multi


def test_text_and_namespaces():
    text = ('<root xmlns:a="urn:a" xmlns:android="http://schemas.android.com/apk/res/android" a:k="v">\n'
            '  <b android:name="n">hello &amp; bye</b>\n  <!-- dropped -->\n  <c xmlns:z="urn:z" z:y="1"/>\n</root>\n')
    data = axml.encode(text)
    decoded = axml.decode(data)
    assert axml.encode(decoded) == data
    assert '<root xmlns:a="urn:a" xmlns:android=' in decoded and 'a:k="v"' in decoded
    assert "hello &amp; bye" in decoded and '<c xmlns:z="urn:z" z:y="1"/>' in decoded and "dropped" not in decoded


def test_decode_refuses_what_it_cannot_write_back():
    data = bytearray(axml.encode(f'<manifest {ANDROID} android:versionCode="1"/>'))
    ids_at = 8 + struct.unpack_from("<I", data, 12)[0] + 8
    struct.pack_into("<I", data, ids_at, 0x0101FFFF)  # versionCode with another id
    with pytest.raises(axml.AxmlError, match="resource id"):
        axml.decode(bytes(data))
    with pytest.raises(axml.AxmlError, match="not a binary XML"):
        axml.decode(b"<manifest/>")


def test_attribute_table():
    t = axml.table()
    assert t.attrs["name"].id == 0x01010003 and t.attrs["versionCode"].id == 0x0101021B
    assert t.attrs["configChanges"].flags["screenSize"] == 0x400
    assert t.attrs["launchMode"].enum == {"standard": 0, "singleTop": 1, "singleTask": 2, "singleInstance": 3,
                                          "singleInstancePerTask": 4}
    assert t.attrs["minSdkVersion"].formats == axml.FORMATS["integer"] | axml.FORMATS["string"]
    assert t.resources["style"]["Theme.Material.NoActionBar"] == 0x0103022E
    # every attribute and value patch.py writes is in the table
    for kind in FRAGMENTS:
        added = "".join(patch.fragment_parts(FRAGMENTS[kind], "p.q", "p.q.Main"))
        for name in re.findall(r"android:(\w+)=", added):
            assert name in t.attrs, name


# --- resources.arsc -----------------------------------------------------------------------------------------------

def _pool(strings: list[str]) -> bytes:
    return axml.build_pool(strings, utf8=False)


def _arsc(package_id: int, types: list[str], entries: dict[str, list[str]]) -> bytes:
    """A minimal resource table: one package, one default-configuration type chunk per type in `entries`."""
    keys = sorted({k for ks in entries.values() for k in ks})
    type_pool, key_pool = _pool(types), _pool(keys)
    hsize = 288
    chunks = b""
    for name, names in entries.items():
        tid = types.index(name) + 1
        spec = struct.pack(f"<HHIBBHI{len(names)}I", axml.TABLE_TYPE_SPEC, 16, 16 + 4 * len(names), tid, 0, 0,
                           len(names), *([0] * len(names)))
        config = struct.pack("<I", 64) + b"\0" * 60
        th = 20 + len(config)
        entry_data = b"".join(struct.pack("<HHIHBBI", 8, 0, keys.index(k), 8, 0, axml.T_STRING, 0) for k in names)
        offsets = struct.pack(f"<{len(names)}I", *(16 * i for i in range(len(names))))
        start = th + len(offsets)
        typ = struct.pack("<HHIBBHII", axml.TABLE_TYPE, th, start + len(entry_data), tid, 0, 0, len(names), start)
        chunks += spec + typ + config + offsets + entry_data
    pkg_size = hsize + len(type_pool) + len(key_pool) + len(chunks)
    pkg = (struct.pack("<HHII", axml.TABLE_PACKAGE, hsize, pkg_size, package_id)
           + "com.example.game".encode("utf-16-le").ljust(256, b"\0")
           + struct.pack("<IIIII", hsize, len(types), hsize + len(type_pool), len(keys), 0)
           + type_pool + key_pool + chunks)
    values = _pool([])
    return struct.pack("<HHII", axml.RES_TABLE, 12, 12 + len(values) + len(pkg), 1) + values + pkg


def test_arsc_resource_id_and_resolver():
    arsc = _arsc(0x7F, ["attr", "string", "mipmap"], {"string": ["other", "app_name"], "mipmap": ["app_icon"]})
    assert axml.arsc_resource_id(arsc, "string", "app_name") == 0x7F020001
    assert axml.arsc_resource_id(arsc, "mipmap", "app_icon") == 0x7F030000
    with pytest.raises(axml.AxmlError, match="no resource string/nope"):
        axml.arsc_resource_id(arsc, "string", "nope")
    text = MANIFEST.replace(' android:theme="@style/Game"', "")
    data = axml.encode(text, axml.arsc_resolver(arsc))
    assert ("application", "label", axml.T_REFERENCE, 0x7F020001) in _attrs(data)
    named = axml.decode(data, arsc)  # app references by name, as apktool writes them
    assert 'android:label="@string/app_name"' in named and 'android:icon="@mipmap/app_icon"' in named
    assert axml.encode(named, axml.arsc_resolver(arsc)) == data


# --- aapt2 equivalence --------------------------------------------------------------------------------------------

def _aapt2() -> Path:
    from arcane_patcher import sdktools

    try:
        return sdktools.tool(sdktools.find_build_tools(tools=("aapt2",)), "aapt2")
    except SystemExit:
        pytest.skip("no Android SDK build-tools (aapt2)")


def _aapt2_compile(tmp_path: Path, xml: str) -> bytes:
    """What aapt2 makes of res/xml/network_security_config.xml (the patcher's former tool, as a reference)."""
    from arcane_patcher import sdktools

    aapt2, src = _aapt2(), tmp_path / "nsc"
    (src / "res/xml").mkdir(parents=True)
    (src / "res/xml/network_security_config.xml").write_text(xml)
    (src / "AndroidManifest.xml").write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="x.nsc"><application/></manifest>')
    subprocess.run([str(aapt2), "compile", "-o", str(src / "c.zip"), "--dir", str(src / "res")], check=True)
    subprocess.run([str(aapt2), "link", "-o", str(src / "nsc.apk"), "-I", str(sdktools.android_jar()),
                    "--manifest", str(src / "AndroidManifest.xml"), str(src / "c.zip")], check=True)
    with zipfile.ZipFile(src / "nsc.apk") as z:
        return z.read(patch.NSC_ENTRY)


def _apktool_manifest(tmp_path: Path, apk: Path, edit) -> bytes:
    """The manifest the patcher used to build with apktool (decode, edit the text, build), as a reference."""
    dec = tmp_path / "apktool"
    subprocess.run(["apktool", "d", "-s", "-f", "-o", str(dec), str(apk)], check=True, stdout=subprocess.DEVNULL)
    mf = dec / "AndroidManifest.xml"
    mf.write_text(edit(mf.read_text()))
    subprocess.run(["apktool", "b", "-o", str(tmp_path / "manifest.apk"), str(dec)], check=True,
                   stdout=subprocess.DEVNULL)
    with zipfile.ZipFile(tmp_path / "manifest.apk") as z:
        return z.read("AndroidManifest.xml")


def _xmltree(tmp_path: Path, data: bytes, name: str = "m") -> str:
    with zipfile.ZipFile(tmp_path / f"{name}.zip", "w") as z:
        z.writestr("AndroidManifest.xml", data)
    return subprocess.run([str(_aapt2()), "dump", "xmltree", "--file", "AndroidManifest.xml",
                           str(tmp_path / f"{name}.zip")],
                          check=True, capture_output=True, text=True).stdout


NSC_TREE = """E: network-security-config (line=2)
    E: base-config (line=3)
      A: cleartextTrafficPermitted=false
        E: trust-anchors (line=4)
    E: domain-config (line=6)
      A: cleartextTrafficPermitted=true
        E: domain (line=7)
          A: includeSubdomains=false
            T: '127.0.0.1'
        E: domain (line=8)
          A: includeSubdomains=false
            T: 'localhost'
"""


def test_network_security_config(tmp_path):
    """compile_xml(NSC_XML) is the network security config aapt2 compiles (same bytes when aapt2 is installed)."""
    data = axml.compile_xml(patch.NSC_XML)
    assert axml.read_pool(data, 8) == ["127.0.0.1", "base-config", "cleartextTrafficPermitted", "domain",
                                       "domain-config", "includeSubdomains", "localhost", "network-security-config",
                                       "trust-anchors"]
    assert "certificates" not in patch.NSC_XML  # no certificate authority is trusted: no TLS connection completes
    assert ("base-config", "cleartextTrafficPermitted", axml.T_INT_BOOLEAN, 0) in _attrs(data)
    assert ("domain-config", "cleartextTrafficPermitted", axml.T_INT_BOOLEAN, 0xFFFFFFFF) in _attrs(data)
    assert _xmltree(tmp_path, data) == NSC_TREE  # (aapt2 dumps files of an APK, so it goes in as the manifest)
    assert data == patch.compile_nsc() == _aapt2_compile(tmp_path, patch.NSC_XML)


def _input_base(tmp_path: Path) -> tuple[Path, bytes, bytes]:
    base = patch._base_apk(patch.read_inputs(APK), tmp_path)
    with zipfile.ZipFile(base) as z:
        return base, z.read("AndroidManifest.xml"), z.read("resources.arsc")


def _no_lines(tree: str) -> str:
    return re.sub(r" \(line=\d+\)", "", tree)


@needs_apk
def test_real_v29_manifest_round_trip(tmp_path):
    """decode -> encode gives the input manifest back byte for byte, except the line numbers of elements the input
    records at line 0 (merged in after the rest: the text cannot place a tag before the previous one)."""
    _, mf, arsc = _input_base(tmp_path)
    for text, resolve in ((axml.decode(mf), None), (axml.decode(mf, arsc), axml.arsc_resolver(arsc))):
        out = axml.encode(text, resolve)
        assert len(out) == len(mf)
        assert _no_lines(_xmltree(tmp_path, out)) == _no_lines(_xmltree(tmp_path, mf, name="orig"))
        before, after = _xmltree(tmp_path, mf, name="orig").splitlines(), _xmltree(tmp_path, out).splitlines()
        changed = [(b, a) for b, a in zip(before, after) if b != a]
        assert changed and all("(line=0)" in b for b, _ in changed)
        lines0 = sum("(line=0)" in b for b in before)
        assert len(changed) == lines0
    assert '<uses-sdk android:minSdkVersion="21"' in axml.decode(mf)  # kept in the text (apktool moves it out)


@needs_apk
def test_real_v29_edits_match_the_apktool_build(tmp_path):
    """patch.py's manifest edits on the decoded text, encoded: the expected new elements; and, with apktool
    installed, the same tree (apart from line numbers) as the former apktool build gives."""
    import shutil

    base, mf, arsc = _input_base(tmp_path)
    text = axml.decode(mf, arsc)
    pkg = patch.axml_package(mf)
    assert patch.manifest_package(text) == pkg
    game = patch.launcher_activity(text)
    assert game.startswith(pkg + ".")

    def edit(kind: str, label: str):
        return lambda t: patch.rebrand_manifest(patch.launcher_manifest(t, FRAGMENTS[kind]), f"{pkg}.{kind}", label)

    mine = {kind: axml.encode(edit(kind, kind.title())(text), axml.arsc_resolver(arsc))
            for kind in FRAGMENTS}
    dump = _xmltree(tmp_path, patch.axml_set_version_code(mine["multiplayer"], 2026112000))
    assert "versionCode(0x0101021b)=2026112000" in dump and 'versionName(0x0101021c)="29.0"' in dump
    assert f'package="{pkg}.multiplayer"' in dump and '"android.permission.CAMERA"' in dump
    for feature in ("android.hardware.camera", "android.hardware.camera.any", "android.hardware.camera.autofocus"):
        block = dump[dump.index(f'name(0x01010003)="{feature}"'):][:300]
        assert "required(0x0101028e)=false" in block, feature
    assert 'scheme(0x01010027)="arcane"' in dump and 'host(0x01010028)="connect"' in dump
    assert '"arcane.multiplayer.ScanActivity"' in dump and 'process(0x01010011)=":arcane_restart"' in dump
    assert f'value(0x01010024)="{game}"' in dump
    # minSdkVersion is set in the text
    raised = re.sub(r'(<uses-sdk\b[^>]*android:minSdkVersion=)"\d+"', r'\1"26"', text)
    assert "minSdkVersion(0x0101020c)=26" in _xmltree(tmp_path, axml.encode(raised, axml.arsc_resolver(arsc)))
    if not shutil.which("apktool"):
        pytest.skip("no apktool to compare with")
    # one variant (apktool takes seconds per build): offline adds the most (settings / restart activities, labels)
    ref = _apktool_manifest(tmp_path, base, edit("offline", "Offline"))
    assert _no_lines(_xmltree(tmp_path, mine["offline"])) == _no_lines(_xmltree(tmp_path, ref, name="ref"))


def test_attribute_table_is_generated_from_the_sdk():
    """android_attrs.json is what tools/gen_android_attrs.py makes from the newest installed platform."""
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("gen_android_attrs", Path(__file__).parent.parent / "tools/gen_android_attrs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    try:
        from arcane_patcher import sdktools

        plat = gen.platform(sdktools.android_sdk(), None)
    except SystemExit:
        pytest.skip("no Android SDK platform")
    committed = json.loads(axml.ATTRS_JSON.read_text())
    if committed["source"].split()[0] != plat.name:
        pytest.skip(f"the table comes from {committed['source'].split()[0]}, the SDK has {plat.name}")
    assert {k: v for k, v in committed.items() if k != "source"} == gen.read_table(plat / "android.jar")
