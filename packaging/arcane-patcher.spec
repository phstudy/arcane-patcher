# PyInstaller spec of the release download: one executable per platform holding the patcher (a generic applier) and the
# Android attribute table. What a build changes, the kit and the server come in the release's patch file
# (arcane-patch-<version>.zip, release.py), shipped next to the executable.
#   uv sync --locked --group release
#   uv run python packaging/notices.py --out build/THIRD_PARTY_NOTICES.txt
#   uv run pyinstaller --noconfirm --clean packaging/arcane-patcher.spec      (from the repository root, on CPython 3.14)
# Use a final 3.14 release (python.org, actions/setup-python, or a current uv-managed CPython): early 3.14 betas of
# the standalone builds abort inside Tk on Linux as soon as a window has a few widgets.
# The result is dist/arcane-patcher (dist/arcane-patcher.exe on Windows).
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
PKG = ROOT / "src" / "arcane_patcher"

try:  # the window needs Tk: a Python without it would make a download that cannot open its window
    import tkinter  # noqa: F401
except ImportError:
    raise SystemExit("this Python has no tkinter; package with a Python that includes Tk (e.g. uv's managed CPython)")

NOTICES = ROOT / "build" / "THIRD_PARTY_NOTICES.txt"
if not NOTICES.is_file():
    raise SystemExit("write the third-party notices first: python packaging/notices.py --out build/THIRD_PARTY_NOTICES.txt")

# the kit travels in the patch file, not in the program
datas = collect_data_files("arcane_patcher", excludes=["**/__pycache__/**", "kit/**", "android/**"])
datas += [(str(ROOT / "LICENSE"), "."), (str(NOTICES), ".")]
# macOS: the release's patch file inside the app (ARCANE_BUNDLED_PATCH), signed with it; see patchfile.search_dirs
if sys.platform == "darwin" and os.environ.get("ARCANE_BUNDLED_PATCH"):
    datas += [(os.environ["ARCANE_BUNDLED_PATCH"], "patch")]

a = Analysis([str(ROOT / "packaging" / "entry.py")], pathex=[str(ROOT / "src")], datas=datas,
             hiddenimports=["arcane_patcher.gui", "tkinter", "tkinter.ttk", "tkinter.filedialog",
                            "tkinter.messagebox", "tkinter.simpledialog"],
             excludes=["pytest", "segno"], noarchive=False)
pyz = PYZ(a.pure)
# macOS: an .app bundle (Gatekeeper opens a notarized app with its ticket stapled, offline too; a bare
# command-line program depends on an online lookup that Finder does not always do), signed with a Developer ID
# (hardened runtime) when ARCANE_CODESIGN_IDENTITY names one; PyInstaller signs every bundled library with it.
# The command line stays available as arcane-patcher.app/Contents/MacOS/arcane-patcher. Elsewhere: one file.
if sys.platform == "darwin":
    identity = os.environ.get("ARCANE_CODESIGN_IDENTITY") or None
    entitlements = str(ROOT / "packaging" / "entitlements.plist") if identity else None
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="arcane-patcher", console=False, upx=False,
              strip=False, codesign_identity=identity, entitlements_file=entitlements)
    coll = COLLECT(exe, a.binaries, a.datas, name="arcane-patcher", upx=False, strip=False)
    app = BUNDLE(coll, name="arcane-patcher.app", bundle_identifier="io.github.phstudy.arcane-patcher",
                 version=None, info_plist={"NSHighResolutionCapable": True},
                 codesign_identity=identity, entitlements_file=entitlements)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="arcane-patcher", console=True, upx=False,
              strip=False, runtime_tmpdir=None)
