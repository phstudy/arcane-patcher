# SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
# Copyright (C) the arcane-patcher contributors
# What this module writes into a client build is covered by the additional permission (GPL section 7) in
# LICENSES/AdditionRef-arcane-client-linking.txt; it grants no rights to the game client itself.
"""The patch definitions, for maintainers: they are written as data into patchdata/rules/<build-id>.json, which the
release packs into the patch file (release.py). The patcher program only applies that data (patch.py, rules.py); it
needs none of this module.

    python -m arcane_patcher.rulesgen --apk YOUR-v29.apk     # writes patchdata/rules/<build-id>.json

The game library patches below are instruction sequences at fixed file offsets of one client build; the bytes they
replace are read from the APK when the rules are written and kept there only as their length and sha256. The consent
dialog patch is computed from the APK's dex (dexpatch.py) and written as the byte edits it makes. A test checks that
applying the written rules gives exactly the bytes these definitions give.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from arcane_patcher.patch import GAME_LIB, PatchError, elf_build_id

ROOT = Path(__file__).resolve().parents[2]
RULES_DIR = ROOT / "patchdata" / "rules"
RULES_FORMAT = 2
RULES_LICENSE = "GPL-3.0-only WITH AdditionRef-arcane-client-linking"  # LICENSES/

# --- game library patches (v29 build 1587910 arm64): local multiplayer battles, guest login, ad rewards ---------

V29_LIB_BUILD_ID = bytes.fromhex("7dfd46f8a9cae37af48aebbf281180a7a82a5ecf")


class A64:
    """Encoder for the few AArch64 instruction forms the patches use."""

    @staticmethod
    def r(reg: str) -> int:
        return 31 if reg in ("xzr", "wzr") else int(reg[1:])

    @classmethod
    def adrp(cls, rd: str, pc: int, target: int) -> int:
        d = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
        return 0x90000000 | ((d & 3) << 29) | (((d >> 2) & 0x7FFFF) << 5) | cls.r(rd)

    @classmethod
    def ldr(cls, rt: str, rn: str, off: int) -> int:  # unsigned offset; 64-bit for x, 32-bit for w
        if rt[0] == "x":
            return 0xF9400000 | ((off // 8) << 10) | (cls.r(rn) << 5) | cls.r(rt)
        return 0xB9400000 | ((off // 4) << 10) | (cls.r(rn) << 5) | cls.r(rt)

    @classmethod
    def ldrb(cls, rt: str, rn: str, off: int) -> int:
        return 0x39400000 | (off << 10) | (cls.r(rn) << 5) | cls.r(rt)

    @classmethod
    def mov(cls, rd: str, rm: str) -> int:
        return (0xAA0003E0 if rd[0] == "x" else 0x2A0003E0) | (cls.r(rm) << 16) | cls.r(rd)

    @classmethod
    def movz(cls, rd: str, imm: int) -> int:
        return (0xD2800000 if rd[0] == "x" else 0x52800000) | (imm << 5) | cls.r(rd)

    @classmethod
    def cmp(cls, rn: str, rm: str) -> int:  # 32-bit
        return 0x6B00001F | (cls.r(rm) << 16) | (cls.r(rn) << 5)

    @staticmethod
    def b(pc: int, target: int) -> int:
        return 0x14000000 | (((target - pc) >> 2) & 0x3FFFFFF)

    @staticmethod
    def bl(pc: int, target: int) -> int:
        return 0x94000000 | (((target - pc) >> 2) & 0x3FFFFFF)

    @staticmethod
    def b_eq(pc: int, target: int) -> int:
        return 0x54000000 | ((((target - pc) >> 2) & 0x7FFFF) << 5)

    @classmethod
    def cbnz(cls, rt: str, pc: int, target: int) -> int:
        return (0xB5000000 if rt[0] == "x" else 0x35000000) | ((((target - pc) >> 2) & 0x7FFFF) << 5) | cls.r(rt)

    @classmethod
    def cbz(cls, rt: str, pc: int, target: int) -> int:
        return (0xB4000000 if rt[0] == "x" else 0x34000000) | ((((target - pc) >> 2) & 0x7FFFF) << 5) | cls.r(rt)

    @classmethod
    def ldr_reg(cls, rt: str, rn: str, rm: str) -> int:  # ldr xt, [xn, xm]
        return 0xF8606800 | (cls.r(rm) << 16) | (cls.r(rn) << 5) | cls.r(rt)

    @classmethod
    def add(cls, rd: str, rn: str, imm: int) -> int:  # 64-bit, unshifted imm12
        return 0x91000000 | (imm << 10) | (cls.r(rn) << 5) | cls.r(rd)

    @classmethod
    def str_w(cls, rt: str, rn: str, off: int) -> int:
        return 0xB9000000 | ((off // 4) << 10) | (cls.r(rn) << 5) | cls.r(rt)

    @classmethod
    def eor1(cls, rd: str, rn: str) -> int:  # eor wd, wn, #1
        return 0x52000000 | (cls.r(rn) << 5) | cls.r(rd)

    @classmethod
    def stp_pre(cls, rt: str, rt2: str, off: int) -> int:  # stp xt, xt2, [sp, #off]!
        return 0xA9800000 | (((off // 8) & 0x7F) << 15) | (cls.r(rt2) << 10) | (31 << 5) | cls.r(rt)

    @classmethod
    def stp(cls, rt: str, rt2: str, off: int) -> int:  # stp xt, xt2, [sp, #off]
        return 0xA9000000 | (((off // 8) & 0x7F) << 15) | (cls.r(rt2) << 10) | (31 << 5) | cls.r(rt)

    @classmethod
    def ldp_post(cls, rt: str, rt2: str, off: int) -> int:  # ldp xt, xt2, [sp], #off
        return 0xA8C00000 | (((off // 8) & 0x7F) << 15) | (cls.r(rt2) << 10) | (31 << 5) | cls.r(rt)

    @classmethod
    def ldp(cls, rt: str, rt2: str, off: int) -> int:  # ldp xt, xt2, [sp, #off]
        return 0xA9400000 | (((off // 8) & 0x7F) << 15) | (cls.r(rt2) << 10) | (31 << 5) | cls.r(rt)

    @classmethod
    def br(cls, rn: str) -> int:
        return 0xD61F0000 | (cls.r(rn) << 5)

    NOP = 0xD503201F
    RET = 0xD65F03C0


Patch = tuple[int, list]  # (file offset, instructions: words or callables pc -> word); the original bytes at the
# site are read from the APK when the rules are written (generate) and kept there only as their sha256

# Offsets below are file offsets in the v29 build 1587910 arm64 game library (build-id V29_LIB_BUILD_ID), which are
# also its load addresses.
LOCAL_BATTLE_CLASS_PAGE, LOCAL_BATTLE_CLASS_OFF = 0x3C51000, 0x090  # where the local battle's class is found
LOCAL_BATTLE_NEW = 0x139CE30  # creates a local battle (player id, battle data, run threaded)


def _install(init: int, alloc_page: int, alloc_slot: int, ctor: int, cache_bl: int) -> list[Patch]:
    """A mode's battle start: the battle runs locally on the phone, as a campaign battle does, instead of connecting
    to a battle server; and it is not remembered for reconnecting (a relaunch would try to reach a battle server)."""
    a = A64
    return [
        (init, [lambda pc: a.adrp("x0", pc, LOCAL_BATTLE_CLASS_PAGE), a.ldr("x0", "x0", LOCAL_BATTLE_CLASS_OFF)]),
        (alloc_page, [lambda pc: a.adrp("x9", pc, LOCAL_BATTLE_CLASS_PAGE)]),
        (alloc_slot, [a.ldr("x9", "x9", LOCAL_BATTLE_CLASS_OFF)]),
        (ctor, [a.mov("w1", "w19"), a.mov("x2", "x28"), a.movz("w3", 1), a.mov("x4", "xzr"),
                                      a.NOP, a.NOP, a.NOP, a.mov("x23", "x0"), lambda pc: a.bl(pc, LOCAL_BATTLE_NEW)]),
        (cache_bl, [a.NOP]),
    ]


def _end_handler(check: int, ok: int, load_id: int, dc: str, battle: str, ctx_off: int) -> list[Patch]:
    """A mode's battle end: the result is reported for a local battle too, with the start response's battle id when
    the player's team won and its match id when not (the server tells them apart)."""
    a = A64
    t, back = check + 4, load_id + 4
    return [
        (check, [lambda pc: a.b(pc, ok)]),
        (t, [a.ldr("x9", dc, 0x18), a.ldr("w9", "x9", 0x18), a.ldrb("w10", battle, 0x41),
                      a.ldr("x8", "x19", ctx_off), a.ldr("x8", "x8", 0x28), a.ldr("x1", "x8", 0x40),
                      a.cmp("w9", "w10"), lambda pc: a.b_eq(pc, back), a.ldr("x1", "x8", 0x50),
                      lambda pc: a.b(pc, back)]),
        (load_id, [lambda pc: a.b(pc, t)]),
    ]


