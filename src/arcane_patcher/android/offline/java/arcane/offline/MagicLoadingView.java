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

import android.animation.ValueAnimator;
import android.content.Context;
import android.graphics.BlurMaskFilter;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.os.Build;
import android.os.SystemClock;
import android.provider.Settings;
import android.view.View;

/**
 * The launcher's loading animation, drawn in code: a magic circle of runes turning one way, a star turning the other,
 * sparkles orbiting it and a glowing spell orb bouncing in the middle. Abstract magic only (no characters, no logos).
 * Redrawn every frame (postOnAnimation) while shown; a single still frame when the device turns animations off.
 */
public final class MagicLoadingView extends View {
    private static final int GOLD = Color.rgb(255, 205, 90), VIOLET = Color.rgb(170, 120, 255), CYAN = Color.rgb(90, 220, 255);
    private static final int SPARKLES = 14;

    private final Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint rune = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint star = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint glow = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint orb = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint sparkle = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();
    private final long start = SystemClock.uptimeMillis();
    private boolean running, still;

    public MagicLoadingView(Context ctx) {
        super(ctx);
        float d = ctx.getResources().getDisplayMetrics().density;
        ring.setStyle(Paint.Style.STROKE);
        ring.setStrokeWidth(2.5f * d);
        ring.setColor(VIOLET);
        rune.setStyle(Paint.Style.STROKE);
        rune.setStrokeWidth(2f * d);
        rune.setStrokeCap(Paint.Cap.ROUND);
        rune.setColor(GOLD);
        star.setStyle(Paint.Style.STROKE);
        star.setStrokeWidth(1.8f * d);
        star.setStrokeJoin(Paint.Join.ROUND);
        star.setColor(CYAN);
        glow.setStyle(Paint.Style.STROKE);
        glow.setStrokeWidth(8f * d);
        glow.setColor(Color.argb(90, 170, 120, 255));
        glow.setMaskFilter(new BlurMaskFilter(8f * d, BlurMaskFilter.Blur.NORMAL));
        setLayerType(LAYER_TYPE_SOFTWARE, null);  // the blur mask filter needs a software layer
        sparkle.setStyle(Paint.Style.FILL);
    }

    /** Always one still frame (the setup screen's faint header circle). */
    public void setStill(boolean still) {
        this.still = still;
        invalidate();
    }

    /** False when the device turns animations off ("remove animations" / animator duration scale 0). */
    public static boolean motionAllowed(Context ctx) {
        if (Build.VERSION.SDK_INT >= 26) return ValueAnimator.areAnimatorsEnabled();
        return Settings.Global.getFloat(ctx.getContentResolver(), Settings.Global.ANIMATOR_DURATION_SCALE, 1f) != 0f;
    }

