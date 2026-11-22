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

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Process;

/**
 * Restarts the app into the setup screen, which then connects to the saved server and opens the game. Runs in its
 * own process (manifest: android:process=":arcane_restart"): the game engine cannot be shut down and started again
 * inside one process, and connections the running game holds stay on the server it started with. Used when the player
 * picks another server (a scanned code or a deep link) while the game runs.
 */
public final class RestartActivity extends Activity {
    private static final String EXTRA_PID = "arcane.multiplayer.pid";

    /** From the main process: hand over to the restart process and die. Does not return. */
    static void restartAndPlay(Context context) {
        Context ctx = context.getApplicationContext();
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
        setup.putExtra(SetupActivity.EXTRA_PLAY, true);
        startActivity(setup);
        finish();
        Process.killProcess(Process.myPid());
    }
}
