# PyInstaller spec of the release download: one executable per platform holding the patcher (a generic applier) and the
# Android attribute table. What a build changes, the kit and the server come in the release's patch file
# (arcane-patch-<version>.zip, release.py), shipped next to the executable.
#   uv sync --locked --group release
#   uv run python packaging/notices.py --out build/THIRD_PARTY_NOTICES.txt
#   uv run pyinstaller --noconfirm --clean packaging/arcane-patcher.spec      (from the repository root, on CPython 3.14)
# Use a final 3.14 release (python.org, actions/setup-python, or a current uv-managed CPython): early 3.14 betas of
# the standalone builds abort inside Tk on Linux as soon as a window has a few widgets.
# The result is dist/arcane-patcher (dist/arcane-patcher.exe on Windows).
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

a = Analysis([str(ROOT / "packaging" / "entry.py")], pathex=[str(ROOT / "src")], datas=datas,
             hiddenimports=["arcane_patcher.gui", "tkinter", "tkinter.ttk", "tkinter.filedialog",
                            "tkinter.messagebox", "tkinter.simpledialog"],
             excludes=["pytest", "segno"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="arcane-patcher", console=True, upx=False,
          strip=False, runtime_tmpdir=None)
