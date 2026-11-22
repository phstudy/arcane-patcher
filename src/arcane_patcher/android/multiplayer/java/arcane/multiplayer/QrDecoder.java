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

import com.google.zxing.BarcodeFormat;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.LuminanceSource;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.ReaderException;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;

import java.util.Collections;
import java.util.EnumMap;
import java.util.Map;

/**
 * QR codes from camera frames (the Y plane of a YUV frame) or ARGB pixels, with ZXing core (embedded: no Google
 * Play services, so it works on sideloaded installs, emulators and BlueStacks). Plain Java: tests run it on a desktop
 * JVM. Not thread safe: one per decoding thread.
 */
public final class QrDecoder {
    private static final Map<DecodeHintType, Object> HINTS = new EnumMap<>(DecodeHintType.class);

    static {
        HINTS.put(DecodeHintType.POSSIBLE_FORMATS, Collections.singletonList(BarcodeFormat.QR_CODE));
        HINTS.put(DecodeHintType.CHARACTER_SET, "UTF-8");
    }

    private final QRCodeReader reader = new QRCodeReader();

    /** The text of a QR code in a greyscale frame (one byte per pixel, rows of `width`), or null if none is found. */
    public String decodeLuminance(byte[] y, int width, int height) {
        return decode(new PlanarYUVLuminanceSource(y, width, height, 0, 0, width, height, false));
    }

    /** The text of a QR code in ARGB pixels, or null. */
    public String decodeArgb(int[] pixels, int width, int height) {
        return decode(new RGBLuminanceSource(width, height, pixels));
    }

    private String decode(LuminanceSource src) {
        for (LuminanceSource s : new LuminanceSource[] {src, src.invert()}) {  // also light-on-dark codes
            try {
                return reader.decode(new BinaryBitmap(new HybridBinarizer(s)), HINTS).getText();
            } catch (ReaderException e) {
                // none in this frame
            } finally {
                reader.reset();
            }
        }
        return null;
    }
}
