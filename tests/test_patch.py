"""patch.py, the applier: the build-id check against the patch file's rules, the manifest (launcher activity and
package read from the input, rebranding, the variants' manifest fragments), the variants, the versionCode, the consent
dex. Tests that need the real APK run only when ARCANE_TEST_APK names it."""

import re
import struct
import zipfile
from pathlib import Path

import pytest
from conftest import APK, FRAGMENTS, VARIANTS, make_patch_file, needs_apk, keys_for_tests

from arcane_patcher import patch, rulesgen

# --- build-id check before anything is written ----------------------------------------------------------------


def _elf_with_build_id(build_id: bytes) -> bytes:
    """A minimal ELF64 with one PT_NOTE segment holding a GNU build-id note."""
    note = struct.pack("<III", 4, len(build_id), 3) + b"GNU\0" + build_id
    phoff, phentsize = 0x40, 56
    note_off = phoff + phentsize
    head = bytearray(0x40)
    head[:4] = b"\x7fELF"
    struct.pack_into("<Q", head, 0x20, phoff)
    struct.pack_into("<HH", head, 0x36, phentsize, 1)
    ph = struct.pack("<IIQQQQQQ", 4, 0, note_off, 0, 0, len(note), len(note), 4)
    return bytes(head) + ph + note


MINI_MANIFEST_XML = "AndroidManifest.xml"


def test_rejects_unknown_build_id(tmp_path, patch_file):
    """An APK of another build: a clear error naming its build-id, before anything is written (no SDK needed)."""
    other = bytes.fromhex("00112233445566778899aabbccddeeff00112233")
    so = _elf_with_build_id(other)
    assert patch.elf_build_id(so) == other
    apk = tmp_path / "other.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr(MINI_MANIFEST_XML, b"not checked before the build-id")
        z.writestr(patch.GAME_LIB, so)
    keystore = tmp_path / "k.keystore"
    keystore.write_bytes(b"x")
    out = tmp_path / "out" / "game.apk"
    for variant in ("offline", "multi"):
        with pytest.raises(SystemExit, match=f"has no rules for build-id {other.hex()}.*v29.0"):
            patch.patch(apk, out, variant, keystore, None, env={}, patch_file=patch_file)
    assert not out.exists() and not out.parent.exists()
    # the same inside a split bundle (the library is in the arm64 split)
    split = tmp_path / "split.apk"
    with zipfile.ZipFile(split, "w") as z:
        z.writestr(patch.GAME_LIB, so)
    bundle = tmp_path / "other.apkm"
    empty = tmp_path / "base.apk"
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr(MINI_MANIFEST_XML, b"")
    with zipfile.ZipFile(bundle, "w") as z:
        z.write(empty, "base.apk")
        z.write(split, patch.EMBED_ABI_SPLIT)
    with pytest.raises(SystemExit, match=other.hex()):
        patch.patch(bundle, out, "multi", keystore, None, env={}, patch_file=patch_file)
    assert not out.exists()


# --- manifest: launcher activity and package from the input ---------------------------------------------------

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.game">
    <uses-permission android:name="android.permission.INTERNET"/>
    <permission android:name="com.example.game.permission.C2D_MESSAGE" android:protectionLevel="signature"/>
    <uses-permission android:name="com.example.game.permission.C2D_MESSAGE"/>
    <permission android:name="com.example.game.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION" android:protectionLevel="signature"/>
    <uses-permission android:name="com.example.game.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"/>
    <application android:icon="@mipmap/app_icon" android:label="@string/app_name" android:name=".App">
        <activity android:label="@string/app_name" android:name=".Main">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
            <meta-data android:name="unityplayer.UnityActivity" android:value="true"/>
        </activity>
        <activity android:label="Other" android:name="Helper"/>
        <activity-alias android:label="Shortcut" android:name=".Alias" android:targetActivity=".Main">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity-alias>
        <receiver android:name="com.other.Receiver" android:permission="com.example.game.permission.C2D_MESSAGE"/>
        <data android:scheme="com.example.game"/>
        <provider android:authorities="com.example.game.firebaseinitprovider" android:name="com.google.firebase.provider.FirebaseInitProvider"/>
        <provider android:authorities="com.example.game.androidx-startup;com.example.game" android:name="androidx.startup.InitializationProvider"/>
        <provider android:authorities="com.example.gameX.other" android:name="x.Y"/>
    </application>
