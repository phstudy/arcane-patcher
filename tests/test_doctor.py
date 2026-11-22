"""doctor (what a build needs, with hints: the patch file first), the kit check, and the window's commands (it only
fills in options)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from conftest import APK, fake_kit, keys_for_tests, make_patch_file, needs_apk

from arcane_patcher import cli, doctor, gui, kit


def test_kit_checks(tmp_path):
    assert "the kit is missing" in kit.problems(tmp_path / "none")[0]
    k = fake_kit(tmp_path / "kit")
    assert kit.problems(k) == []
    (k / "offline.dex").write_bytes(b"other")
    assert kit.problems(k) == ["kit file damaged: offline.dex"]
    info = json.loads((k / "kit.json").read_text())
    info["inputs"] = "0" * 64
    (k / "kit.json").write_text(json.dumps(info))
    assert any("other sources" in p for p in kit.problems(k))


def test_kit_inputs_follow_the_sources(tmp_path):
    src = tmp_path / "android"
    shutil.copytree(kit.ANDROID_DIR, src)
    before = kit.inputs_hash(src)
    java = next(src.rglob("*.java"))
    java.write_text(java.read_text() + "\n// changed\n")
    assert kit.inputs_hash(src) != before


@pytest.fixture
def no_default_patch_file(monkeypatch, tmp_path):
    from arcane_patcher import patchfile

    monkeypatch.setattr(patchfile, "search_dirs", lambda: [tmp_path / "nowhere"])


def test_doctor_lists_missing_pieces_with_hints(tmp_path, no_default_patch_file):
    found = doctor.checks("multi", apk=tmp_path / "game.apk", keystore=tmp_path / "k.keystore",
                          out=tmp_path / "out.apk", env={})
    by = {c.name: c for c in found}
    assert not by["patch file"].ok and "arcane-patch-<version>.zip" in by["patch file"].hint
    assert not by["APK"].ok and "v29.0" in by["APK"].hint
    assert not by["keystore"].ok and "keystore new" in by["keystore"].hint
    assert by["output"].ok
    text = doctor.report(found)
    assert "[missing] patch file" in text and "-> download the current arcane-patch" in text
    assert {c.name for c in doctor.blocking(found)} == {"patch file", "APK", "keystore"}


def test_doctor_patch_file_trust(tmp_path, monkeypatch):
    """Found, signature, compatible: an official file passes; an unsigned one is blocking unless allowed, then a
    warning; one for a newer patcher is blocking."""
    from arcane_patcher import patchfile

    official = make_patch_file(tmp_path / "a")
    monkeypatch.setattr(patchfile, "TRUSTED_KEYS", keys_for_tests())
    by = {c.name: c for c in doctor.checks(patch_file=official)}
    assert by["patch file"].ok and "version 1.2.3" in by["patch file"].detail and "v29.0" in by["patch file"].detail
    assert by["signature"].ok and "official" in by["signature"].detail and by["compatible"].ok
    unsigned = make_patch_file(tmp_path / "b", sign=False)
    by = {c.name: c for c in doctor.checks(patch_file=unsigned)}
    assert not by["signature"].ok and by["signature"].blocking and "--allow-unofficial" in by["signature"].hint
    by = {c.name: c for c in doctor.checks(patch_file=unsigned, allow_unofficial=True)}
    assert not by["signature"].ok and not by["signature"].blocking and "UNOFFICIAL" in by["signature"].detail
    newer = make_patch_file(tmp_path / "c", edit=lambda info, files: info.update(min_patcher="99.0"))
    by = {c.name: c for c in doctor.checks(patch_file=newer)}
    assert not by["compatible"].ok and "download the current patcher" in by["compatible"].hint
    broken = tmp_path / "arcane-patch-9.zip"
    broken.write_bytes(b"not a zip")
    by = {c.name: c for c in doctor.checks(patch_file=broken)}
    assert not by["patch file"].ok and "not a patch file" in by["patch file"].detail


def test_doctor_rules_for_the_apk(tmp_path, monkeypatch, patch_file):
    """With an APK: whether the patch file has rules for its game library's build-id."""
    from arcane_patcher import patch

    monkeypatch.setattr(patch, "read_inputs", lambda apk: patch.Inputs(Path(apk), []))
    monkeypatch.setattr(patch, "input_package", lambda inputs: "com.example.game")
    monkeypatch.setattr(patch, "read_game_lib", lambda inputs: b"\x7fELF" + bytes(0x100))  # no build-id
    monkeypatch.setattr(doctor, "_patch_file", lambda p, allow: ([], patch_file))
    by = {c.name: c for c in doctor.checks("multi", apk=tmp_path / "game.apk")}
    assert by["APK"].ok and not by["rules"].ok and "has no rules for build-id none" in by["rules"].detail
    assert "v29.0 build 1587910 (build-id 7dfd46f8a9ca" in by["rules"].detail


