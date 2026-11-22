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
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Process;

/**
 * Restarts the app into the setup screen. Runs in its own process (manifest: android:process=":arcane_restart"): the
 * main process asks for it and kills itself (the game engine cannot be shut down and started again inside one
 * process); this activity makes sure the old process is gone, opens SetupActivity in a fresh task (a new main
 * process) and ends its own process.
 *
 * Used when the game activity comes up without the setup screen in front of it (Android restoring the task from
 * recents after the process died) and when the player changes speed / account while the game runs (SettingsActivity).
 */
public final class RestartActivity extends Activity {
    private static final String EXTRA_PID = "arcane.offline.pid";

    /** From the main process: stop the server, hand over to the restart process and die. Does not return. */
    static void restartToSetup(Context context) {
        Context ctx = context.getApplicationContext();
        OfflineServer.stop(5_000);  // a clean shutdown of the server (its database) before the process dies
        Intent i = new Intent(ctx, RestartActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        i.putExtra(EXTRA_PID, Process.myPid());
        ctx.startActivity(i);
        Process.killProcess(Process.myPid());
        System.exit(0);
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        int pid = getIntent().getIntExtra(EXTRA_PID, -1);
        if (pid > 0 && pid != Process.myPid()) Process.killProcess(pid);
        Intent setup = new Intent(this, SetupActivity.class);
        setup.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        startActivity(setup);
        finish();
        Process.killProcess(Process.myPid());
    }
}