</manifest>
"""
GAME = "com.example.game.Main"


def test_package_and_activity_read_from_manifest():
    assert patch.manifest_package(MANIFEST) == "com.example.game"
    assert patch.launcher_activity(MANIFEST) == GAME  # relative name made absolute; the alias is not an activity
    other = MANIFEST.replace('package="com.example.game"', 'package="org.sample.app"').replace(
        'android:name=".Main"', 'android:name="org.sample.app.Play"')
    assert patch.launcher_activity(other) == "org.sample.app.Play"
    offline, multi = (patch.VariantSpec.of(VARIANTS[v]) for v in ("offline", "multi"))
    assert patch.output_package("com.example.game", offline) == "com.example.game.offline"
    assert patch.output_package("com.example.game", multi) == "com.example.game.multiplayer"
    assert patch.output_package("com.example.game", multi, "org.me.game") == "org.me.game"
    with pytest.raises(SystemExit, match="not a valid package"):
        patch.output_package("com.example.game", offline, "bad name")
    # the setup screens learn the game activity from the meta-data the patcher writes
    out = patch.launcher_manifest(MANIFEST, FRAGMENTS["offline"])
    assert f'<meta-data android:name="{patch.GAME_ACTIVITY_META}" android:value="{GAME}"/>' in out
    assert patch.GAME_ACTIVITY_META == "arcane.game_activity"
    no_launcher = MANIFEST.replace('<category android:name="android.intent.category.LAUNCHER"/>', "", 1)
    with pytest.raises(SystemExit, match="one launcher activity"):
        patch.launcher_activity(no_launcher)


def _attr(text: str, tag: str, name: str, attr: str) -> str:
    start = re.search(rf'<{tag}\b[^>]*android:name="{re.escape(name)}"[^>]*>', text).group(0)
    return re.search(rf'\sandroid:{attr}="([^"]*)"', start).group(1)


def test_rebrand_package_keeps_classes_and_moves_authorities_and_permissions():
    out = patch.rebrand_manifest(MANIFEST, package="com.example.game.offline")
    assert 'package="com.example.game.offline"' in out
    # component names resolve to the original classes
    assert 'android:name="com.example.game.App"' in out
    assert 'android:name="com.example.game.Main"' in out
    assert 'android:name="com.example.game.Helper"' in out
    assert _attr(out, "activity-alias", "com.example.game.Alias", "targetActivity") == "com.example.game.Main"
    assert 'android:name="com.other.Receiver"' in out and 'android:name="unityplayer.UnityActivity"' in out
    # authorities unique to the new package; foreign prefixes left alone
    assert 'android:authorities="com.example.game.offline.firebaseinitprovider"' in out
    assert 'android:authorities="com.example.game.offline.androidx-startup;com.example.game.offline"' in out
    assert 'android:authorities="com.example.gameX.other"' in out
    # own permissions renamed consistently (declaration, use, and protection of a component)
    assert "com.example.game.permission" not in out and "com.example.game.DYNAMIC" not in out
    assert out.count('"com.example.game.offline.permission.C2D_MESSAGE"') == 3
    assert out.count('"com.example.game.offline.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION"') == 2
    assert 'android:name="android.permission.INTERNET"' in out
    # not a package reference: a URL scheme
    assert 'android:scheme="com.example.game"' in out
    assert out.count('android:label="@string/app_name"') == 2  # label untouched without a label


def test_rebrand_label_on_application_and_launcher_activities():
    out = patch.rebrand_manifest(MANIFEST, label='My "Offline" & co')
    lit = 'android:label="My &quot;Offline&quot; &amp; co"'
    assert 'package="com.example.game"' in out and 'android:name=".Main"' in out  # no package change
    assert lit in patch.START_TAG.search(out[out.index("<application"):]).group(0)
    assert _attr(out, "activity", ".Main", "label") == "My &quot;Offline&quot; &amp; co"  # showed the app label
    assert _attr(out, "activity-alias", ".Alias", "label") == "My &quot;Offline&quot; &amp; co"  # launcher
    assert _attr(out, "activity", "Helper", "label") == "Other"
    assert "@string/app_name" not in out


def test_rebrand_label_added_when_missing():
    text = MANIFEST.replace(' android:label="@string/app_name" android:name=".App"', ' android:name=".App"')
    out = patch.rebrand_manifest(text, label="Offline")
    assert '<application android:label="Offline" android:icon="@mipmap/app_icon" android:name=".App">' in out


def test_icon_is_never_replaced():
    """Every variant keeps the APK's own launcher icon: the manifest keeps its icon reference, and the patcher has no
    icon code at all."""
    for kind in FRAGMENTS:
        out = patch.rebrand_manifest(patch.launcher_manifest(MANIFEST, FRAGMENTS[kind]), "com.example.game.x", "X")
        assert 'android:icon="@mipmap/app_icon"' in out
    assert not [n for n in vars(patch) if "icon" in n.lower()]


def test_offline_launcher_components():
    out = patch.rebrand_manifest(patch.launcher_manifest(MANIFEST, FRAGMENTS["offline"]), "com.example.game.offline", "Offline")
    assert 'android:authorities="com.example.game.offline.arcaneoffline"' in out
    assert 'android:name="arcane.offline.SetupActivity"' in out and 'android:name="arcane.offline.ServerProvider"' in out
    setup = patch.START_TAG.search(out[out.index('<activity android:name="arcane.offline.SetupActivity"'):]).group(0)
    assert 'android:label="Offline"' in setup
    game = out[out.index(f'android:name="{GAME}"'):]
    assert "category.LAUNCHER" not in game[:game.index("</activity>")]
    # the meta-data keeps the game's class (classes are not renamed with the package)
    assert f'android:name="{patch.GAME_ACTIVITY_META}" android:value="{GAME}"' in out


def test_offline_settings_and_restart_components():
    """The setup screen is reachable while the game runs (Android's app info settings entry, the launcher shortcut's
    action) in a task of its own, and the restart into it runs in its own process; affinities follow the package."""
    out = patch.rebrand_manifest(patch.launcher_manifest(MANIFEST, FRAGMENTS["offline"]), "com.example.game.offline", "Offline")
    settings = out[out.index('<activity android:name="arcane.offline.SettingsActivity"'):]
    settings = settings[:settings.index("</activity>")]
    assert "android.intent.action.APPLICATION_PREFERENCES" in settings and "arcane.offline.action.SETTINGS" in settings
    assert 'android:taskAffinity="com.example.game.offline.arcane_settings"' in settings
    assert 'android:exported="true"' in settings and "category.LAUNCHER" not in settings
    restart = patch.START_TAG.search(out[out.index('<activity android:name="arcane.offline.RestartActivity"'):]).group(0)
    assert 'android:process=":arcane_restart"' in restart and 'android:exported="false"' in restart
    assert 'android:taskAffinity="com.example.game.offline.arcane_restart"' in restart
    assert out.count("category.LAUNCHER") == 2  # only the setup screen (+ the sample's alias): one icon
    assert "arcane.offline." not in patch.launcher_manifest(MANIFEST, FRAGMENTS["multiplayer"])


def test_multiplayer_launcher_components():
    out = patch.rebrand_manifest(patch.launcher_manifest(MANIFEST, FRAGMENTS["multiplayer"]), "com.example.game.multiplayer",
                                 "Multiplayer")
    assert 'package="com.example.game.multiplayer"' in out
    assert 'android:authorities="com.example.game.multiplayer.arcanemultiplayer"' in out
    assert 'android:name="arcane.multiplayer.ProxyProvider"' in out and "arcane.offline" not in out
    setup = out[out.index('<activity android:name="arcane.multiplayer.SetupActivity"'):]
    setup = setup[:setup.index("</activity>")]
    assert 'android:label="Multiplayer"' in setup and "category.LAUNCHER" in setup
    game = out[out.index(f'android:name="{GAME}"'):]
    assert "category.LAUNCHER" not in game[:game.index("</activity>")]
    assert out.count("category.LAUNCHER") == 2


def test_multiplayer_scans_qr_codes_and_opens_deep_links():
    """The camera permission (asked at run time) with every camera feature optional, so devices without a camera
    still install it; the connect link arcane://connect opens the setup screen; the scanner and restart activities."""
    out = patch.rebrand_manifest(patch.launcher_manifest(MANIFEST, FRAGMENTS["multiplayer"]), "com.example.game.multiplayer",
                                 "Multiplayer")
    top = out[:out.index("<application")]
    assert '<uses-permission android:name="android.permission.CAMERA"/>' in top
    features = re.findall(r'<uses-feature android:name="([^"]+)" android:required="false"/>', top)
    assert set(features) == {"android.hardware.camera", "android.hardware.camera.any", "android.hardware.camera.autofocus"}
    assert top.count("<uses-feature") == 3  # none required
    setup = out[out.index('<activity android:name="arcane.multiplayer.SetupActivity"'):]
    setup = setup[:setup.index("</activity>")]
    view = re.search(r"<intent-filter>((?:(?!</intent-filter>).)*action.VIEW.*?)</intent-filter>", setup, re.S).group(1)
    assert "category.DEFAULT" in view and "category.BROWSABLE" in view
    assert '<data android:scheme="arcane" android:host="connect"/>' in view
    assert 'android:name="arcane.multiplayer.ScanActivity" android:exported="false"' in out
    restart = out[out.index('android:name="arcane.multiplayer.RestartActivity"'):]
    assert 'android:process=":arcane_restart"' in restart[:restart.index("/>")]
    offline = patch.launcher_manifest(MANIFEST, FRAGMENTS["offline"])
    assert "CAMERA" not in offline and 'scheme="arcane"' not in offline and "ScanActivity" not in offline


