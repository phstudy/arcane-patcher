"""arcane-patcher: build a variant of your own v29 APK.

    arcane-patcher build {offline,multi} --apk PATH --out PATH --keystore PATH [--ks-alias A]
        [--patch FILE] [--allow-unofficial] [--label TEXT] [--package NAME] [--version-code N]
        [--emulator-zip PATH | --server-dir PATH]
    arcane-patcher doctor [{offline,multi}] [--apk PATH] [--out PATH] [--keystore PATH] ...
    arcane-patcher keystore new --out PATH [--ks-alias A]
    arcane-patcher gui
    arcane-patcher notices

The keystore's password comes from the environment variable ARCANE_KS_PASS ("android" for the alias androiddebugkey
when unset). `doctor` lists what a build with those options needs, with a hint for each missing piece; `build` runs
the same checks first. What a build changes comes from a patch file (arcane-patch-<version>.zip): --patch, else the
newest one next to the program or in the user's data folder; one not signed by the project's release key is refused
unless --allow-unofficial. Started without arguments (e.g. by a double-click), the program opens its window, a helper
that fills in these options. See README.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from arcane_patcher.patch import VARIANT_NAMES
from arcane_patcher.signing import DEBUG_ALIAS, PASSWORD_ENV

FLAGS = (("guest-login", "log in as guest without the sign-in popup"),
         ("ad-rewards", "rewarded-video placements grant their reward without a video"),
         ("skip-rate-prompt", "never show the store-rating popup"),
         ("store-timers", "store cards count down to their sale's end"),
         ("skip-consent", "answer the first-launch consent dialog without showing it"))


def _build_options(b: argparse.ArgumentParser, required: bool) -> None:
    b.add_argument("--apk", type=Path, required=required,
                   help="your v29 APK, or a split bundle (.apkm / .xapk / zip)")
    b.add_argument("--out", type=Path, required=required,
                   help="the output .apk (single APK input) or directory of splits (bundle input)")
    b.add_argument("--keystore", type=Path, required=required,
                   help=f"your signing keystore (password in the environment variable {PASSWORD_ENV}); "
                        "`arcane-patcher keystore new` makes one")
    b.add_argument("--ks-alias", default=DEBUG_ALIAS, help=f"the key's alias (default {DEBUG_ALIAS})")
    b.add_argument("--patch", type=Path, metavar="FILE",
                   help="the patch file (arcane-patch-<version>.zip; default: the newest next to the program or in "
                        "the user's data folder)")
    b.add_argument("--allow-unofficial", action="store_true",
                   help="accept a patch file not signed by the project's release key (it adds code to your app: only "
                        "if you trust its source)")
    src = b.add_mutually_exclusive_group()
    src.add_argument("--emulator-zip", type=Path, help="offline: a server release zip instead of the patch file's "
                                                       "(development)")
    src.add_argument("--server-dir", type=Path, help="offline: a server checkout instead of the patch file's release "
                                                     "(development)")
    for flag, what in FLAGS:
        b.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=True, help=f"{what} (default on)")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="arcane-patcher", description="Build a variant of your own v29 APK.",
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build one variant", allow_abbrev=False, description=(
        "offline: the game server runs on the phone; multi: the server (a multiplayer server with its battle "
        "server) is chosen on the phone at every launch."))
    b.add_argument("variant", choices=VARIANT_NAMES)
    _build_options(b, required=True)
    b.add_argument("--label", help="the launcher label (default: the patch file's, \"Offline\" / \"Multiplayer\")")
    b.add_argument("--package", help="install under this package name (default: the input's plus \".offline\" / "
                                     "\".multiplayer\")")
    b.add_argument("--version-code", type=int, help="android:versionCode (default: from the patch file's version, "
                                                    "so a build with a newer patch file installs as an update)")
    b.add_argument("--log-level", help="the client's default log level (e.g. debug)")

    d = sub.add_parser("doctor", help="list what a build needs, with hints for what is missing", allow_abbrev=False)
    d.add_argument("variant", nargs="?", choices=VARIANT_NAMES)
    _build_options(d, required=False)

    k = sub.add_parser("keystore", help="signing keystores", allow_abbrev=False)
    ksub = k.add_subparsers(dest="keystore_command", required=True)
    kn = ksub.add_parser("new", help="create a new keystore (password from the environment variable "
                                     f"{PASSWORD_ENV}, else asked)", allow_abbrev=False)
    kn.add_argument("--out", type=Path, required=True, help="the new keystore file (must not exist)")
    kn.add_argument("--ks-alias", default="arcane", help="the key's alias (default arcane)")
    kn.add_argument("--name", default="arcane-patcher user", help="the certificate's common name")

    sub.add_parser("gui", help="open the window (a helper that fills in these options)")
    sub.add_parser("notices", help="show the license and the third-party notices of this program")
    return ap


def _doctor(a: argparse.Namespace) -> list:
    from arcane_patcher import doctor

    return doctor.checks(a.variant, a.apk, a.keystore, a.ks_alias, a.out, a.emulator_zip, a.server_dir,
                         options(a), a.patch, a.allow_unofficial)


def options(a: argparse.Namespace) -> dict[str, bool]:
    return {flag.replace("-", "_"): getattr(a, flag.replace("-", "_")) for flag, _ in FLAGS}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from arcane_patcher import gui

        return gui.run()
    a = parser().parse_args(argv)
    if a.command == "gui":
        from arcane_patcher import gui

        return gui.run()
    if a.command == "doctor":
        from arcane_patcher import doctor

        found = _doctor(a)
        print(doctor.report(found))
        bad = doctor.blocking(found)
        print("ready to build" if not bad else f"{len(bad)} thing(s) missing: see the hints above")
        return 1 if bad else 0
    if a.command == "keystore":
        return _new_keystore(a)
    if a.command == "notices":
        return _notices()
    from arcane_patcher import doctor
    from arcane_patcher.patch import patch

    found = _doctor(a)
    if bad := doctor.blocking(found):
        print(doctor.report(bad))
        raise SystemExit(f"cannot build: {len(bad)} thing(s) missing (see the hints above)")
    patch(a.apk, a.out, a.variant, a.keystore, a.label, ks_alias=a.ks_alias, package=a.package,
          version_code=a.version_code, emulator_zip=a.emulator_zip, server_dir=a.server_dir, log_level=a.log_level,
          guest_login=a.guest_login, ad_rewards=a.ad_rewards, skip_rate_prompt=a.skip_rate_prompt,
          store_timers=a.store_timers, skip_consent=a.skip_consent, patch_file=a.patch,
          allow_unofficial=a.allow_unofficial)
    return 0


def _notices() -> int:
    """The program's license and, in a packaged executable, the notices of what it bundles."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    print("arcane-patcher: GNU General Public License version 3 (LICENSE); Corresponding Source: "
          "https://github.com/phstudy/arcane-patcher")
    notices = base / "THIRD_PARTY_NOTICES.txt"
    if notices.is_file():
        print(notices.read_text(encoding="utf-8"))
    else:
        print("(third-party notices are written into the packaged executables: python packaging/notices.py)")
    return 0


def _new_keystore(a: argparse.Namespace) -> int:
    from arcane_patcher import apksign

    if a.out.exists():
        raise SystemExit(f"{a.out} exists; choose another name (a keystore is never overwritten)")
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        import getpass

        password = getpass.getpass("New keystore password: ")
        if password != getpass.getpass("Again: "):
            raise SystemExit("the passwords differ")
    if len(password) < 6:
        raise SystemExit("the password needs at least 6 characters")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    apksign.create_keystore(a.out, a.ks_alias, password, common_name=a.name)
    print(f"created {a.out} (key {a.ks_alias!r}). Keep it and its password: updates must be signed with the same key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
