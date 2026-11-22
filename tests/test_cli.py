"""The arcane-patcher command line: arguments reach patch(); a patch file without a server is reported clearly."""

from pathlib import Path

import pytest

from arcane_patcher import cli, patch


def test_build_passes_arguments(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(patch, "patch", lambda *a, **k: seen.update(args=a, kw=k))
    monkeypatch.setattr(cli, "_doctor", lambda a: [])  # the requirements are doctor's tests
    assert cli.main(["build", "multi", "--apk", "in.apk", "--out", "o.apk", "--keystore", "k", "--label", "Mine",
                     "--no-ad-rewards", "--version-code", "2026112001"]) == 0
    assert seen["args"] == (Path("in.apk"), Path("o.apk"), "multi", Path("k"), "Mine")
    assert seen["kw"]["ad_rewards"] is False and seen["kw"]["guest_login"] is True
    assert seen["kw"]["version_code"] == 2026112001 and seen["kw"]["ks_alias"] == "androiddebugkey"
    with pytest.raises(SystemExit):
        cli.main(["build", "offline", "--apk", "a", "--out", "o", "--keystore", "k", "--emulator-zip", "z",
                  "--server-dir", "d"])


def test_only_two_variants_and_no_server_option():
    for argv in (["build", "single", "--apk", "a", "--out", "o", "--keystore", "k"],
                 ["build", "multi", "--apk", "a", "--out", "o", "--keystore", "k", "--server", "http://x"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2  # argparse usage error


def test_patch_file_options_reach_the_build(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(patch, "patch", lambda *a, **k: seen.update(kw=k))
    monkeypatch.setattr(cli, "_doctor", lambda a: [])
    cli.main(["build", "multi", "--apk", "a", "--out", "o", "--keystore", "k", "--patch", "p.zip",
              "--allow-unofficial"])
    assert seen["kw"]["patch_file"] == Path("p.zip") and seen["kw"]["allow_unofficial"] is True
    cli.main(["build", "multi", "--apk", "a", "--out", "o", "--keystore", "k"])
    assert seen["kw"]["patch_file"] is None and seen["kw"]["allow_unofficial"] is False


def test_offline_without_a_server(tmp_path, monkeypatch, capsys):
    """A patch file without a server release: the offline build is refused before anything is built, with the
    options that give one."""
    from conftest import make_patch_file

    pf = make_patch_file(tmp_path / "pf", sign=False)
    keystore = tmp_path / "k.keystore"
    keystore.write_bytes(b"x")
    out = tmp_path / "out" / "offline.apk"
    with pytest.raises(SystemExit, match="cannot build"):
        cli.main(["build", "offline", "--apk", str(tmp_path / "a.apk"), "--out", str(out), "--keystore", str(keystore),
                  "--patch", str(pf), "--allow-unofficial"])
    printed = capsys.readouterr().out
    assert "holds no server release" in printed and "--emulator-zip PATH" in printed
    assert not out.parent.exists()
