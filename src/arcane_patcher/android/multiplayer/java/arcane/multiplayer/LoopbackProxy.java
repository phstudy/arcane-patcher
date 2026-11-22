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

import arcane.content.GamedataDelta;
import arcane.content.Files;
import arcane.content.LocalCdn;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

import javax.net.ssl.SSLSocket;

/**
 * The game's server address is fixed in the APK (bootstrap_config service_url = http://127.0.0.1:PORT/); this
 * reverse proxy on that loopback port forwards every connection to the server the player chose (ServerUrl).
 * The game reaches the game server only through it: /meta, /auth_token, /status/maintenance.json and the RPC
 * websocket. Each request's Host header is rewritten to the chosen server's, so the server sees the address the
 * player typed and hands out the battle server on it (the game reaches that directly).
 *
 * The client's own files (bundles, localization, gamedata) come from the player's imported content set, not from
 * the server: the /meta answer's download addresses are pointed at this proxy (MetaRewrite) and it answers those
 * paths itself (LocalCdn); the gamedata is the imported one with the server's changes (GamedataDelta, fetched from
 * the server's /gamedata/{version}/gamedata-static.delta). The bundle list (/assets/{version}/metadata) and
 * everything else is forwarded.
 *
 * Per forwarded connection: the request head is read and rewritten (Host; "Connection: close" unless it is a
 * websocket upgrade, so one request per connection and no HTTP parsing beyond the head), then bytes are piped both
 * ways until the server closes. Plain Java (no Android classes): tests run it on a desktop JVM through main().
 */
public final class LoopbackProxy {
    private static final int MAX_HEAD = 64 * 1024;
    private static final int CONNECT_TIMEOUT_MS = 15000;
    private static volatile ServerUrl upstream;
    private static ServerSocket listening;
    private static volatile String error;
    private static volatile LocalCdn cdn;
    private static volatile String localBase;
    private static final Map<String, byte[]> gamedata = new LinkedHashMap<>();  // "server version" -> served gamedata

    private LoopbackProxy() {}

