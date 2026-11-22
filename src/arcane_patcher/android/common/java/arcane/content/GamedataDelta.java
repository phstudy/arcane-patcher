/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
package arcane.content;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.security.MessageDigest;
import java.util.Arrays;

/**
 * The gamedata a server hands out is the client's own gamedata with the server's changes (season times, its News page
 * address, game-speed times). A server that hands out no client files sends only those changes, as a delta:
 *
 *   "AGD1" | sha256(original) (32 bytes) | u32 LE size of the result | u32 LE count | count x (u32 LE offset,
 *   u32 LE length, bytes)
 *
 * The result is the original cut or zero-padded to that size, with each run written at its offset. A delta made for
 * another original is refused. Plain Java: tests run it on a desktop JVM, against the server's own delta when the
 * server repository is next to this one.
 */
public final class GamedataDelta {
    private static final byte[] MAGIC = {'A', 'G', 'D', '1'};
    /** The most the server's changes may add to the gamedata (they append a few strings and tables). */
    static final long MAX_GROWTH = 16L << 20;

    private GamedataDelta() {}

    public static byte[] apply(byte[] original, byte[] delta) {
        if (delta.length < 44 || !Arrays.equals(Arrays.copyOf(delta, 4), MAGIC)) {
            throw new IllegalArgumentException("not a gamedata delta");
        }
        MessageDigest md = Files.digest();
        if (!Arrays.equals(Arrays.copyOfRange(delta, 4, 36), md.digest(original))) {
            throw new IllegalArgumentException("the gamedata delta is for another gamedata than the imported one");
        }
        ByteBuffer b = ByteBuffer.wrap(delta).order(ByteOrder.LITTLE_ENDIAN);
        b.position(36);
        long size = b.getInt() & 0xFFFFFFFFL, count = b.getInt() & 0xFFFFFFFFL;
        if (size > original.length + MAX_GROWTH) throw new IllegalArgumentException("gamedata delta: size out of range");
        byte[] out = Arrays.copyOf(original, (int) size);
        for (long i = 0; i < count; i++) {
            if (b.remaining() < 8) throw new IllegalArgumentException("gamedata delta: truncated");
            long off = b.getInt() & 0xFFFFFFFFL, len = b.getInt() & 0xFFFFFFFFL;
            if (off + len > size || len > b.remaining()) throw new IllegalArgumentException("gamedata delta: run out of range");
            b.get(out, (int) off, (int) len);
        }
        if (b.hasRemaining()) throw new IllegalArgumentException("gamedata delta: trailing bytes");
        return out;
    }
}
