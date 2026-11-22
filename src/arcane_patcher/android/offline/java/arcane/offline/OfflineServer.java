/*
 * SPDX-License-Identifier: GPL-3.0-only WITH AdditionRef-arcane-client-linking
 * Copyright (C) the arcane-patcher contributors
 *
 * This file is part of arcane-patcher: free software under the GNU General Public License version 3 only, with the
 * additional permission (GPL section 7) in LICENSES/AdditionRef-arcane-client-linking.txt: this Program, or any
 * covered work, may be linked or combined with the game client it is applied to. That permission grants no rights to
 * the game client itself, which belongs to its owners; this project does not distribute it.
 */
package arcane.offline;

import arcane.content.DataImport;
import android.content.Context;
import android.content.res.AssetManager;
import android.util.Log;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/**
 * The game server embedded in the offline build: `python -m arcane_server` as a child process on 127.0.0.1. It is the
 * same server as the hosted one; only its settings (ARCANE_HOST, ARCANE_PORT, ARCANE_DATA_DIR, ARCANE_CLIENT, ARCANE_DB, ARCANE_GAME_SPEED, ARCANE_CLUB_WAR_CLUBS, ARCANE_POPULATION_GROWTH,
 * ARCANE_ACCOUNT_PICKER, ARCANE_CLIENT_PACKAGE) differ.
 *
 * Game speed (the server's game clock, clock.py: everything time-based runs N times faster): chosen on the setup screen before every launch and kept in the
 * app's preferences. The setup screen starts the server at the saved speed right away (it lists the accounts to pick
 * from); the server rebases its clock when started with another speed, and a server already running at another
 * speed is restarted (applySpeed; isReady waits for the restarted one).
 *
 * Layout (app-internal files dir unless noted):
 *   arcane_offline/root/      Python home + the server's src/ tree, extracted from assets/arcane_offline/payload.zip per build
 *   arcane_offline/arcane.sqlite3
 *   (external files dir)/logs/server.log
 *   (external files dir)/arcane_data/   imported game data (assets, gamedata, localization), see DataImport
 *
 * Android kills the app's whole process group when the game process dies, so the server lives exactly as long
 * as the game. A watcher thread restarts the server if it exits while the game is running.
 */
public final class OfflineServer {
    public static final String TAG = "ArcaneOffline";
    /** Written by the patcher for the offline variant (the game's bootstrap_config). */
    static final String PORT_ASSET = "arcane_offline/port";

    private static volatile int port;

    static final String PREFS = "arcane_offline";
    static final String SPEED_KEY = "game_speed";
    static final int MIN_SPEED = 1, MAX_SPEED = 7;
    /** Clubs per Club Conquest war (ARCANE_CLUB_WAR_CLUBS, config.Toggles.club_war_clubs): 2 (the default) or 3. */
    static final String WAR_CLUBS_KEY = "club_war_clubs";
    static final int[] WAR_CLUBS = {2, 3};
    /** The inbox lists only mail with a reward (ARCANE_INBOX_REWARD_MAIL_ONLY, config.Toggles.inbox_reward_mail_only). */
    static final String REWARD_MAIL_ONLY_KEY = "inbox_reward_mail_only";
    /** Whom the server-played players' strength follows (ARCANE_POPULATION_GROWTH, config.Toggles.population_growth):
     *  "current" (the account played now, the default), "strongest" (the strongest account of the last 7 days) or
     *  "off" (they keep their strength). */
    static final String POPULATION_KEY = "population_growth";
    static final String[] POPULATION_MODES = {"current", "strongest", "off"};
    /** ServerProvider's start waits this long for the setup screen to take over (it then starts after the choice). */
    static final long PROVIDER_START_DELAY_MS = 1500;

    private static Thread watcher;
    private static volatile String status = "Not started";
    private static volatile Process proc;
    private static volatile int runningSpeed;
    private static volatile int runningWarClubs;
    private static volatile boolean runningRewardMailOnly;
    private static volatile String runningPopulation;
    private static volatile boolean restartNow;
    private static volatile boolean held;
    private static volatile boolean gameLaunched;
    private static volatile boolean stopping;

    private OfflineServer() {}

    public static String status() {
        return status;
    }

    static void setStatus(String s) {
        status = s;
        Log.i(TAG, s);
    }

