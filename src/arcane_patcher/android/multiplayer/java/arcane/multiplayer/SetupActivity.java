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

import arcane.content.DataImport;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.ConnectException;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;

/**
 * Launcher of the multiplayer variant. The first time it asks for the game data zip (the player's own copy of the
 * game's files, DataImport): the game's downloads are answered on the phone from it (LoopbackProxy), no server hands
 * them out. Then the player enters the game server's address (e.g. http://203.0.113.5:18080) by typing it,
 * by scanning the QR code of the server's join page (ScanActivity), or by opening its deep link
 * arcane://connect?url=... (a phone's camera app scanning that code; JoinLink). Whichever way, the address is
 * checked the same way (GET /status/maintenance.json, directly and through the loopback proxy the game uses), saved,
 * and the game opens. Every launch shows the saved address, ready to play or to change.
 * Another server while the game runs restarts the app (RestartActivity), which then connects and plays.
 */
public final class SetupActivity extends Activity {
    /** Meta-data the patcher writes into the manifest: the game's own launcher activity (read from the input APK). */
    static final String GAME_ACTIVITY_META = "arcane.game_activity";
    private static volatile String gameActivity;

    /** The game's activity class, from the manifest's meta-data GAME_ACTIVITY_META. */
    static String gameActivity(android.content.Context ctx) {
        String name = gameActivity;
        if (name == null) {
            try {
                android.os.Bundle meta = ctx.getPackageManager().getApplicationInfo(ctx.getPackageName(),
                        android.content.pm.PackageManager.GET_META_DATA).metaData;
                name = meta == null ? null : meta.getString(GAME_ACTIVITY_META);
            } catch (android.content.pm.PackageManager.NameNotFoundException e) {
                name = null;
            }
            if (name == null) throw new IllegalStateException("manifest meta-data " + GAME_ACTIVITY_META + " missing");
            gameActivity = name;
        }
        return name;
    }
    static final String EXTRA_PLAY = "arcane.multiplayer.play";  // connect to the saved server right away (after a restart)
    private static final int TIMEOUT_MS = 10000;
    private static final int REQ_CAMERA = 1, REQ_SCAN = 2, REQ_DATA = 3;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private EditText address;
    private Button play, scan, data;
    private ProgressBar progress;
    private boolean importing;
    private TextView status;
    private LinearLayout column;
    private boolean busy, launched;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Intent i = getIntent();
        if (!isTaskRoot() && i != null && Intent.ACTION_MAIN.equals(i.getAction()) && i.hasCategory(Intent.CATEGORY_LAUNCHER)) {
            finish();  // the game is already running in this task
            return;
        }
        buildUi();
        ServerUrl saved = ProxyProvider.saved(this);
        if (!DataImport.isImported(this)) {
            showNeedData(null);
        } else if (saved != null) {
            address.setText(saved.toString());
            say("Last server: " + saved + "\nTap Play, or enter or scan another address to change server.", false);
        } else {
            say("Enter the address of the game server you play on, as its host gave it to you (host name or IP and "
                    + "port, e.g. http://203.0.113.5:18080), or scan the QR code of its join page.", false);
        }
        if (i != null && Intent.ACTION_VIEW.equals(i.getAction()) && i.getDataString() != null) {
            fromLink(i.getDataString());
        } else if (i != null && i.getBooleanExtra(EXTRA_PLAY, false) && saved != null && state == null
                && DataImport.isImported(this)) {
            connect();
        }
        if (LoopbackProxy.error() != null) say(LoopbackProxy.error(), true);
    }

    /** A deep link (a phone's camera app or a browser opened the server's join link): fill in its address. */
    private void fromLink(String link) {
        try {
            ServerUrl url = JoinLink.parseDeepLink(link);
            address.setText(url.toString());
            say("Server from the link: " + url + "\nTap Play to connect.", false);
        } catch (IllegalArgumentException e) {
            say(e.getMessage(), true);
        }
    }

    // --- layout ---

    private void buildUi() {
        int dp16 = Style.dp(this, 16);
        ImageView icon = new ImageView(this);
        icon.setImageDrawable(getApplicationInfo().loadIcon(getPackageManager()));
        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.setPadding(Style.dp(this, 14), 0, 0, 0);
        titles.addView(Style.title(this, getApplicationInfo().loadLabel(getPackageManager()).toString()));
        TextView subtitle = Style.text(this, 13, Style.LAVENDER);
        subtitle.setText("Choose a game server");
        titles.addView(subtitle);
        LinearLayout header = new LinearLayout(this);
        header.setGravity(Gravity.CENTER_VERTICAL);
        header.setPadding(0, Style.dp(this, 8), 0, Style.dp(this, 24));
        header.addView(icon, new LinearLayout.LayoutParams(Style.dp(this, 52), Style.dp(this, 52)));
        header.addView(titles);

        TextView label = Style.text(this, 16, Style.PARCHMENT);
        label.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        label.setText("Game server address");
        label.setPadding(0, 0, 0, Style.dp(this, 8));
        address = new EditText(this);
        address.setSingleLine(true);
        address.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        address.setImeOptions(EditorInfo.IME_ACTION_GO | EditorInfo.IME_FLAG_NO_EXTRACT_UI);  // no full-screen editor in landscape
        address.setHint("http://host:port");
        address.setTextSize(17);
        address.setTypeface(Typeface.MONOSPACE);
        address.setTextColor(Style.PARCHMENT);
        address.setHintTextColor(Style.alpha(Style.LAVENDER, 0.7f));
        address.setBackground(Style.shape(this, Style.alpha(Style.INK, 0.7f), 12, Style.alpha(Style.LAVENDER, 0.5f), 1));
        address.setPadding(dp16, Style.dp(this, 14), dp16, Style.dp(this, 14));
        address.setOnEditorActionListener((v, action, ev) -> {
            if (action == EditorInfo.IME_ACTION_GO || (ev != null && ev.getKeyCode() == KeyEvent.KEYCODE_ENTER)) {
                connect();
                return true;
            }
            return false;
        });
        play = Style.primary(this, "Play");
        play.setOnClickListener(v -> connect());
        scan = Style.secondary(this, "Scan QR code");
        scan.setOnClickListener(v -> scan());
        data = Style.primary(this, "Choose the game data zip");
        data.setOnClickListener(v -> chooseZip());
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setVisibility(android.view.View.GONE);
        status = Style.text(this, 15, Style.PARCHMENT);
        status.setPadding(0, dp16, 0, 0);

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(Style.dp(this, 20), Style.dp(this, 20), Style.dp(this, 20), Style.dp(this, 20));
        card.setBackground(Style.shape(this, Style.alpha(Style.VIOLET, 0.92f), 20, Style.alpha(Style.GOLD, 0.45f), 1));
        LinearLayout.LayoutParams dataAt = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Style.dp(this, 56));
        dataAt.bottomMargin = Style.dp(this, 20);
        card.addView(data, dataAt);
        card.addView(progress, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        data.setVisibility(DataImport.isImported(this) ? android.view.View.GONE : android.view.View.VISIBLE);
        card.addView(label);
        card.addView(address, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams playAt = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Style.dp(this, 56));
        playAt.topMargin = Style.dp(this, 20);
        card.addView(play, playAt);
        LinearLayout.LayoutParams scanAt = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, Style.dp(this, 56));
        scanAt.topMargin = Style.dp(this, 12);
        card.addView(scan, scanAt);
        card.addView(status);

        TextView footer = Style.text(this, 13, Style.LAVENDER);
        footer.setText("The server's host can show you its join page (the server address followed by /join) with a QR "
                + "code to scan.");
        footer.setPadding(Style.dp(this, 4), Style.dp(this, 20), Style.dp(this, 4), 0);

        column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(Style.dp(this, 24), Style.dp(this, 24), Style.dp(this, 24), Style.dp(this, 24));
        column.addView(header);
        column.addView(card);
        column.addView(footer);
        FrameLayout centre = new FrameLayout(this);
        FrameLayout.LayoutParams columnAt = new FrameLayout.LayoutParams(
                Math.min(getResources().getDisplayMetrics().widthPixels, Style.dp(this, 560)),
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        centre.addView(column, columnAt);
        TextView build = Style.text(this, 11, Style.alpha(Style.LAVENDER, 0.6f));
        build.setText("build " + versionCode());  // the build's versionCode (the patcher's --version-code)
        build.setPadding(0, 0, Style.dp(this, 10), Style.dp(this, 6));
        centre.addView(build, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM | Gravity.END));
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(centre);
        Style.background(this, scroll);
        setContentView(scroll);
    }

    @Override
    public void onConfigurationChanged(android.content.res.Configuration c) {
        super.onConfigurationChanged(c);  // the manifest keeps the activity on rotation: re-centre the column
        if (column == null) return;
        ViewGroup.LayoutParams lp = column.getLayoutParams();
        lp.width = Math.min(getResources().getDisplayMetrics().widthPixels, Style.dp(this, 560));
        column.setLayoutParams(lp);
    }

    @SuppressWarnings("deprecation")  // getLongVersionCode is API 28+; the code fits an int
    private long versionCode() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
        } catch (PackageManager.NameNotFoundException e) {
            return 0;
        }
    }

    private void say(String message, boolean problem) {
        status.setTextColor(problem ? Style.ROSE : Style.PARCHMENT);
        status.setText(message);
    }

    // --- scanning ---

    /** "Scan QR code": the camera permission is asked for now (not at install), then the scanner opens. */
    private void scan() {
        if (busy || launched) return;
        if (!getPackageManager().hasSystemFeature(PackageManager.FEATURE_CAMERA_ANY)) {
            say("This device has no camera. Type the server's address, or open its join link (arcane://...) on "
                    + "this device.", true);
            return;
        }
        if (Build.VERSION.SDK_INT >= 23 && checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        startActivityForResult(new Intent(this, ScanActivity.class), REQ_SCAN);
    }

    @Override
    public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        if (request != REQ_CAMERA) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
            startActivityForResult(new Intent(this, ScanActivity.class), REQ_SCAN);
        } else if (Build.VERSION.SDK_INT >= 23 && !shouldShowRequestPermissionRationale(Manifest.permission.CAMERA)) {
            say("Camera access is turned off for this app. Type the server's address instead, or allow the camera "
                    + "in Android Settings > Apps > (this app) > Permissions.", true);
        } else {
            say("Without the camera this app cannot scan. Type the server's address instead.", true);
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == REQ_DATA) {
            if (result == RESULT_OK && data != null && data.getData() != null) importZip(data.getData());
            return;
        }
        if (request != REQ_SCAN) return;
        if (result == RESULT_OK && data != null && data.getStringExtra(ScanActivity.EXTRA_URL) != null) {
            address.setText(data.getStringExtra(ScanActivity.EXTRA_URL));
            connect();  // checked like a typed address
        } else if (data != null && data.getStringExtra(ScanActivity.EXTRA_ERROR) != null) {
            say(data.getStringExtra(ScanActivity.EXTRA_ERROR), true);
        }
    }

    // --- the game data zip ---

    private void showNeedData(String error) {
        data.setVisibility(android.view.View.VISIBLE);
        say((error != null ? "The game data could not be imported: " + error + "\n\n" : "")
                + "The first time, the game needs its data file, the game data zip (about 1.6 GB), made from your own "
                + "copy of the game (see the patcher's README). Copy it to this phone (for example to Downloads), then "
                + "choose it here. It is imported once; the zip can be deleted afterwards.", error != null);
    }

    private void chooseZip() {
        if (importing) return;
        Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType("*/*");
        pick.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"application/zip", "application/x-zip-compressed", "application/octet-stream"});
        startActivityForResult(pick, REQ_DATA);
    }

    private void importZip(Uri uri) {
        importing = true;
        data.setEnabled(false);
        play.setEnabled(false);
        progress.setVisibility(android.view.View.VISIBLE);
        say("Importing the game data…", false);
        final android.content.Context app = getApplicationContext();
        new Thread(() -> {
            String error = null;
            try {
                DataImport.run(app, uri, (done, total, entry) -> {
                    if (total > 0) ui.post(() -> {
                        progress.setProgress((int) (done * 1000 / total));
                        say("Importing the game data… " + (done * 100 / total) + "%", false);
                    });
                });
            } catch (java.io.IOException | RuntimeException e) {
                android.util.Log.w(ProxyProvider.TAG, "data import failed", e);
                error = e.getMessage() != null ? e.getMessage() : e.toString();
            }
            final String err = error;
            ui.post(() -> {
                importing = false;
                data.setEnabled(true);
                play.setEnabled(true);
                progress.setVisibility(android.view.View.GONE);
                if (isFinishing()) return;
                if (err != null) {
                    showNeedData(err);
                    return;
                }
                ProxyProvider.useImported(this);
                data.setVisibility(android.view.View.GONE);
                say("Game data imported. Enter the game server's address and tap Play.", false);
            });
        }, "arcane-multiplayer-import").start();
    }

    // --- connecting ---

    private void connect() {
        if (busy || launched || importing) return;
        if (!DataImport.isImported(this)) {
            showNeedData(null);
            return;
        }
        final ServerUrl url;
        try {
            url = ServerUrl.parse(address.getText().toString());
        } catch (IllegalArgumentException e) {
            say(e.getMessage(), true);
            return;
        }
        final int port = ProxyProvider.port(this);
        if (url.isLoopback() && url.port == port) {
            say("Port " + port + " on this phone is the multiplayer app's own; enter the game server's address.", true);
            return;
        }
        if (LoopbackProxy.error() != null) {
            say(LoopbackProxy.error(), true);
            return;
        }
        ServerUrl current = LoopbackProxy.upstream();
        // the running game keeps its connections to the server it started with: another server needs a restart
        final boolean restart = ProxyProvider.gameRunning() && (current == null || !current.toString().equals(url.toString()));
        busy = true;
        play.setEnabled(false);
        scan.setEnabled(false);
        say("Connecting to " + url + "…", false);
        new Thread(() -> {
            String error = checkDirect(url);
            if (error == null) error = checkRelease(url);
            if (error == null && !restart) {
                LoopbackProxy.setUpstream(url);
                error = check("http://127.0.0.1:" + port, url);  // the path the game takes
            }
            final String err = error;
            ui.post(() -> {
                busy = false;
                play.setEnabled(true);
                scan.setEnabled(true);
                if (isFinishing()) return;
                if (err != null) {
                    say(err, true);
                    return;
                }
                ProxyProvider.save(this, url);
                address.setText(url.toString());
                if (restart) {
                    say("Switching to " + url + "…", false);
                    RestartActivity.restartAndPlay(this);
                } else {
                    launchGame();
                }
            });
        }, "arcane-multiplayer-check").start();
    }

    /**
     * null if the server answers /status/maintenance.json as a game server, else a message for the player. Asked over
     * the proxy's own connection (ServerUrl.connect): the app's network security config lets the client reach only
     * this phone.
     */
    private static String checkDirect(ServerUrl url) {
        try {
            return maintenance(new String(LoopbackProxy.fetch(url, "/status/maintenance.json"), StandardCharsets.UTF_8), url);
        } catch (UnknownHostException e) {
            return "Unknown host: " + url.host + ". Check the address.";
        } catch (ConnectException e) {
            return "Cannot connect to " + url + " (refused or unreachable). Is the server running and its port open?";
        } catch (SocketTimeoutException e) {
            return "No answer from " + url + " (timed out). Check the address and that the port is open.";
        } catch (IOException e) {
            String m = String.valueOf(e.getMessage());
            if (m.contains(" answered HTTP ")) return url + " answered HTTP " + m.substring(m.lastIndexOf(' ') + 1)
                    + ": not a game server (or the wrong port).";
            return "Cannot reach " + url + ": " + m;
        }
    }

    /**
     * null if the server runs the same client release as the imported game data (its gamedata changes are made for
     * this gamedata), else a message. A server without the delta route is not checked.
     */
    private String checkRelease(ServerUrl url) {
        byte[] delta;
        try {
            delta = LoopbackProxy.fetch(url, "/gamedata/current/gamedata-static.delta");
        } catch (IOException e) {
            return null;  // an older server, or none of its business here: the game reports what fails
        }
        try {
            java.io.File gamedata = new java.io.File(new java.io.File(DataImport.contentDir(this), "gamedata"),
                    "gamedata-static.bin");
            arcane.content.GamedataDelta.apply(arcane.content.Files.readBytes(gamedata), delta);
            return null;
        } catch (IllegalArgumentException e) {
            return url + " runs a different client release than this app's game data (" + e.getMessage() + "). Use "
                    + "the app and game data zip of that server's release.";
        } catch (IOException e) {
            return "The imported game data cannot be read: " + e.getMessage();
        }
    }

    private static String maintenance(String body, ServerUrl url) {
        if (!body.contains("\"UnderMaintenance\"")) return url + " is not a game server (or the wrong port).";
        if (body.replace(" ", "").contains("\"UnderMaintenance\":true")) return url + " is under maintenance; try again later.";
        return null;
    }

    /** null if `base` answers /status/maintenance.json as a game server, else a message for the player. */
    private static String check(String base, ServerUrl url) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/status/maintenance.json").openConnection();
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setUseCaches(false);
            int code = c.getResponseCode();
            if (code != 200) return url + " answered HTTP " + code + ": not a game server (or the wrong port).";
            return maintenance(read(c.getInputStream()), url);
        } catch (UnknownHostException e) {
            return "Unknown host: " + url.host + ". Check the address.";
        } catch (ConnectException e) {
            return "Cannot connect to " + url + " (refused or unreachable). Is the server running and its port open?";
        } catch (SocketTimeoutException e) {
            return "No answer from " + url + " (timed out). Check the address and that the port is open.";
        } catch (IOException e) {
            return "Cannot reach " + url + ": " + e.getMessage();
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(InputStream in) throws IOException {
        try (InputStream s = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = s.read(buf)) >= 0 && out.size() < 65536) out.write(buf, 0, n);
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    private void launchGame() {
        if (launched) return;
        launched = true;
        Intent game = new Intent();
        game.setClassName(getPackageName(), gameActivity(this));
        game.setAction(Intent.ACTION_MAIN);
        startActivity(game);
        finish();
    }
}