def _pvp_result() -> list[Patch]:
    """Arena battles: the result is reported with the battle id when the player won and the match id when not. The
    shared result request takes the id to report from its caller (friendly battles too, see _social_pvp_result)."""
    a = A64
    check, ok, entry, load = 0x144F318, 0x144F3F0, 0x144F218, 0x144F410
    t1, t2, t3 = 0x144F34C, 0x144F358, 0x144F368  # unused space for the added code
    call_x3 = 0x14CEA54  # where the arena battle end asks for the result
    return [
        (check, [lambda pc: a.b(pc, ok)]),
        (entry, [lambda pc: a.b(pc, t1)]),
        (t1, [a.mov("x19", "x0"), a.mov("x24", "x3"), lambda pc: a.b(pc, entry + 4)]),
        (t2, [a.mov("x1", "x24"), lambda pc: a.cbnz("x24", pc, load + 4), a.ldr("x1", "x22", 0x130), lambda pc: a.b(pc, load + 4)]),
        (t3, [a.ldr("x8", "x19", 0x50), a.ldr("x8", "x8", 0x28), a.ldrb("w9", "x20", 0x18),
          a.ldr("x3", "x8", 0x30),  # the battle id if the player won
          lambda pc: a.cbnz("w9", pc, pc + 8),
          a.ldr("x3", "x8", 0x50),  # else the match id
          lambda pc: a.b(pc, call_x3 + 4)]),
        (load, [lambda pc: a.b(pc, t2)]),
        (call_x3, [lambda pc: a.b(pc, t3)]),
    ]


