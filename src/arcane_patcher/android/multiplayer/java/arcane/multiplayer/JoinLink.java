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

import java.io.UnsupportedEncodingException;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.util.Locale;

/**
 * A game server handed over as a link instead of typed: the multiplayer app's deep link arcane://connect?url=<address>
 * (what a server's join page shows as a QR code and link; the intent filter is in the patch file's multiplayer
 * manifest fragment) or, from the QR scanner, a plain http(s) address or the join page's own URL. The address it names
 * is parsed like a typed one (ServerUrl) and then checked like one. Plain Java (tests run it on a desktop JVM).
 */
public final class JoinLink {
    public static final String SCHEME = "arcane", HOST = "connect";
    private static final String NOT_OURS = "This is not a game server code.";

    private JoinLink() {}

    /** The server a deep link (intent data) names; IllegalArgumentException with a message for the player. */
    public static ServerUrl parseDeepLink(String link) {
        URI u;
        try {
            u = new URI(link == null ? "" : link.trim());
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException(NOT_OURS);
        }
        if (!SCHEME.equalsIgnoreCase(u.getScheme()) || u.getHost() == null || !HOST.equalsIgnoreCase(u.getHost())) {
            throw new IllegalArgumentException(NOT_OURS);
        }
        String url = queryParam(u.getRawQuery(), "url");
        if (url == null || url.trim().isEmpty()) throw new IllegalArgumentException("The link names no server address.");
        return ServerUrl.parse(url);
    }

    /** The server a scanned QR code names: a deep link, an http(s) address, or a server's join page URL. */
    public static ServerUrl parseScanned(String text) {
        String s = text == null ? "" : text.trim();
        String lower = s.toLowerCase(Locale.ROOT);
        if (lower.startsWith(SCHEME + ":")) return parseDeepLink(s);
        if (!lower.startsWith("http://") && !lower.startsWith("https://")) throw new IllegalArgumentException(NOT_OURS);
        String joinPage = s.replaceFirst("(?i)/join/?$", "");
        return ServerUrl.parse(joinPage);
    }

    private static String queryParam(String rawQuery, String name) {
        if (rawQuery == null) return null;
        for (String pair : rawQuery.split("&")) {
            int eq = pair.indexOf('=');
            String key = eq < 0 ? pair : pair.substring(0, eq);
            if (!key.equals(name)) continue;
            try {
                return URLDecoder.decode(eq < 0 ? "" : pair.substring(eq + 1), "UTF-8");
            } catch (UnsupportedEncodingException | IllegalArgumentException e) {
                throw new IllegalArgumentException("The link's server address is malformed.");
            }
        }
        return null;
    }

    /** For tests on a desktop JVM: one line per argument ("link:<deep link>" or a scanned text): the address or "error: ...". */
    public static void main(String[] args) {
        for (String a : args) {
            try {
                System.out.println(a.startsWith("link:") ? parseDeepLink(a.substring(5)) : parseScanned(a));
            } catch (IllegalArgumentException e) {
                System.out.println("error: " + e.getMessage());
            }
        }
    }
}
