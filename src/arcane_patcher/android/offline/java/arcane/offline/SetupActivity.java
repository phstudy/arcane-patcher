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
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.database.Cursor;
import android.content.res.ColorStateList;
import android.content.res.Configuration;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.OpenableColumns;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.function.IntConsumer;

/**
 * Launcher of the offline build. First run: asks for the game data zip and imports it. Every cold start (ServerProvider
 * restarts into this screen when Android restores the game directly): starts the embedded server at the saved game
 * speed and shows the choices: the game speed (1x-7x, the server's game clock; remembered) and the account to play
 * (a club, then one of its members, or a new player; the current account is preselected; export / import an account
 * file). Start binds the picked account to this device (server side, account_picker.py), restarts the server if the
 * speed changed, waits until it answers, then opens the game and finishes.
 *
 * Settings mode: opened while the game runs (SettingsActivity: Android's app info settings entry, the launcher's
 * long-press shortcut). Start with another speed or account saves the choice and restarts the app
 * (RestartActivity); the fresh setup screen then starts at once with it. Back returns to the running game.
 */
public class SetupActivity extends Activity {
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
    private static final int PICK_ZIP = 1, EXPORT_ACCOUNT = 2, IMPORT_ACCOUNT = 3;
    private static final int MAX_IMPORT_BYTES = 64 << 20;
    private static final String NEW = "new";
    static final String SETTINGS_ACTION = "arcane.offline.action.SETTINGS";
    private static final String SHORTCUT_ID = "settings";
    private static volatile boolean importing;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView title, status, accountHeader, accountStatus, playerLabel, hint;
    private MagicLoadingView loading;
    private LinearLayout loadingBox;
    private Button retry;
    private long waitDeadline;
    private int hintIndex;
    /** Lines under the loading animation, one after another. */
    private static final String[] HINTS = {"Brewing potions…", "Polishing the arena…", "Summoning heroes…",
            "Sharpening swords…", "Charging the spell orbs…", "Waking the villains…", "Shuffling the spell tokens…",
            "Counting gold coins…", "Drawing the magic circle…", "Feeding the dragons…"};
    /** How long the loading screen waits before it offers Retry (the first start of a new version takes longest). */
    private static final long WAIT_MS = 4 * 60_000;
    private String nowPlaying = "";  // first line of accountStatus; notices (export, import) go below it
    private ProgressBar progress;
    private Button pick, start;
    private TextView exportButton, importButton;
    private Spinner clubSpinner, playerSpinner;
    private LinearLayout accountBox;
    private boolean launched, starting;
    private boolean settingsMode;  // the game runs in this process: changes restart the app
    private Button back;

    /** The server's listing: clubs (id "" = accounts in no club), each with its members. */
    private final List<Club> clubs = new ArrayList<>();
    private String currentId = NEW;  // the account the device plays now (NEW: a fresh one at the next login)
    private boolean listed;
    private String exportId;
    private String preferredId = NEW;
    /** importAccount's playerId for "every player of the file" (a whole club). */
    static final String WHOLE_CLUB = "*";
    private long importModified = -1;  // last-modified time (epoch s) of the file being imported  // the member the player spinner shows when its club is (re)selected

    static final class Member {
        final String id, name, clubName;
        final int level;

        Member(JSONObject o) {
            id = o.optString("id");
            name = o.optString("name", id);
            clubName = o.optString("club_name");
            level = o.optInt("level");
        }

        @Override
        public String toString() {
            return name + ", level " + level;
        }
    }

    static final class Club {
        final String id, name;
        final List<Member> members = new ArrayList<>();

        Club(String id, String name) {
            this.id = id;
            this.name = name;
        }

        @Override
        public String toString() {
            return (id.isEmpty() ? "No club" : name) + " (" + members.size() + ")";
        }
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Intent i = getIntent();
        if (!isTaskRoot() && i != null && Intent.ACTION_MAIN.equals(i.getAction()) && i.hasCategory(Intent.CATEGORY_LAUNCHER)) {
            finish();  // the game is already running in this task
            return;
        }
        settingsMode = OfflineServer.gameLaunched();
        if (!settingsMode) OfflineServer.hold();  // the setup screen starts the server itself (ServerProvider's start is cancelled)
        publishShortcut();
        buildUi();
        if (importing) {
            showImporting();
        } else if (DataImport.isImported(this)) {
            showChoices();
        } else {
            showNeedData(null);
        }
    }

    // --- layout ---------------------------------------------------------------------------------------------------
    //
    // One designed screen: midnight ink background, a storybook serif title in starlight gold over a faint still
    // copy of the loading animation's magic circle, game speed as round pills on the background, the account in the
    // one card (deep violet, gold hairline), Start as the boldest thing (a gold pill pinned at the bottom). Pressing
    // Start grows the faint circle into the full loading animation. Landscape / tablets: two columns, content at most
    // 720dp wide. Views are built once; arrange() places them for the current orientation.

    static final int INK = Color.rgb(0x14, 0x14, 0x33), INK_GLOW = Color.rgb(0x1E, 0x1B, 0x4B);
    static final int VIOLET = Color.rgb(0x2A, 0x1F, 0x5C), GOLD = Color.rgb(0xE8, 0xB8, 0x4A);
    static final int PARCHMENT = Color.rgb(0xF3, 0xEC, 0xDA), LAVENDER = Color.rgb(0xA9, 0x9B, 0xD6);
    static final int ROSE = Color.rgb(0xE0, 0x6A, 0x86);

    private FrameLayout root;
    private LinearLayout header, speedSection, choices, startBar;
    private TextView build;  // "build <versionCode>": in the flow under Start (or the loading animation), never over it
    private MagicLoadingView headerCircle;
    private TextView speedHelp;
    private final List<TextView> speedPills = new ArrayList<>();
    private int selectedSpeed;
    private final List<TextView> warPills = new ArrayList<>(), mailPills = new ArrayList<>(),
            populationPills = new ArrayList<>();
    private TextView warHelp, mailHelp, populationHelp;
    private String selectedPopulation;
    private int selectedWarClubs;
    private boolean selectedRewardMailOnly;
    private LinearLayout optionsSection;
    private boolean loadingShown;

    private int dp(float v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private static int alpha(int color, float a) {
        return Color.argb(Math.round(255 * a), Color.red(color), Color.green(color), Color.blue(color));
    }

    private GradientDrawable shape(int fill, int radiusDp, int strokeColor, int strokeDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(radiusDp));
        if (strokeDp > 0) g.setStroke(dp(strokeDp), strokeColor);
        return g;
    }

    /** A pressed / focused state everyone can see: a gold ripple over `content`. */
    private Drawable touchable(Drawable content, Drawable mask) {
        return new RippleDrawable(ColorStateList.valueOf(alpha(GOLD, 0.35f)), content, mask);
    }

    private TextView text(float sp, int color) {
        TextView t = new TextView(this);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setLineSpacing(0, 1.15f);
        return t;
    }

    private TextView heading(String s) {
        TextView t = text(18, PARCHMENT);
        t.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        t.setText(s);
        t.setPadding(0, dp(20), 0, dp(10));
        return t;
    }

