"""The window: a helper around the command line. It only fills in the options of `arcane-patcher doctor` /
`build` / `keystore new`, runs them as the command line does (the same program, with those arguments) and shows
their output. Every check and every build step lives in the command line (doctor.py, patch.py), so the two never
disagree. Opens with `arcane-patcher gui`, or when the program is started without arguments (double-click).
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

FLAGS = (("guest_login", "--guest-login", "Log in as guest (no sign-in popup)"),
         ("ad_rewards", "--ad-rewards", "Rewarded videos grant their reward at once"),
         ("skip_rate_prompt", "--skip-rate-prompt", "Never show the store-rating popup"),
         ("store_timers", "--store-timers", "Store cards count down"),
         ("skip_consent", "--skip-consent", "Answer the first-launch consent dialog"))


@dataclass
class Form:
    variant: str = "multi"
    apk: str = ""
    out: str = ""
    keystore: str = ""
    ks_alias: str = "androiddebugkey"
    password: str = ""
    label: str = ""
    package: str = ""
    emulator_zip: str = ""
    patch_file: str = ""  # empty: the command line's default (the newest patch file found)
    allow_unofficial: bool = False
    flags: dict[str, bool] = field(default_factory=lambda: {k: True for k, _, _ in FLAGS})


def default_out(apk: str, variant: str) -> str:
    """Next to the input: <name>-<variant>.apk for a single APK, a <name>-<variant> folder for a split bundle."""
    if not apk:
        return ""
    p = Path(apk)
    stem = f"{p.stem}-{'multiplayer' if variant == 'multi' else variant}"
    return str(p.with_name(stem + (".apk" if p.suffix.lower() == ".apk" else "")))


def program() -> list[str]:
    """How to run the command line: this executable (packaged download) or this Python with the package."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "arcane_patcher"]


def _options(form: Form) -> list[str]:
    args = [form.variant]
    if form.apk:
        args += ["--apk", form.apk]
    if form.out or form.apk:
        args += ["--out", form.out or default_out(form.apk, form.variant)]
    if form.keystore:
        args += ["--keystore", form.keystore, "--ks-alias", form.ks_alias or "androiddebugkey"]
    if form.patch_file:
        args += ["--patch", form.patch_file]
    if form.allow_unofficial:
        args.append("--allow-unofficial")
    if form.variant == "offline" and form.emulator_zip:
        args += ["--emulator-zip", form.emulator_zip]
    for key, flag, _ in FLAGS:
        args.append(flag if form.flags.get(key, True) else "--no-" + flag[2:])
    return args


def doctor_command(form: Form) -> list[str]:
    return [*program(), "doctor", *_options(form)]


def build_command(form: Form) -> list[str]:
    args = [*program(), "build", *_options(form)]
    if form.label:
        args += ["--label", form.label]
    if form.package:
        args += ["--package", form.package]
    return args


def default_patch_file() -> str:
    """The patch file the command line would use without --patch (shown in the window's Patch file row)."""
    from arcane_patcher import patchfile

    found = patchfile.find_default()
    return str(found) if found else ""


def keystore_command(path: str, alias: str) -> list[str]:
    return [*program(), "keystore", "new", "--out", path, "--ks-alias", alias]


