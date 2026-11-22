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

import android.app.Activity;
import android.app.Application;
import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;

/**
 * Created at every start of the game process, before any activity. It makes the setup screen come first: when the
 * game activity is created in a process whose setup screen did not open it (Android restored the task straight into
 * the game, e.g. from recents after the process was killed), the app restarts into the setup screen
 * (RestartActivity), so the speed and account are chosen at every cold start. It also keeps the old fallback: the
 * server starts shortly unless the setup screen holds the start (OfflineServer.startSoon). It serves no data.
 */
public final class ServerProvider extends ContentProvider {
    @Override
    public boolean onCreate() {
        Application app = (Application) getContext().getApplicationContext();
        app.registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
            @Override
            public void onActivityCreated(Activity activity, Bundle state) {
                if (SetupActivity.gameActivity(activity).equals(activity.getClass().getName()) && !OfflineServer.gameLaunched()) {
                    Log.i(OfflineServer.TAG, "game activity restored without the setup screen: restarting into it");
                    RestartActivity.restartToSetup(activity);
                }
            }

            @Override
            public void onActivityStarted(Activity activity) {}

            @Override
            public void onActivityResumed(Activity activity) {}

            @Override
            public void onActivityPaused(Activity activity) {}

            @Override
            public void onActivityStopped(Activity activity) {}

            @Override
            public void onActivitySaveInstanceState(Activity activity, Bundle out) {}

            @Override
            public void onActivityDestroyed(Activity activity) {}
        });
        OfflineServer.startSoon(getContext());
        return true;
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
