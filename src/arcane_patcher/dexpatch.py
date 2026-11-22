# SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
# Copyright (C) the arcane-patcher contributors
# What this module writes into a client build is covered by the additional permission (GPL section 7) in
# LICENSES/AdditionRef-arcane-client-linking.txt; it grants no rights to the game client itself.
"""Small, checked edits of a dex file in place (no reassembly): the consent dialog patch.

The SDK's consent dialog class (ConsentUI) asks for a birth month, terms and ads choices in a WebView and reports the
answer to its listener as a status JSON. Its `showConsent(String config, IConsentListener listener)` stores the
listener, then calls `doShowConsent()` (shows the dialog) and `load(config)` (loads the page). The patch replaces
those two calls (6 code units) with code of exactly the same size that hands the listener an answer right away,
without showing anything (rulesgen.CONSENT_STATUS: only "no personalised ads"):

    const-string p1, "<answer JSON>"                                   (2 units)
    const/4 v0, 0x0                                                    (1 unit)
    invoke-direct {p0, p1, v0}, ConsentUI->onConsentStatusEventReceived(String, Throwable)   (3 units)

and raises the method's outs_size to the 3 argument words that call passes.

The answer needs a string the dex does not have, and adding one would renumber the whole string table. Instead an
existing string is rewritten in place, with the same length: `ANSWER_SLOT`, the message of an exception a coroutine
library builds only on an error path (its only use is checked first). The new text is the answer JSON with JSON
whitespace around it; the leading whitespace keeps it in the same place of the sorted string table, which is checked
against its neighbours. The header checksum and signature are recomputed.

Every expected original (instructions, method ids, the slot's text and uses) is checked before anything is written;
a dex that differs is refused.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass


class DexPatchError(ValueError):
    pass


# The string rewritten to hold the answer: a coroutine library's exception message, built only when a flow breaks
# its exception transparency (an error path); ANSWER_SLOT_USERS are the only methods allowed to load it.
ANSWER_SLOT = ("\n            Flow exception transparency is violated:\n                Previous 'emit' call has "
               "thrown exception ")
ANSWER_SLOT_USERS = {"Lkotlinx/coroutines/flow/internal/SafeCollector;->exceptionTransparencyViolated"}
ANSWER_INDENT = "\n" + " " * 13  # sorts between the slot's neighbours (they share "\n" + 12 spaces)


def _mutf8(b: bytes) -> str:
    """Modified UTF-8 (dex strings): NUL as C0 80, supplementary characters as two 3-byte surrogates."""
    units, i = [], 0
    while i < len(b):
        c = b[i]
        if c < 0x80:
            units.append(c)
            i += 1
        elif c >> 5 == 0x6:
            units.append(((c & 0x1F) << 6) | (b[i + 1] & 0x3F))
            i += 2
        else:
            units.append(((c & 0x0F) << 12) | ((b[i + 1] & 0x3F) << 6) | (b[i + 2] & 0x3F))
            i += 3
    return struct.pack(f"<{len(units)}H", *units).decode("utf-16-le", "surrogatepass")


def _uleb(d: bytes, o: int) -> tuple[int, int]:
    r = s = 0
    while True:
        b = d[o]
        o += 1
        r |= (b & 0x7F) << s
        s += 7
        if b < 0x80:
            return r, o


@dataclass
class Dex:
    data: bytearray

    def __post_init__(self) -> None:
        d = self.data
        if not bytes(d[:4]) == b"dex\n":
            raise DexPatchError("not a dex file")
        self.string_count, self.string_ids = struct.unpack_from("<II", d, 0x38)
        self.type_count, self.type_ids = struct.unpack_from("<II", d, 0x40)
        self.proto_count, self.proto_ids = struct.unpack_from("<II", d, 0x48)
        self.method_count, self.method_ids = struct.unpack_from("<II", d, 0x58)
        self.class_count, self.class_defs = struct.unpack_from("<II", d, 0x60)

    # --- tables ---

    def string_data_off(self, i: int) -> int:
        return struct.unpack_from("<I", self.data, self.string_ids + 4 * i)[0]

    def string(self, i: int) -> str:
        o = self.string_data_off(i)
        _, o = _uleb(self.data, o)
        end = self.data.index(0, o)
        return _mutf8(bytes(self.data[o:end]))

    def type_name(self, i: int) -> str:
        return self.string(struct.unpack_from("<I", self.data, self.type_ids + 4 * i)[0])

    def proto(self, i: int) -> str:
        _shorty, ret, params = struct.unpack_from("<III", self.data, self.proto_ids + 12 * i)
        args = ""
        if params:
            (n,) = struct.unpack_from("<I", self.data, params)
            args = "".join(self.type_name(struct.unpack_from("<H", self.data, params + 4 + 2 * k)[0]) for k in range(n))
        return f"({args}){self.type_name(ret)}"

    def method(self, i: int) -> str:
        cls, proto, name = struct.unpack_from("<HHI", self.data, self.method_ids + 8 * i)
        return f"{self.type_name(cls)}->{self.string(name)}{self.proto(proto)}"

    def method_index(self, ref: str) -> int:
        found = [i for i in range(self.method_count) if self.method(i) == ref]
        if len(found) != 1:
            raise DexPatchError(f"method {ref} referenced {len(found)} times, expected once")
        return found[0]

    def string_index(self, text: str) -> int:
        found = [i for i in range(self.string_count) if self.string(i) == text]
        if len(found) != 1:
            raise DexPatchError(f"string {text[:40]!r}... found {len(found)} times, expected once")
        return found[0]

    # --- code ---

    def methods_with_code(self):
        """(method ref without proto, code item offset) of every method with code."""
        d = self.data
        for ci in range(self.class_count):
            class_data = struct.unpack_from("<I", d, self.class_defs + 32 * ci + 24)[0]
            if not class_data:
                continue
            o = class_data
            sf, o = _uleb(d, o)
            inf, o = _uleb(d, o)
            dm, o = _uleb(d, o)
            vm, o = _uleb(d, o)
            for _ in range(sf + inf):
                _, o = _uleb(d, o)
                _, o = _uleb(d, o)
            idx = 0
            for k in range(dm + vm):
                if k == dm:
                    idx = 0
                diff, o = _uleb(d, o)
                idx += diff
                _, o = _uleb(d, o)  # access flags
                code, o = _uleb(d, o)
                if code:
                    yield idx, code

    def code_units(self, code: int) -> tuple[int, int, list[int]]:
        """(registers_size, insns offset, instruction units) of a code item."""
        regs, _ins, _outs, _tries, _debug, size = struct.unpack_from("<HHHHII", self.data, code)
        insns = code + 16
        return regs, insns, list(struct.unpack_from(f"<{size}H", self.data, insns))

    def string_users(self, index: int) -> set[str]:
        """Methods whose code loads string `index` (const-string / const-string/jumbo; a word scan, so it may also
        report a method whose data payload happens to hold those values: conservative)."""
        users = set()
        for midx, code in self.methods_with_code():
            _, _, w = self.code_units(code)
            for j, u in enumerate(w):
                op = u & 0xFF
                if (op == 0x1A and j + 1 < len(w) and w[j + 1] == index) or (
                        op == 0x1B and j + 2 < len(w) and (w[j + 1] | w[j + 2] << 16) == index):
                    m = self.method(midx)
                    users.add(m[:m.index("(")])
        return users

    def finish(self) -> bytes:
        """The dex with its SHA-1 signature and adler32 checksum recomputed."""
        d = self.data
        d[12:32] = hashlib.sha1(bytes(d[32:])).digest()
        struct.pack_into("<I", d, 8, zlib.adler32(bytes(d[12:])))
        return bytes(d)


def _invoke_direct(method: int, *regs: int) -> list[int]:
    """invoke-direct {regs...} (format 35c, up to 5 registers)."""
    if not 0 < len(regs) <= 4 or method > 0xFFFF:
        raise DexPatchError("invoke-direct: unsupported operands")
    r = list(regs) + [0] * (4 - len(regs))
    return [(len(regs) << 12) | 0x70, method, (r[3] << 12) | (r[2] << 8) | (r[1] << 4) | r[0]]


def patch_consent(dex: bytes, consent_ui: str, answer_json: str) -> bytes:
    """`dex` with ConsentUI.showConsent answering `answer_json` at once instead of showing the dialog (see the module
    docstring). `consent_ui` is the class descriptor (e.g. "Lsome/sdk/consent/ConsentUI;")."""
    dx = Dex(bytearray(dex))
    package = consent_ui[:consent_ui.rindex("/") + 1]
    show = dx.method_index(f"{consent_ui}->showConsent(Ljava/lang/String;{package}IConsentListener;)V")
    do_show = dx.method_index(f"{consent_ui}->doShowConsent()V")
    load = dx.method_index(f"{consent_ui}->load(Ljava/lang/String;)V")
    received = dx.method_index(f"{consent_ui}->onConsentStatusEventReceived(Ljava/lang/String;Ljava/lang/Throwable;)V")

    codes = [code for midx, code in dx.methods_with_code() if midx == show]
    if len(codes) != 1:
        raise DexPatchError("ConsentUI.showConsent has no code (or more than one definition)")
    regs, insns, w = dx.code_units(codes[0])
    # registers: v0 (locals 1), then p0 (this), p1 (config), p2 (listener)
    if regs != 4:
        raise DexPatchError(f"ConsentUI.showConsent uses {regs} registers, expected 4")
    v0, p0, p1 = 0, 1, 2
    old = _invoke_direct(do_show, p0) + _invoke_direct(load, p0, p1)
    at = [j for j in range(len(w) - 5) if w[j:j + 6] == old]
    if len(at) != 1:
        raise DexPatchError("ConsentUI.showConsent does not match the SDK version the consent patch was made for")

    slot = dx.string_index(ANSWER_SLOT)
    users = dx.string_users(slot)
    if users != ANSWER_SLOT_USERS:
        raise DexPatchError(f"the answer's string slot is used by {sorted(users)}, expected {sorted(ANSWER_SLOT_USERS)}")
    if slot > 0xFFFF:
        raise DexPatchError("the answer's string slot needs const-string/jumbo")
    text = ANSWER_INDENT + answer_json
    if not text.isascii() or len(text) > len(ANSWER_SLOT):
        raise DexPatchError("the consent answer does not fit its string slot")
    text += " " * (len(ANSWER_SLOT) - len(text))
    prev, nxt = dx.string(slot - 1), dx.string(slot + 1) if slot + 1 < dx.string_count else None
    if not (prev < text and (nxt is None or text < nxt)):
        raise DexPatchError("the consent answer would break the dex's string order")
    o = dx.string_data_off(slot)
    _, o = _uleb(dx.data, o)
    dx.data[o:o + len(text)] = text.encode("ascii")  # same length, same utf-16 size: the header stays valid

    new = [0x001A | (p1 << 8), slot,  # const-string p1, slot
           0x0012 | (0 << 12) | (v0 << 8),  # const/4 v0, 0x0
           *_invoke_direct(received, p0, p1, v0)]
    struct.pack_into("<6H", dx.data, insns + 2 * at[0], *new)
    # outs_size: the most argument words a call in this method passes; the new call passes 3 (the verifier rejects
    # the class when a call needs more than the code item declares)
    (outs,) = struct.unpack_from("<H", dx.data, codes[0] + 4)
    struct.pack_into("<H", dx.data, codes[0] + 4, max(outs, 3))
    return dx.finish()
