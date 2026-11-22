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
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ImageFormat;
import android.graphics.LinearGradient;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.SurfaceTexture;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.util.Size;
import android.view.Gravity;
import android.view.Surface;
import android.view.TextureView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * "Scan QR code": the camera preview (Camera2, the platform API: no Google Play services) with a viewfinder; frames
 * are decoded with the embedded ZXing (QrDecoder) until a code names a game server (JoinLink.parseScanned). Returns
 * that address (EXTRA_URL, normalized) to SetupActivity, which checks it like a typed one; RESULT_CANCELED with
 * EXTRA_ERROR when the camera cannot be used. Started only once the CAMERA permission is granted (SetupActivity).
 */
public final class ScanActivity extends Activity implements TextureView.SurfaceTextureListener {
    static final String EXTRA_URL = "arcane.multiplayer.url", EXTRA_ERROR = "arcane.multiplayer.error";
    private static final int MAX_AREA = 1280 * 960;  // enough pixels for a code across the viewfinder, fast to decode

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final AtomicBoolean decoding = new AtomicBoolean();
    private final QrDecoder decoder = new QrDecoder();
    private TextureView preview;
    private TextView hint;
    private HandlerThread cameraThread, decodeThread;
    private Handler cameraHandler, decodeHandler;
    private CameraDevice camera;
    private CameraCaptureSession session;
    private ImageReader reader;
    private Size size;
    private int sensorOrientation;
    private volatile boolean done;
    private String rejected;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        preview = new TextureView(this);
        preview.setSurfaceTextureListener(this);
        root.addView(preview, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(new Viewfinder(this), new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.VERTICAL);
        bar.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = Style.dp(this, 16);
        bar.setPadding(pad, pad, pad, pad);
        TextView title = Style.text(this, 20, Style.GOLD);
        title.setTypeface(android.graphics.Typeface.create(android.graphics.Typeface.SERIF, android.graphics.Typeface.BOLD));
        title.setText("Scan the server's QR code");
        title.setGravity(Gravity.CENTER);
        hint = Style.text(this, 15, Style.PARCHMENT);
        hint.setGravity(Gravity.CENTER);
        hint.setText("Point the camera at the code on the server's join page.");
        TextView type = Style.quiet(this, "Type the address instead");
        type.setOnClickListener(v -> {
            setResult(RESULT_CANCELED);
            finish();
        });
        bar.addView(title);
        bar.addView(hint);
        bar.addView(type);
        FrameLayout.LayoutParams barAt = new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM);
        root.addView(bar, barAt);
        setContentView(root);
        getWindow().setNavigationBarColor(Color.BLACK);
    }

    @Override
    protected void onResume() {
        super.onResume();
        cameraThread = new HandlerThread("arcane-multiplayer-camera");
        cameraThread.start();
        cameraHandler = new Handler(cameraThread.getLooper());
        decodeThread = new HandlerThread("arcane-multiplayer-decode");
        decodeThread.start();
        decodeHandler = new Handler(decodeThread.getLooper());
        if (preview.isAvailable()) openCamera();
    }

    @Override
    protected void onPause() {
        closeCamera();
        cameraThread.quitSafely();
        decodeThread.quitSafely();
        super.onPause();
    }

    // --- TextureView ---

    @Override
    public void onSurfaceTextureAvailable(SurfaceTexture st, int w, int h) {
        if (cameraHandler != null && camera == null) openCamera();
    }

    @Override
    public void onSurfaceTextureSizeChanged(SurfaceTexture st, int w, int h) {
        fitPreview();
    }

    @Override
    public boolean onSurfaceTextureDestroyed(SurfaceTexture st) {
        return true;
    }

    @Override
    public void onSurfaceTextureUpdated(SurfaceTexture st) {}

    // --- camera ---

    private void openCamera() {
        CameraManager m = (CameraManager) getSystemService(Context.CAMERA_SERVICE);
        try {
            String id = pickCamera(m);
            if (id == null) {
                fail("This device has no camera. Type the server's address instead.");
                return;
            }
            CameraCharacteristics ch = m.getCameraCharacteristics(id);
            StreamConfigurationMap map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
            Integer orientation = ch.get(CameraCharacteristics.SENSOR_ORIENTATION);
            sensorOrientation = orientation == null ? 0 : orientation;
            size = map == null ? null : pickSize(map.getOutputSizes(ImageFormat.YUV_420_888), map.getOutputSizes(SurfaceTexture.class));
            if (size == null) {
                fail("The camera offers no usable picture size. Type the server's address instead.");
                return;
            }
            int[] afModes = ch.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES);
            boolean af = false;
            for (int mode : afModes == null ? new int[0] : afModes) af |= mode == CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE;
            final boolean continuousAf = af;
            reader = ImageReader.newInstance(size.getWidth(), size.getHeight(), ImageFormat.YUV_420_888, 2);
            reader.setOnImageAvailableListener(this::onFrame, cameraHandler);
            fitPreview();
            m.openCamera(id, new CameraDevice.StateCallback() {
                @Override
                public void onOpened(CameraDevice cd) {
                    camera = cd;
                    startPreview(continuousAf);
                }

                @Override
                public void onDisconnected(CameraDevice cd) {
                    cd.close();
                    if (camera == cd) camera = null;
                }

                @Override
                public void onError(CameraDevice cd, int error) {
                    cd.close();
                    if (camera == cd) camera = null;
                    fail("The camera could not be opened (error " + error + "; another app may be using it). "
                            + "Type the server's address instead.");
                }
            }, cameraHandler);
        } catch (CameraAccessException | SecurityException | IllegalArgumentException e) {
            Log.w(ProxyProvider.TAG, "camera", e);
            fail("The camera is not available (" + e.getMessage() + "). Type the server's address instead.");
        }
    }

    /** The back camera if there is one, else any (emulators, BlueStacks and tablets may only have a front / external one). */
    private static String pickCamera(CameraManager m) throws CameraAccessException {
        String any = null;
        for (String id : m.getCameraIdList()) {
            Integer facing = m.getCameraCharacteristics(id).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == CameraCharacteristics.LENS_FACING_BACK) return id;
            if (any == null) any = id;
        }
        return any;
    }

    /** The largest size (at most MAX_AREA) the camera streams both as YUV frames and as preview; else the smallest. */
    private static Size pickSize(Size[] yuv, Size[] previews) {
        if (yuv == null || previews == null) return null;
        Set<String> previewSizes = new HashSet<>();
        for (Size s : previews) previewSizes.add(s.toString());
        Size best = null, smallest = null;
        for (Size s : yuv) {
            if (!previewSizes.contains(s.toString())) continue;
            long area = (long) s.getWidth() * s.getHeight();
            if (area <= MAX_AREA && (best == null || area > (long) best.getWidth() * best.getHeight())) best = s;
            if (smallest == null || area < (long) smallest.getWidth() * smallest.getHeight()) smallest = s;
        }
        return best != null ? best : smallest;
    }

    private void startPreview(boolean continuousAf) {
        try {
            SurfaceTexture st = preview.getSurfaceTexture();
            if (st == null || camera == null) return;
            st.setDefaultBufferSize(size.getWidth(), size.getHeight());
            Surface surface = new Surface(st);
            final CaptureRequest.Builder req = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
            req.addTarget(surface);
            req.addTarget(reader.getSurface());
            if (continuousAf) req.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
            camera.createCaptureSession(Arrays.asList(surface, reader.getSurface()), new CameraCaptureSession.StateCallback() {
                @Override
                public void onConfigured(CameraCaptureSession s) {
                    if (camera == null) return;
                    session = s;
                    try {
                        s.setRepeatingRequest(req.build(), null, cameraHandler);
                    } catch (CameraAccessException | IllegalStateException e) {
                        Log.w(ProxyProvider.TAG, "camera preview", e);
                    }
                }

                @Override
                public void onConfigureFailed(CameraCaptureSession s) {
                    fail("The camera preview could not start. Type the server's address instead.");
                }
            }, cameraHandler);
        } catch (CameraAccessException | IllegalStateException e) {
            Log.w(ProxyProvider.TAG, "camera preview", e);
            fail("The camera preview could not start. Type the server's address instead.");
        }
    }

    private void closeCamera() {
        if (session != null) session.close();
        session = null;
        if (camera != null) camera.close();
        camera = null;
        if (reader != null) reader.close();
        reader = null;
    }

    /** Camera thread: the newest frame's luminance goes to the decoder unless it is still busy with the last one. */
    private void onFrame(ImageReader r) {
        Image img;
        try {
            img = r.acquireLatestImage();
        } catch (IllegalStateException e) {
            return;
        }
        if (img == null) return;
        try {
            if (done || !decoding.compareAndSet(false, true)) return;
            final int w = img.getWidth(), h = img.getHeight();
            final byte[] y = luminance(img.getPlanes()[0], w, h);
            decodeHandler.post(() -> {
                try {
                    String text = decoder.decodeLuminance(y, w, h);
                    if (text != null) ui.post(() -> onScanned(text));
                } finally {
                    decoding.set(false);
                }
            });
        } catch (RuntimeException e) {
            decoding.set(false);
        } finally {
            img.close();
        }
    }

    private static byte[] luminance(Image.Plane plane, int w, int h) {
        ByteBuffer buf = plane.getBuffer();
        int stride = plane.getRowStride(), step = plane.getPixelStride();
        byte[] out = new byte[w * h];
        if (stride == w && step == 1) {
            buf.get(out, 0, Math.min(out.length, buf.remaining()));
            return out;
        }
        byte[] row = new byte[stride];
        for (int j = 0; j < h; j++) {
            buf.position(j * stride);
            buf.get(row, 0, Math.min(stride, buf.remaining()));
            for (int i = 0; i < w; i++) out[j * w + i] = row[i * step];
        }
        return out;
    }

    private void onScanned(String text) {
        if (done || isFinishing()) return;
        try {
            ServerUrl url = JoinLink.parseScanned(text);
            done = true;
            setResult(RESULT_OK, new Intent().putExtra(EXTRA_URL, url.toString()));
            finish();
        } catch (IllegalArgumentException e) {
            if (!text.equals(rejected)) {  // keep scanning; say once per code why this one does not work
                rejected = text;
                hint.setTextColor(Style.ROSE);
                hint.setText(e.getMessage() + " Scan the code on the server's join page.");
            }
        }
    }

    private void fail(String message) {
        ui.post(() -> {
            if (done || isFinishing()) return;
            done = true;
            setResult(RESULT_CANCELED, new Intent().putExtra(EXTRA_ERROR, message));
            finish();
        });
    }

    /**
     * Shows the frames upright and filling the view without distortion (centre crop), whatever the display rotation:
     * the camera framework already turns them by the sensor orientation for the device's natural orientation and
     * stretches them to the view; this undoes the stretch, turns them back by the display rotation and scales them
     * to cover the view.
     */
    private void fitPreview() {
        ui.post(() -> {
            float vw = preview.getWidth(), vh = preview.getHeight();
            if (size == null || vw == 0 || vh == 0) return;
            int deg = 90 * getWindowManager().getDefaultDisplay().getRotation();  // Surface.ROTATION_* = 0..3
            boolean sideways = sensorOrientation % 180 != 0;
            float nw = sideways ? size.getHeight() : size.getWidth(), nh = sideways ? size.getWidth() : size.getHeight();
            Matrix m = new Matrix();
            m.postScale(nw / vw, nh / vh);
            m.postTranslate(-nw / 2f, -nh / 2f);
            m.postRotate(-deg);
            boolean turned = deg % 180 != 0;
            float dw = turned ? nh : nw, dh = turned ? nw : nh;
            float s = Math.max(vw / dw, vh / dh);
            m.postScale(s, s);
            m.postTranslate(vw / 2f, vh / 2f);
            preview.setTransform(m);
        });
    }

    /** The viewfinder: the frame dimmed in midnight ink around a square with gold corners and a drifting gold line. */
    private static final class Viewfinder extends View {
        private final Paint dim = new Paint(), corner = new Paint(Paint.ANTI_ALIAS_FLAG), line = new Paint();
        private final Path corners = new Path();
        private final RectF box = new RectF();
        private final long start = SystemClock.uptimeMillis();

        Viewfinder(Context c) {
            super(c);
            dim.setColor(Style.alpha(Style.INK, 0.6f));
            corner.setColor(Style.GOLD);
            corner.setStyle(Paint.Style.STROKE);
            corner.setStrokeWidth(Style.dp(c, 4));
            corner.setStrokeCap(Paint.Cap.ROUND);
            corner.setStrokeJoin(Paint.Join.ROUND);
        }

        @Override
        protected void onDraw(Canvas canvas) {
            float w = getWidth(), h = getHeight();
            float side = Math.min(w, h * 0.78f) * 0.62f;  // leave room for the text below
            float left = (w - side) / 2f, top = (h * 0.42f) - side / 2f;
            box.set(left, top, left + side, top + side);
            canvas.drawRect(0, 0, w, box.top, dim);
            canvas.drawRect(0, box.bottom, w, h, dim);
            canvas.drawRect(0, box.top, box.left, box.bottom, dim);
            canvas.drawRect(box.right, box.top, w, box.bottom, dim);
            float arm = side * 0.14f;
            corners.reset();
            corners.moveTo(box.left, box.top + arm); corners.lineTo(box.left, box.top); corners.lineTo(box.left + arm, box.top);
            corners.moveTo(box.right - arm, box.top); corners.lineTo(box.right, box.top); corners.lineTo(box.right, box.top + arm);
            corners.moveTo(box.right, box.bottom - arm); corners.lineTo(box.right, box.bottom); corners.lineTo(box.right - arm, box.bottom);
            corners.moveTo(box.left + arm, box.bottom); corners.lineTo(box.left, box.bottom); corners.lineTo(box.left, box.bottom - arm);
            canvas.drawPath(corners, corner);
            float t = ((SystemClock.uptimeMillis() - start) % 2400) / 2400f;
            float y = box.top + side * (0.08f + 0.84f * (t < 0.5f ? t * 2 : 2 - t * 2));
            line.setShader(new LinearGradient(box.left, y, box.right, y,
                    new int[] {Color.TRANSPARENT, Style.alpha(Style.GOLD, 0.85f), Color.TRANSPARENT}, null, Shader.TileMode.CLAMP));
            canvas.drawRect(box.left + side * 0.06f, y - Style.dp(getContext(), 1), box.right - side * 0.06f, y + Style.dp(getContext(), 1), line);
            postInvalidateOnAnimation();
        }
    }
}
