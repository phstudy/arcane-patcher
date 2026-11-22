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
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.widget.Button;
import android.widget.TextView;

/**
 * the multiplayer app's look, shared by its screens and matching the offline launcher's magical-arena theme: midnight ink
 * background with a violet glow, deep violet cards with a gold hairline, starlight gold for titles and the main
 * action, parchment text, lavender for secondary text. Original artwork only.
 */
final class Style {
    static final int INK = Color.rgb(0x14, 0x14, 0x33), INK_GLOW = Color.rgb(0x1E, 0x1B, 0x4B);
    static final int VIOLET = Color.rgb(0x2A, 0x1F, 0x5C), GOLD = Color.rgb(0xE8, 0xB8, 0x4A);
    static final int PARCHMENT = Color.rgb(0xF3, 0xEC, 0xDA), LAVENDER = Color.rgb(0xA9, 0x9B, 0xD6);
    static final int ROSE = Color.rgb(0xE0, 0x6A, 0x86);

    private Style() {}

    static int dp(Context c, float v) {
        return Math.round(v * c.getResources().getDisplayMetrics().density);
    }

    static int alpha(int color, float a) {
        return Color.argb(Math.round(255 * a), Color.red(color), Color.green(color), Color.blue(color));
    }

    static GradientDrawable shape(Context c, int fill, int radiusDp, int strokeColor, float strokeDp) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(c, radiusDp));
        if (strokeDp > 0) g.setStroke(dp(c, strokeDp), strokeColor);
        return g;
    }

    /** A pressed / focused state everyone can see: a gold ripple over `content`. */
    static Drawable touchable(Context c, Drawable content, int radiusDp) {
        return new RippleDrawable(ColorStateList.valueOf(alpha(GOLD, 0.35f)), content, shape(c, Color.WHITE, radiusDp, 0, 0));
    }

    /** The screen background: midnight ink with a violet glow from the top; also colours the system bars. */
    static void background(Activity a, android.view.View root) {
        GradientDrawable bg = new GradientDrawable();
        bg.setGradientType(GradientDrawable.RADIAL_GRADIENT);
        bg.setColors(new int[] {INK_GLOW, INK});
        bg.setGradientCenter(0.5f, 0f);
        bg.setGradientRadius(a.getResources().getDisplayMetrics().heightPixels * 0.8f);
        root.setBackground(bg);
        a.getWindow().setStatusBarColor(INK);
        a.getWindow().setNavigationBarColor(INK);
    }

    static TextView text(Context c, float sp, int color) {
        TextView t = new TextView(c);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setLineSpacing(0, 1.15f);
        return t;
    }

    static TextView title(Context c, String s) {
        TextView t = text(c, 30, GOLD);
        t.setTypeface(Typeface.create(Typeface.SERIF, Typeface.BOLD));
        t.setLetterSpacing(0.02f);
        t.setText(s);
        return t;
    }

    private static ColorStateList enabled(int on, int off) {
        return new ColorStateList(new int[][] {{-android.R.attr.state_enabled}, {}}, new int[] {off, on});
    }

    private static Button button(Context c, String label) {
        Button b = new Button(c);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(18);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setMinHeight(dp(c, 56));
        b.setStateListAnimator(null);
        return b;
    }

    /** The main action: a gold pill. */
    static Button primary(Context c, String label) {
        Button b = button(c, label);
        b.setTextColor(enabled(INK, alpha(INK, 0.5f)));
        b.setBackground(touchable(c, shape(c, GOLD, 28, 0, 0), 28));
        return b;
    }

    /** A second action next to the main one: a gold outline pill. */
    static Button secondary(Context c, String label) {
        Button b = button(c, label);
        b.setTextColor(enabled(GOLD, alpha(LAVENDER, 0.4f)));
        b.setBackground(touchable(c, shape(c, alpha(INK, 0.35f), 28, GOLD, 1.5f), 28));
        return b;
    }

    /** A quiet text button (gold text, no frame). */
    static TextView quiet(Context c, String label) {
        TextView b = text(c, 15, GOLD);
        b.setText(label);
        b.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        b.setGravity(Gravity.CENTER);
        b.setMinHeight(dp(c, 48));
        b.setPadding(dp(c, 16), 0, dp(c, 16), 0);
        b.setClickable(true);
        b.setFocusable(true);
        b.setBackground(touchable(c, null, 24));
        return b;
    }
}
