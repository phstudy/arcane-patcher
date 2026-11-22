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

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.StatFs;
import android.provider.OpenableColumns;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FilterInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/**
 * Game data (asset bundles, gamedata, localization: ~1.6 GB) is not in the APK and no server hands it out. It comes as
 * the game data zip, made by the player from their own copy of the game, picked once in the setup screen and
 * extracted to (external files dir)/arcane_data. A marker file is written last, so an interrupted import is simply
 * redone. The zip holds the content set of one client release (content/NAME/, the server's ARCANE_DATA_DIR layout);
 * this build uses the release the asset arcane_content/content.json names (written by the patcher), so data of another
 * release counts as not imported and the setup screen asks for the zip again. Its gamedata must be the release's
 * (sha256 in content.json). Player data lives elsewhere and survives a re-import.
 */
public final class DataImport {
    static final String MARKER = ".import_complete";
    public static final String CONTENT_ASSET = "arcane_content/content.json";

    private DataImport() {}

    public static File dataDir(Context ctx) {
        File ext = ctx.getExternalFilesDir(null);
        return new File(ext != null ? ext : ctx.getFilesDir(), "arcane_data");
    }

    public static boolean isImported(Context ctx) {
        return new File(dataDir(ctx), MARKER).exists() && contentDir(ctx).isDirectory();
    }

    /** The imported content set of this build's client release: dataDir/content/NAME/. */
    public static File contentDir(Context ctx) {
        return contentDir(ctx, dataDir(ctx));
    }

    static File contentDir(Context ctx, File root) {
        return new File(new File(root, "content"), client(ctx));
    }

    private static JSONObject info(Context ctx) {
        try {
            return new JSONObject(Files.readAll(ctx.getAssets().open(CONTENT_ASSET)));
        } catch (IOException | JSONException e) {
            throw new IllegalStateException("missing or unreadable asset " + CONTENT_ASSET, e);
        }
    }

    /** The client release this build uses (the server's ARCANE_CLIENT). */
    public static String client(Context ctx) {
        return info(ctx).optString("client");
    }

    /** Language -> sha256 of the release's localization files (LocalCdn serves those, English otherwise). */
    public static Map<String, String> localizationSha256(Context ctx) {
        Map<String, String> out = new HashMap<>();
        JSONObject loc = info(ctx).optJSONObject("localization_sha256");
        if (loc != null) {
            for (Iterator<String> it = loc.keys(); it.hasNext(); ) {
                String k = it.next();
                out.put(k, loc.optString(k));
            }
        }
        return out;
    }

    public interface Listener {
        void progress(long done, long total, String entry);
    }

    /** Extract the zip at `uri` into dataDir (blocking; call off the UI thread). */
    public static void run(Context ctx, Uri uri, Listener listener) throws IOException {
        long total = size(ctx, uri);
        File dest = dataDir(ctx);
        File tmp = new File(dest.getParentFile(), "arcane_data.tmp");
        Files.deleteTree(tmp);
        tmp.mkdirs();
        long free = new StatFs(tmp.getPath()).getAvailableBytes();
        if (total > 0 && free < total + (200L << 20)) {
            throw new IOException("Not enough free space: need " + (total >> 20) + " MB, have " + (free >> 20) + " MB");
        }
        InputStream raw = ctx.getContentResolver().openInputStream(uri);
        if (raw == null) throw new IOException("cannot open " + uri);
        final long[] done = {0};
        InputStream counting = new FilterInputStream(raw) {
            @Override
            public int read(byte[] b, int off, int len) throws IOException {
                int n = super.read(b, off, len);
                if (n > 0) done[0] += n;
                return n;
            }
        };
        try {
            Files.unzip(counting, tmp, name -> listener.progress(done[0], total, name));
            File content = contentDir(ctx, tmp);
            if (!new File(content, "asset_bundle_metadata.json").exists() || !new File(content, "assets").isDirectory()) {
                throw new IOException("This zip is not the game data zip of this version (no "
                        + content.getParentFile().getName() + "/" + content.getName() + "/asset_bundle_metadata.json)");
            }
            String missing = missing(content);
            if (!missing.isEmpty()) throw new IOException("The game data zip is incomplete. Missing: " + missing);
            String want = info(ctx).optString("gamedata_sha256");
            File gamedata = new File(new File(content, "gamedata"), "gamedata-static.bin");
            if (!want.isEmpty() && !Files.sha256(gamedata).equals(want)) {
                throw new IOException("This zip's gamedata is not the one of this game version (gamedata-static.bin differs)");
            }
        } catch (IOException | RuntimeException e) {
            Files.deleteTree(tmp);  // an interrupted or refused import leaves nothing behind
            throw e;
        }
        Files.deleteTree(dest);
        if (!tmp.renameTo(dest)) throw new IOException("rename " + tmp + " -> " + dest);
        if (!new File(dest, MARKER).createNewFile()) throw new IOException("cannot write " + MARKER);
    }

    /** What the content set at `content` lacks (empty: complete): bundles of its list, gamedata, English text. */
    static String missing(File content) throws IOException {
        StringBuilder out = new StringBuilder();
        java.util.List<String> bundles = new java.util.ArrayList<>();
        java.util.Set<String> names = LocalCdn.bundleNames(new File(content, "asset_bundle_metadata.json"));
        for (String name : names) if (!new File(new File(content, "assets"), name).isFile()) bundles.add(name);
        if (names.isEmpty()) out.append("the bundle list is empty or unreadable");
        if (!bundles.isEmpty()) {
            java.util.Collections.sort(bundles);
            out.append(out.length() > 0 ? "; " : "").append(bundles.size()).append(" of ").append(names.size())
                    .append(" asset bundles (e.g. ").append(String.join(", ", bundles.subList(0, Math.min(3, bundles.size()))))
                    .append(")");
        }
        for (String f : new String[] {"gamedata/gamedata-static.bin", "localization/Loc_" + LocalCdn.FALLBACK_LANGUAGE + ".txt"}) {
            if (!new File(content, f).isFile()) out.append(out.length() > 0 ? "; " : "").append(f);
        }
        return out.toString();
    }

    private static long size(Context ctx, Uri uri) {
        try (Cursor c = ctx.getContentResolver().query(uri, new String[] {OpenableColumns.SIZE}, null, null, null)) {
            if (c != null && c.moveToFirst() && !c.isNull(0)) return c.getLong(0);
        } catch (RuntimeException ignored) {
        }
        return -1;
    }
}