    private Button primaryButton(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(18);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setTextColor(new ColorStateList(new int[][] {{-android.R.attr.state_enabled}, {}},
                new int[] {alpha(INK, 0.5f), INK}));
        b.setMinHeight(dp(56));
        b.setStateListAnimator(null);
        GradientDrawable fill = shape(GOLD, 28, 0, 0);
        b.setBackground(touchable(fill, shape(Color.WHITE, 28, 0, 0)));
        return b;
    }

    private TextView quietButton(String label) {
        TextView b = text(15, GOLD);
        b.setTextColor(new ColorStateList(new int[][] {{-android.R.attr.state_enabled}, {}},
                new int[] {alpha(LAVENDER, 0.4f), GOLD}));
        b.setText(label);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(dp(48));
        b.setPadding(dp(16), 0, dp(16), 0);
        b.setClickable(true);
        b.setFocusable(true);
        b.setBackground(touchable(null, shape(Color.WHITE, 24, 0, 0)));
        return b;
    }

    private void buildUi() {
        title = text(30, GOLD);
        title.setTypeface(Typeface.create(Typeface.SERIF, Typeface.BOLD));
        title.setLetterSpacing(0.02f);
        title.setText(getApplicationInfo().loadLabel(getPackageManager()));
        TextView subtitle = text(13, LAVENDER);
        subtitle.setText("Game setup");
        ImageView icon = new ImageView(this);
        icon.setImageDrawable(getApplicationInfo().loadIcon(getPackageManager()));
        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        titles.setPadding(dp(14), 0, 0, 0);
        titles.addView(title);
        titles.addView(subtitle);
        LinearLayout headerRow = new LinearLayout(this);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);
        headerRow.addView(icon, new LinearLayout.LayoutParams(dp(48), dp(48)));
        headerRow.addView(titles);
        headerCircle = new MagicLoadingView(this);
        headerCircle.setStill(true);
        headerCircle.setAlpha(0.11f);
        FrameLayout headerFrame = new FrameLayout(this);
        FrameLayout.LayoutParams circleAt = new FrameLayout.LayoutParams(dp(200), dp(200), Gravity.END | Gravity.CENTER_VERTICAL);
        circleAt.rightMargin = -dp(40);
        headerFrame.addView(headerCircle, circleAt);
        FrameLayout.LayoutParams rowAt = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER_VERTICAL);
        headerFrame.addView(headerRow, rowAt);
        headerFrame.setMinimumHeight(dp(120));
        header = new LinearLayout(this);
        header.setOrientation(LinearLayout.VERTICAL);
        header.addView(headerFrame);

        status = text(15, PARCHMENT);
        status.setPadding(0, dp(4), 0, dp(8));
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setProgressTintList(ColorStateList.valueOf(GOLD));
        progress.setProgressBackgroundTintList(ColorStateList.valueOf(alpha(LAVENDER, 0.4f)));
        progress.setVisibility(View.GONE);
        pick = primaryButton("Choose the game data zip");
        pick.setOnClickListener(v -> chooseZip());
        pick.setVisibility(View.GONE);
        header.addView(status);
        header.addView(progress);
        header.addView(pick, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        buildSpeedSection();
        buildAccountBox();
        start = primaryButton("Start");
        start.setOnClickListener(v -> onStartPressed());
        back = new Button(this);  // settings mode only: a quiet text button under Start
        back.setText("Back to the game");
        back.setAllCaps(false);
        back.setTextColor(GOLD);
        back.setTextSize(15);
        back.setStateListAnimator(null);
        back.setBackground(touchable(null, shape(Color.WHITE, 24, 0, 0)));
        back.setOnClickListener(v -> backToGame());
        back.setVisibility(View.GONE);
        startBar = new LinearLayout(this);  // Start and Back, placed by arrange()
        startBar.setOrientation(LinearLayout.VERTICAL);
        startBar.setPadding(0, dp(12), 0, dp(12));
        startBar.setVisibility(View.GONE);
        build = text(12, LAVENDER);  // the build, for bug reports
        build.setText(buildLabel());
        build.setAlpha(0.8f);
        build.setGravity(Gravity.END);
        build.setPadding(0, dp(4), 0, 0);
        buildLoadingBox();

        root = new FrameLayout(this);
        GradientDrawable bg = new GradientDrawable();
        bg.setGradientType(GradientDrawable.RADIAL_GRADIENT);
        bg.setColors(new int[] {INK_GLOW, INK});
        bg.setGradientCenter(0.5f, 0f);
        bg.setGradientRadius(getResources().getDisplayMetrics().heightPixels * 0.8f);
        root.setBackground(bg);
        // where the system draws its bars over the app (edge-to-edge: Android 15+ for apps targeting it, some
        // vendors' gesture navigation), keep the screen inside them: nothing under the gesture bar or the camera cutout
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            if (android.os.Build.VERSION.SDK_INT >= 30) {
                int types = android.view.WindowInsets.Type.systemBars() | android.view.WindowInsets.Type.displayCutout();
                android.graphics.Insets in = insets.getInsets(types);
                v.setPadding(in.left, in.top, in.right, in.bottom);
            }
            return insets;
        });
        getWindow().setStatusBarColor(INK);
        getWindow().setNavigationBarColor(INK);
        arrange();
        setContentView(root);
    }

    @Override
    public void onConfigurationChanged(Configuration c) {
        super.onConfigurationChanged(c);  // the manifest keeps the activity on rotation: lay the views out again
        if (root != null) arrange();
    }

    private static void detach(View v) {
        if (v.getParent() instanceof ViewGroup) ((ViewGroup) v.getParent()).removeView(v);
    }

    private ScrollView scroller(View content) {
        ScrollView s = new ScrollView(this);
        s.setFillViewport(true);
        s.setClipToPadding(false);
        s.addView(content);
        return s;
    }

    /** Place the sections: portrait one column (Start pinned at the bottom), landscape / wide two columns. */
    private void arrange() {
        for (View v : new View[] {header, speedSection, optionsSection, accountBox, startBar, loadingBox, build, start,
                back}) detach(v);
        startBar.removeAllViews();
        root.removeAllViews();
        android.util.DisplayMetrics m = getResources().getDisplayMetrics();
        float wDp = m.widthPixels / m.density;
        boolean wide = getResources().getConfiguration().orientation == Configuration.ORIENTATION_LANDSCAPE || wDp >= 600;
        int gutter = dp(wDp < 400 ? 16 : 24);
        int side = Math.max(gutter, (int) ((m.widthPixels - dp(wide ? 960 : 720)) / 2));  // max content width
        LinearLayout.LayoutParams fill = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        // landscape phones are short: a smaller header circle keeps the speed explanation above Start
        boolean shortScreen = m.heightPixels / m.density < 480;
        FrameLayout.LayoutParams circle = (FrameLayout.LayoutParams) headerCircle.getLayoutParams();
        circle.width = circle.height = dp(shortScreen ? 120 : 200);
        circle.rightMargin = shortScreen ? 0 : -dp(40);
        ((View) headerCircle.getParent()).setMinimumHeight(dp(shortScreen ? 88 : 120));
        headerCircle.setLayoutParams(circle);
        if (loadingShown) {  // the wait: the animation and its hint line centred on the screen, the header above
            LinearLayout col = new LinearLayout(this);
            col.setOrientation(LinearLayout.VERTICAL);
            col.setGravity(Gravity.CENTER_HORIZONTAL);
            col.addView(header, fill);
            col.addView(loadingBox, fill);
            col.addView(build, fill);
            col.setPadding(side, 0, side, dp(16));
            int circleDp = (int) Math.max(120, Math.min(220, m.heightPixels / m.density - 230));  // all of it on screen
            LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) loading.getLayoutParams();
            lp.width = lp.height = dp(circleDp);
            loading.setLayoutParams(lp);
            ScrollView sc = scroller(col);
            root.addView(sc);
            stackStartAndBack();  // the bar is not shown while loading
        } else if (!wide) {
            LinearLayout col = new LinearLayout(this);
            col.setOrientation(LinearLayout.VERTICAL);
            col.addView(header, fill);
            col.addView(speedSection, fill);
            col.addView(optionsSection, fill);
            LinearLayout.LayoutParams card = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT);
            card.topMargin = dp(24);
            col.addView(accountBox, card);
            col.addView(loadingBox, fill);
            col.setPadding(side, 0, side, dp(16));
            LinearLayout page = new LinearLayout(this);
            page.setOrientation(LinearLayout.VERTICAL);
            page.addView(scroller(col), new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            startBar.setPadding(side, dp(10), side, dp(14));
            stackStartAndBack();
            page.addView(startBar, fill);
            root.addView(page);
        } else {
            LinearLayout main = new LinearLayout(this);
            main.setOrientation(LinearLayout.VERTICAL);
            main.addView(header, fill);
            main.addView(speedSection, fill);
            main.addView(loadingBox, fill);
            LinearLayout mainCol = new LinearLayout(this);
            mainCol.setOrientation(LinearLayout.VERTICAL);
            mainCol.addView(scroller(main), new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            startBar.setPadding(0, dp(8), 0, dp(12));
            // Back beside Start, not under it: a landscape phone's column is short, and a second row of buttons
            // would cover the speed pills in settings mode
            LinearLayout buttons = new LinearLayout(this);
            buttons.setGravity(Gravity.CENTER_VERTICAL);
            LinearLayout.LayoutParams backAt = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(48));
            backAt.rightMargin = dp(8);
            buttons.addView(back, backAt);
            buttons.addView(start, new LinearLayout.LayoutParams(0, dp(56), 1));
            startBar.addView(buttons, fill);
            mainCol.addView(startBar, fill);
            LinearLayout accountCol = new LinearLayout(this);
            accountCol.setOrientation(LinearLayout.VERTICAL);
            accountCol.setPadding(0, dp(24), 0, dp(16));
            accountCol.addView(accountBox, fill);
            accountCol.addView(optionsSection, fill);
            accountCol.addView(build, fill);  // here, not under Start: the right column has no height to spare
            LinearLayout cols = new LinearLayout(this);
            cols.setPadding(side, 0, side, 0);
            LinearLayout.LayoutParams half = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1);
            // account and Club Conquest on the left; header, speed and Start on the right, under the thumb of most
            // players (right-handed)
            cols.addView(scroller(accountCol), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1));
            View gap = new View(this);
            cols.addView(gap, new LinearLayout.LayoutParams(dp(24), 1));
            cols.addView(mainCol, half);
            root.addView(cols);
        }
        if (!loadingShown && !wide) startBar.addView(build, fill);  // under Start (and Back), right-aligned
    }

    /** Start with Back under it (portrait, and the loading screen, where the bar is hidden). */
    private void stackStartAndBack() {
        startBar.addView(start, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
        startBar.addView(back, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)));
    }

    /** "build <versionCode>" of this install. */
    private String buildLabel() {
        try {
            android.content.pm.PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            long code = android.os.Build.VERSION.SDK_INT >= 28 ? pi.getLongVersionCode() : pi.versionCode;
            return "build " + code;
        } catch (android.content.pm.PackageManager.NameNotFoundException e) {
            return "";
        }
    }

    private void buildSpeedSection() {
        speedSection = new LinearLayout(this);
        speedSection.setOrientation(LinearLayout.VERTICAL);
        speedSection.setVisibility(View.GONE);
        speedSection.addView(heading("Game speed"));
        LinearLayout row = new LinearLayout(this);
        selectedSpeed = OfflineServer.speed(this);
        for (int sp = OfflineServer.MIN_SPEED; sp <= OfflineServer.MAX_SPEED; sp++) {
            final int value = sp;
            FrameLayout cell = new FrameLayout(this);  // the touch target: a 1/7 slice of the row, 48dp high
            TextView pill = text(15, LAVENDER);
            pill.setText(sp + "×");
            pill.setGravity(Gravity.CENTER);
            pill.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
            pill.setContentDescription(sp == 1 ? "Normal speed" : sp + " times faster");
            cell.addView(pill, new FrameLayout.LayoutParams(dp(44), dp(44), Gravity.CENTER));
            cell.setClickable(true);
            cell.setFocusable(true);
            GradientDrawable mask = new GradientDrawable();
            mask.setShape(GradientDrawable.OVAL);
            mask.setColor(Color.WHITE);
            cell.setForeground(new RippleDrawable(ColorStateList.valueOf(alpha(GOLD, 0.35f)), null, mask));
            cell.setOnClickListener(v -> selectSpeed(value));
            speedPills.add(pill);
            row.addView(cell, new LinearLayout.LayoutParams(0, dp(48), 1));
        }
        speedSection.addView(row);
        speedHelp = text(15, LAVENDER);
        speedHelp.setPadding(0, dp(10), 0, 0);
        speedSection.addView(speedHelp);
        selectSpeed(selectedSpeed);
        // The other settings, under the speed in portrait and under the account card in landscape (the speed / Start
        // column has no room): the Club Conquest format (ARCANE_CLUB_WAR_CLUBS), the inbox
        // (ARCANE_INBOX_REWARD_MAIL_ONLY) and the other players' strength (ARCANE_POPULATION_GROWTH), each a row of
        // pill buttons in the speed's style.
        optionsSection = new LinearLayout(this);
        optionsSection.setOrientation(LinearLayout.VERTICAL);
        optionsSection.setVisibility(View.GONE);
        selectedWarClubs = OfflineServer.warClubs(this);
        warHelp = choiceRow("Club Conquest", new String[] {"2 clubs (1v1)", "3 clubs"}, warPills,
                i -> selectWarClubs(OfflineServer.WAR_CLUBS[i]));
        selectWarClubs(selectedWarClubs);
        selectedRewardMailOnly = OfflineServer.rewardMailOnly(this);
        mailHelp = choiceRow("Inbox", new String[] {"All mail", "Rewards only"}, mailPills,
                i -> selectRewardMailOnly(i == 1));
        selectRewardMailOnly(selectedRewardMailOnly);
        selectedPopulation = OfflineServer.population(this);
        populationHelp = choiceRow("Other players", new String[] {"Around me", "Around my best", "Fixed"},
                populationPills, i -> selectPopulation(OfflineServer.POPULATION_MODES[i]));
        selectPopulation(selectedPopulation);
    }

    /** A heading, a row of pill buttons (added to `pills`; a tap calls `onPick` with the pill's index) and a help
     *  line under them (returned), appended to the options section. */
    private TextView choiceRow(String title, String[] labels, List<TextView> pills, IntConsumer onPick) {
        TextView heading = heading(title);
        heading.setPadding(0, dp(18), 0, dp(6));
        optionsSection.addView(heading);
        LinearLayout row = new LinearLayout(this);
        for (int i = 0; i < labels.length; i++) {
            final int index = i;
            TextView pill = text(15, LAVENDER);
            pill.setText(labels[i]);
            pill.setGravity(Gravity.CENTER);
            pill.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
            pill.setPadding(dp(16), 0, dp(16), 0);
            pill.setClickable(true);
            pill.setFocusable(true);
            pill.setOnClickListener(v -> onPick.accept(index));
            pills.add(pill);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, dp(48));
            lp.setMargins(0, 0, dp(10), 0);
            row.addView(pill, lp);
        }
        optionsSection.addView(row);
        TextView help = text(15, LAVENDER);
        help.setPadding(0, dp(10), 0, 0);
        optionsSection.addView(help);
        return help;
    }

    /** Gold for the selected pill, an outline for the others. */
    private void markPills(List<TextView> pills, int selected) {
        for (int i = 0; i < pills.size(); i++) {
            TextView p = pills.get(i);
            boolean on = i == selected;
            GradientDrawable g = new GradientDrawable();
            g.setCornerRadius(dp(24));
            g.setColor(on ? GOLD : Color.TRANSPARENT);
            if (!on) g.setStroke(dp(1.5f), alpha(LAVENDER, 0.6f));
            p.setBackground(g);
            p.setTextColor(on ? INK : LAVENDER);
            p.setSelected(on);
        }
    }

    private void selectWarClubs(int n) {
        selectedWarClubs = n;
        markPills(warPills, n == OfflineServer.WAR_CLUBS[0] ? 0 : 1);
        warHelp.setText(n == 2 ? "Two clubs face off, as in the game's later years. Applies from the next war."
                : "Three clubs share one bigger map, the older format. Applies from the next war.");
    }

    private void selectRewardMailOnly(boolean on) {
        selectedRewardMailOnly = on;
        markPills(mailPills, on ? 1 : 0);
        mailHelp.setText(on ? "The inbox shows only mail with a reward to claim. Other mail is kept and comes back "
                + "with All mail." : "Every mail, as in the original game: rewards, news and notices.");
    }

    private void selectPopulation(String mode) {
        selectedPopulation = mode;
        int i = java.util.Arrays.asList(OfflineServer.POPULATION_MODES).indexOf(mode);
        markPills(populationPills, i);
        populationHelp.setText(i == 0
                ? "Club members and opponents stay around the account you play: you are in the middle. They grow "
                        + "with you each day, and drop at once for a new or weaker account."
                : i == 1 ? "They stay around your strongest account of the last 7 days, whichever account you play."
                : "They keep the strength they have now.");
    }

    private void selectSpeed(int speed) {
        selectedSpeed = speed;
        for (int i = 0; i < speedPills.size(); i++) {
            TextView p = speedPills.get(i);
            boolean on = i + OfflineServer.MIN_SPEED == speed;
            GradientDrawable g = new GradientDrawable();
            g.setShape(GradientDrawable.OVAL);
            g.setColor(on ? GOLD : Color.TRANSPARENT);
            if (!on) g.setStroke(dp(1.5f), alpha(LAVENDER, 0.6f));
            p.setBackground(g);
            p.setTextColor(on ? INK : LAVENDER);
            ((View) p.getParent()).setSelected(on);
        }
        speedHelp.setText(speed == 1 ? "Time runs as in the original game."
                : "One real day is " + speed + " game days. Events, energy and timers all run " + speed
                + " times faster. Changing it later keeps every timer's remaining time.");
    }

    /** The wait before the game opens: the magic animation, a rotating hint line, Try again after a failure. */
    private void buildLoadingBox() {
        loadingBox = new LinearLayout(this);
        loadingBox.setOrientation(LinearLayout.VERTICAL);
        loadingBox.setGravity(Gravity.CENTER_HORIZONTAL);
        loadingBox.setPadding(0, dp(8), 0, dp(8));
        loadingBox.setVisibility(View.GONE);
        loading = new MagicLoadingView(this);
        hint = text(17, GOLD);
        hint.setGravity(Gravity.CENTER_HORIZONTAL);
        hint.setPadding(0, dp(14), 0, dp(14));
        retry = primaryButton("Try again");
        retry.setVisibility(View.GONE);
        retry.setOnClickListener(v -> {
            retry.setVisibility(View.GONE);
            loading.setVisibility(View.VISIBLE);
            hint.setVisibility(View.VISIBLE);
            status.setTextColor(PARCHMENT);
            status.setText("Getting the arena ready…");
            OfflineServer.applySpeed(this);
            waitDeadline = System.currentTimeMillis() + WAIT_MS;
            poll();
        });
        LinearLayout.LayoutParams center = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        center.gravity = Gravity.CENTER_HORIZONTAL;
        loadingBox.addView(loading, center);
        loadingBox.addView(hint, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        loadingBox.addView(retry, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
    }

    private final Runnable nextHint = new Runnable() {
        @Override
        public void run() {
            hint.setText(HINTS[hintIndex++ % HINTS.length]);
            ui.postDelayed(this, 2600);
        }
    };

    /** Start pressed: the choices give way and the header's faint circle grows into the loading animation. */
    private void showLoading() {
        if (loadingShown) return;
        loadingShown = true;
        speedSection.setVisibility(View.GONE);
        optionsSection.setVisibility(View.GONE);
        accountBox.setVisibility(View.GONE);
        startBar.setVisibility(View.GONE);
        pick.setVisibility(View.GONE);
        progress.setVisibility(View.GONE);
        status.setTextColor(PARCHMENT);
        status.setText("Getting the arena ready…");
        loadingBox.setVisibility(View.VISIBLE);
        headerCircle.setVisibility(View.INVISIBLE);  // it becomes the big one
        arrange();
        if (MagicLoadingView.motionAllowed(this)) {
            loading.setAlpha(0.11f);
            loading.setScaleX(0.55f);
            loading.setScaleY(0.55f);
            loading.animate().alpha(1f).scaleX(1f).scaleY(1f).setDuration(650)
                    .setInterpolator(new android.view.animation.DecelerateInterpolator()).start();
        }
        ui.removeCallbacks(nextHint);
        nextHint.run();
    }

    private void buildAccountBox() {
        accountBox = new LinearLayout(this);
        accountBox.setOrientation(LinearLayout.VERTICAL);
        accountBox.setPadding(dp(20), dp(4), dp(20), dp(8));
        accountBox.setBackground(shape(VIOLET, 20, alpha(GOLD, 0.25f), 1));
        accountBox.setVisibility(View.GONE);
        LinearLayout.LayoutParams top = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT);
        accountBox.setLayoutParams(top);
        accountHeader = heading("Who's playing");
        accountStatus = text(15, PARCHMENT);
        accountStatus.setPadding(0, 0, 0, dp(6));
        clubSpinner = field();
        playerSpinner = field();
        clubSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                showMembers(position, null);
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {}
        });
        LinearLayout buttons = new LinearLayout(this);
        buttons.setGravity(Gravity.END);
        buttons.setPadding(0, dp(8), 0, 0);
        exportButton = quietButton("Export…");
        exportButton.setOnClickListener(v -> chooseExportFile());
        importButton = quietButton("Import…");
        importButton.setOnClickListener(v -> chooseImportFile());
        buttons.addView(exportButton);
        buttons.addView(importButton);
        accountBox.addView(accountHeader);
        accountBox.addView(accountStatus);
        accountBox.addView(label("Club"));
        accountBox.addView(clubSpinner, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)));
        playerLabel = label("Player");
        accountBox.addView(playerLabel);
        accountBox.addView(playerSpinner, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)));
        accountBox.addView(buttons, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));
        setAccountControlsEnabled(false);
    }

    /** A dropdown drawn as an outlined field: lavender outline, parchment text, a dark popup with readable rows. */
    private Spinner field() {
        Spinner s = new Spinner(this, Spinner.MODE_DROPDOWN);
        s.setBackground(touchable(shape(Color.TRANSPARENT, 12, alpha(LAVENDER, 0.4f), 1), shape(Color.WHITE, 12, 0, 0)));
        s.setPadding(dp(14), 0, dp(14), 0);
        s.setPopupBackgroundDrawable(shape(VIOLET, 12, alpha(GOLD, 0.25f), 1));
        s.setDropDownVerticalOffset(dp(54));
        return s;
    }

    /** The closed dropdown's down chevron, in lavender. */
    private Drawable chevron() {
        final int size = dp(16);
        final android.graphics.Paint p = new android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG);
        p.setColor(LAVENDER);
        p.setStyle(android.graphics.Paint.Style.STROKE);
        p.setStrokeWidth(dp(2));
        p.setStrokeCap(android.graphics.Paint.Cap.ROUND);
        Drawable d = new Drawable() {
            @Override
            public void draw(android.graphics.Canvas c) {
                android.graphics.Rect b = getBounds();
                float cx = b.exactCenterX(), cy = b.exactCenterY(), w = b.width() * 0.3f;
                c.drawLine(cx - w, cy - w / 2, cx, cy + w / 2, p);
                c.drawLine(cx, cy + w / 2, cx + w, cy - w / 2, p);
            }

            @Override
            public void setAlpha(int a) {}

            @Override
            public void setColorFilter(android.graphics.ColorFilter f) {}

            @Override
            public int getOpacity() {
                return android.graphics.PixelFormat.TRANSLUCENT;
            }

            @Override
            public int getIntrinsicWidth() {
                return size;
            }

            @Override
            public int getIntrinsicHeight() {
                return size;
            }
        };
        return d;
    }

    private TextView label(String s) {
        TextView t = text(13, LAVENDER);
        t.setPadding(0, dp(12), 0, dp(6));
        t.setText(s);
        return t;
    }

    /** Dropdown rows: parchment on the dark field and popup, a chevron on the closed field. */
    private <T> ArrayAdapter<T> adapter(List<T> items) {
        ArrayAdapter<T> a = new ArrayAdapter<T>(this, android.R.layout.simple_spinner_item, items) {
            @Override
            public View getView(int position, View convertView, ViewGroup parent) {
                TextView v = (TextView) super.getView(position, convertView, parent);
                v.setTextColor(PARCHMENT);
                v.setTextSize(16);
                v.setSingleLine(true);
                v.setEllipsize(android.text.TextUtils.TruncateAt.END);
                v.setPadding(0, 0, 0, 0);
                v.setCompoundDrawablesRelativeWithIntrinsicBounds(null, null, chevron(), null);
                return v;
            }

            @Override
            public View getDropDownView(int position, View convertView, ViewGroup parent) {
                TextView v = (TextView) super.getDropDownView(position, convertView, parent);
                v.setTextColor(PARCHMENT);
                v.setTextSize(16);
                v.setMinHeight(dp(48));
                v.setPadding(dp(16), 0, dp(16), 0);
                v.setBackground(touchable(null, new android.graphics.drawable.ColorDrawable(Color.WHITE)));
                return v;
            }
        };
        a.setDropDownViewResource(android.R.layout.simple_list_item_1);
        return a;
    }

    // --- screens --------------------------------------------------------------------------------------------------

    private void showNeedData(String error) {
        progress.setVisibility(View.GONE);
        pick.setVisibility(View.VISIBLE);
        status.setTextColor(PARCHMENT);
        status.setText((error != null ? "The game data could not be imported. Check that you chose the game data zip and "
                + "that the phone has about 1.7 GB free, then choose it again.\n(" + error + ")\n\n" : "")
                + "The first time, the game needs its data file, the game data zip (about 1.6 GB). Copy it to the phone "
                + "(for example to Downloads), then choose it below. You can delete the zip afterwards.");
    }

    private void showChoices() {
        pick.setVisibility(View.GONE);
        progress.setVisibility(View.GONE);
        speedSection.setVisibility(View.VISIBLE);
        optionsSection.setVisibility(View.VISIBLE);
        accountBox.setVisibility(View.VISIBLE);
        startBar.setVisibility(View.VISIBLE);
        if (settingsMode) {
            start.setText("Restart with these settings");
            back.setVisibility(View.VISIBLE);
            status.setText("The game is running. Change what you like, then restart it.");  // one line: landscape is short
            accountStatus.setText("Loading the players…");
        } else {
            status.setVisibility(View.GONE);
            accountStatus.setText("Loading the players…");
            OfflineServer.applySpeed(this);  // up while the player chooses; restarted if another speed is picked
        }
        loadAccounts(null, null);
    }

    // --- accounts -------------------------------------------------------------------------------------------------

    private void setAccountControlsEnabled(boolean on) {
        clubSpinner.setEnabled(on);
        playerSpinner.setEnabled(on);
        exportButton.setEnabled(on);
        importButton.setEnabled(on);
    }

    /**
     * Wait for the server, fetch the listing, fill the spinners (preselect `select`, else the current account) and
     * show `notice` (e.g. an import's result) under the current account.
     */
    /** account_picker.LIST_SCHEMA_VERSION of the tables readStoredListing reads (another version: wait for HTTP). */
    static final int LIST_SCHEMA_VERSION = 1;

    /**
     * The account listing the server keeps in its database (account_picker.publish_list: picker_list, picker_meta),
     * read directly and read-only, so the lists show at once while the server is still starting; null when there is
     * none yet (first start) or its version differs. Shaped like GET /offline/accounts.
     */
    private JSONObject readStoredListing() {
        java.io.File f = new java.io.File(OfflineServer.baseDir(this), "arcane.sqlite3");
        if (!f.isFile()) return null;
        try (android.database.sqlite.SQLiteDatabase db = android.database.sqlite.SQLiteDatabase.openDatabase(
                f.getPath(), null, android.database.sqlite.SQLiteDatabase.OPEN_READONLY)) {
            String version = null, cur = NEW;
            try (Cursor c = db.rawQuery("SELECT key, value FROM picker_meta", null)) {
                while (c.moveToNext()) {
                    if ("version".equals(c.getString(0))) version = c.getString(1);
                    if ("current".equals(c.getString(0))) cur = c.getString(1);
                }
            }
            if (!String.valueOf(LIST_SCHEMA_VERSION).equals(version)) return null;
            JSONArray clubsJ = new JSONArray();
            JSONObject current = null, club = null;
            String clubKey = null;
            try (Cursor c = db.rawQuery("SELECT club_pos, club_id, club_name, public_id, name, level, role "
                    + "FROM picker_list ORDER BY club_pos, pos", null)) {
                while (c.moveToNext()) {
                    String key = c.getString(0);
                    if (!key.equals(clubKey)) {
                        clubKey = key;
                        club = new JSONObject().put("id", c.getString(1)).put("name", c.getString(2))
                                .put("members", new JSONArray());
                        clubsJ.put(club);
                    }
                    JSONObject m = new JSONObject().put("id", c.getString(3)).put("name", c.getString(4))
                            .put("level", c.getInt(5)).put("role", c.getInt(6)).put("club_id", c.getString(1))
                            .put("club_name", c.getString(2)).put("account", true);
                    club.getJSONArray("members").put(m);
                    if (m.getString("id").equals(cur)) current = m;
                }
            }
            JSONObject l = new JSONObject().put("clubs", clubsJ);
            if (current != null) l.put("current", current);
            return l;
        } catch (RuntimeException | JSONException e) {  // no tables yet, locked, ...: the server's answer instead
            android.util.Log.i(OfflineServer.TAG, "stored account list not readable: " + e);
            return null;
        }
    }

    private void loadAccounts(String select, String notice) {
        boolean fromCache = false;
        if (select == null && notice == null && !listed && !settingsMode) {
            JSONObject stored = readStoredListing();
            if (stored != null) {
                fillAccounts(stored, null, null);
                exportButton.setEnabled(false);  // these need the server: on when its list is in
                importButton.setEnabled(false);
                fromCache = true;
            }
        }
        final boolean shownFromCache = fromCache;
        new Thread(() -> {
            long deadline = System.currentTimeMillis() + 180_000;
            while (!OfflineServer.isUp() && System.currentTimeMillis() < deadline && !isFinishing()) {
                OfflineServer.sleep(300);
            }
            String err = null;
            JSONObject listing = null;
            try {
                listing = new JSONObject(new String(OfflineServer.request("GET", "/offline/accounts", null, null),
                        StandardCharsets.UTF_8));
            } catch (IOException | JSONException e) {
                err = e.getMessage();
            }
            final JSONObject l = listing;
            final String e = err;
            ui.post(() -> {
                if (isFinishing() || starting) return;
                String restart = settingsMode ? null : OfflineServer.takeRestartAccount(this);
                if (l == null) {
                    android.util.Log.w(OfflineServer.TAG, "account listing failed: " + e);
                    accountStatus.setText("The players couldn't be loaded. Start plays the current player.");
                    if (restart != null) onStartPressed();
                    return;
                }
                // a list shown from the stored copy keeps what the player picked meanwhile
                String keep = select == null && shownFromCache ? chosenId() : select;
                fillAccounts(l, restart != null ? restart : keep, notice);
                if (restart != null) onStartPressed();  // the app restarted after a change in settings mode
            });
        }, "arcane-offline-accounts").start();
    }

    private void fillAccounts(JSONObject l, String select, String notice) {
        clubs.clear();
        JSONArray cs = l.optJSONArray("clubs");
        for (int i = 0; cs != null && i < cs.length(); i++) {
            JSONObject c = cs.optJSONObject(i);
            Club club = new Club(c.optString("id"), c.optString("name"));
            JSONArray ms = c.optJSONArray("members");
            for (int j = 0; ms != null && j < ms.length(); j++) {
                JSONObject m = ms.optJSONObject(j);
                if (m.optBoolean("account")) club.members.add(new Member(m));
            }
            if (!club.members.isEmpty()) clubs.add(club);
        }
        JSONObject cur = l.optJSONObject("current");
        currentId = cur != null ? cur.optString("id") : NEW;
        Member current = cur != null ? new Member(cur) : null;
        nowPlaying = (current != null
                ? "Now playing: " + current.name + ", level " + current.level
                + (current.clubName.isEmpty() ? "" : ", " + current.clubName)
                : "Now playing: a new player (created when the game starts)");
        accountStatus.setText(nowPlaying);
        if (notice != null) notice(notice);
        List<Object> clubItems = new ArrayList<>(clubs);
        clubItems.add("New player");
        clubSpinner.setAdapter(adapter(clubItems));
        listed = true;
        setAccountControlsEnabled(true);
        preselect(select != null ? select : currentId);
    }

    /** A line under "Now playing" (the result of an export, import, switch). */
    private void notice(String text) {
        accountStatus.setText(nowPlaying.isEmpty() ? text : nowPlaying + "\n" + text);
    }

    private void preselect(String id) {
        preferredId = id;
        for (int c = 0; c < clubs.size(); c++) {
            for (Member m : clubs.get(c).members) {
                if (m.id.equals(id)) {
                    clubSpinner.setSelection(c, false);
                    showMembers(c, id);
                    return;
                }
            }
        }
        clubSpinner.setSelection(clubs.size(), false);  // New player
        showMembers(clubs.size(), null);
    }

    private void showMembers(int clubIndex, String select) {
        boolean fresh = clubIndex >= clubs.size();
        playerSpinner.setVisibility(fresh ? View.GONE : View.VISIBLE);
        playerLabel.setVisibility(fresh ? View.GONE : View.VISIBLE);
        exportButton.setEnabled(!fresh);
        if (fresh) return;
        List<Member> members = clubs.get(clubIndex).members;
        @SuppressWarnings("unchecked")
        ArrayAdapter<Member> old = (ArrayAdapter<Member>) playerSpinner.getAdapter();
        if (old == null || old.getCount() != members.size() || (old.getCount() > 0 && old.getItem(0) != members.get(0))) {
            playerSpinner.setAdapter(adapter(members));
        }
        String want = select != null ? select : preferredId;
        for (int i = 0; i < members.size(); i++) {
            if (members.get(i).id.equals(want)) {
                playerSpinner.setSelection(i, false);
                return;
            }
        }
    }

    /** The picked account: a public id, or NEW. */
    private String chosenId() {
        int c = clubSpinner.getSelectedItemPosition();
        if (c < 0 || c >= clubs.size()) return NEW;
        Object m = playerSpinner.getSelectedItem();
        return m instanceof Member ? ((Member) m).id : NEW;
    }

    private Member chosenMember() {
        int c = clubSpinner.getSelectedItemPosition();
        Object m = c >= 0 && c < clubs.size() ? playerSpinner.getSelectedItem() : null;
        return m instanceof Member ? (Member) m : null;
    }

    // --- export / import ------------------------------------------------------------------------------------------

    private void chooseExportFile() {
        Member m = chosenMember();
        if (m == null) return;
        exportId = m.id;
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("application/json");
        intent.putExtra(Intent.EXTRA_TITLE, "account-" + m.id + "-" + m.name.replaceAll("[^\\p{L}\\p{N}_-]", "") + ".json");
        startActivityForResult(intent, EXPORT_ACCOUNT);
    }

    private void export(Uri uri) {
        final String id = exportId;
        notice("Exporting…");
        new Thread(() -> {
            String msg;
            try {
                byte[] body = OfflineServer.request("GET", "/offline/accounts/" + id + "/export", null, null);
                try (OutputStream o = getContentResolver().openOutputStream(uri, "wt")) {
                    if (o == null) throw new IOException("cannot write " + uri);
                    o.write(body);
                }
                msg = "Exported " + id + " to " + displayName(uri) + ".";
            } catch (IOException | RuntimeException e) {
                android.util.Log.w(OfflineServer.TAG, "export failed", e);
                msg = "The export didn't work (" + e.getMessage() + "). Choose another place and try again.";
            }
            final String m = msg;
            ui.post(() -> notice(m));
        }, "arcane-offline-export").start();
    }

    private void chooseImportFile() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        startActivityForResult(intent, IMPORT_ACCOUNT);
    }

    /** The document's last-modified time (epoch s), or -1: dates a source whose content has none. */
    private long lastModified(Uri uri) {
        String col = android.provider.DocumentsContract.Document.COLUMN_LAST_MODIFIED;
        try (Cursor c = getContentResolver().query(uri, new String[] {col}, null, null, null)) {
            if (c != null && c.moveToFirst() && !c.isNull(0)) return c.getLong(0) / 1000;
        } catch (RuntimeException ignored) {
        }
        return -1;
    }

    private String displayName(Uri uri) {
        try (Cursor c = getContentResolver().query(uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst() && c.getString(0) != null) return c.getString(0);
        } catch (RuntimeException ignored) {
        }
        String last = uri.getLastPathSegment();
        return last != null ? last : "account.json";
    }

    private void importAccount(Uri uri, String playerId, byte[] cached, String cachedName) {
        notice("Importing…");
        setAccountControlsEnabled(false);
        new Thread(() -> {
            String err = null;
            JSONObject res = null;
            byte[] data = cached;
            String name = cachedName;
            try {
                if (data == null) {
                    name = displayName(uri);
                    importModified = lastModified(uri);
                    try (InputStream in = getContentResolver().openInputStream(uri)) {
                        if (in == null) throw new IOException("cannot read " + uri);
                        data = OfflineServer.readBytes(new java.io.FilterInputStream(in) {
                            long total;

                            @Override
                            public int read(byte[] b, int off, int len) throws IOException {
                                int n = super.read(b, off, len);
                                if (n > 0 && (total += n) > MAX_IMPORT_BYTES) throw new IOException("file too large");
                                return n;
                            }
                        });
                    }
                }
                String path = "/offline/accounts/import?name=" + URLEncoder.encode(name, "UTF-8")
                        + (WHOLE_CLUB.equals(playerId) ? "&all=1"
                                : playerId != null ? "&player_id=" + URLEncoder.encode(playerId, "UTF-8") : "")
                        + (importModified > 0 ? "&modified=" + importModified : "");
                res = new JSONObject(new String(OfflineServer.request("POST", path, data, "application/octet-stream"),
                        StandardCharsets.UTF_8));
            } catch (IOException | JSONException | RuntimeException e) {
                err = e.getMessage();
            }
            final JSONObject r = res;
            final String e = err;
            final byte[] d = data;
            final String n = name;
            ui.post(() -> {
                setAccountControlsEnabled(listed);
                if (r == null) {
                    android.util.Log.w(OfflineServer.TAG, "account import failed: " + e);
                    notice("That file couldn't be imported (" + e + "). Choose a player file exported from this app.");
                } else if ("choose".equals(r.optString("status"))) {
                    choosePlayerToImport(r.optJSONArray("players"), r.optString("club_name"), d, n);
                } else if ("imported_club".equals(r.optString("status"))) {
                    String done = clubImportResult(r);
                    notice(done);
                    loadAccounts(null, done);
                } else {
                    String done = importResult(r) + " It's selected: press Start to play.";
                    notice(done);
                    loadAccounts(r.optString("id"), done);
                }
            });
        }, "arcane-offline-import-account").start();
    }

    /**
     * What an import did (account_picker.import_file "outcome"; account_merge.py: imports always merge, the newer
     * values win, lists are the union): a new account, the file's newer data merged on top of the account here, or
     * the file was older and only added what the account here lacked.
     */
    static String importResult(JSONObject r) {
        String name = r.optString("name");
        int level = r.optInt("level");
        switch (r.optString("outcome")) {
            case "merged":
                return "Imported " + name + " (level " + r.optInt("source_level")
                        + " in the file), merged with the newer progress already here: level " + level + ".";
            case "merged_newer":
                return "Imported " + name + ", level " + level + ", merged with the newer progress from the file.";
            default:
                return "Imported " + name + ", level " + level + ".";
        }
    }

    /** What a whole-club import did (account_picker.import_file with all=1). */
    static String clubImportResult(JSONObject r) {
        String club = r.optString("club_name");
        int count = r.optInt("count");
        String msg = "Imported " + (club.isEmpty() ? "" : club + ": ") + count + (count == 1 ? " player" : " players")
                + ". Choose who to play, then press Start.";
        JSONArray missing = r.optJSONArray("not_in_club");
        if (missing != null && missing.length() > 0) {
            msg += " " + missing.length() + (missing.length() == 1 ? " player isn't" : " players aren't")
                    + " in the club (it was full, or their newer progress here is in another club).";
        }
        return msg;
    }

    /** A file with several players (a club export): import the whole club, or pick one player. */
    private void choosePlayerToImport(JSONArray players, String clubName, byte[] data, String name) {
        if (players == null || players.length() == 0) {
            notice("The file holds no player.");
            return;
        }
        String[] labels = new String[players.length() + 1];
        String[] ids = new String[players.length() + 1];
        ids[0] = WHOLE_CLUB;
        labels[0] = "The whole club" + (clubName.isEmpty() ? "" : " (" + clubName + ")") + ": all "
                + players.length() + " players";
        for (int i = 0; i < players.length(); i++) {
            JSONObject p = players.optJSONObject(i);
            ids[i + 1] = p.optString("id");
            labels[i + 1] = p.optString("name") + ", level " + p.optInt("level");
        }
        notice(name + " holds " + players.length() + " players: import the whole club, or choose one.");
        new AlertDialog.Builder(this)
                .setTitle("Import the club or one player?")
                .setItems(labels, (dlg, which) -> importAccount(null, ids[which], data, name))
                .setNegativeButton("Cancel", null)
                .show();
    }

    // --- data zip -------------------------------------------------------------------------------------------------

    private void chooseZip() {
        Intent pickIntent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        pickIntent.addCategory(Intent.CATEGORY_OPENABLE);
        pickIntent.setType("*/*");
        pickIntent.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"application/zip", "application/x-zip-compressed", "application/octet-stream"});
        startActivityForResult(pickIntent, PICK_ZIP);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) return;
        if (requestCode == PICK_ZIP) importZip(data.getData());
        else if (requestCode == EXPORT_ACCOUNT) export(data.getData());
        else if (requestCode == IMPORT_ACCOUNT) importAccount(data.getData(), null, null, null);
    }

    private void showImporting() {
        pick.setVisibility(View.GONE);
        progress.setVisibility(View.VISIBLE);
        status.setVisibility(View.VISIBLE);
        status.setText("Importing the game data…");
    }

    private void importZip(Uri uri) {
        importing = true;
        showImporting();
        final android.content.Context app = getApplicationContext();
        new Thread(() -> {
            String error = null;
            try {
                DataImport.run(app, uri, (done, total, entry) -> {
                    if (total > 0) ui.post(() -> {
                        progress.setProgress((int) (done * 1000 / total));
                        status.setText("Importing the game data… " + (done * 100 / total) + "%");
                    });
                });
            } catch (IOException | RuntimeException e) {
                android.util.Log.w(OfflineServer.TAG, "data import failed", e);
                error = e.getMessage() != null ? e.getMessage() : e.toString();
            }
            importing = false;
            final String err = error;
            ui.post(() -> {
                if (isFinishing()) return;
                if (err != null) showNeedData(err);
                else showChoices();
            });
        }, "arcane-offline-import").start();
    }

    // --- start ----------------------------------------------------------------------------------------------------

    private void onStartPressed() {
        if (starting) return;
        int speed = selectedSpeed;
        final String choice = listed ? chosenId() : currentId;
        if (settingsMode) {
            if (speed == OfflineServer.speed(this) && selectedWarClubs == OfflineServer.warClubs(this)
                    && selectedRewardMailOnly == OfflineServer.rewardMailOnly(this)
                    && selectedPopulation.equals(OfflineServer.population(this))
                    && choice.equals(currentId)) {
                backToGame();  // nothing changed
                return;
            }
            starting = true;
            start.setEnabled(false);
            back.setEnabled(false);
            setAccountControlsEnabled(false);
            notice("Restarting the game…");
            OfflineServer.setSpeed(this, speed);
            OfflineServer.setWarClubs(this, selectedWarClubs);
            OfflineServer.setRewardMailOnly(this, selectedRewardMailOnly);
            OfflineServer.setPopulation(this, selectedPopulation);
            OfflineServer.setRestartAccount(this, choice);
            new Thread(() -> RestartActivity.restartToSetup(this), "arcane-offline-restart").start();
            return;
        }
        OfflineServer.setSpeed(this, speed);
        OfflineServer.setWarClubs(this, selectedWarClubs);
        OfflineServer.setRewardMailOnly(this, selectedRewardMailOnly);
        OfflineServer.setPopulation(this, selectedPopulation);
        starting = true;
        start.setEnabled(false);
        setAccountControlsEnabled(false);
        if (choice.equals(currentId)) {
            startServerAndGame();
            return;
        }
        notice("Switching player…");
        new Thread(() -> {  // before a speed restart: the running server binds it (the game is not connected yet)
            String err = null;
            long deadline = System.currentTimeMillis() + 180_000;  // picked from the cached list: the server may still start
            while (!OfflineServer.isUp() && System.currentTimeMillis() < deadline && !isFinishing()) {
                OfflineServer.sleep(300);
            }
            try {
                JSONObject body = new JSONObject().put("id", choice);
                OfflineServer.request("POST", "/offline/accounts/select", body.toString().getBytes(StandardCharsets.UTF_8),
                        "application/json");
            } catch (IOException | JSONException e) {
                err = e.getMessage();
            }
            final String e = err;
            ui.post(() -> {
                if (e != null) {
                    starting = false;
                    start.setEnabled(true);
                    setAccountControlsEnabled(true);
                    android.util.Log.w(OfflineServer.TAG, "account switch failed: " + e);
                    notice("Couldn't switch to that player (" + e + "). Try again.");
                } else {
                    startServerAndGame();
                }
            });
        }, "arcane-offline-select").start();
    }

    private void startServerAndGame() {
        status.setVisibility(View.VISIBLE);
        showLoading();
        OfflineServer.applySpeed(this);
        waitDeadline = System.currentTimeMillis() + WAIT_MS;
        poll();
    }

    private void poll() {
        if (launched || isFinishing()) return;
        new Thread(() -> {
            boolean up = OfflineServer.isReady(getApplicationContext());
            ui.post(() -> {
                if (up) {
                    launchGame();
                } else if (isFinishing()) {
                    return;
                } else if (System.currentTimeMillis() > waitDeadline) {
                    // details stay in logcat / server.log (OfflineServer logs every status)
                    android.util.Log.w(OfflineServer.TAG, "game not ready after " + WAIT_MS / 1000 + " s: " + OfflineServer.status());
                    status.setTextColor(ROSE);
                    status.setText("The game couldn't get ready. Try again, or restart the app if it keeps happening.");
                    loading.setVisibility(View.GONE);
                    hint.setVisibility(View.GONE);
                    retry.setVisibility(View.VISIBLE);
                } else {
                    ui.postDelayed(this::poll, 500);
                }
            });
        }, "arcane-offline-poll").start();
    }

    private void launchGame() {
        if (launched) return;
        launched = true;
        ui.removeCallbacks(nextHint);
        OfflineServer.setGameLaunched();
        Intent game = new Intent();
        game.setClassName(getPackageName(), gameActivity(this));
        game.setAction(Intent.ACTION_MAIN);
        startActivity(game);
        finish();
    }

    // --- while the game runs --------------------------------------------------------------------------------------

    @Override
    public void onBackPressed() {
        if (settingsMode && !starting) backToGame();
        else super.onBackPressed();
    }

    /** Settings mode: bring the running game back to the front (its activity is singleTask) and close this screen. */
    private void backToGame() {
        Intent game = new Intent();
        game.setClassName(getPackageName(), gameActivity(this));
        game.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(game);
        finish();
    }

    /**
     * The launcher's long-press shortcut to the settings screen (dynamic: the build keeps the game's resources, so
     * there is no shortcuts.xml). Launchers start a shortcut with NEW_TASK | CLEAR_TASK: it opens SettingsActivity,
     * whose own task keeps the game's task untouched.
     */
    private void publishShortcut() {
        if (android.os.Build.VERSION.SDK_INT < 25) return;
        try {
            android.content.pm.ShortcutManager sm = getSystemService(android.content.pm.ShortcutManager.class);
            if (sm == null) return;
            Intent open = new Intent(SETTINGS_ACTION);
            open.setClassName(getPackageName(), SettingsActivity.class.getName());
            android.content.pm.ShortcutInfo info = new android.content.pm.ShortcutInfo.Builder(this, SHORTCUT_ID)
                    .setShortLabel("Settings")
                    .setLongLabel("Speed, account and other settings")
                    .setIcon(android.graphics.drawable.Icon.createWithResource(this, getApplicationInfo().icon))
                    .setIntent(open)
                    .build();
            sm.setDynamicShortcuts(java.util.Collections.singletonList(info));
        } catch (RuntimeException e) {
            android.util.Log.w(OfflineServer.TAG, "could not publish the settings shortcut", e);
        }
    }
}