REPORTED_ID_FN = 0x144F384  # unused space after _pvp_result's added code


def _social_pvp_result() -> list[Patch]:
    """Friendly battles: the result is reported like an arena battle's, with the battle id when the player won and
    the match id when not (REPORTED_ID_FN picks it)."""
    a = A64
    call_x3 = 0x13F7CC0  # where the friendly battle end asks for the result
    return [
        (REPORTED_ID_FN, [a.ldr("x8", "x19", 0x50), a.ldr("x8", "x8", 0x28), a.ldrb("w9", "x20", 0x18),
          a.ldr("x3", "x8", 0x30),  # the battle id if the player won
          lambda pc: a.cbnz("w9", pc, pc + 8),
          a.ldr("x3", "x8", 0x50),  # else the match id
          a.RET]),
        (call_x3, [lambda pc: a.bl(pc, REPORTED_ID_FN)]),
    ]


def _lta_install() -> list[Patch]:
    """Limited-time arena battles start locally, as _install does for the other modes (laid out differently)."""
    a = A64
    return [
        (0x11F9610, [lambda pc: a.adrp("x0", pc, LOCAL_BATTLE_CLASS_PAGE), a.ldr("x0", "x0", LOCAL_BATTLE_CLASS_OFF)]),
        (0x11F9690, [lambda pc: a.adrp("x8", pc, LOCAL_BATTLE_CLASS_PAGE), a.ldr("x8", "x8", LOCAL_BATTLE_CLASS_OFF)]),
        (0x11F96A8, [a.mov("w1", "w19"), a.mov("x2", "x23"), a.movz("w3", 1), a.mov("x4", "xzr"), a.NOP, a.NOP, a.NOP,
          a.mov("x24", "x0"), lambda pc: a.bl(pc, LOCAL_BATTLE_NEW)]),
        (0x11F980C, [a.NOP]),  # not remembered for reconnecting
    ]


def _lta_result() -> list[Patch]:
    """Limited-time arena battles: the result is reported for a local battle too, with the battle id when the player
    won and the match id when not."""
    a = A64
    check, after_check, load_id = 0xFA4874, 0xFA48AC, 0xFA4E24
    t = check + 8
    return [
        (check, [a.mov("x24", "x8"), lambda pc: a.b(pc, after_check),
          a.ldr("x8", "x19", 0x10), a.ldr("x8", "x8", 0x50), a.ldr("x8", "x8", 0x38), a.ldrb("w9", "x20", 0x10),
          a.ldr("x1", "x8", 0x30),  # the battle id if the player won
          lambda pc: a.cbnz("w9", pc, pc + 8),
          a.ldr("x1", "x8", 0x50),  # else the match id
          lambda pc: a.b(pc, load_id + 4),
          a.NOP, a.NOP, a.NOP, a.NOP]),
        (load_id, [lambda pc: a.b(pc, t)]),
    ]


INIT_CLASS = 0xEC8508  # prepares a class for use
OBJECT_NEW = 0xEC85D4  # creates an object
BATTLE_ENDED_CLASS_PAGE, BATTLE_ENDED_CLASS_OFF = 0x3EBE000, 0xC08  # where the battle-ended event's class is found
BATTLE_ENDED_NEW = 0x23BC2D0  # initializes a battle-ended event
FORFEIT = 4  # the battle-ended reason "forfeit"


def _quit_as_forfeit(offset: int, end_handler: int | None) -> list[Patch]:
    """Quitting a local battle counts as a forfeit: the game ends the battle with the other team as the winner
    (reason FORFEIT) and reports the loss to the server like any other battle end (the battle server used to do
    this). `end_handler`: the mode's battle end, or None for the shared one."""
    a = A64
    body = [
        a.stp_pre("x20", "x19", -0x20), a.stp("x29", "x30", 0x10), a.mov("x19", "x0"),
        lambda pc: a.adrp("x0", pc, BATTLE_ENDED_CLASS_PAGE), a.add("x0", "x0", BATTLE_ENDED_CLASS_OFF),
        lambda pc: a.bl(pc, INIT_CLASS),
        lambda pc: a.adrp("x8", pc, BATTLE_ENDED_CLASS_PAGE), a.ldr("x0", "x8", BATTLE_ENDED_CLASS_OFF),
        lambda pc: a.bl(pc, OBJECT_NEW),
        a.mov("x20", "x0"), a.mov("x1", "xzr"), lambda pc: a.bl(pc, BATTLE_ENDED_NEW),
        a.ldr("x8", "x19", 0x10), a.ldrb("w8", "x8", 0x41),
        a.eor1("w8", "w8"), a.str_w("w8", "x20", 0x18),  # the winner: the other team
        a.movz("w8", FORFEIT), a.str_w("w8", "x20", 0x38),  # the reason: forfeit
        a.mov("x0", "x19"), a.mov("x1", "x20"),
        a.ldp("x29", "x30", 0x10), a.ldp_post("x20", "x19", 0x20),
    ]
    if end_handler is not None:
        body += [a.mov("x2", "xzr"), lambda pc: a.b(pc, end_handler)]
    else:  # the shared battle end
        body += [a.ldr("x8", "x0", 0), a.ldr("x2", "x8", 0x1B0), a.ldr("x8", "x8", 0x1A8), a.br("x8")]
    return [(offset, body)]


