package com.voicetyper.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.IBinder;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.Toast;
import androidx.core.app.NotificationCompat;

public class FloatingBubbleService extends Service {
    private static final int NOTIF_ID = 1001;
    private static final String CHANNEL_ID = "voicetyper_fg";
    private WindowManager windowManager;
    private View bubbleView;
    private WindowManager.LayoutParams params;
    private float initialX, initialY, initialTouchX, initialTouchY;
    private static final int CLICK_THRESHOLD = 10;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        startForeground(NOTIF_ID, buildNotification());
        setupBubble();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "语音助手",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("保持语音助手在后台运行");
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }

    private Notification buildNotification() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("语音助手运行中")
                .setContentText("点击回到应用")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentIntent(pi)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void setupBubble() {
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);

        // 悬浮窗布局参数
        int overlayType;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            overlayType = WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY;
        } else {
            overlayType = WindowManager.LayoutParams.TYPE_PHONE;
        }

        params = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                overlayType,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                PixelFormat.TRANSLUCENT
        );
        params.gravity = Gravity.TOP | Gravity.START;
        params.x = 100;
        params.y = 400;

        // 气泡布局
        LayoutInflater inflater = LayoutInflater.from(this);
        bubbleView = inflater.inflate(R.layout.bubble_layout, null);

        ImageView bubbleIcon = bubbleView.findViewById(R.id.bubble_icon);

        // 触摸事件：拖动 + 点击
        bubbleIcon.setOnTouchListener(new View.OnTouchListener() {
            @Override
            public boolean onTouch(View v, MotionEvent event) {
                switch (event.getAction()) {
                    case MotionEvent.ACTION_DOWN:
                        initialX = params.x;
                        initialY = params.y;
                        initialTouchX = event.getRawX();
                        initialTouchY = event.getRawY();
                        return true;

                    case MotionEvent.ACTION_MOVE:
                        params.x = (int) (initialX + event.getRawX() - initialTouchX);
                        params.y = (int) (initialY + event.getRawY() - initialTouchY);
                        windowManager.updateViewLayout(bubbleView, params);
                        return true;

                    case MotionEvent.ACTION_UP:
                        float dx = Math.abs(event.getRawX() - initialTouchX);
                        float dy = Math.abs(event.getRawY() - initialTouchY);
                        if (dx < CLICK_THRESHOLD && dy < CLICK_THRESHOLD) {
                            // 点击 → 打开主界面
                            Intent intent = new Intent(FloatingBubbleService.this, MainActivity.class);
                            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                            startActivity(intent);
                        }
                        return true;
                }
                return false;
            }
        });

        windowManager.addView(bubbleView, params);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (bubbleView != null && windowManager != null) {
            try {
                windowManager.removeView(bubbleView);
            } catch (Exception e) {}
        }
    }
}