def test_setup_builds_need_a_loopback_server():
    assert patch.loopback_port("http://127.0.0.1:28180") == 28180
    for bad in ("http://10.0.0.1:18480", "https://127.0.0.1:28180", "http://127.0.0.1"):
        with pytest.raises(SystemExit, match="needs http://127.0.0.1:PORT"):
            patch.loopback_port(bad)


def test_variants(tmp_path, patch_file):
    """The patch file's two variants, both with a setup screen and a loopback server URL; development server sources
    only offline."""
    specs = {v: patch.variant_spec(patch_file, v) for v in patch.VARIANT_NAMES}
    assert {v: s.label for v, s in specs.items()} == {"offline": "Offline", "multi": "Multiplayer"}
    assert {v: s.lib_sets for v, s in specs.items()} == {"offline": ("local_battles", "publisher_services"),
                                                        "multi": ("publisher_services",)}
    assert {v: s.dex_sets for v, s in specs.items()} == {"offline": ("analytics_upload",), "multi": ("analytics_upload",)}
    assert {v: (s.min_sdk, s.raise_min_sdk) for v, s in specs.items()} == {"offline": (24, True), "multi": (24, True)}
    assert all(patch.loopback_port(s.loopback) for s in specs.values())
    apk, ks = tmp_path / "missing.apk", tmp_path / "k"
    with pytest.raises(SystemExit, match="no variant 'single'"):
        patch.patch(apk, tmp_path / "o.apk", "single", ks, None, env={}, patch_file=patch_file)
    with pytest.raises(SystemExit, match="for the offline variant"):
        patch.patch(apk, tmp_path / "o.apk", "multi", ks, None, server_dir=tmp_path, env={}, patch_file=patch_file)