    @Override
    protected void onMeasure(int widthSpec, int heightSpec) {
        // a square that fits small screens and landscape: at most 220dp, at most the available width / height
        float d = getResources().getDisplayMetrics().density;
        int want = (int) (220 * d);
        int w = MeasureSpec.getMode(widthSpec) == MeasureSpec.UNSPECIFIED ? want : Math.min(want, MeasureSpec.getSize(widthSpec));
        int h = MeasureSpec.getMode(heightSpec) == MeasureSpec.UNSPECIFIED ? want : Math.min(want, MeasureSpec.getSize(heightSpec));
        int side = Math.max((int) (96 * d), Math.min(w, h));
        if (MeasureSpec.getMode(widthSpec) == MeasureSpec.EXACTLY && MeasureSpec.getMode(heightSpec) == MeasureSpec.EXACTLY) {
            side = Math.min(MeasureSpec.getSize(widthSpec), MeasureSpec.getSize(heightSpec));
        }
        setMeasuredDimension(side, side);
    }

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        running = true;
        postInvalidateOnAnimation();
    }

    @Override
    protected void onDetachedFromWindow() {
        running = false;
        super.onDetachedFromWindow();
    }

    @Override
    protected void onDraw(Canvas c) {
        boolean motion = !still && motionAllowed(getContext());
        float t = motion ? (SystemClock.uptimeMillis() - start) / 1000f : 0.6f;
        float cx = getWidth() / 2f, cy = getHeight() / 2f, r = Math.min(cx, cy) * 0.9f;

        // outer ring and glow, breathing slowly
        float breath = 1f + 0.03f * (float) Math.sin(t * 2.2);
        c.drawCircle(cx, cy, r * 0.82f * breath, glow);
        c.drawCircle(cx, cy, r * 0.82f * breath, ring);
        c.drawCircle(cx, cy, r * 0.64f, ring);

        // runes between the two rings, turning clockwise
        c.save();
        c.rotate(t * 24f, cx, cy);
        int runes = 12;
        float rr = r * 0.73f, s = r * 0.055f;
        for (int i = 0; i < runes; i++) {
            c.save();
            c.rotate(360f * i / runes, cx, cy);
            float x = cx, y = cy - rr;
            path.reset();
            switch (i % 4) {  // four simple glyphs
                case 0:
                    path.moveTo(x - s, y + s); path.lineTo(x, y - s); path.lineTo(x + s, y + s);
                    break;
                case 1:
                    path.moveTo(x, y - s); path.lineTo(x, y + s); path.moveTo(x - s, y); path.lineTo(x + s, y - s);
                    break;
                case 2:
                    path.addCircle(x, y, s * 0.6f, Path.Direction.CW); path.moveTo(x, y + s * 0.6f); path.lineTo(x, y + s * 1.2f);
                    break;
                default:
                    path.moveTo(x - s, y - s); path.lineTo(x + s, y + s); path.moveTo(x + s, y - s); path.lineTo(x - s, y + s);
                    break;
            }
            c.drawPath(path, rune);
            c.restore();
        }
        c.restore();

        // a seven-pointed star inside, turning the other way
        c.save();
        c.rotate(-t * 40f, cx, cy);
        path.reset();
        int points = 7;
        float sr = r * 0.6f;
        for (int i = 0; i <= points; i++) {
            double a = -Math.PI / 2 + i * 2 * Math.PI * 3 / points;  // every third vertex: {7/3}
            float x = cx + (float) (sr * Math.cos(a)), y = cy + (float) (sr * Math.sin(a));
            if (i == 0) path.moveTo(x, y);
            else path.lineTo(x, y);
        }
        c.drawPath(path, star);
        c.restore();

        // sparkles orbiting at different speeds, twinkling
        for (int i = 0; i < SPARKLES; i++) {
            double a = i * 2.39996 + t * (0.6 + (i % 5) * 0.18) * (i % 2 == 0 ? 1 : -1);
            float dist = r * (0.3f + 0.6f * ((i * 37) % 100) / 100f);
            float x = cx + (float) (dist * Math.cos(a)), y = cy + (float) (dist * Math.sin(a));
            float tw = 0.5f + 0.5f * (float) Math.sin(t * 5 + i * 1.7);
            sparkle.setColor(i % 3 == 0 ? GOLD : i % 3 == 1 ? CYAN : Color.WHITE);
            sparkle.setAlpha((int) (60 + 195 * tw));
            float ss = r * 0.02f * (0.6f + tw);
            path.reset();  // a four-pointed twinkle
            path.moveTo(x, y - ss * 2.2f); path.lineTo(x + ss * 0.5f, y - ss * 0.5f); path.lineTo(x + ss * 2.2f, y);
            path.lineTo(x + ss * 0.5f, y + ss * 0.5f); path.lineTo(x, y + ss * 2.2f); path.lineTo(x - ss * 0.5f, y + ss * 0.5f);
            path.lineTo(x - ss * 2.2f, y); path.lineTo(x - ss * 0.5f, y - ss * 0.5f); path.close();
            c.drawPath(path, sparkle);
        }

        // the spell orb, bouncing and squashing a little at the bottom of each hop
        float hop = Math.abs((float) Math.sin(t * 3.0));
        float or = r * 0.17f;
        float oy = cy + r * 0.12f - hop * r * 0.22f;
        float squash = 1f + 0.12f * (1f - hop) * (1f - hop);
        orb.setShader(new RadialGradient(cx - or * 0.3f, oy - or * 0.3f, or * 1.4f,
                new int[] {Color.WHITE, CYAN, VIOLET, Color.argb(0, 170, 120, 255)}, new float[] {0f, 0.35f, 0.75f, 1f},
                Shader.TileMode.CLAMP));
        c.save();
        c.scale(squash, 1f / squash, cx, oy + or);
        c.drawCircle(cx, oy, or * 1.4f, orb);
        c.restore();

        if (running && motion && isShown()) postInvalidateOnAnimation();
    }

    @Override
    protected void onVisibilityChanged(View changed, int visibility) {
        super.onVisibilityChanged(changed, visibility);
        if (visibility == VISIBLE && running) postInvalidateOnAnimation();
    }
}