CLIENT_PATCHES: dict[str, list[Patch]] = {
    "KOTH battle start": _install(0x15E1134, 0x15E1194, 0x15E11A8, 0x15E11B8, 0x15E1320),
    "arena battle start": _install(0x14CF2B8, 0x14CF318, 0x14CF32C, 0x14CF33C, 0x14CF4A4),
    "friendly battle start": _install(0x13F82A4, 0x13F8310, 0x13F8324, 0x13F8334, 0x13F849C),
    "club war battle start": _install(0x173CEE8, 0x173CF48, 0x173CF5C, 0x173CF6C, 0x173D0D4),
    "limited-time arena battle start": _lta_install(),
    "limited-time arena quit": [(0x11F9540, [A64.movz("w0", 0), A64.RET])],
    "limited-time arena result": _lta_result(),
    # quitting a local battle is handled on the phone, as in a campaign battle, not by a battle server
    "KOTH quit": [(0x15E1064, [A64.movz("w0", 0), A64.RET])],
    "arena quit": [(0x14CF1E8, [A64.movz("w0", 0), A64.RET])],
    "friendly battle quit": [(0x13F81E0, [A64.movz("w0", 0), A64.RET])],
    "KOTH result": _end_handler(0x15E08DC, 0x15E09A8, 0x15E09E8, "x23", "x21", 0x40),
    "club war result": _end_handler(0x173C010, 0x173C0E8, 0x173C104, "x21", "x22", 0x50),
    "arena result": _pvp_result(),
    "friendly battle result": _social_pvp_result(),
    # forfeiting a local battle = a loss reported like any other battle end
    "KOTH forfeit": _quit_as_forfeit(0x15E0C40, 0x15E07A4),
    "arena and friendly battle forfeit": _quit_as_forfeit(0x144F160, None),
    "club war forfeit": _quit_as_forfeit(0x173C5B8, 0x173BE90),
}


# Guest login: the game logs in as a guest, with its anonymous id (what the server uses), instead of opening the
# sign-in popup of the store's game service.
GUEST_LOGIN_PATCHES: dict[str, list[Patch]] = {
    "guest login": [(0x14189A8, [A64.movz("w0", 1), A64.RET])],
}


# Rewarded videos: every "watch a video" placement (store ad chest, the ad chest after a pack opening, energy refills
# and bonus generation, club dungeon bonus attempt, daily quest, double check-in reward, KOTH cooldown). There is no
# ad network any more, so the placements' buttons are always shown (they used to depend on a remote setting), and a
# tap grants the reward at once, as when a video has finished.
SHOW_RV_FLAG_OFFSET = 0x110
AD_REWARD_PATCHES: dict[str, list[Patch]] = {
    # the placements' buttons are shown
    "rewarded placements shown": [(0x112E294, [0x39000000 | (SHOW_RV_FLAG_OFFSET << 10) | (20 << 5) | 8])],
    # the remote setting no longer hides them
    "rewarded placements stay shown": [(0x1137AA4, [A64.NOP])],
    # a tap counts as a finished video: the reward is granted
    "reward without a video": [(0x1130480, [lambda pc: A64.b(pc, 0x1130500)])],
}


# "Rate now" prompt: the store-rating popup (linking to a store listing that no longer exists) is never shown.
RATE_PROMPT_PATCHES: dict[str, list[Patch]] = {
    "rate prompt": [(0x1A2A1D4, [A64.movz("w0", 0), A64.RET])],
}


# Timed store cards: the store updates the countdown of the cards on the selected tab only, and the tab of the timed
# special-offer cards (tab kind 10) was missing from that list, so its cards showed "00h 00m" instead of counting down
# to the end of their sale. The tab lookup (one entry per tab kind 1..12 in a table) is rewritten more compactly, which
# makes room for that tab; the other tabs are found exactly as before (the tests check every tab kind).
STORE_SCROLL_RECT_SWITCH = 0x15B964C  # the rewritten tab lookup
STORE_SCROLL_RECT_TABLE = 0x2D639A0
STORE_VIEW_GET_FIELDS = 0x15B9500
STORE_SCROLL_RECT_RET, STORE_SCROLL_RECT_NRE = 0x15B9644, 0x15B96D8
STORE_SCROLL_RECT_FIELDS = ((2, 0x30), (7, 0xD0), (3, 0x38), (9, 0x68), (12, 0x80), (4, 0x40), (6, 0xB8), (10, 0x70))


