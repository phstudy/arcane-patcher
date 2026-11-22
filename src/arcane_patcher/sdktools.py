"""The Android SDK, NDK and JDK, for maintainers only: building the kit (kitbuild.py) and cross-checks in the tests.
A user's build never needs any of them."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path


class ToolError(SystemExit):
    """A missing SDK / NDK / JDK tool, reported as a clear message."""


NDK_VERSION = "27.3.13750724"


def android_sdk() -> Path:
    """The Android SDK: ANDROID_HOME / ANDROID_SDK_ROOT, else the default location of Android Studio's SDK."""
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(var):
            return Path(os.environ[var])
    if sys.platform == "darwin":
        return Path.home() / "Library/Android/sdk"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Android/Sdk"
    return Path.home() / "Android/Sdk"


def find_build_tools(sdk: Path | None = None, tools: tuple[str, ...] = ("d8",)) -> Path:
    """The newest build-tools directory holding every tool in `tools`."""
    sdk = sdk or android_sdk()
    root = sdk / "build-tools"
    if not root.is_dir():
        raise ToolError(f"no Android SDK build-tools under {sdk} (set ANDROID_HOME)")
    versions = sorted(root.iterdir(), key=lambda p: [int(x) if x.isdigit() else 0 for x in p.name.split(".")])
    for v in reversed(versions):
        if all((v / t).exists() or (v / f"{t}.bat").exists() or (v / f"{t}.exe").exists() for t in tools):
            return v
    raise ToolError(f"no build-tools with {' / '.join(tools)} found under {root}")


def tool(bt: Path, name: str) -> Path:
    """A build-tools program (its .bat / .exe form on Windows)."""
    for candidate in (bt / name, bt / f"{name}.bat", bt / f"{name}.exe"):
        if candidate.exists():
            return candidate
    raise ToolError(f"{name} not found in {bt}")


def android_jar(sdk: Path | None = None) -> Path:
    sdk = sdk or android_sdk()
    jars = sorted((sdk / "platforms").glob("android-*/android.jar"),
                  key=lambda p: int(re.sub(r"\D", "", p.parent.name.split("-")[1]) or 0))
    if not jars:
        raise ToolError(f"no Android platform android.jar found under {sdk}")
    return jars[-1]


def ndk_tool(name: str) -> Path:
    prebuilt = android_sdk() / "ndk" / NDK_VERSION / "toolchains/llvm/prebuilt"
    if not prebuilt.is_dir():
        raise ToolError(f"building the kit needs the Android NDK {NDK_VERSION} ({prebuilt} not found)")
    found = next(prebuilt.iterdir()) / "bin"
    for candidate in (found / name, found / f"{name}.cmd", found / f"{name}.exe"):
        if candidate.exists():
            return candidate
    raise ToolError(f"{name} not found in {found}")


def javac() -> str:
    found = shutil.which("javac")
    if found is None:
        raise ToolError("javac not found on PATH (a JDK 17+ is needed to build the kit)")
    return found
