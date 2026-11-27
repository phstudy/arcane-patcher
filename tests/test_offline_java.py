"""The offline variant's choice of server runtime on a desktop JVM (android/offline/java ServerRuntime uses no Android
classes): the runtime for the device's real CPU (Build.SUPPORTED_ABIS), and the fallback to the next runtime when the
chosen one does not start. Needs a JDK."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from arcane_patcher import kit, offline

JAVA_SRC = kit.ANDROID_DIR / "offline" / "java" / "arcane" / "offline" / "ServerRuntime.java"
TABLE = offline.runtimes_table().decode().strip().replace("\n", ";")  # the APK's table (kit.SERVER_ABIS)
pytestmark = pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")), reason="no JDK")


@pytest.fixture(scope="module")
def classes(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("classes")
    subprocess.run(["javac", "-nowarn", "--release", "11", "-d", str(out), str(JAVA_SRC)], check=True)
    return out


def run(classes: Path, *args: str) -> list[str]:
    return subprocess.run(["java", "-cp", str(classes), "arcane.offline.ServerRuntime", *args], check=True,
                          capture_output=True, text=True).stdout.splitlines()


@pytest.mark.parametrize("abis, forced, order", [
    ("arm64-v8a,armeabi-v7a,armeabi", "-", "[arm64, x86_64]"),  # a phone
    ("x86_64,x86,arm64-v8a,armeabi-v7a,armeabi", "-", "[x86_64, arm64]"),  # an x86_64 emulator with ARM translation
    ("x86,armeabi-v7a", "-", "[x86_64, arm64]"),  # x86 listed before any arm ABI
    ("arm64-v8a,x86_64", "-", "[arm64, x86_64]"),  # an arm ABI first
    ("riscv64", "-", "[arm64, x86_64]"),  # none known: the table's first
    ("", "-", "[arm64, x86_64]"),
    ("arm64-v8a,armeabi-v7a", "x86_64", "[x86_64, arm64]"),  # the debug property forces the first try
    ("x86_64", "mips", "[x86_64, arm64]"),  # an unknown forced name is ignored
])
def test_runtime_follows_the_devices_cpu(classes, abis, forced, order):
    assert run(classes, "order", TABLE, abis, forced) == [order]


def test_a_runtime_that_starts_is_kept(classes):
    assert run(classes, "start", TABLE, "ok") == [
        "server runtime arm64: starting libarcanepython.so", "server runtime arm64: running", "runtime arm64"]


@pytest.mark.parametrize("outcome", [
    "exit:2:/data/app/x/lib/arm64/libarcanepython.so[1]: syntax error: unexpected '('",  # the shell read an ELF
    "exit:127:arcanepython: cannot load /data/app/x/lib/arm64/libpython3.14.so: dlopen failed",
    "exit:1:",  # ended at once, whatever it said
    "alive:CANNOT LINK EXECUTABLE \"/data/app/x/libarcanepython.so\": library \"libpython3.14.so\" not found",
    "alive:/system/bin/sh: /data/app/x/libarcanepython.so: cannot execute: Exec format error",
    "throw:Cannot run program: error=8, Exec format error",
])
def test_a_runtime_that_does_not_start_falls_back_to_the_next_once(classes, outcome):
    out = run(classes, "start", TABLE, outcome, "ok")
    assert out[0] == "server runtime arm64: starting libarcanepython.so"
    assert out[1].startswith("server runtime arm64 failed to start") and out[1].endswith("trying x86_64")
    assert out[2:] == ["server runtime x86_64: starting libarcanepython_x86_64.so", "server runtime x86_64: running",
                       "runtime x86_64"]


def test_a_healthy_start_that_mentions_such_words_is_not_a_failure(classes):
    """Only the shell's and the linker's lines (naming the executable) count, not the server's own output."""
    assert run(classes, "start", TABLE, "alive:INFO arcane_server: news image not found, skipped")[-1] == "runtime arm64"


def test_when_no_runtime_starts_the_first_one_is_kept(classes):
    out = run(classes, "start", TABLE, "exit:2:syntax error", "exit:127:arcanepython: cannot load x")
    assert out[-2:] == ["no server runtime started; arm64 is retried", "runtime arm64"]
    assert len([line for line in out if "failed to start" in line]) == 2  # each tried once