def _store_timer_patches() -> dict[str, list[Patch]]:
    code: list = []
    starts = {}
    for i, (size, field) in enumerate(STORE_SCROLL_RECT_FIELDS):
        starts[size] = STORE_SCROLL_RECT_SWITCH + 4 * len(code)
        code += [A64.mov("x0", "x8"), lambda pc: A64.bl(pc, STORE_VIEW_GET_FIELDS), A64.movz("x9", field)]
        if i < len(STORE_SCROLL_RECT_FIELDS) - 1:
            code.append(None)  # b <tail>, placed below
    tail = STORE_SCROLL_RECT_SWITCH + 4 * len(code)
    code = [(lambda pc: A64.b(pc, tail)) if w is None else w for w in code]
    code += [lambda pc: A64.cbz("x0", pc, STORE_SCROLL_RECT_NRE), A64.ldr_reg("x0", "x0", "x9"),
             lambda pc: A64.b(pc, STORE_SCROLL_RECT_RET)]
    code += [A64.NOP] * ((STORE_SCROLL_RECT_NRE - STORE_SCROLL_RECT_SWITCH) // 4 - len(code))
    # the table holds each tab kind's code as an offset from the table: the kinds with a list point at their new code
    # (the others keep their entries)
    return {
        "timed store cards": [(STORE_SCROLL_RECT_SWITCH, code)],
        "timed store cards (tab table)": [(STORE_SCROLL_RECT_TABLE + 4 * (size - 1),
                                           [(start - STORE_SCROLL_RECT_TABLE) & 0xFFFFFFFF])
                                          for size, start in sorted(starts.items())],
    }


STORE_TIMER_PATCHES: dict[str, list[Patch]] = _store_timer_patches()


# The publisher's services: no build reaches a third-party host (its network security config trusts no certificate
# authority), and the game must not wait for them.
# - Store: the game asks the publisher's commerce service for its in-app purchase catalog and the store's products,
#   and several steps (leaving the login state, the daily rewards tab) wait until it answers. Nothing can be bought
#   any more, so both requests finish at once, with no products: they call the caller's "finished" action right away.
# - Account status: after the game server's login the game asks the publisher's account service whether the account
#   was deleted there, and any answer but "active" (here: not reachable) ends the login with a connection error. The
#   game server is the only account service now, so the login goes on as for an active account.
ACTION_INVOKE = 0x212480C  # Action.Invoke (the delegate in x0)
IAP_CATALOG_REQUEST = 0x112BA58  # (this, Action finished): requests the catalog, then calls finished
IAP_PRODUCTS_QUERY = 0x112B130  # (this, HashSet products, Action finished): queries them, then calls finished


def _finish_at_once(entry: int, finished: str) -> Patch:
    """`entry` calls its `finished` action (register `finished`) at once and returns (nothing when it is null)."""
    a = A64
    return (entry, [lambda pc: a.cbz(finished, pc, pc + 16), a.mov("x0", finished), a.mov("x1", "xzr"),
                    lambda pc: a.b(pc, ACTION_INVOKE), a.RET])


ACCOUNT_STATUS_ACTIVE_CHECK = 0x1361D7C  # tbz w0, <not the "active" status> (in the status callback of the login)
PUBLISHER_SERVICE_PATCHES: dict[str, list[Patch]] = {
    "store catalog not requested": [_finish_at_once(IAP_CATALOG_REQUEST, "x1")],
    "store products not queried": [_finish_at_once(IAP_PRODUCTS_QUERY, "x2")],
    "account status taken as active": [(ACCOUNT_STATUS_ACTIVE_CHECK, [A64.NOP])],
}


@dataclass(frozen=True)
class ClientBuild:
    """The game library patch sets for one client build (keyed by its ELF build-id in CLIENT_BUILDS)."""

    name: str
    local_battles: dict[str, list[Patch]]
    guest_login: dict[str, list[Patch]]
    ad_rewards: dict[str, list[Patch]]
    rate_prompt: dict[str, list[Patch]]
    store_timers: dict[str, list[Patch]]
    publisher_services: dict[str, list[Patch]]


CLIENT_BUILDS: dict[bytes, ClientBuild] = {
    V29_LIB_BUILD_ID: ClientBuild("v29.0 build 1587910", CLIENT_PATCHES, GUEST_LOGIN_PATCHES, AD_REWARD_PATCHES,
                                       RATE_PROMPT_PATCHES, STORE_TIMER_PATCHES, PUBLISHER_SERVICE_PATCHES),
}


def words_bytes(offset: int, items: list) -> bytes:
    """The bytes of a patch's instructions placed at `offset` (callables get their own address)."""
    words = [it(offset + 4 * i) if callable(it) else it for i, it in enumerate(items)]
    return b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words)


def apply_definitions(so: bytes, patch_sets: dict[str, list[Patch]]) -> bytes:
    """`so` with the instructions of `patch_sets` written in (no check of what they replace: the rules carry that,
    patch.apply_edits checks it). Tests compare the rules' result with this."""
    out = bytearray(so)
    for patches in patch_sets.values():
        for offset, items in patches:
            new = words_bytes(offset, items)
            out[offset:offset + len(new)] = new
    return bytes(out)