def test_option_sets():
    """Each build option names a rules set; the variant's own sets come first."""
    plan = patch.plan_for(["local_battles"], {"guest_login": True, "ad_rewards": False, "skip_rate_prompt": True,
                                              "store_timers": True, "skip_consent": True})
    assert plan == patch.Plan(("local_battles", "guest_login", "rate_prompt", "store_timers"), ("consent",))
    assert patch.plan_for([], {}) == patch.Plan()
    with pytest.raises(SystemExit, match="have no lib/store_timers"):
        patch.check_plan(plan, {"client": "x", "lib": {"local_battles": {}, "guest_login": {}, "rate_prompt": {}},
                                "dex": {"consent": {}}})


def test_apply_edits_checks_every_site_first():
    import hashlib

    data = bytes(range(16))
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    edits = [{"note": "a", "offset": 2, "length": 2, "original_sha256": sha(b"\x02\x03"), "new": "ffff"},
             {"note": "b", "offset": 8, "length": 2, "original_sha256": sha(b"\x00\x00"), "new": "eeee"},
             {"note": "c", "offset": 15, "length": 2, "original_sha256": sha(b"\x0f"), "new": "dddd"}]
    with pytest.raises(SystemExit, match=r"(?s)b @ 0x8: the 2 bytes there are not the expected ones.*c @ 0xf"):
        patch.apply_edits(data, edits, "lib")
    assert patch.apply_edits(data, edits[:1], "lib")[:5] == bytes([0, 1, 0xFF, 0xFF, 4])
    with pytest.raises(SystemExit, match="differ in length"):
        patch.apply_edits(data, [{"offset": 0, "length": 1, "original_sha256": sha(b"\0"), "new": "0000"}], "lib")


# --- versionCode -------------------------------------------------------------------------------------------------


