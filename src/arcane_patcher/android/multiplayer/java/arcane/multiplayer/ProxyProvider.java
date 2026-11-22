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
import arcane.content.LocalCdn;

import android.app.Activity;
import android.app.Application;
import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Starts the loopback proxy (LoopbackProxy) towards the saved game server at every start of the game process:
 * providers are created before any activity, including when Android restores the game straight into its Unity
 * activity (skipping SetupActivity). Also holds the saved server address and knows whether the game runs in this
 * process (SetupActivity: another server needs a restart). It serves no data itself; the proxy serves the client's
 * files from the imported game data (useImported).
 */
public final class ProxyProvider extends ContentProvider {
    static final String TAG = "ArcaneMultiplayer";
    private static final String PREFS = "arcane_multiplayer";
    private static final String KEY_URL = "server_url";
    private static final AtomicInteger gameActivities = new AtomicInteger();

    @Override
    public boolean onCreate() {
        Context ctx = getContext();
        ServerUrl saved = saved(ctx);
        if (saved != null) LoopbackProxy.setUpstream(saved);
        if (DataImport.isImported(ctx)) useImported(ctx);
        if (ctx.getApplicationContext() instanceof Application) {
            ((Application) ctx.getApplicationContext()).registerActivityLifecycleCallbacks(new GameTracker());
        }
        try {
            LoopbackProxy.start(port(ctx));
        } catch (IOException e) {
            Log.e(TAG, "proxy not started", e);
        }
        return true;
    }

    /** The proxy answers the client's downloads from the imported game data. */
    static void useImported(Context ctx) {
        LoopbackProxy.setContent(new LocalCdn(DataImport.contentDir(ctx), DataImport.localizationSha256(ctx)));
    }

    /** True while the game's activity exists in this process (the engine is running, connected to the upstream). */
    static boolean gameRunning() {
        return gameActivities.get() > 0;
    }

    private static final class GameTracker implements Application.ActivityLifecycleCallbacks {
        private static boolean isGame(Activity a) {
            return a.getClass().getName().equals(SetupActivity.gameActivity(a));
        }

        @Override public void onActivityCreated(Activity a, Bundle b) { if (isGame(a)) gameActivities.incrementAndGet(); }
        @Override public void onActivityDestroyed(Activity a) { if (isGame(a)) gameActivities.decrementAndGet(); }
        @Override public void onActivityStarted(Activity a) {}
        @Override public void onActivityResumed(Activity a) {}
        @Override public void onActivityPaused(Activity a) {}
        @Override public void onActivityStopped(Activity a) {}
        @Override public void onActivitySaveInstanceState(Activity a, Bundle b) {}
    }

    /** The game server the player chose last (null before the first choice). */
    static ServerUrl saved(Context ctx) {
        String s = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, null);
        if (s == null) return null;
        try {
            return ServerUrl.parse(s);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    static void save(Context ctx, ServerUrl url) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_URL, url.toString()).commit();
    }

    /** The loopback port the game's bootstrap_config points at (written by the patcher for the multiplayer variant). */
    static int port(Context ctx) {
        try (InputStream in = ctx.getAssets().open("arcane_multiplayer/port")) {
            byte[] b = new byte[16];
            int n = in.read(b);
            return Integer.parseInt(new String(b, 0, Math.max(n, 0), StandardCharsets.US_ASCII).trim());
        } catch (IOException | NumberFormatException e) {
            throw new IllegalStateException("assets/arcane_multiplayer/port missing: not built by the patcher's multiplayer variant", e);
        }
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