def client_build(so: bytes) -> ClientBuild:
    """The patch sets for this game library, or PatchError naming its build-id when there are none."""
    build_id = elf_build_id(so) or b""
    build = CLIENT_BUILDS.get(build_id)
    if build is None:
        known = ", ".join(f"{b.name} (build-id {k.hex()})" for k, b in CLIENT_BUILDS.items())
        raise PatchError(f"{GAME_LIB}: unsupported build-id {build_id.hex() or 'none'}; the patches exist for {known}. "
                         "Use the supported APK.")
    return build


# --- the consent / age dialog (default on) ------------------------------------------------------------------------
#
# On first launch the publisher SDK's consent dialog (class ConsentUI) would ask for a birth month and terms / ads
# choices in a full-screen web view (its pages came from the publisher's servers) and report a status JSON to its
# listener. ConsentUI.showConsent is patched to report, right away and without showing anything, the most
# privacy-protective answer the game still proceeds with: only "no personalised ads" (CONSENT_STATUS). It asserts
# no age and accepts no terms; nothing is sent anywhere (the services it reported to are removed, see below). The
# class is found by name in the APK's dex files (CONSENT_CLASS), whatever package the SDK put it in.

CONSENT_CLASS = "/consent/ConsentUI;"
CONSENT_STATUS = {"ads": "non_personalized"}


def dex_classes(dex: bytes) -> list[str]:
    """The descriptors (e.g. "Lcom/x/Y;") of the classes a dex file defines."""
    if not dex.startswith(b"dex\n"):
        return []
    string_ids_off, type_ids_off = struct.unpack_from("<I", dex, 0x3C)[0], struct.unpack_from("<I", dex, 0x44)[0]
    class_defs_size, class_defs_off = struct.unpack_from("<II", dex, 0x60)

    def type_name(type_idx: int) -> str:
        (string_idx,) = struct.unpack_from("<I", dex, type_ids_off + 4 * type_idx)
        (off,) = struct.unpack_from("<I", dex, string_ids_off + 4 * string_idx)
        while dex[off] & 0x80:  # skip the uleb128 utf16 length
            off += 1
        return dex[off + 1:dex.index(b"\0", off + 1)].decode("utf-8", "replace")

    return [type_name(struct.unpack_from("<I", dex, class_defs_off + 32 * i)[0]) for i in range(class_defs_size)]


def dex_defines(dex: bytes, descriptor: str) -> bool:
    return descriptor in dex_classes(dex)


def find_consent_ui(apk: Path) -> tuple[str, str]:
    """(dex entry name, class descriptor) of the SDK's consent dialog class in `apk`; exactly one must exist."""
    found = []
    with zipfile.ZipFile(apk) as z:
        for n in z.namelist():
            if re.fullmatch(r"classes\d*\.dex", n):
                found += [(n, c) for c in dex_classes(z.read(n)) if c.endswith(CONSENT_CLASS)]
    if len(found) != 1:
        raise PatchError(f"the consent dialog class (*{CONSENT_CLASS}) is defined {len(found)} times in {apk.name}, "
                         "expected once (use --no-skip-consent)")
    return found[0]


def legacy_consent_entries(base_apk: Path) -> dict[str, bytes]:
    """{dex name: dex}: the dex defining the consent class, with showConsent answering at once (dexpatch)."""
    from arcane_patcher import dexpatch

    name, consent_ui = find_consent_ui(base_apk)
    with zipfile.ZipFile(base_apk) as z:
        dex = z.read(name)
    try:
        out = dexpatch.patch_consent(dex, consent_ui, json.dumps(CONSENT_STATUS, separators=(",", ":")))
    except dexpatch.DexPatchError as e:
        raise PatchError(f"consent dialog patch: {e} (use --no-skip-consent)") from None
    print(f"{name}: consent dialog not shown (recorded: no personalised ads)")
    return {name: out}




