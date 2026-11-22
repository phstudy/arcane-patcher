"""What every release must carry for its licenses: the GPL header with the section 7 permission on every Android
source, third-party license texts for what the kit adds to an APK, the source location in a patch file."""

from pathlib import Path

from conftest import make_patch_file

from arcane_patcher import kit, patchfile, release

HEADER = (kit.ANDROID_DIR / "LICENSE_HEADER.txt").read_text()


def test_every_android_source_carries_the_license_header():
    sources = [f for f in kit.ANDROID_DIR.rglob("*") if f.suffix in (".java", ".c")]
    assert sources
    missing = [str(f.relative_to(kit.ANDROID_DIR)) for f in sources if not f.read_text().startswith(HEADER)]
    assert not missing, f"add android/LICENSE_HEADER.txt at the top of: {missing}"
    assert "SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking" in HEADER
    assert "section 7" in HEADER and "grants no rights to" in HEADER


def test_notice_texts_are_in_the_repository():
    lic = kit.ANDROID_DIR / "licenses"
    assert (lic / "Apache-2.0.txt").read_text().startswith("Apache License")
    assert "Permission is hereby granted" in (lic / "utf8_range-MIT.txt").read_text()
    assert "NOTICES.txt" in kit.FILES
    for f in ("tcl-license.terms", "tk-license.terms"):
        assert "Regents of the University of" in (Path(__file__).parents[1] / "packaging" / "licenses" / f).read_text()


def test_patch_file_names_its_source(tmp_path, monkeypatch):
    pf = patchfile.PatchFile.open(make_patch_file(tmp_path), keys={})
    assert pf.info["source"]["license"] == "GPL-3.0-only" and pf.info["source"]["tag"] == "v1.2.3"
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/arcane-patcher")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("GITHUB_REF_TYPE", "tag")
    monkeypatch.setenv("GITHUB_REF_NAME", "v2.0.0")
    assert release.source("2.0.0") == {"repository": "https://github.com/someone/arcane-patcher", "tag": "v2.0.0",
                                       "commit": "abc123", "license": "GPL-3.0-only"}


def test_executable_notices_cover_the_locked_runtime_dependencies():
    """packaging/notices.py lists the licenses of exactly what the executable bundles: arcane-patcher's runtime
    dependencies as uv.lock resolves them."""
    import importlib.util
    import tomllib

    lock = tomllib.loads((Path(__file__).parents[1] / "uv.lock").read_text())
    packages = {p["name"]: p for p in lock["package"]}
    closure: set[str] = set()

    def walk(name: str) -> None:
        for dep in packages[name].get("dependencies", []):
            if dep["name"] not in closure:
                closure.add(dep["name"])
                walk(dep["name"])

    walk("arcane-patcher")
    spec = importlib.util.spec_from_file_location("notices", Path(__file__).parents[1] / "packaging" / "notices.py")
    notices = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notices)
    assert set(notices.BUNDLED) == closure


def test_client_facing_code_carries_the_additional_permission():
    """Everything a build writes into the client names the section 7 permission (LICENSES/)."""
    import json

    root = Path(__file__).parents[1]
    text = (root / "LICENSES" / "AdditionRef-arcane-client-linking.txt").read_text()
    assert "section 7" in text and "grants no rights to the game client" in text
    py = (root / "src" / "arcane_patcher" / "LICENSE_HEADER_PY.txt").read_text()
    for name in ("rulesgen.py", "dexpatch.py", "patch.py"):
        assert (root / "src" / "arcane_patcher" / name).read_text().startswith(py), name
    for f in (root / "patchdata" / "rules").glob("*.json"):
        assert json.loads(f.read_text())["license"] == "GPL-3.0-only WITH AdditionRef-arcane-client-linking"
    for f in (root / "patchdata" / "manifest").glob("*.xml"):
        assert "AdditionRef-arcane-client-linking" in f.read_text().splitlines()[0]
