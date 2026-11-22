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

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.Locale;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * A game server address as the player types it: "host", "host:port", "http://host:port" or "https://host[:port]"
 * (no scheme = http). Only a server root: no path, query or user info. Plain Java (no Android classes), so the
 * proxy can be tested on a desktop JVM.
 */
public final class ServerUrl {
    public final String scheme;  // http | https
    public final String host;  // as in a URL ("[2001:db8::1]" for IPv6)
    public final int port;  // the effective port
    public final String hostHeader;  // host[:port] as a client sends it (no port when it is the scheme's default)

    private ServerUrl(String scheme, String host, int port) {
        this.scheme = scheme;
        this.host = host;
        this.port = port;
        this.hostHeader = port == defaultPort(scheme) ? host : host + ":" + port;
    }

    private static int defaultPort(String scheme) {
        return "https".equals(scheme) ? 443 : 80;
    }

    /** Parses what the player typed; throws IllegalArgumentException with a message for the player. */
    public static ServerUrl parse(String input) {
        String s = input == null ? "" : input.trim();
        if (s.isEmpty()) throw new IllegalArgumentException("Enter the server address.");
        if (!s.contains("://")) s = "http://" + s;
        URI u;
        try {
            u = new URI(s);
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("Not a valid address: " + input.trim());
        }
        String scheme = u.getScheme() == null ? "" : u.getScheme().toLowerCase(Locale.ROOT);
        if (!scheme.equals("http") && !scheme.equals("https")) {
            throw new IllegalArgumentException("The address must start with http:// or https://");
        }
        if (u.getHost() == null || u.getHost().isEmpty()) {
            throw new IllegalArgumentException("Not a valid address: " + input.trim());
        }
        if (u.getRawUserInfo() != null || u.getRawQuery() != null || u.getRawFragment() != null
                || (u.getRawPath() != null && !u.getRawPath().isEmpty() && !u.getRawPath().equals("/"))) {
            throw new IllegalArgumentException("Enter only the server's address (host and port), without a path.");
        }
        int port = u.getPort() == -1 ? defaultPort(scheme) : u.getPort();
        if (port < 1 || port > 65535) throw new IllegalArgumentException("Port must be 1-65535.");
        return new ServerUrl(scheme, u.getHost().toLowerCase(Locale.ROOT), port);
    }

    /** The normalized address, e.g. "http://203.0.113.5:18080" (what is saved and shown). */
    @Override
    public String toString() {
        return scheme + "://" + hostHeader;
    }

    /** For tests on a desktop JVM: one line per argument, the normalized address or "error: <message>". */
    public static void main(String[] args) {
        for (String a : args) {
            try {
                System.out.println(parse(a));
            } catch (IllegalArgumentException e) {
                System.out.println("error: " + e.getMessage());
            }
        }
    }

    public boolean isLoopback() {
        return host.equals("127.0.0.1") || host.equals("localhost") || host.equals("[::1]");
    }

    private static SSLSocketFactory tls;

    /**
     * TLS trusting the device's certificate authorities. Not the app's default: the build's network security config
     * trusts none, so the client's third-party SDKs reach no host; the chosen game server is reached only here.
     * Plain Java: a desktop JVM (no AndroidCAStore) uses its default.
     */
    static synchronized SSLSocketFactory tls() {
        if (tls == null) {
            try {
                java.security.KeyStore cas = java.security.KeyStore.getInstance("AndroidCAStore");
                cas.load(null, null);
                javax.net.ssl.TrustManagerFactory tmf =
                        javax.net.ssl.TrustManagerFactory.getInstance(javax.net.ssl.TrustManagerFactory.getDefaultAlgorithm());
                tmf.init(cas);
                javax.net.ssl.SSLContext ctx = javax.net.ssl.SSLContext.getInstance("TLS");
                ctx.init(null, tmf.getTrustManagers(), null);
                tls = ctx.getSocketFactory();
            } catch (java.security.GeneralSecurityException | IOException e) {
                tls = (SSLSocketFactory) SSLSocketFactory.getDefault();
            }
        }
        return tls;
    }

    /** An open connection to the server (TLS for https, with the host name verified). */
    public Socket connect(int timeoutMs) throws IOException {
        String name = host.startsWith("[") ? host.substring(1, host.length() - 1) : host;
        Socket plain = new Socket();
        plain.setTcpNoDelay(true);
        plain.connect(new InetSocketAddress(name, port), timeoutMs);
        if (!scheme.equals("https")) return plain;
        SSLSocket tls = (SSLSocket) tls().createSocket(plain, name, port, true);
        tls.startHandshake();
        if (!HttpsURLConnection.getDefaultHostnameVerifier().verify(name, tls.getSession())) {
            tls.close();
            throw new IOException("TLS certificate does not match " + name);
        }
        return tls;
    }
}