# --- third-party services in the manifest (every variant) -----------------------------------------------------
#
# The client ships analytics, crash reporting, push messaging, advertising / ad mediation and attribution SDKs. None
# of them is the project's, their services are of no use to a build, and the user does not want them: their
# manifest components (activities, services, receivers, providers), their discovery meta-data and the permissions
# only they use are removed by name prefix (patch.strip_manifest), and the collection switches the SDKs read from
# the manifest are set to off for any code path that still reaches them.
THIRD_PARTY_PREFIXES = (
    # analytics, push messaging and the event transport. Firebase itself (its init provider and component registrars)
    # stays: the game's native code needs an initialized app object and aborts without one; with the collection
    # switches below off it collects and sends nothing.
    "com.google.firebase.messaging.", "com.google.firebase.iid.", "com.google.android.gms.measurement.",
    "com.google.android.datatransport.", "backend:com.google.android.datatransport.", "com.google.android.c2dm.",
    # the publisher SDK's own components (package csdk: its push messaging and marketing links)
    "csdk.",
    # advertising, ad mediation and ad networks
    "com.google.android.gms.ads.", "com.applovin.", "com.facebook.ads.", "com.ironsource.", "com.vungle.",
    "com.mbridge.", "com.fyber.", "com.tapjoy.", "com.unity3d.services.", "io.bidmachine.", "com.explorestack.",
    # engagement messaging, attribution, marketing
    "com.appboy.", "com.braze.", "com.singular.", "com.amazonaws.mobileconnectors.pinpoint.",
    # the advertising id and install referrer permissions
    "com.google.android.gms.permission.AD_ID", "com.google.android.finsky.permission.BIND_GET_INSTALL_REFERRER_SERVICE",
    "BIND_GET_INSTALL_REFERRER_SERVICE",
)
COLLECTION_OFF = {
    "firebase_analytics_collection_enabled": "false",
    "firebase_analytics_collection_deactivated": "true",
    "firebase_crashlytics_collection_enabled": "false",
    "firebase_messaging_auto_init_enabled": "false",
    "firebase_performance_collection_enabled": "false",
    "firebase_inapp_messaging_auto_data_collection_enabled": "false",
    "google_analytics_adid_collection_enabled": "false",
    "google_analytics_ssaid_collection_enabled": "false",
    "google_analytics_default_allow_ad_personalization_signals": "false",
    "google_analytics_default_allow_ad_user_data": "false",
    "google_analytics_default_allow_analytics_storage": "false",
    "google_analytics_default_allow_ad_storage": "false",
    "google_analytics_automatic_screen_reporting_enabled": "false",
}


# Analytics upload: the publisher's analytics hands its events to an AWS Kinesis recorder, which stores them on the
# phone and uploads them with its own HTTPS client (measured on the device: it reaches its host where every other
# client of the app is refused by the network security config). The recorder's methods that store and submit records
# return at once (each method's code becomes return-void followed by nops), so nothing is stored or sent.
ANALYTICS_UPLOAD_TITLE = "analytics events neither stored nor uploaded (the Kinesis recorder returns at once)"
_KINESIS = "Lcom/amazonaws/mobileconnectors/kinesis/kinesisrecorder/"
ANALYTICS_UPLOAD_METHODS = (
    f"{_KINESIS}AbstractKinesisRecorder;->submitAllRecords()V",
    f"{_KINESIS}AbstractKinesisRecorder;->saveRecord(Ljava/lang/String;Ljava/lang/String;)V",
    f"{_KINESIS}AbstractKinesisRecorder;->saveRecord([BLjava/lang/String;)V",
    f"{_KINESIS}KinesisRecorder;->saveRecord([BLjava/lang/String;)V",
    f"{_KINESIS}KinesisFirehoseRecorder;->saveRecord([BLjava/lang/String;)V",
)


def return_at_once_edits(dexes: dict[str, bytes], methods: tuple[str, ...]) -> dict[str, list[dict]]:
    """{dex name: edits} making each void method of `methods` return at once: its code becomes return-void followed by
    nops; each method must be defined with code in exactly one dex."""
    from arcane_patcher import dexpatch

    wanted = set(methods)
    codes: dict[str, list[tuple[str, int]]] = {m: [] for m in methods}
    for name, data in sorted(dexes.items()):
        dx = dexpatch.Dex(bytearray(data))
        for m, code in dx.methods_with_code():
            ref = dx.method(m)
            if ref in wanted:
                codes[ref].append((name, code))
    out: dict[str, list[dict]] = {}
    for method in methods:
        found = codes[method]
        if len(found) != 1 or not method.endswith(")V"):
            raise PatchError(f"{method}: defined with code {len(found)} times (expected once, a void method)")
        name, code = found[0]
        original = dexes[name]
        units = struct.unpack_from("<I", original, code + 12)[0]  # insns_size of the code item
        at = code + 16  # its instructions: return-void, then nop for every other unit (the verifier decodes them all)
        patched = bytearray(original)
        patched[at:at + 2 * units] = b"\x0e\x00" + bytes(2 * units - 2)
        out.setdefault(name, []).extend(dex_edits(original, bytes(patched), method.split("->")[1].split("(")[0]
                                                  + " returns at once") or [])
    return out


def manifest_section() -> dict:
    return {"title": "third-party analytics, crash reporting, push and advertising services removed",
            "remove": list(THIRD_PARTY_PREFIXES), "meta_data": dict(COLLECTION_OFF)}


# --- writing the rules --------------------------------------------------------------------------------------------

# option sets of a build (the rules' "lib" keys): ClientBuild field -> rules key
LIB_SETS = {"local_battles": "local_battles", "guest_login": "guest_login", "ad_rewards": "ad_rewards",
            "rate_prompt": "rate_prompt", "store_timers": "store_timers",
            "publisher_services": "publisher_services"}
LIB_TITLES = {"local_battles": "local multiplayer battles", "guest_login": "guest login (no sign-in popup)",
              "ad_rewards": "ad rewards (no rewarded video)", "rate_prompt": "rate-now prompt (never shown)",
              "store_timers": "store timers (store cards count down)",
              "publisher_services": "no waiting for the publisher's services (store catalog, account status)"}