def test_doctor_offline_needs_a_server(tmp_path, monkeypatch, patch_file):
    monkeypatch.setattr(doctor, "_patch_file", lambda p, allow: ([], patch_file))
    found = {c.name: c for c in doctor.checks("offline", env={})}
    assert not found["server"].ok and "--emulator-zip" in found["server"].hint
    assert found["downloads"].blocking is False
    found = {c.name: c for c in doctor.checks("offline", emulator_zip=tmp_path, env={})}
    assert not found["server"].ok  # a folder is not a zip
    assert "server" not in {c.name for c in doctor.checks("multi", env={})}


def test_build_stops_on_missing_pieces_before_building(tmp_path, monkeypatch, capsys, no_default_patch_file):
    from arcane_patcher import patch

    def no_build(*a, **k):
        raise AssertionError("the build started")

    monkeypatch.setattr(patch, "patch", no_build)
    with pytest.raises(SystemExit, match="cannot build"):
        cli.main(["build", "multi", "--apk", str(tmp_path / "game.apk"), "--out", str(tmp_path / "o.apk"),
                  "--keystore", str(tmp_path / "k.keystore")])
    assert "-> " in capsys.readouterr().out  # the hints are printed


def test_doctor_exit_code(tmp_path, capsys, no_default_patch_file):
    assert cli.main(["doctor", "multi", "--apk", str(tmp_path / "missing.apk")]) == 1
    assert "thing(s) missing" in capsys.readouterr().out


def test_no_arguments_opens_the_window(monkeypatch):
    opened = []
    monkeypatch.setattr(gui, "run", lambda: opened.append(1) or 0)
    assert cli.main([]) == 0 and opened == [1]


def test_window_only_fills_in_command_line_options():
    f = gui.Form(variant="offline", apk="/x/game.apkm", keystore="/x/k.keystore", ks_alias="me", password="secret",
                 label="Mine", emulator_zip="/x/server.zip", patch_file="/x/arcane-patch-1.0.0.zip",
                 allow_unofficial=True)
    f.flags["ad_rewards"] = False
    cmd = gui.build_command(f)
    assert cmd[:len(gui.program())] == gui.program()
    args = cmd[len(gui.program()):]
    assert args[:2] == ["build", "offline"]
    assert args[args.index("--apk") + 1] == "/x/game.apkm" and args[args.index("--out") + 1] == "/x/game-offline"
    assert args[args.index("--patch") + 1] == "/x/arcane-patch-1.0.0.zip" and "--allow-unofficial" in args
    assert "--patch" not in gui.build_command(gui.Form(apk="/x/a.apk"))  # empty: the command line's default
    assert "--no-ad-rewards" in args and "--guest-login" in args and args[args.index("--label") + 1] == "Mine"
    assert "secret" not in cmd  # the password goes through the environment only
    assert gui.child_env(f)["ARCANE_KS_PASS"] == "secret"
    # every option the window passes is one the command line knows
    cli.parser().parse_args(args)
    cli.parser().parse_args(gui.doctor_command(f)[len(gui.program()):])
    cli.parser().parse_args(gui.keystore_command("/x/new.keystore", "me")[len(gui.program()):])
    assert gui.default_out("/x/game.apk", "multi") == "/x/game-multiplayer.apk"


def test_program_runs_this_package():
    assert gui.program() == [sys.executable, "-m", "arcane_patcher"]


@needs_apk
def test_real_v29_doctor_rules_apply(patch_file, monkeypatch):
    monkeypatch.setattr(doctor, "_patch_file", lambda p, allow: ([], patch_file))
    for variant in ("offline", "multi"):
        by = {c.name: c for c in doctor.checks(variant, apk=APK)}
        assert by["rules"].ok and "rule sets apply" in by["rules"].detail, by["rules"].detail