    /** Starts listening on 127.0.0.1:port (once per process). */
    public static synchronized void start(int port) throws IOException {
        if (listening != null) return;
        ServerSocket ss = new ServerSocket();
        ss.setReuseAddress(true);
        try {
            ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 64);
        } catch (IOException e) {
            ss.close();
            error = "Port " + port + " on this phone is in use: " + e.getMessage();
            throw e;
        }
        listening = ss;
        localBase = "http://127.0.0.1:" + port;
        error = null;
        Thread t = new Thread(() -> acceptLoop(ss), "arcane-multiplayer-proxy");
        t.setDaemon(true);
        t.start();
    }

    /** Where new connections go (connections already open keep their server). */
    public static void setUpstream(ServerUrl url) {
        upstream = url;
    }

    public static ServerUrl upstream() {
        return upstream;
    }

    /** The imported content set the client's files are served from (null: not imported; those requests get 404). */
    public static void setContent(LocalCdn content) {
        cdn = content;
        synchronized (gamedata) {
            gamedata.clear();
        }
    }

    /** Why the proxy is not listening, or null. */
    public static String error() {
        return error;
    }

    private static void acceptLoop(ServerSocket ss) {
        while (!ss.isClosed()) {
            try {
                Socket client = ss.accept();
                Thread t = new Thread(() -> handle(client), "arcane-multiplayer-conn");
                t.setDaemon(true);
                t.start();
            } catch (IOException e) {
                if (ss.isClosed()) return;
            }
        }
    }

    private static void handle(Socket client) {
        try (Socket c = client) {
            c.setTcpNoDelay(true);
            InputStream in = new BufferedInputStream(c.getInputStream());
            byte[] head = readHead(in);
            if (head == null) return;
            try {
                route(c, in, new String(head, StandardCharsets.ISO_8859_1));
            } catch (RuntimeException e) {  // an odd request or server answer: an error answer, never a crash
                respond(c, "502 Bad Gateway", "proxy error: " + e);
            }
        } catch (IOException ignored) {
            // a connection that broke: the game retries
        }
    }

    private static void route(Socket c, InputStream in, String headText) throws IOException {
        String[] request = headText.substring(0, Math.max(headText.indexOf("\r\n"), 0)).split(" ");
        String method = request.length > 1 ? request[0] : "", target = request.length > 1 ? request[1] : "";
        int query = target.indexOf('?');
        String path = query < 0 ? target : target.substring(0, query);
        ServerUrl up = upstream;
        LocalCdn.Kind kind = LocalCdn.kind(method, path);
        if (kind != LocalCdn.Kind.NONE) {
            serveLocal(c, kind, path, up);
            return;
        }
        if (up == null) {
            respond(c, "503 Service Unavailable", "No game server chosen: open the app from the launcher.");
            return;
        }
        if (method.equals("POST") && path.equals("/meta")) {
            forwardMeta(c, in, headText, up);
            return;
        }
        Socket server;
        try {
            server = up.connect(CONNECT_TIMEOUT_MS);
        } catch (IOException e) {
            respond(c, "502 Bad Gateway", "Cannot reach " + up + ": " + e.getMessage());
            return;
        }
        try (Socket s = server) {
            OutputStream toServer = s.getOutputStream();
            toServer.write(rewriteHead(headText, up.hostHeader).getBytes(StandardCharsets.ISO_8859_1));
            toServer.flush();
            Thread pump = new Thread(() -> {
                try {
                    copy(in, toServer);
                    if (s instanceof SSLSocket) s.close();  // TLS has no half-close
                    else s.shutdownOutput();
                } catch (IOException ignored) {
                    // the other direction ends the connection
                }
            }, "arcane-multiplayer-up");
            pump.setDaemon(true);
            pump.start();
            copy(s.getInputStream(), c.getOutputStream());
        }
    }

    // --- the client's files, from the imported content set ---

    private static void serveLocal(Socket c, LocalCdn.Kind kind, String path, ServerUrl up) throws IOException {
        LocalCdn content = cdn;
        if (content == null) {
            respond(c, "404 Not Found", "The game data is not imported: open the app from the launcher and import it.");
            return;
        }
        switch (kind) {
            case ASSET: {
                File f = content.asset(path);
                if (f == null) respond(c, "404 Not Found", "no such bundle in the imported game data");
                else sendFile(c, f, "application/octet-stream");
                return;
            }
            case LOCALIZATION: {
                File f = content.localization(path);
                if (f == null) respond(c, "404 Not Found", "no localization in the imported game data");
                else sendFile(c, f, "text/plain; charset=utf-8");
                return;
            }
            default: {
                byte[] body;
                try {
                    body = servedGamedata(content, LocalCdn.gamedataVersion(path), up);
                } catch (IOException | IllegalArgumentException e) {
                    respond(c, "502 Bad Gateway", "gamedata: " + e.getMessage());
                    return;
                }
                send(c, "200 OK", "application/octet-stream", body);
            }
        }
    }

    /** The imported gamedata with the chosen server's changes for `version` (cached per server and version). */
    static byte[] servedGamedata(LocalCdn content, String version, ServerUrl up) throws IOException {
        if (up == null) throw new IOException("no game server chosen");
        String key = up + " " + version;
        synchronized (gamedata) {
            byte[] hit = gamedata.get(key);
            if (hit != null) return hit;
        }
        byte[] original = Files.readBytes(content.gamedataFile());
        // the server builds the gamedata's own addresses (its News page) on the address the game reaches it at: this
        // proxy, which forwards them (the app reaches no other host)
        byte[] delta = fetch(up, "/gamedata/" + version + "/gamedata-static.delta", PUBLIC_BASE_HEADER + ": " + localBase);
        byte[] served = GamedataDelta.apply(original, delta);
        synchronized (gamedata) {
            gamedata.put(key, served);
            while (gamedata.size() > 4) gamedata.remove(gamedata.keySet().iterator().next());
        }
        return served;
    }

    /** The header that asks the server to build the delta's addresses on the proxy's loopback address. */
    static final String PUBLIC_BASE_HEADER = "X-Arcane-Public-Base";
    static final int READ_TIMEOUT_MS = 60000;
    static final int MAX_BODY = 64 << 20;

    /** GET `path` from the server (with the header lines `headers`): the body of a 200 answer, else IOException. */
    static byte[] fetch(ServerUrl up, String path, String... headers) throws IOException {
        try (Socket s = up.connect(CONNECT_TIMEOUT_MS)) {
            s.setSoTimeout(READ_TIMEOUT_MS);
            OutputStream out = s.getOutputStream();
            StringBuilder req = new StringBuilder("GET ").append(path).append(" HTTP/1.1\r\nHost: ").append(up.hostHeader)
                    .append("\r\nConnection: close\r\n");
            for (String h : headers) req.append(h).append("\r\n");
            out.write(req.append("\r\n").toString().getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
            Response r = readResponse(new BufferedInputStream(s.getInputStream()));
            if (r.status != 200) throw new IOException(up + path + " answered HTTP " + r.status);
            return r.body;
        }
    }

    /** POST /meta: forwarded, and its answer's download addresses pointed at this proxy. */
    private static void forwardMeta(Socket c, InputStream in, String headText, ServerUrl up) throws IOException {
        int length = contentLength(headText);
        if (length > MAX_BODY) throw new IOException("request too large");
        if (headerValue(headText, "expect").equalsIgnoreCase("100-continue")) {
            c.getOutputStream().write("HTTP/1.1 100 Continue\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
            c.getOutputStream().flush();
        }
        byte[] body = new byte[Math.max(length, 0)];
        int off = 0;
        while (off < body.length) {
            int n = in.read(body, off, body.length - off);
            if (n < 0) throw new IOException("request body cut short");
            off += n;
        }
        Response r;
        try (Socket s = up.connect(CONNECT_TIMEOUT_MS)) {
            s.setSoTimeout(READ_TIMEOUT_MS);
            OutputStream toServer = s.getOutputStream();
            // the answer is rewritten: no compressed answer, no 100-continue (the body is sent with the head)
            toServer.write(withoutHeaders(rewriteHead(headText, up.hostHeader), "accept-encoding", "expect")
                    .getBytes(StandardCharsets.ISO_8859_1));
            toServer.write(body);
            toServer.flush();
            r = readResponse(new BufferedInputStream(s.getInputStream()));
        } catch (IOException e) {
            respond(c, "502 Bad Gateway", "Cannot reach " + up + ": " + e.getMessage());
            return;
        }
        byte[] answer = r.body;
        if (r.status == 200) {
            try {
                answer = MetaRewrite.rewrite(r.body, localBase);
            } catch (IllegalArgumentException e) {
                respond(c, "502 Bad Gateway", "unreadable /meta answer: " + e.getMessage());
                return;
            }
        }
        send(c, r.status + " " + r.reason, r.contentType, answer);
    }

    static final class Response {
        int status;
        String reason = "";
        String contentType = "application/octet-stream";
        byte[] body;
    }

    /**
     * A whole HTTP/1.1 response of a "Connection: close" request (Content-Length, chunked, or up to EOF; interim 1xx
     * answers skipped); IOException for anything else, and for a body over MAX_BODY.
     */
    static Response readResponse(InputStream in) throws IOException {
        Response r = new Response();
        String text;
        do {
            byte[] head = readHead(in);
            if (head == null) throw new IOException("no answer");
            text = new String(head, StandardCharsets.ISO_8859_1);
            String[] status = text.split("\r\n", 2)[0].split(" ", 3);
            if (status.length < 2 || !status[0].startsWith("HTTP/")) throw new IOException("not an HTTP answer");
            try {
                r.status = Integer.parseInt(status[1].trim());
            } catch (NumberFormatException e) {
                throw new IOException("not an HTTP answer");
            }
            r.reason = status.length > 2 ? status[2] : "";
        } while (r.status >= 100 && r.status < 200);
        String[] lines = text.split("\r\n");
        boolean chunked = false;
        for (String line : lines) {
            String name = headerName(line);
            if (name.equals("content-type")) r.contentType = line.substring(line.indexOf(':') + 1).trim();
            if (name.equals("transfer-encoding") && line.toLowerCase(Locale.ROOT).contains("chunked")) chunked = true;
        }
        int length = contentLength(text);
        ByteArrayOutputStream body = new ByteArrayOutputStream(Math.max(length, 1024));
        if (chunked) {
            while (true) {
                String size = readLine(in).trim();
                int semi = size.indexOf(';');
                int n;
                try {
                    n = Integer.parseInt(semi < 0 ? size : size.substring(0, semi), 16);
                } catch (NumberFormatException e) {
                    throw new IOException("bad chunk size");
                }
                if (n < 0 || body.size() + (long) n > MAX_BODY) throw new IOException("answer too large");
                if (n == 0) break;
                copyExactly(in, body, n);
                readLine(in);
            }
        } else if (length >= 0) {
            if (length > MAX_BODY) throw new IOException("answer too large");
            copyExactly(in, body, length);
        } else {
            byte[] buf = new byte[32 * 1024];
            int n;
            while ((n = in.read(buf)) >= 0) {
                if (body.size() + n > MAX_BODY) throw new IOException("answer too large");
                body.write(buf, 0, n);
            }
        }
        r.body = body.toByteArray();
        return r;
    }

    private static int contentLength(String head) throws IOException {
        String v = headerValue(head, "content-length");
        if (v.isEmpty()) return -1;
        try {
            int n = Integer.parseInt(v);
            if (n < 0) throw new IOException("bad Content-Length");
            return n;
        } catch (NumberFormatException e) {
            throw new IOException("bad Content-Length");
        }
    }

    /** The value of header `name` (lower case) in a head, or "". */
    static String headerValue(String head, String name) {
        for (String line : head.split("\r\n")) {
            if (headerName(line).equals(name)) return line.substring(line.indexOf(':') + 1).trim();
        }
        return "";
    }

    /** `head` without the header lines named `names` (lower case). */
    static String withoutHeaders(String head, String... names) {
        StringBuilder out = new StringBuilder(head.length());
        outer:
        for (String line : head.split("\r\n", -1)) {
            for (String n : names) if (headerName(line).equals(n)) continue outer;
            out.append(line).append("\r\n");
        }
        return out.substring(0, out.length() - 2);
    }

    private static String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int b;
        while ((b = in.read()) >= 0 && b != '\n') if (b != '\r') sb.append((char) b);
        if (b < 0 && sb.length() == 0) throw new IOException("answer cut short");
        return sb.toString();
    }

    private static void copyExactly(InputStream in, OutputStream out, int n) throws IOException {
        byte[] buf = new byte[32 * 1024];
        while (n > 0) {
            int got = in.read(buf, 0, Math.min(buf.length, n));
            if (got < 0) throw new IOException("answer cut short");
            out.write(buf, 0, got);
            n -= got;
        }
    }

    private static void sendFile(Socket c, File f, String type) throws IOException {
        OutputStream out = c.getOutputStream();
        out.write(("HTTP/1.1 200 OK\r\nContent-Type: " + type + "\r\nContent-Length: " + f.length()
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        try (InputStream in = new FileInputStream(f)) {
            copy(in, out);
        }
        out.flush();
    }

    private static void send(Socket c, String status, String type, byte[] body) throws IOException {
        OutputStream out = c.getOutputStream();
        out.write(("HTTP/1.1 " + status + "\r\nContent-Type: " + type + "\r\nContent-Length: " + body.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    /** The request head up to and including the blank line, or null at EOF before one. */
    static byte[] readHead(InputStream in) throws IOException {
        ByteArrayOutputStream head = new ByteArrayOutputStream(1024);
        int last4 = 0;  // the last four bytes read
        while (true) {
            int b = in.read();
            if (b < 0) return null;
            head.write(b);
            last4 = (last4 << 8) | b;
            if (last4 == 0x0D0A0D0A) return head.toByteArray();
            if (head.size() > MAX_HEAD) throw new IOException("request head too large");
        }
    }

    /**
     * The request head for the server: Host = the chosen server's; a plain request also gets "Connection: close"
     * (one request per proxied connection), a websocket upgrade keeps its Connection / Upgrade headers.
     */
    public static String rewriteHead(String head, String host) {
        String[] lines = head.split("\r\n", -1);
        boolean upgrade = false;
        for (int i = 1; i < lines.length; i++) {
            if (headerName(lines[i]).equals("upgrade")) upgrade = true;
        }
        StringBuilder out = new StringBuilder(head.length() + 64).append(lines[0]).append("\r\n");
        out.append("Host: ").append(host).append("\r\n");
        for (int i = 1; i < lines.length; i++) {
            String name = headerName(lines[i]);
            if (lines[i].isEmpty() || name.equals("host")) continue;
            if (!upgrade && (name.equals("connection") || name.equals("keep-alive") || name.equals("proxy-connection"))) continue;
            out.append(lines[i]).append("\r\n");
        }
        if (!upgrade) out.append("Connection: close\r\n");
        return out.append("\r\n").toString();
    }

    private static String headerName(String line) {
        int colon = line.indexOf(':');
        return colon < 0 ? "" : line.substring(0, colon).trim().toLowerCase(Locale.ROOT);
    }

    private static void respond(Socket c, String status, String message) throws IOException {
        byte[] body = message.getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 " + status + "\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: "
                + body.length + "\r\nConnection: close\r\n\r\n";
        OutputStream out = c.getOutputStream();
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    private static void copy(InputStream in, OutputStream out) throws IOException {
        byte[] buf = new byte[32 * 1024];
        int n;
        while ((n = in.read(buf)) >= 0) {
            out.write(buf, 0, n);
            out.flush();
        }
    }

    /**
     * For tests on a desktop JVM: java arcane.multiplayer.LoopbackProxy PORT SERVER_URL [CONTENT_DIR] (prints "ready"
     * when listening; CONTENT_DIR: an imported content set, every present localization file counts as the release's).
     */
    public static void main(String[] args) throws Exception {
        setUpstream(ServerUrl.parse(args[1]));
        if (args.length > 2) setContent(new LocalCdn(new File(args[2]), null));
        start(Integer.parseInt(args[0]));
        System.out.println("ready");
        System.out.flush();
        Thread.currentThread().join();
    }
}