def edit(note: str, offset: int, original: bytes, new: bytes) -> dict:
    """One byte edit as the rules hold it: the original bytes only as their length and sha256 (the patch file carries
    no bytes of the game), the new bytes in full (the project's own code / data)."""
    if len(original) != len(new):
        raise PatchError(f"{note} @ {offset:#x}: patch and original differ in length")
    return {"note": note, "offset": offset, "length": len(new), "original_sha256": hashlib.sha256(original).hexdigest(),
            "new": new.hex()}


def lib_edits(patch_sets: dict[str, list[Patch]], so: bytes) -> list[dict]:
    """The byte edits of `patch_sets` on the game library `so` (whose original bytes are hashed)."""
    out = []
    for name, patches in patch_sets.items():
        for offset, items in patches:
            new = words_bytes(offset, items)
            if offset + len(new) > len(so):
                raise PatchError(f"{name} @ {offset:#x}: beyond the end of {GAME_LIB}")
            out.append(edit(name, offset, so[offset:offset + len(new)], new))
    return out


def dex_edits(original: bytes, patched: bytes, note: str) -> list[dict]:
    """The changed runs after the dex header's checksum and signature (the patcher recomputes those); each run holds
    changed bytes only, so the rules carry none of the dex's own bytes."""
    if len(original) != len(patched):
        raise PatchError("dex edits must keep the file's length")
    runs: list[list[int]] = []
    for i in range(32, len(original)):
        if original[i] != patched[i]:
            if runs and runs[-1][1] == i:
                runs[-1][1] = i + 1
            else:
                runs.append([i, i + 1])
    return [edit(note, a, original[a:b], patched[a:b]) for a, b in runs]


def base_apk_bytes(apk: Path) -> bytes:
    """The base APK of a single APK or a split bundle."""
    with zipfile.ZipFile(apk) as z:
        names = z.namelist()
        if "base.apk" in names:
            return z.read("base.apk")
    return Path(apk).read_bytes()


def generate(apk: Path) -> dict:
    """The rules of the client build `apk` holds (its game library's build-id must be one of CLIENT_BUILDS)."""
    import io

    from arcane_patcher import dexpatch

    base = base_apk_bytes(apk)
    so = None
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        if GAME_LIB in z.namelist():
            so = z.read(GAME_LIB)
    if so is None:  # a split bundle: the arm64 split
        with zipfile.ZipFile(apk) as outer:
            for m in sorted(n for n in outer.namelist() if n.endswith(".apk")):
                with zipfile.ZipFile(io.BytesIO(outer.read(m))) as split:
                    if GAME_LIB in split.namelist():
                        so = split.read(GAME_LIB)
                        break
    if so is None:
        raise PatchError(f"{apk}: no {GAME_LIB}")
    build_id = elf_build_id(so) or b""
    build = CLIENT_BUILDS.get(build_id)
    if build is None:
        raise PatchError(f"no patch definitions for build-id {build_id.hex()}")
    rules: dict = {"format": RULES_FORMAT, "license": RULES_LICENSE, "client": build.name, "build_id": build_id.hex(),
                   "game_lib": GAME_LIB,
                   "lib": {}, "dex": {}, "manifest": manifest_section()}
    for field, key in LIB_SETS.items():
        rules["lib"][key] = {"title": LIB_TITLES[key], "edits": lib_edits(getattr(build, field), so)}
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        found = [(n, c) for n in z.namelist() if re.fullmatch(r"classes\d*\.dex", n)
                 for c in dex_classes(z.read(n)) if c.endswith(CONSENT_CLASS)]
        if len(found) != 1:
            raise PatchError(f"the consent dialog class is defined {len(found)} times, expected once")
        name, consent_ui = found[0]
        dex = z.read(name)
    patched = dexpatch.patch_consent(dex, consent_ui, json.dumps(CONSENT_STATUS, separators=(",", ":")))
    rules["dex"]["consent"] = {
        "title": "consent dialog not shown (recorded: no personalised ads; no age or terms answered)",
        "files": {name: dex_edits(dex, patched, "consent dialog: showConsent reports at once")}}
    with zipfile.ZipFile(io.BytesIO(base)) as z:
        dexes = {n: z.read(n) for n in z.namelist() if re.fullmatch(r"classes\d*\.dex", n)}
    rules["dex"]["analytics_upload"] = {"title": ANALYTICS_UPLOAD_TITLE,
                                        "files": return_at_once_edits(dexes, ANALYTICS_UPLOAD_METHODS)}
    return rules


def rules_text(rules: dict) -> str:
    return json.dumps(rules, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m arcane_patcher.rulesgen", description=__doc__.split("\n\n")[0])
    ap.add_argument("--apk", type=Path, required=True, help="the client APK (or split bundle) the rules are for")
    ap.add_argument("--out", type=Path, default=RULES_DIR, help=f"directory (default {RULES_DIR})")
    a = ap.parse_args(argv)
    rules = generate(a.apk)
    a.out.mkdir(parents=True, exist_ok=True)
    path = a.out / f"{rules['build_id']}.json"
    path.write_text(rules_text(rules))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