    static File baseDir(Context ctx) {
        return new File(ctx.getFilesDir(), "arcane_offline");
    }

    /** In the external files dir (Android/data/<package>/files/logs) so it can be copied off the phone. */
    static File logFile(Context ctx) {
        File ext = ctx.getExternalFilesDir("logs");
        return new File(ext != null ? ext : baseDir(ctx), "server.log");
    }

    /** The chosen game speed (1..7, default 1). */
    public static int speed(Context ctx) {
        int s = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt(SPEED_KEY, MIN_SPEED);
        return Math.max(MIN_SPEED, Math.min(MAX_SPEED, s));
    }

    /** The chosen clubs per Club Conquest war (2 or 3, default 2). */
    public static int warClubs(Context ctx) {
        int n = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt(WAR_CLUBS_KEY, WAR_CLUBS[0]);
        return n == 3 ? 3 : 2;
    }

    public static void setWarClubs(Context ctx, int n) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putInt(WAR_CLUBS_KEY, n == 3 ? 3 : 2).apply();
    }

    /** Whether the inbox lists only mail with a reward (default: all mail, as the original). */
    public static boolean rewardMailOnly(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(REWARD_MAIL_ONLY_KEY, false);
    }

    public static void setRewardMailOnly(Context ctx, boolean on) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(REWARD_MAIL_ONLY_KEY, on).apply();
    }

    /** The chosen population mode (one of POPULATION_MODES, default "current"). */
    public static String population(Context ctx) {
        String m = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(POPULATION_KEY, POPULATION_MODES[0]);
        for (String known : POPULATION_MODES) if (known.equals(m)) return known;
        return POPULATION_MODES[0];
    }

    public static void setPopulation(Context ctx, String mode) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(POPULATION_KEY, mode).apply();
    }

    /** True if the running server has the chosen settings (speed, Club Conquest size, inbox, population). */
    private static boolean runningAsChosen(Context ctx) {
        return runningSpeed == speed(ctx) && runningWarClubs == warClubs(ctx)
                && runningRewardMailOnly == rewardMailOnly(ctx) && population(ctx).equals(runningPopulation);
    }

    public static void setSpeed(Context ctx, int speed) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                .putInt(SPEED_KEY, Math.max(MIN_SPEED, Math.min(MAX_SPEED, speed))).commit();
    }

    static final String RESTART_ACCOUNT_KEY = "restart_account";

    /** Settings mode: the account to start with after the restart (a public id or "new"); commit: the process dies next. */
    static void setRestartAccount(Context ctx, String id) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(RESTART_ACCOUNT_KEY, id).commit();
    }

    /** The account a settings-mode restart asked for (then forgotten), or null after an ordinary start. */
    static String takeRestartAccount(Context ctx) {
        android.content.SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String id = p.getString(RESTART_ACCOUNT_KEY, null);
        if (id != null) p.edit().remove(RESTART_ACCOUNT_KEY).commit();
        return id;
    }

    /** The setup screen will start the server after the speed choice: cancel ServerProvider's pending start. */
    public static void hold() {
        held = true;
    }

    /** The setup screen opened the game in this process (ServerProvider restarts into the setup screen otherwise). */
    static boolean gameLaunched() {
        return gameLaunched;
    }

    static void setGameLaunched() {
        gameLaunched = true;
    }

    /** Stop the server for good (the app is about to restart): SIGTERM, wait up to `waitMs` for it to exit. */
    static void stop(long waitMs) {
        stopping = true;
        Process p = proc;
        if (p == null) return;
        p.destroy();
        long deadline = System.currentTimeMillis() + waitMs;
        while (System.currentTimeMillis() < deadline) {
            try {
                p.exitValue();
                return;
            } catch (IllegalThreadStateException running) {
                sleep(100);
            }
        }
    }

    /** ServerProvider: start soon unless the setup screen holds the start (the game restored straight into Unity). */
    public static void startSoon(Context context) {
        final Context ctx = context.getApplicationContext();
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
            if (!held) start(ctx);
        }, PROVIDER_START_DELAY_MS);
    }

    /** Start the server at the chosen speed, or restart it if it is running at another speed. */
    public static synchronized void applySpeed(Context context) {
        Context ctx = context.getApplicationContext();
        start(ctx);
        Process p = proc;
        if (p != null && !runningAsChosen(ctx)) {
            setStatus("Restarting the server at " + speed(ctx) + "x, " + warClubs(ctx) + "-club Conquest"
                    + (rewardMailOnly(ctx) ? ", reward mail only" : "") + ", other players: " + population(ctx));
            restartNow = true;
            p.destroy();
        }
    }

    /** Start the server (idempotent). Does nothing until the game data has been imported. */
    public static synchronized void start(Context context) {
        final Context ctx = context.getApplicationContext();
        port(ctx);
        if (watcher != null && watcher.isAlive()) return;
        if (!DataImport.isImported(ctx)) {
            setStatus("Game data not imported");
            return;
        }
        watcher = new Thread(() -> watch(ctx), "arcane-offline-server");
        watcher.setDaemon(true);
        watcher.start();
    }

    private static void watch(Context ctx) {
        try {
            ensurePayload(ctx);
        } catch (IOException e) {
            setStatus("Could not unpack the server: " + e);
            return;
        }
        long backoffMs = 1000;
        while (!stopping) {
            if (isUp()) {  // e.g. a server left over from a previous game process that is still shutting down
                sleep(3000);
                continue;
            }
            long started = System.currentTimeMillis();
            try {
                setStatus("Starting server");
                Process p = launch(ctx);
                proc = p;
                int code = p.waitFor();
                setStatus("Server exited with code " + code + " (see " + logFile(ctx) + ")");
            } catch (IOException | InterruptedException e) {
                setStatus("Server failed to start: " + e);
            }
            proc = null;
            if (stopping) return;
            if (restartNow) {  // applySpeed: back up right away at the new speed
                restartNow = false;
                backoffMs = 1000;
                sleep(200);
                continue;
            }
            backoffMs = System.currentTimeMillis() - started > 60_000 ? 1000 : Math.min(backoffMs * 2, 30_000);
            sleep(backoffMs);
        }
    }

    private static Process launch(Context ctx) throws IOException {
        File root = new File(baseDir(ctx), "root");
        String libDir = ctx.getApplicationInfo().nativeLibraryDir;
        ProcessBuilder pb = new ProcessBuilder(new File(libDir, "libarcanepython.so").getPath(), "-m", "arcane_server");
        Map<String, String> env = pb.environment();
        env.put("LD_LIBRARY_PATH", libDir);
        env.put("PYTHONHOME", new File(root, "python").getPath());
        env.put("PYTHONPATH", new File(root, "app/src").getPath());
        env.put("PYTHONUNBUFFERED", "1");
        env.put("PYTHONDONTWRITEBYTECODE", "1");
        env.put("HOME", baseDir(ctx).getPath());
        env.put("TMPDIR", ctx.getCacheDir().getPath());
        // the payload's protobuf has its upb C extension (the patcher's offline runtime); the server refuses to start
        // if the backend asked for here is not the one that loaded (startup.runtime_problems)
        env.put("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "upb");
        env.put("ARCANE_HOST", "127.0.0.1");
        env.put("ARCANE_PORT", String.valueOf(port(ctx)));
        env.put("ARCANE_DATA_DIR", DataImport.dataDir(ctx).getPath());
        env.put("ARCANE_CLIENT", DataImport.client(ctx));
        env.put("ARCANE_DB", new File(baseDir(ctx), "arcane.sqlite3").getPath());
        runningSpeed = speed(ctx);
        env.put("ARCANE_GAME_SPEED", String.valueOf(runningSpeed));
        runningWarClubs = warClubs(ctx);
        env.put("ARCANE_CLUB_WAR_CLUBS", String.valueOf(runningWarClubs));  // from the next war on (a war keeps its own)
        runningRewardMailOnly = rewardMailOnly(ctx);
        env.put("ARCANE_INBOX_REWARD_MAIL_ONLY", runningRewardMailOnly ? "1" : "0");
        runningPopulation = population(ctx);
        env.put("ARCANE_POPULATION_GROWTH", runningPopulation);
        // the setup screen's account picker (/offline/accounts)
        env.put("ARCANE_CLIENT_PACKAGE", ctx.getPackageName());  // the News page's links open this app
        env.put("ARCANE_ACCOUNT_PICKER", "1");
        pb.directory(baseDir(ctx));
        pb.redirectErrorStream(true);
        File log = logFile(ctx);
        if (log.length() > 4 << 20) {
            File old = new File(log.getParentFile(), "server.log.1");
            old.delete();
            log.renameTo(old);
        }
        pb.redirectOutput(ProcessBuilder.Redirect.appendTo(log));
        return pb.start();
    }

    static int port(Context ctx) {
        if (port == 0) {
            try {
                port = Integer.parseInt(readAll(ctx.getAssets().open(PORT_ASSET)).trim());
            } catch (IOException | NumberFormatException e) {
                throw new IllegalStateException("missing asset " + PORT_ASSET, e);
            }
        }
        return port;
    }

    /** True if the server answers and runs at the chosen speed (not the old server of a pending speed restart). */
    public static boolean isReady(Context ctx) {
        return !restartNow && proc != null && runningAsChosen(ctx) && isUp();
    }

    /** True if the server answers on 127.0.0.1. */
    public static boolean isUp() {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL("http://127.0.0.1:" + port + "/status/maintenance.json").openConnection();
            c.setConnectTimeout(1000);
            c.setReadTimeout(2000);
            return c.getResponseCode() == 200;
        } catch (IOException e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /**
     * A request to the server's account picker (/offline/...). Returns the response body; an error status throws
     * IOException with the server's {"error"} text.
     */
    static byte[] request(String method, String path, byte[] body, String contentType) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL("http://127.0.0.1:" + port + path).openConnection();
        try {
            c.setConnectTimeout(2000);
            c.setReadTimeout(60_000);
            c.setRequestMethod(method);
            if (body != null) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", contentType);
                c.setFixedLengthStreamingMode(body.length);
                try (OutputStream o = c.getOutputStream()) {
                    o.write(body);
                }
            }
            int code = c.getResponseCode();
            InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
            byte[] out = in != null ? readBytes(in) : new byte[0];
            if (code >= 400) {
                String msg = new String(out, StandardCharsets.UTF_8);
                try {
                    msg = new org.json.JSONObject(msg).optString("error", msg);
                } catch (org.json.JSONException ignored) {
                }
                throw new IOException(msg.isEmpty() ? "HTTP " + code : msg);
            }
            return out;
        } finally {
            c.disconnect();
        }
    }

    static byte[] readBytes(InputStream in) throws IOException {
        try (InputStream i = in) {
            java.io.ByteArrayOutputStream b = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = i.read(buf)) > 0) b.write(buf, 0, n);
            return b.toByteArray();
        }
    }

    /** Last lines of the server log, for the setup screen. */
    public static String logTail(Context ctx, int maxBytes) {
        File f = logFile(ctx);
        if (!f.exists()) return "";
        try (RandomAccessFile r = new RandomAccessFile(f, "r")) {
            long start = Math.max(0, r.length() - maxBytes);
            byte[] buf = new byte[(int) (r.length() - start)];
            r.seek(start);
            r.readFully(buf);
            return new String(buf, StandardCharsets.UTF_8);
        } catch (IOException e) {
            return "";
        }
    }

    /** Unpack assets/arcane_offline/payload.zip unless this build's payload is already unpacked. */
    static void ensurePayload(Context ctx) throws IOException {
        AssetManager am = ctx.getAssets();
        String buildId = readAll(am.open("arcane_offline/build_id")).trim();
        File base = baseDir(ctx);
        File root = new File(base, "root");
        File stamp = new File(base, "payload_build_id");
        if (root.isDirectory() && stamp.exists() && readAll(new FileInputStream(stamp)).trim().equals(buildId)) return;
        setStatus("Unpacking the server (first start of this build)");
        File tmp = new File(base, "root.tmp");
        deleteTree(tmp);
        tmp.mkdirs();
        unzip(am.open("arcane_offline/payload.zip"), tmp, null);
        deleteTree(root);
        if (!tmp.renameTo(root)) throw new IOException("rename " + tmp + " -> " + root);
        try (OutputStream o = new FileOutputStream(stamp)) {
            o.write(buildId.getBytes(StandardCharsets.UTF_8));
        }
    }

    static void unzip(InputStream in, File dest, arcane.content.Files.Progress progress) throws IOException {
        arcane.content.Files.unzip(in, dest, progress);
    }

    static void deleteTree(File f) {
        arcane.content.Files.deleteTree(f);
    }

    static String readAll(InputStream in) throws IOException {
        return arcane.content.Files.readAll(in);
    }

    static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }
}