@needs_apk
def test_real_v29_manifest_version_code(tmp_path):
    """The binary manifest's versionCode is replaced in place; versionName stays the game's."""
    mf = _input_manifest()
    assert patch.axml_version_code(mf) == patch.ORIGINAL_VERSION_CODE
    out = patch.axml_set_version_code(mf, 2026112000)
    assert patch.axml_version_code(out) == 2026112000 and len(out) == len(mf)
    dump = _xmltree(tmp_path, out)
    assert "versionCode(0x0101021b)=2026112000" in dump and 'versionName(0x0101021c)="29.0"' in dump
    with pytest.raises(SystemExit, match="above"):
        patch.axml_set_version_code(mf, patch.ORIGINAL_VERSION_CODE)


def _input_base(tmp_path: Path) -> Path:
    return patch._base_apk(patch.read_inputs(APK), tmp_path)


def _input_manifest() -> bytes:
    inputs = patch.read_inputs(APK)
    with zipfile.ZipFile(APK) as z:
        if not inputs.bundle:
            return z.read("AndroidManifest.xml")
        with z.open("base.apk") as f, zipfile.ZipFile(f) as base:
            return base.read("AndroidManifest.xml")


def _aapt2() -> Path:
    """aapt2 from the Android SDK: an independent reader of what the patcher writes (the test skips without it)."""
    from arcane_patcher import sdktools

    try:
        return sdktools.tool(sdktools.find_build_tools(tools=("aapt2",)), "aapt2")
    except SystemExit:
        pytest.skip("no Android SDK build-tools (aapt2) for the cross-check")


