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

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The client's download paths answered from the player's own imported content set (DataImport), as a game server's
 * content routes answer them:
 *
 *   GET /assets/{version}/{bundle}                  the bundle file (assets/{bundle}; the name percent-decoded, and
 *                                                   only a name of the imported bundle list, asset_bundle_metadata.json);
 *                                                   the list the client gets (/assets/{version}/metadata) is the
 *                                                   server's and is not served here
 *   GET /localization/{version}/Loc_{lang}.txt      localization/Loc_{lang}.txt when it is the release's file (its
 *                                                   sha256 as pinned), else Loc_English.txt, as the server does
 *   GET /gamedata/{version}/gamedata-static.bin     gamedata/gamedata-static.bin with the server's changes applied
 *                                                   (GamedataDelta; the caller fetches the delta)
 *
 * Plain Java (tests run it on a desktop JVM).
 */
public final class LocalCdn {
    public enum Kind { NONE, ASSET, LOCALIZATION, GAMEDATA }

    public static final String FALLBACK_LANGUAGE = "English";
    private static final Pattern ASSET = Pattern.compile("/assets/([^/?]+)/([^/?]+)");
    private static final Pattern LOC = Pattern.compile("/localization/([^/?]+)/Loc_([A-Za-z]+)\\.txt");
    private static final Pattern GAMEDATA = Pattern.compile("/gamedata/([^/?]+)/gamedata-static\\.bin");
    private static final Pattern BUNDLE_NAME = Pattern.compile("\"bundleName\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");

    private final File contentDir;
    private final Map<String, String> localizationSha256;  // null: any present file
    private final Map<String, Boolean> pinned = new HashMap<>();
    private Set<String> bundles;  // the imported bundle list's names (read once)

    public LocalCdn(File contentDir, Map<String, String> localizationSha256) {
        this.contentDir = contentDir;
        this.localizationSha256 = localizationSha256;
    }

    public File contentDir() {
        return contentDir;
    }

    /** What kind of local path `path` (query stripped) is for `method`. */
    public static Kind kind(String method, String path) {
        if (!"GET".equals(method)) return Kind.NONE;
        Matcher m = ASSET.matcher(path);
        if (m.matches()) return m.group(2).equals("metadata") ? Kind.NONE : Kind.ASSET;
        if (LOC.matcher(path).matches()) return Kind.LOCALIZATION;
        if (GAMEDATA.matcher(path).matches()) return Kind.GAMEDATA;
        return Kind.NONE;
    }

    /** The version part of a gamedata path. */
    public static String gamedataVersion(String path) {
        Matcher m = GAMEDATA.matcher(path);
        if (!m.matches()) throw new IllegalArgumentException(path);
        return m.group(1);
    }

    /**
     * The bundle file of an asset path, or null when it names no bundle of the imported list (or the set lacks the
     * file). The name is percent-decoded (UTF-8; some bundle names have spaces).
     */
    public File asset(String path) throws IOException {
        Matcher m = ASSET.matcher(path);
        if (!m.matches()) return null;
        String name = percentDecode(m.group(2));
        if (name == null || !isSafeName(name) || !bundleNames().contains(name)) return null;
        File f = new File(new File(contentDir, "assets"), name);
        return f.isFile() ? f : null;
    }

    static boolean isSafeName(String name) {
        if (name.isEmpty() || name.equals(".") || name.contains("..") || name.contains("/") || name.contains("\\")) {
            return false;
        }
        for (int i = 0; i < name.length(); i++) if (name.charAt(i) < 0x20 || name.charAt(i) == 0x7F) return false;
        return true;
    }

    /** `s` with %XX escapes decoded as UTF-8 ('+' stays '+'), or null when an escape is malformed. */
    static String percentDecode(String s) {
        if (s.indexOf('%') < 0) return s;
        ByteArrayOutputStream out = new ByteArrayOutputStream(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '%') {
                if (i + 2 >= s.length()) return null;
                int hi = Character.digit(s.charAt(i + 1), 16), lo = Character.digit(s.charAt(i + 2), 16);
                if (hi < 0 || lo < 0) return null;
                out.write(hi << 4 | lo);
                i += 2;
            } else {
                byte[] b = String.valueOf(c).getBytes(StandardCharsets.UTF_8);
                out.write(b, 0, b.length);
            }
        }
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    /** The names of the imported bundle list (asset_bundle_metadata.json). */
    public synchronized Set<String> bundleNames() throws IOException {
        if (bundles == null) bundles = bundleNames(new File(contentDir, "asset_bundle_metadata.json"));
        return bundles;
    }

    public static Set<String> bundleNames(File metadataJson) throws IOException {
        Set<String> out = new HashSet<>();
        if (!metadataJson.isFile()) return out;
        Matcher m = BUNDLE_NAME.matcher(new String(Files.readBytes(metadataJson), StandardCharsets.UTF_8));
        while (m.find()) out.add(jsonUnescape(m.group(1)));
        return out;
    }

    static String jsonUnescape(String s) {
        if (s.indexOf('\\') < 0) return s;
        StringBuilder out = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c != '\\' || i + 1 >= s.length()) {
                out.append(c);
                continue;
            }
            char e = s.charAt(++i);
            switch (e) {
                case 'n': out.append('\n'); break;
                case 't': out.append('\t'); break;
                case 'r': out.append('\r'); break;
                case 'b': out.append('\b'); break;
                case 'f': out.append('\f'); break;
                case 'u':
                    if (i + 4 < s.length()) {
                        out.append((char) Integer.parseInt(s.substring(i + 1, i + 5), 16));
                        i += 4;
                    }
                    break;
                default: out.append(e);  // \" \\ \/
            }
        }
        return out.toString();
    }

    /** The localization file of a localization path (the English one when the language is absent or not the pinned file). */
    public File localization(String path) throws IOException {
        Matcher m = LOC.matcher(path);
        if (!m.matches()) return null;
        String lang = m.group(2);
        File dir = new File(contentDir, "localization");
        File f = new File(dir, "Loc_" + lang + ".txt");
        if (!lang.equals(FALLBACK_LANGUAGE) && !isPinned(lang, f)) f = new File(dir, "Loc_" + FALLBACK_LANGUAGE + ".txt");
        return f.isFile() ? f : null;
    }

    private synchronized boolean isPinned(String lang, File f) throws IOException {
        Boolean ok = pinned.get(lang);
        if (ok == null) {
            if (!f.isFile()) ok = false;
            else if (localizationSha256 == null) ok = true;
            else ok = Files.sha256(f).equals(localizationSha256.get(lang));
            pinned.put(lang, ok);
        }
        return ok;
    }

    public File gamedataFile() {
        return new File(new File(contentDir, "gamedata"), "gamedata-static.bin");
    }
}
