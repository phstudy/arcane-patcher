"""rulesgen.py (maintainer tool): the patch definitions (A64 code at fixed offsets, keyed by build-id), the
third-party services removed from the manifest, and the rules they are written as. The rules carry no bytes of the
game: an edit names its site's original bytes only by length and sha256. The committed rules (patchdata/rules/)
must be what rulesgen writes for the real v29 APK, and applying them must give the same bytes as the definitions.
Tests use synthetic bytes; those that need the real APK run only when ARCANE_TEST_APK names it."""

import hashlib
import json
import struct

import pytest
from conftest import APK, DATA, needs_apk

from arcane_patcher import patch, rulesgen

GUEST_AT = 0x14189A8
MOV_W0_1_RET = bytes.fromhex("20008052c0035fd6")


def _synthetic_so(size: int = 0x3000000) -> bytes:
    """Stand-in game library bytes (not the game's): a repeating pattern, so every site has some hashable content."""
    return (bytes(range(251)) * (size // 251 + 1))[:size]


def _rules(patch_sets: dict, so: bytes, key: str = "set") -> dict:
    return {"lib": {key: {"title": key, "edits": rulesgen.lib_edits(patch_sets, so)}}}


def test_edits_hold_only_a_hash_of_the_original():
    so = _synthetic_so()
    [e] = rulesgen.lib_edits(rulesgen.GUEST_LOGIN_PATCHES, so)
    assert set(e) == {"note", "offset", "length", "original_sha256", "new"}
    assert e["offset"] == GUEST_AT and e["length"] == 8 and e["new"] == MOV_W0_1_RET.hex()
    assert e["original_sha256"] == hashlib.sha256(so[GUEST_AT:GUEST_AT + 8]).hexdigest()
    out = patch.patch_game_lib(so, patch.Plan(lib=("set",)), _rules(rulesgen.GUEST_LOGIN_PATCHES, so))
    assert out[GUEST_AT:GUEST_AT + 8] == MOV_W0_1_RET
    assert out[:GUEST_AT] == so[:GUEST_AT] and out[GUEST_AT + 8:] == so[GUEST_AT + 8:]


def test_rules_refuse_other_bytes():
    so = _synthetic_so()
    rules = _rules(rulesgen.GUEST_LOGIN_PATCHES, so, "guest_login")
    other = bytearray(so)
    other[GUEST_AT] ^= 0xFF
    with pytest.raises(SystemExit, match=r"(?s)guest_login.*@ 0x14189a8: the 8 bytes there are not the expected ones"):
        patch.patch_game_lib(bytes(other), patch.Plan(lib=("guest_login",)), rules)
    with pytest.raises(SystemExit, match="beyond the end"):
        rulesgen.lib_edits(rulesgen.GUEST_LOGIN_PATCHES, so[:GUEST_AT + 4])


def test_rules_apply_like_the_definitions():
    so = _synthetic_so()
    build = rulesgen.CLIENT_BUILDS[rulesgen.V29_LIB_BUILD_ID]
    for field in rulesgen.LIB_SETS:
        sets = getattr(build, field)
        assert patch.patch_game_lib(so, patch.Plan(lib=("set",)), _rules(sets, so)) == rulesgen.apply_definitions(so, sets)


def test_unknown_builds_are_refused():
    with pytest.raises(SystemExit, match="unsupported build-id none"):
        rulesgen.client_build(b"\x7fELF" + bytes(0x100))


def test_rules_cover_every_definition_without_overlaps():
    """Every set of the definitions is a rules set; the edits of one build never overlap."""
    so = _synthetic_so()
    build = rulesgen.CLIENT_BUILDS[rulesgen.V29_LIB_BUILD_ID]
    sites = sorted((e["offset"], e["length"]) for field in rulesgen.LIB_SETS
                   for e in rulesgen.lib_edits(getattr(build, field), so))
    assert all(a + n <= b for (a, n), (b, _) in zip(sites, sites[1:]))


def test_ad_reward_and_rate_prompt_code():
    so = _synthetic_so()
    out = rulesgen.apply_definitions(so, rulesgen.AD_REWARD_PATCHES)
    assert out[0x112E294:0x112E298] == bytes.fromhex("88420439")  # _showOfferWallAndRV = w8 (1)
    assert out[0x1137AA4:0x1137AA8] == bytes.fromhex("1f2003d5")  # the A/B tag no longer sets it
    assert out[0x1130480:0x1130484] == bytes.fromhex("20000014")  # a tap counts as a finished video
    out = rulesgen.apply_definitions(so, rulesgen.RATE_PROMPT_PATCHES)
    assert out[0x1A2A1D4:0x1A2A1DC] == bytes.fromhex("00008052c0035fd6")  # never ready


def _scroll_rect_cases(so: bytes) -> dict[int, int]:
    """Store tab kind -> the list the store updates for that tab (a field offset; None: no list), read by following
    the tab lookup the way the game does, before or after the patch."""
    out = {}
    for size in range(1, 13):
        target = rulesgen.STORE_SCROLL_RECT_TABLE + struct.unpack_from("<i", so, rulesgen.STORE_SCROLL_RECT_TABLE + 4 * (size - 1))[0]
        if target == rulesgen.STORE_SCROLL_RECT_RET:
            out[size] = None
            continue
        words = struct.unpack_from("<4I", so, target)
        assert words[0] == rulesgen.A64.mov("x0", "x8") and words[1] == rulesgen.A64.bl(target + 4, rulesgen.STORE_VIEW_GET_FIELDS)
        w = words[2] if words[2] >> 23 == 0x1A5 else words[3]
        out[size] = (w >> 5 & 0xFFFF) if w >> 23 == 0x1A5 else (w >> 10 & 0xFFF) * 8
    return out


def test_store_timer_patch_points_every_listed_tab_at_its_code():
    """Synthetic: the tab table entries of the tabs with a list are rewritten (the others are not touched)."""
    so = _synthetic_so()
    out = rulesgen.apply_definitions(so, rulesgen.STORE_TIMER_PATCHES)
    sizes = {size for size, _ in rulesgen.STORE_SCROLL_RECT_FIELDS}
    for size in range(1, 13):
        at = rulesgen.STORE_SCROLL_RECT_TABLE + 4 * (size - 1)
        assert (out[at:at + 4] != so[at:at + 4]) == (size in sizes), size
    for size, field in rulesgen.STORE_SCROLL_RECT_FIELDS:
        at = rulesgen.STORE_SCROLL_RECT_TABLE + 4 * (size - 1)
        target = rulesgen.STORE_SCROLL_RECT_TABLE + struct.unpack_from("<i", out, at)[0]
        words = struct.unpack_from("<3I", out, target)
        assert words[:2] == (rulesgen.A64.mov("x0", "x8"), rulesgen.A64.bl(target + 4, rulesgen.STORE_VIEW_GET_FIELDS))
        assert words[2] == rulesgen.A64.movz("x9", field)


def test_manifest_section_removes_third_party_services():
    section = rulesgen.manifest_section()
    text = """<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="p.q">
    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="com.google.android.gms.permission.AD_ID"/>
    <application android:name=".App">
        <activity android:name="com.applovin.adview.AppLovinFullscreenActivity" android:exported="false"/>
        <service android:name="com.google.firebase.messaging.FirebaseMessagingService" android:exported="false">
            <intent-filter>
                <action android:name="com.google.firebase.MESSAGING_EVENT"/>
            </intent-filter>
        </service>
        <provider android:name="androidx.startup.InitializationProvider" android:authorities="p.q.androidx-startup">
            <meta-data android:name="com.unity3d.services.core.configuration.AdsSdkInitializer" android:value="androidx.startup"/>
            <meta-data android:name="androidx.work.WorkManagerInitializer" android:value="androidx.startup"/>
        </provider>
        <provider android:name="com.google.firebase.provider.FirebaseInitProvider" android:authorities="p.q.fip"/>
        <meta-data android:name="firebase_crashlytics_collection_enabled" android:value="true"/>
        <activity android:name=".Main"/>
    </application>
</manifest>
"""
    out = patch.strip_manifest(text, section)
    for gone in ("AD_ID", "AppLovin", "FirebaseMessagingService", "MESSAGING_EVENT", "AdsSdkInitializer"):
        assert gone not in out, gone
    for kept in ("android.permission.INTERNET", "InitializationProvider", "WorkManagerInitializer", "FirebaseInitProvider",
                 'android:name=".Main"'):
        assert kept in out, kept
    assert out.count("firebase_crashlytics_collection_enabled") == 1
    assert '<meta-data android:name="firebase_crashlytics_collection_enabled" android:value="false"/>' in out
    assert '<meta-data android:name="google_analytics_adid_collection_enabled" android:value="false"/>' in out
    from arcane_patcher import axml

    axml.encode(out)  # still a well-formed manifest


def test_dex_edits_hold_changed_bytes_only():
    original = bytes(range(200))
    patched = bytearray(original)
    patched[8:12] = b"\1" * 4  # header (checksum): recomputed, not an edit
    patched[40:42] = b"\xaa\xaa"
    patched[60] = 0xBB  # a separate run: the unchanged bytes between are not carried
    patched[150] = 0xCC
    edits = rulesgen.dex_edits(original, bytes(patched), "n")
    assert [(e["offset"], e["new"]) for e in edits] == [(40, "aaaa"), (60, "bb"), (150, "cc")]
    assert patch.apply_edits(original, edits, "x")[32:] == bytes(patched[32:])


def test_committed_rules_are_rules_text():
    [f] = sorted((DATA / "rules").glob("*.json"))
    rules = json.loads(f.read_text())
    assert f.stem == rules["build_id"] == rulesgen.V29_LIB_BUILD_ID.hex() and rules["game_lib"] == patch.GAME_LIB
    assert rules["format"] == patch.RULES_FORMAT == rulesgen.RULES_FORMAT
    assert f.read_text() == rulesgen.rules_text(rules)  # written by rulesgen (formatting)
    assert set(rules["lib"]) == set(rulesgen.LIB_SETS.values())
    assert set(rules["dex"]) == {"consent", "analytics_upload"}
    assert rules["manifest"] == rulesgen.manifest_section()
    edits = [e for section in rules["lib"].values() for e in section["edits"]]
    edits += [e for section in rules["dex"].values() for es in section["files"].values() for e in es]
    for e in edits:
        assert set(e) == {"note", "offset", "length", "original_sha256", "new"}  # no original bytes
    # the sets the build options and variants name are all there
    assert {name for section, name in patch.OPTION_SETS.values()} <= set(rules["lib"]) | set(rules["dex"])
    variants = json.loads((DATA / "variants.json").read_text())
    for v in variants.values():
        assert set(v["lib_sets"]) <= set(rules["lib"]) and set(v["dex_sets"]) <= set(rules["dex"])


def test_consent_answer_is_the_privacy_protective_one():
    assert rulesgen.CONSENT_STATUS == {"ads": "non_personalized"}  # no age, no terms


# --- with the real APK -------------------------------------------------------------------------------------------


def _v29_so() -> bytes:
    return patch.read_game_lib(patch.read_inputs(APK))


@needs_apk
def test_real_v29_rules_regenerate_identically():
    """rulesgen on the real APK writes exactly the committed rules."""
    rules = rulesgen.generate(APK)
    assert rulesgen.rules_text(rules) == (DATA / "rules" / f"{rules['build_id']}.json").read_text()


@needs_apk
def test_real_v29_rules_apply_like_the_definitions(tmp_path):
    """The applier with the committed rules gives the same bytes as the definitions written in directly: the game
    library for every combination of sets a build makes, and the consent dex."""
    so = _v29_so()
    assert patch.elf_build_id(so) == rulesgen.V29_LIB_BUILD_ID
    rules = json.loads((DATA / "rules" / f"{patch.elf_build_id(so).hex()}.json").read_text())
    build = rulesgen.CLIENT_BUILDS[rulesgen.V29_LIB_BUILD_ID]
    for lib in (("local_battles", "guest_login", "ad_rewards", "rate_prompt", "store_timers"), (), ("guest_login",
                "rate_prompt")):
        expected = so
        for field, key in rulesgen.LIB_SETS.items():
            if key in lib:
                expected = rulesgen.apply_definitions(expected, getattr(build, field))
        assert patch.patch_game_lib(so, patch.Plan(lib=lib), rules) == expected
    base = tmp_path / "base.apk"
    base.write_bytes(rulesgen.base_apk_bytes(APK))
    assert patch.dex_entries(base, patch.Plan(dex=("consent",)), rules) == rulesgen.legacy_consent_entries(base)


@needs_apk
def test_real_v29_code(tmp_path):
    so = _v29_so()
    out = rulesgen.apply_definitions(so, {**rulesgen.CLIENT_PATCHES, **rulesgen.GUEST_LOGIN_PATCHES})
    a = rulesgen.A64
    word = lambda at: int.from_bytes(out[at:at + 4], "little")  # noqa: E731
    assert out[GUEST_AT:GUEST_AT + 8] == MOV_W0_1_RET
    assert out[0x1418820:0x1418824] == so[0x1418820:0x1418824]  # the login still asks the patched guest check
    # the battle starts locally; the result is reported with the battle id / match id
    assert word(0x11F96C8) == a.bl(0x11F96C8, rulesgen.LOCAL_BATTLE_NEW)
    assert word(0xFA4874) == a.mov("x24", "x8") and word(0xFA4E24) == a.b(0xFA4E24, 0xFA487C)
    assert word(0x11F9540) == a.movz("w0", 0)


@needs_apk
def test_real_v29_store_timer_patch():
    so = _v29_so()
    before = _scroll_rect_cases(so)
    assert before[10] is None  # the timed store cards' tab: no list is updated
    after = _scroll_rect_cases(rulesgen.apply_definitions(so, rulesgen.STORE_TIMER_PATCHES))
    assert after == {**before, 10: 0x70}  # now its cards' list; every other tab unchanged


def test_publisher_service_patches_finish_at_once():
    so = _synthetic_so()
    out = rulesgen.apply_definitions(so, rulesgen.PUBLISHER_SERVICE_PATCHES)
    a = rulesgen.A64
    word = lambda at: int.from_bytes(out[at:at + 4], "little")  # noqa: E731
    for entry, finished in ((rulesgen.IAP_CATALOG_REQUEST, "x1"), (rulesgen.IAP_PRODUCTS_QUERY, "x2")):
        assert word(entry) == a.cbz(finished, entry, entry + 16) and word(entry + 4) == a.mov("x0", finished)
        assert word(entry + 12) == a.b(entry + 12, rulesgen.ACTION_INVOKE) and word(entry + 16) == a.RET
    assert word(rulesgen.ACCOUNT_STATUS_ACTIVE_CHECK) == a.NOP


def test_return_at_once_makes_the_whole_method_return_void():
    """A void method's code becomes return-void and nops (the verifier decodes every unit)."""
    from arcane_patcher import dexpatch

    if APK is None or not APK.is_file():
        pytest.skip("ARCANE_TEST_APK not set")
    import io
    import zipfile

    base = rulesgen.base_apk_bytes(APK)
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        dexes = {n: z.read(n) for n in z.namelist() if n.startswith("classes") and n.endswith(".dex")}
    files = rulesgen.return_at_once_edits(dexes, rulesgen.ANALYTICS_UPLOAD_METHODS)
    for name, edits in files.items():
        out = patch.apply_edits(dexes[name], edits, name)
        dx = dexpatch.Dex(bytearray(out))
        for m, code in dx.methods_with_code():
            if dx.method(m) in rulesgen.ANALYTICS_UPLOAD_METHODS:
                regs, insns, units = dx.code_units(code)
                assert units[0] == 0x000E and not any(units[1:])
    with pytest.raises(SystemExit, match="defined with code 0 times"):
        rulesgen.return_at_once_edits(dexes, ("Lno/Such;->m()V",))