def child_env(form: Form) -> dict[str, str]:
    """The keystore password reaches the command line through its environment variable, never its arguments."""
    from arcane_patcher.signing import PASSWORD_ENV

    env = dict(os.environ)
    if form.password:
        env[PASSWORD_ENV] = form.password
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _hide_own_console() -> None:  # pragma: no cover - Windows only
    """A double-clicked console program gets a console window of its own: close it (one started from a terminal
    shares that terminal's console, which stays)."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if kernel32.GetConsoleProcessList((ctypes.c_uint * 2)(), 2) == 1:
        kernel32.FreeConsole()


def run() -> int:  # pragma: no cover - interactive
    _hide_own_console()
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    from arcane_patcher.doctor import checks  # noqa: F401  (imported early: a broken install fails here, visibly)

    root = tk.Tk()
    root.title("arcane-patcher")
    root.minsize(760, 560)
    form = Form()
    v = {name: tk.StringVar(value=getattr(form, name)) for name in
         ("variant", "apk", "out", "keystore", "ks_alias", "password", "label", "package", "emulator_zip",
          "patch_file")}
    v["patch_file"].set(default_patch_file())
    flags = {k: tk.BooleanVar(value=True) for k, _, _ in FLAGS}
    unofficial = tk.BooleanVar(value=False)
    # the worker thread only puts into this queue (Tk is not thread-safe: X11 builds abort on calls from other
    # threads); the main loop's pump does every Tk call: ("line", text) or ("done", exit code, callback)
    out_queue: queue.Queue[tuple] = queue.Queue()
    busy = tk.BooleanVar(value=False)

    def current() -> Form:
        f = Form(**{k: var.get().strip() if k != "password" else var.get() for k, var in v.items()})
        f.flags = {k: var.get() for k, var in flags.items()}
        f.allow_unofficial = unofficial.get()
        return f

    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)
    row = 0

    ttk.Label(frm, text="Variant").grid(row=row, column=0, sticky="w")
    vf = ttk.Frame(frm)
    vf.grid(row=row, column=1, sticky="w")
    ttk.Radiobutton(vf, text="Multiplayer (server chosen on the phone)", value="multi", variable=v["variant"]).pack(side="left")
    ttk.Radiobutton(vf, text="Offline (server on the phone)", value="offline", variable=v["variant"]).pack(side="left", padx=12)
    row += 1

    def picker(label: str, key: str, choose, extra=None):
        nonlocal row
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=v[key]).grid(row=row, column=1, sticky="ew", pady=2)
        bf = ttk.Frame(frm)
        bf.grid(row=row, column=2, sticky="w")
        ttk.Button(bf, text="Browse…", command=choose).pack(side="left")
        if extra:
            ttk.Button(bf, text=extra[0], command=extra[1]).pack(side="left", padx=(4, 0))
        row += 1

    def choose_apk():
        p = filedialog.askopenfilename(title="Your v29 APK or split bundle",
                                       filetypes=[("APK or bundle", "*.apk *.apkm *.xapk *.zip"), ("All files", "*")])
        if p:
            v["apk"].set(p)
            v["out"].set(default_out(p, v["variant"].get()))

    def choose_out():
        if v["apk"].get().lower().endswith(".apk"):
            p = filedialog.asksaveasfilename(title="Output APK", defaultextension=".apk",
                                             initialfile=Path(v["out"].get() or "out.apk").name)
        else:
            p = filedialog.askdirectory(title="Output folder (one APK per split)")
        if p:
            v["out"].set(p)

    def choose_keystore():
        p = filedialog.askopenfilename(title="Your signing keystore",
                                       filetypes=[("Keystore", "*.keystore *.jks *.p12 *.pfx"), ("All files", "*")])
        if p:
            v["keystore"].set(p)

    def create_keystore():
        p = filedialog.asksaveasfilename(title="New keystore", defaultextension=".keystore",
                                         initialfile="arcane.keystore")
        if not p:
            return
        alias = simpledialog.askstring("New keystore", "Key alias:", initialvalue="arcane", parent=root)
        if not alias:
            return
        pw = simpledialog.askstring("New keystore", "Password (at least 6 characters; keep it, updates need it):",
                                    show="*", parent=root)
        if not pw:
            return
        env = dict(os.environ)
        from arcane_patcher.signing import PASSWORD_ENV

        env[PASSWORD_ENV] = pw
        start(keystore_command(p, alias), env, then=lambda ok: ok and (v["keystore"].set(p), v["ks_alias"].set(alias),
                                                                        v["password"].set(pw)))

    def choose_patch_file():
        p = filedialog.askopenfilename(title="Patch file", filetypes=[("Patch file", "arcane-patch-*.zip"),
                                                                       ("All files", "*")])
        if p:
            v["patch_file"].set(p)
            on_check()

    def toggle_unofficial():
        if unofficial.get() and not messagebox.askokcancel(
                "Unofficial patch files", "A patch file not signed by the project's release key can add any code to "
                "your app. Allow it only for a patch file whose source you trust.", icon="warning"):
            unofficial.set(False)
        on_check()

    picker("Patch file", "patch_file", choose_patch_file)
    ttk.Checkbutton(frm, text="Allow unofficial patch files (not signed by the project; only from a source you "
                    "trust)", variable=unofficial, command=toggle_unofficial).grid(row=row, column=1, sticky="w")
    row += 1
    picker("APK", "apk", choose_apk)
    picker("Output", "out", choose_out)
    picker("Keystore", "keystore", choose_keystore, ("Create…", create_keystore))
    for label, key, show in (("Key alias", "ks_alias", ""), ("Password", "password", "*")):
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(frm, textvariable=v[key], show=show).grid(row=row, column=1, sticky="ew", pady=2)
        row += 1

    adv = ttk.LabelFrame(frm, text="Options", padding=6)
    adv.grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
    adv.columnconfigure(1, weight=1)
    for i, (key, _, text) in enumerate(FLAGS):
        ttk.Checkbutton(adv, text=text, variable=flags[key]).grid(row=i // 2, column=i % 2, sticky="w")
    r = (len(FLAGS) + 1) // 2
    for label, key in (("Launcher label (optional)", "label"), ("Package (optional)", "package"),
                       ("Offline: server release zip (optional)", "emulator_zip")):
        ttk.Label(adv, text=label).grid(row=r, column=0, sticky="w")
        ttk.Entry(adv, textvariable=v[key]).grid(row=r, column=1, sticky="ew")
        r += 1
    row += 1

    bar = ttk.Frame(frm)
    bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 4))
    check_btn = ttk.Button(bar, text="Check requirements")
    build_btn = ttk.Button(bar, text="Build")
    check_btn.pack(side="left")
    build_btn.pack(side="left", padx=8)
    status = ttk.Label(bar, text="")
    status.pack(side="left", padx=8)
    row += 1

    log = tk.Text(frm, height=16, wrap="word", font=("TkFixedFont", 10))
    log.grid(row=row, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(row, weight=1)
    log.tag_configure("bad", foreground="#b00020")
    log.tag_configure("hint", foreground="#0b5394")
    log.tag_configure("ok", foreground="#1b7f3b")

    def write(line: str) -> None:
        tag = ("bad" if "[missing]" in line or "UNOFFICIAL" in line or line.startswith(("error", "WARNING")) else "hint" if line.lstrip().startswith("->")
               else "ok" if line.startswith("[     ok]") else None)
        log.insert("end", line, tag)
        log.see("end")

    def start(cmd: list[str], env: dict[str, str], then=None) -> None:
        if busy.get():
            return
        busy.set(True)
        for b in (check_btn, build_btn):
            b.state(["disabled"])
        log.delete("1.0", "end")
        status.config(text="running…")

        def work():
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                     bufsize=1, **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
                for line in p.stdout:
                    out_queue.put(("line", line))
                code = p.wait()
            except OSError as e:
                out_queue.put(("line", f"error: {e}\n"))
                code = 1
            out_queue.put(("done", code, then))

        threading.Thread(target=work, daemon=True).start()

    def finish(code: int, then) -> None:
        busy.set(False)
        for b in (check_btn, build_btn):
            b.state(["!disabled"])
        status.config(text="done" if code == 0 else "stopped: see the hints above")
        if then:
            then(code == 0)

    def pump():
        try:
            while True:
                item = out_queue.get_nowait()
                if item[0] == "line":
                    write(item[1])
                else:
                    finish(item[1], item[2])
        except queue.Empty:
            pass
        root.after(80, pump)

    def on_check():
        f = current()
        start(doctor_command(f), child_env(f))

    def on_build():
        f = current()
        if not f.apk or not f.keystore:
            messagebox.showinfo("arcane-patcher", "Choose your APK and a keystore first (Create… makes a new one).")
            return
        start(build_command(f), child_env(f))

    check_btn.config(command=on_check)
    build_btn.config(command=on_build)
    pump()
    root.after(100, on_check)  # show what is missing right away
    root.mainloop()
    return 0
