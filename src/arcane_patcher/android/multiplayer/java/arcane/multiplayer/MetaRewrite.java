/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
package arcane.multiplayer;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;

/**
 * The server's /meta answer names where the client downloads its files (cdn_base_url, loc_cdn_base_url,
 * asset_cdn_base_url of the metadata response, inside the response envelope's payload). The multiplayer app serves
 * those files on the phone (LocalCdn), so the proxy points the three addresses at itself; everything else in the
 * answer stays as the server sent it. A minimal protobuf rewrite: only length-delimited fields are rebuilt, every
 * other field is copied byte for byte. Plain Java (tests run it on a desktop JVM).
 */
public final class MetaRewrite {
    static final int ENVELOPE_PAYLOAD = 1;
    static final int[] CDN_FIELDS = {3, 5, 6};  // cdn_base_url, loc_cdn_base_url, asset_cdn_base_url

    private MetaRewrite() {}

    /** The envelope `envelope` with its metadata payload's download addresses set to `base`. */
    public static byte[] rewrite(byte[] envelope, String base) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(envelope.length + 64);
        int[] pos = {0};
        while (pos[0] < envelope.length) {
            int start = pos[0];
            long key = varint(envelope, pos);
            int field = (int) (key >>> 3), type = (int) (key & 7);
            if (field == ENVELOPE_PAYLOAD && type == 2) {
                int len = length(envelope, pos);
                byte[] payload = new byte[len];
                System.arraycopy(envelope, pos[0], payload, 0, len);
                pos[0] += len;
                writeBytes(out, ENVELOPE_PAYLOAD, metadata(payload, base));
            } else {
                skip(envelope, pos, type);
                out.write(envelope, start, pos[0] - start);
            }
        }
        return out.toByteArray();
    }

    static byte[] metadata(byte[] msg, String base) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(msg.length + 64);
        int[] pos = {0};
        boolean[] seen = new boolean[CDN_FIELDS.length];
        while (pos[0] < msg.length) {
            int start = pos[0];
            long key = varint(msg, pos);
            int field = (int) (key >>> 3), type = (int) (key & 7);
            skip(msg, pos, type);
            int cdn = indexOf(field);
            if (cdn >= 0 && type == 2) {
                seen[cdn] = true;
                writeBytes(out, field, base.getBytes(StandardCharsets.UTF_8));
            } else {
                out.write(msg, start, pos[0] - start);
            }
        }
        for (int i = 0; i < CDN_FIELDS.length; i++) {
            if (!seen[i]) writeBytes(out, CDN_FIELDS[i], base.getBytes(StandardCharsets.UTF_8));
        }
        return out.toByteArray();
    }

    private static int indexOf(int field) {
        for (int i = 0; i < CDN_FIELDS.length; i++) if (CDN_FIELDS[i] == field) return i;
        return -1;
    }

    private static long varint(byte[] b, int[] pos) {
        long v = 0;
        for (int shift = 0; shift < 64; shift += 7) {
            if (pos[0] >= b.length) throw new IllegalArgumentException("truncated protobuf");
            int x = b[pos[0]++] & 0xFF;
            v |= (long) (x & 0x7F) << shift;
            if (x < 0x80) return v;
        }
        throw new IllegalArgumentException("bad varint");
    }

    private static void skip(byte[] b, int[] pos, int type) {
        switch (type) {
            case 0: varint(b, pos); break;
            case 1: advance(b, pos, 8); break;
            case 2: advance(b, pos, length(b, pos)); break;
            case 5: advance(b, pos, 4); break;
            default: throw new IllegalArgumentException("unsupported protobuf wire type " + type);
        }
    }

    /** A length-delimited field's length: 0 <= length <= the bytes left. */
    private static int length(byte[] b, int[] pos) {
        long len = varint(b, pos);
        if (len < 0 || len > b.length - pos[0]) throw new IllegalArgumentException("truncated protobuf");
        return (int) len;
    }

    private static void advance(byte[] b, int[] pos, int n) {
        if (n > b.length - pos[0]) throw new IllegalArgumentException("truncated protobuf");
        pos[0] += n;
    }

    private static void writeVarint(ByteArrayOutputStream out, long v) {
        while ((v & ~0x7FL) != 0) {
            out.write((int) ((v & 0x7F) | 0x80));
            v >>>= 7;
        }
        out.write((int) v);
    }

    private static void writeBytes(ByteArrayOutputStream out, int field, byte[] data) {
        writeVarint(out, ((long) field << 3) | 2);
        writeVarint(out, data.length);
        out.write(data, 0, data.length);
    }
}