def _xmltree(tmp_path: Path, manifest: bytes) -> str:
    import subprocess

    with zipfile.ZipFile(tmp_path / "m.zip", "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
    return subprocess.run([str(_aapt2()), "dump", "xmltree", "--file", "AndroidManifest.xml",
                           str(tmp_path / "m.zip")], check=True, capture_output=True, text=True).stdout


@needs_apk
def test_real_v29_package_and_launcher_activity(tmp_path):
    pkg = patch.input_package(patch.read_inputs(APK))
    text, arsc = patch.manifest_text(_input_base(tmp_path))
    assert patch.manifest_package(text) == pkg
    game = patch.launcher_activity(text)
    assert game.startswith(pkg + ".")
    mf = patch.rebuild_manifest(text, arsc, lambda t: patch.rebrand_manifest(
        patch.launcher_manifest(t, FRAGMENTS["multiplayer"]), pkg + ".multiplayer", "Multiplayer"))
    off = patch.rebuild_manifest(text, arsc, lambda t: patch.launcher_manifest(t, FRAGMENTS["offline"]), min_sdk=24)
    assert "minSdkVersion(0x0101020c)=24" in _xmltree(tmp_path, off)
    dump = _xmltree(tmp_path, patch.axml_set_version_code(mf, 2026112000))
    assert "versionCode(0x0101021b)=2026112000" in dump and 'versionName(0x0101021c)="29.0"' in dump
    assert '"android.permission.CAMERA"' in dump
    for feature in ("android.hardware.camera", "android.hardware.camera.any", "android.hardware.camera.autofocus"):
        block = dump[dump.index(f'name(0x01010003)="{feature}"'):][:300]
        assert "required(0x0101028e)=false" in block, feature
    assert 'scheme(0x01010027)="arcane"' in dump and 'host(0x01010028)="connect"' in dump
    assert '"arcane.multiplayer.ScanActivity"' in dump and 'process(0x01010011)=":arcane_restart"' in dump
    assert f'value(0x01010024)="{game}"' in dump


@needs_apk
def test_binary_manifest_package_renamed(tmp_path):
    mf = _input_manifest()
    pkg = patch.axml_package(mf)
    out = patch.axml_replace_strings(mf, {pkg: pkg + ".offline"})
    assert patch.axml_replace_strings(mf, {"absent": "x"}) == mf
    tree = _xmltree(tmp_path, out)
    assert f'package="{pkg}.offline"' in tree and f'package="{pkg}"' not in tree


@needs_apk
def test_resource_table_package_renamed(tmp_path):
    import subprocess

    base = _input_base(tmp_path)
    with zipfile.ZipFile(base) as z:
        arsc, manifest = z.read("resources.arsc"), z.read("AndroidManifest.xml")
    pkg = patch.axml_package(manifest)
    out = patch.arsc_rename_package(arsc, pkg, pkg + ".offline")
    assert len(out) == len(arsc) and out != arsc
    with pytest.raises(SystemExit):
        patch.arsc_rename_package(arsc, "not.there", "x.y")
    with zipfile.ZipFile(tmp_path / "r.apk", "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("resources.arsc", out, compress_type=zipfile.ZIP_STORED)
    res = subprocess.run([str(_aapt2()), "dump", "resources", str(tmp_path / "r.apk")],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert f"Package name={pkg}.offline id=7f" in res.stdout


# --- the consent dialog -----------------------------------------------------------------------------------------

def test_dex_helpers():
    from arcane_patcher import dexpatch

    assert dexpatch._mutf8(b"a\xc0\x80b") == "a\0b"
    assert dexpatch._mutf8("\u00e9\u4e2d".encode("utf-8")) == "\u00e9\u4e2d"
    assert dexpatch._mutf8(b"\xed\xa0\xbd\xed\xb8\x80") == "\U0001f600"  # surrogate pair, 3 bytes each
    # invoke-direct {v1, v2, v0}, method@0x1234 (format 35c)
    assert dexpatch._invoke_direct(0x1234, 1, 2, 0) == [0x3070, 0x1234, 0x0021]


@needs_apk
def test_real_v29_consent_dex(tmp_path):
    from arcane_patcher import dexpatch

    base = _input_base(tmp_path)
    name, ui = rulesgen.find_consent_ui(base)
    assert ui.endswith(rulesgen.CONSENT_CLASS)
    out = rulesgen.legacy_consent_entries(base)
    assert list(out) == [name]
    with zipfile.ZipFile(base) as z:
        original = z.read(name)
    new = out[name]
    assert len(new) == len(original) and rulesgen.dex_defines(new, ui)
    answer = b'{"ads":"non_personalized"}'
    assert answer in new and dexpatch.ANSWER_SLOT.encode() in original and dexpatch.ANSWER_SLOT.encode() not in new
    # only the slot's text, the 6 code units and the header (checksum, signature) differ
    changed = [i for i, (a, b) in enumerate(zip(original, new)) if a != b]
    slot_at = original.index(dexpatch.ANSWER_SLOT.encode())
    body = [i for i in changed if i >= 32 and not slot_at <= i < slot_at + len(dexpatch.ANSWER_SLOT)]
    show = dexpatch.Dex(bytearray(new))
    idx = show.method_index(f"{ui}->showConsent(Ljava/lang/String;{ui[:ui.rindex('/') + 1]}IConsentListener;)V")
    [code] = [c for m, c in show.methods_with_code() if m == idx]
    assert struct.unpack_from("<H", new, code + 4)[0] >= 3  # outs_size covers the new 3-argument call
    instructions = [i for i in body if i >= code + 16]
    assert instructions and max(instructions) - min(instructions) < 12  # one 12-byte run of instructions
    assert set(body) - set(instructions) <= {code + 4}  # and, before them, only outs_size
    dx = dexpatch.Dex(bytearray(new))
    assert dx.finish() == new  # checksum and signature are right
    # a dex that is not the expected one is refused, naming why
    with pytest.raises(dexpatch.DexPatchError, match="string"):
        dexpatch.patch_consent(original.replace(dexpatch.ANSWER_SLOT.encode()[:30], b"x" * 30), ui, "{}")
    with pytest.raises(dexpatch.DexPatchError, match="does not match"):
        dexpatch.patch_consent(new, ui, "{}")  # already patched


@needs_apk
def test_real_v29_multiplayer_entries(tmp_path):
    """The multiplayer setup screen + proxy dex under the next free dex name, the proxy's port."""
    from arcane_patcher import kit, patchfile

    if kit.problems():
        pytest.skip("no kit (python -m arcane_patcher.kitbuild)")
    pf = patchfile.PatchFile.open(make_patch_file(tmp_path / "pf", kit_dir=kit.KIT_DIR), keys=keys_for_tests())
    base = _input_base(tmp_path)
    out = patch.multiplayer_entries(base, "http://127.0.0.1:28180", pf)
    dex_name = patch.next_dex_name(base)
    assert set(out) == {dex_name, "assets/arcane_multiplayer/port"} and out["assets/arcane_multiplayer/port"] == b"28180"
    for cls in ("SetupActivity", "ProxyProvider", "LoopbackProxy", "ServerUrl", "ScanActivity", "RestartActivity",
                "JoinLink", "QrDecoder"):
        assert rulesgen.dex_defines(out[dex_name], f"Larcane/multiplayer/{cls};"), cls
    assert rulesgen.dex_defines(out[dex_name], "Lcom/google/zxing/qrcode/QRCodeReader;")  # embedded ZXing
