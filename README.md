# arcane-patcher

An open-source patcher for the v29 Android client of a discontinued mobile game. It turns **your own copy** of that
APK into one of two variants that work with the companion game server (arcane-server):

| Variant | Command | What it does |
| --- | --- | --- |
| Offline | `build offline` | The game server runs on the phone itself; no network needed. The first launch imports the game's content set once; a setup screen picks the game speed and the account before every start. Installs next to the original as `<package>.offline`, label "Offline". |
| Multiplayer | `build multi` | The first launch imports the game's content set once, like the offline variant: the game's downloads are answered on the phone from it, a server hands out none of the game's files. A setup screen asks for the server at every launch: type its address, scan the QR code of the server's join page, or open its connect link `arcane://connect?url=...`. Battles against other players go to that server's battle server, so the server must run arcane-server's multiplayer profile with its battle server (see the server's README). Installs as `<package>.multiplayer`, label "Multiplayer". |

The patcher runs on your machine, on the APK you supply, and writes the result there; it distributes no APK and no
file of the game's client (see Legal for exactly what a patch file holds). Every variant keeps the APK's own
launcher icon.

## Download

Take the zip for your computer from the releases page and unzip it. Its folder holds the program, the release's patch
file `arcane-patch-<version>.zip` next to it (leave it zipped and in the same folder) and the third-party notices;
nothing else needs to be installed (no Python, Android SDK, apktool or Java):

| Computer | Download |
| --- | --- |
| Windows (64-bit) | `arcane-patcher-windows-x64.zip` |
| macOS (Apple silicon / Intel) | `arcane-patcher-macos-arm64.zip` / `arcane-patcher-macos-x64.zip` |
| Linux (x86-64) | `arcane-patcher-linux-x64.zip` |

On macOS the program is `arcane-patcher.app`, signed and notarized: macOS asks once whether to open an app from
the internet (Open), and it carries the release's patch file inside. Its command line is
`arcane-patcher.app/Contents/MacOS/arcane-patcher`. On Windows, SmartScreen may ask first: More info > Run anyway.

Started without arguments (for example by a double-click) it opens a small window: choose the variant, your APK,
where to write the result and your keystore (or create one), check the requirements, build. The window is only a
helper: it fills in the command line's options and runs it, and everything it does can be done on the command line.

## Patch files

The program is a generic applier; what a build changes comes from a **patch file**, `arcane-patch-<version>.zip`,
published with each release. It holds, per supported client build (the game library's ELF build-id), the byte
edits: the project's own new bytes, each site's current bytes identified by their length and a hash (every site
is checked against them before anything is written); the
variants (package suffix, label, minimum Android version, the manifest components they add); the hashes of the
client release's content set (the phone checks the imported data against them); the prebuilt setup screens and
offline runtime parts with their third-party notices (`kit/NOTICES.txt`, also written into every built APK as
`assets/arcane_notices/THIRD_PARTY_NOTICES.txt`); the server release the offline variant embeds; and where its
source is (repository, tag and commit). A new release usually needs only a new patch file, not a new program.

- **Which one is used**: `--patch FILE` (the window's "Patch file" row), else the newest `arcane-patch-*.zip` next to
  the program or in your data folder (`%APPDATA%\arcane-patcher`, `~/Library/Application Support/arcane-patcher`,
  `~/.local/share/arcane-patcher`).
- **Signed**: release patch files are signed by the project's release key (Ed25519), which the program knows. A
  patch file that is unsigned, signed with another key, or changed after signing is unofficial: it is refused unless
  you pass `--allow-unofficial` (the window's "Allow unofficial patch files"), and then used with a warning. Only
  allow one whose source you trust: a patch file adds code to your app. A damaged file (contents not matching its
  list) is always refused.
- `doctor` shows the patch file found, its signature, its version, whether it is compatible with this program, and,
  with `--apk`, whether it has rules for your APK's build-id and every rule applies.

## What you need

- **Your v29 APK**: the v29.0 client (build 1587910), taken from your own install. With the phone on USB (developer
  options, USB debugging):

  ```sh
  adb shell pm list packages            # find the game's package
  adb shell pm path PACKAGE             # its base.apk and split_*.apk
  adb pull /data/app/.../base.apk       # each path the line above printed
  adb pull /data/app/.../split_config.arm64_v8a.apk
  zip game-v29.zip base.apk split_*.apk # one bundle file (or give a single APK as it is)
  ```

  The patcher reads a single APK or a split bundle (a zip holding `base.apk` and `split_*.apk`; the `.apkm` and
  `.xapk` formats are such zips). Only the arm64 build is patched; a single APK's 32-bit libraries are left out. The
  patcher checks the game library's ELF build-id against the patch file's rules and refuses a build it has no rules
  for, naming its build-id, before it writes anything.
- **The game's content set** (asset bundles, gamedata, localization), which the APK does not hold: the files your own
  install downloaded (its folder under `Android/data/`, which `adb pull` copies). The server checks it and writes
  the game data zip both variants import on the phone once (see "The game data zip" below and the server's README).
- **A phone with Android 7.0 or newer** and a 64-bit ARM processor (arm64), for both variants, or an x86_64 PC
  emulator that runs ARM apps (for example BlueStacks). The game is the arm64 build either way; the offline variant's
  server, a separate program the game starts, comes in the APK for arm64 and for x86_64 (an emulator's ARM translation
  covers the game but not that program), and the app picks the one for the device's CPU itself.
- **A signing keystore of your own** (see Signing); the patcher can create one.
- **The patch file** of the release (see Patch files).
- **Network on the first build** of the offline variant: the patch file's pinned downloads (CPython for Android for each
  server CPU, the server's Python packages) are checked against their sha256 and cached (in your user cache folder, or
  `ARCANE_CACHE`).

`arcane-patcher doctor` (and the window's "Check requirements") lists each of these for the options you give,
says which are missing and how to get them; `build` runs the same checks first and stops before writing anything.

## Usage

```sh
export ARCANE_KS_PASS='your keystore password'
arcane-patcher keystore new --out my.keystore --ks-alias me       # once (asks for the password if it is not set)

arcane-patcher doctor offline --apk game-v29.apk --keystore my.keystore --ks-alias me

# offline: the server inside the APK
arcane-patcher build offline --apk game-v29.apk --out out/offline.apk --keystore my.keystore --ks-alias me

# multiplayer: the server is chosen on the phone; a split bundle gives a directory of splits
arcane-patcher build multi --apk game-v29.apkm --out out/multiplayer --keystore my.keystore --ks-alias me
adb install-multiple out/multiplayer/*.apk
```

From a source checkout, run it as `uv run arcane-patcher ...` (see Development).

Options:

| Option | Meaning |
| --- | --- |
| `--patch FILE` | the patch file (default: the newest found, see Patch files) |
| `--allow-unofficial` | accept a patch file not signed by the project's release key (with a warning) |
| `--label TEXT` | the launcher label (default "Offline", "Multiplayer") |
| `--package NAME` | install under this package name |
| `--version-code N` | android:versionCode (default below) |
| `--emulator-zip PATH` / `--server-dir PATH` | offline only, for development: a server release zip, or a server checkout, instead of the patch file's |
| `--log-level LEVEL` | the client's default log level (e.g. `debug`) |

The other switches are listed by `arcane-patcher build --help`.

No real money changes hands: the server refuses purchases with real money and offers packs that were once sold for
real money for in-game gems instead.

The offline variant runs battles against other players locally, like campaign battles (there is no battle server on
the phone); the multiplayer variant keeps them on the server's battle server.

## Signing

Every build is signed with a key you own; the repository holds no keys. Create one once and keep it safe (the
window's "Create…" button does the same):

```sh
arcane-patcher keystore new --out my.keystore --ks-alias me
```

A keystore made by Android Studio or `keytool` (PKCS12 or JKS) works as well. Builds are signed with APK Signature
Scheme v2, plus a v1 (JAR) signature when the variant still installs on Android before 7.0.

Pass it with `--keystore` and `--ks-alias`; the password comes from the environment variable `ARCANE_KS_PASS`
(for Android's debug keystore, alias `androiddebugkey`, it defaults to `android`). Android installs an update only
when it is signed with the same key and has the same package as the installed app: a build signed with another key,
or under another package, must be installed as a new app (export your account from the offline setup screen first).

## versionCode

Each build's `android:versionCode` comes from the patch file's version: `2100000000 + major * 1000000 + minor * 1000 +
patch` (version 1.2.3 gives 2101002003). A build made with a newer patch file installs over an older one as an update
and keeps its data; rebuilding with the same patch file gives the same code (install it with `adb install -r`). The
codes are above the date-based ones of earlier patcher builds. `versionName` stays the game's (29.0).

## The game data zip

On its first launch each variant asks for the game's content set as one zip, copied to the phone (for example to
Downloads) and imported once; the zip can be deleted afterwards. Its gamedata must be the release's (the phone checks
its sha256). It holds the server's data layout:

```
content/v29/asset_bundle_metadata.json
content/v29/assets/...                      the asset bundles
content/v29/gamedata/gamedata-static.bin
content/v29/gamedata/gamedata-static.json
content/v29/localization/...                the Loc_*.txt files
```

The server repository writes it from a content set you have imported and verified there:
`uv run python tools/data_zip.py --out FILE` (run in the server repository).

The multiplayer variant answers the game's downloads (asset bundles, localization, gamedata) on the phone from this
data; the server only sends its changes to the gamedata (season times, its News page address), which the phone
applies to its own copy. A server you join hands out none of the game's files.

## The offline variant's server

The offline variant embeds the release of arcane-server in the patch file (`server/<name>.zip`): the zip holds
`src/arcane_server/`, `content/` and `proto/`, the server's own layout. A release's patch file packs the server
release pinned in `emulator.lock` (`version`, `url`, `sha256`; refused when its sha256 differs from the pin). Until
the first server release is pinned, patch files hold no server and the offline build needs `--emulator-zip PATH` or
`--server-dir PATH`.

On the phone the server starts as `python -m arcane_server`, checks the imported content set on its first start, and
keeps its database inside the app. The offline News page shows its items without images: the art is extracted from
the asset bundles only on a hosted server, not on the phone.

## Development

```sh
uv sync
uv run python -m arcane_patcher.kitbuild   # the kit: needs the Android SDK, NDK 27.3.13750724, a JDK 17+
uv run python -m arcane_patcher.release --version 0.3.0 --unsigned   # dist/arcane-patch-0.3.0.zip (development)
uv run pytest                              # tests needing the real APK run when ARCANE_TEST_APK names it
# ARCANE_SERVER_REPO=PATH (or the server checkout next to this one, named arcane-server): the gamedata delta is
# also checked against the server's own implementation
uv run python tools/public_text.py         # the public-text check (also a git hook: git config core.hooksPath .githooks)
```

What a patch file holds is made from:

- `patchdata/rules/<build-id>.json`: the byte edits per client build, written by the maintainer tool
  `uv run python -m arcane_patcher.rulesgen --apk YOUR.apk` from the patch definitions in `rulesgen.py` (the dex
  edits need the APK, so the rules are committed rather than made in CI); a test checks that the committed rules are
  what it writes for the v29 APK and that applying them gives the same bytes as the definitions;
- `patchdata/variants.json`, `patchdata/manifest/*.xml` (the components each variant adds to the manifest, with
  `{package}` and `{game_activity}` filled in at build time), `patchdata/runtime.json` (the offline runtime's pinned
  downloads), `patchdata/content.json` (the client release's content set: its name and file hashes);
- the kit (`src/arcane_patcher/kit/`, not in git): the setup screens' dex files, the offline variant's interpreter
  executable and lz4 / protobuf built for Android, once per server CPU (`arm64/`, `x86_64/`), and their third-party notices (`NOTICES.txt`, from the pinned
  sources and `src/arcane_patcher/android/licenses/`). Only building the kit needs Android's tools; `release`
  refuses a kit not made from the sources next to it.

`release` signs with the Ed25519 private key in `--key PEM` or the environment variable `ARCANE_PATCH_SIGNING_KEY`
(the PEM text); `--unsigned` makes a development file, which the program uses only with `--allow-unofficial`. The
release is made by `.github/workflows/release.yml` on a `v<version>` tag: the kit and the signed patch file once
(the key from the repository secret `ARCANE_PATCH_SIGNING_KEY`; a manual run makes an unsigned one), then
`pyinstaller packaging/arcane-patcher.spec` on CPython 3.14 for each platform from the locked environment (`uv sync
--locked --group release`), with the executable's third-party notices (`packaging/notices.py`, also shown by
`arcane-patcher notices` and attached as `THIRD_PARTY_NOTICES-<platform>.txt`), smoke-tested with the patch file next
to it; all are attached to the release.

## Legal

- This project is not affiliated with, endorsed or sponsored by the game's developer, its publisher or the owners of
  its characters. All trademarks and game content belong to their owners.
- The program, its releases and its patch files contain no file of the game's client: a patch file holds the
  project's own code and data (byte edits that identify the bytes they replace by length and hash, manifest edits,
  the setup screens, the third-party runtime parts listed in its notices) and the server release the offline
  variant embeds. That server release's `content/` is the server project's server-side data, which the client
  cannot provide (campaign waves, battle setups, event, raid, tower, store and reward definitions describing how the
  discontinued service ran), as that project's README describes; it holds none of the client's files.
- Build only from a copy of the game you obtained lawfully and installed yourself, for your own use. Do not share
  built APKs, the content set or the game data zip.
- No real money changes hands: the server refuses purchases with real money and offers packs that were once sold
  for real money for in-game gems instead.

## License

Copyright (C) the arcane-patcher contributors. GNU General Public License v3.0 only (see LICENSE). What a build
writes into the client (the Android setup screens and runtime in `src/arcane_patcher/android/`, the byte edits and
the code that generates them: `rulesgen.py`, `dexpatch.py`, `patchdata/rules/`, and the manifest and configuration
edits: `patch.py`, `patchdata/manifest/`) carries an additional permission under GPL section 7 for linking or
combining it with the game client it is applied to (`LICENSES/AdditionRef-arcane-client-linking.txt`, SPDX
`GPL-3.0-only WITH AdditionRef-arcane-client-linking`); it grants no rights to that client. Third-party code: see
`arcane-patcher notices`, `kit/NOTICES.txt` in a patch file and `assets/arcane_notices/THIRD_PARTY_NOTICES.txt` in a
built APK.
