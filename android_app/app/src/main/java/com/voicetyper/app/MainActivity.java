package com.voicetyper.app;

import android.Manifest;
import android.app.AlertDialog;
import android.app.Dialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.LayoutInflater;
import android.view.View;
import android.view.Window;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.Toast;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

public class MainActivity extends AppCompatActivity {
    private static final int PERMISSION_REQ = 100;
    private static final String PREFS_NAME = "voicetyper_prefs";
    private static final String KEY_URL = "server_url";
    private String serverUrl;
    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        webView = findViewById(R.id.webview);
        setupWebView();

        // 读取保存的 URL
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        serverUrl = prefs.getString(KEY_URL, "");

        // 检查权限
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    PERMISSION_REQ);
        }

        // 位置权限（安全追踪用）
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION
                    },
                    PERMISSION_REQ + 1);
        }

        // 有保存的 URL 直接加载，否则弹出输入框
        if (!serverUrl.isEmpty()) {
            loadListenPage();
            startFloatingService();
        } else {
            showUrlDialog();
        }
    }

    private void showUrlDialog() {
        // 创建自定义对话框
        Dialog dialog = new Dialog(this);
        dialog.requestWindowFeature(Window.FEATURE_NO_TITLE);

        View view = LayoutInflater.from(this).inflate(R.layout.dialog_url, null);
        dialog.setContentView(view);

        // 透明背景 + 圆角
        if (dialog.getWindow() != null) {
            dialog.getWindow().setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
            dialog.getWindow().setLayout(
                    (int)(getResources().getDisplayMetrics().widthPixels * 0.88),
                    android.view.ViewGroup.LayoutParams.WRAP_CONTENT
            );
        }

        final EditText input = view.findViewById(R.id.urlInput);
        input.setText(serverUrl.isEmpty() ? "https://" : serverUrl);

        Button connectBtn = view.findViewById(R.id.connectBtn);
        connectBtn.setOnClickListener(v -> {
            String url = input.getText().toString().trim();
            if (!url.isEmpty()) {
                if (!url.startsWith("http")) url = "https://" + url;
                serverUrl = url;
                getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                        .edit().putString(KEY_URL, url).apply();
                loadListenPage();
                startFloatingService();
                Toast.makeText(MainActivity.this, "已连接", Toast.LENGTH_SHORT).show();
                dialog.dismiss();
            }
        });

        dialog.setCancelable(false);
        dialog.show();
    }

    private void setupWebView() {
        WebSettings ws = webView.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setAllowFileAccess(false);
        ws.setDomStorageEnabled(true);
        ws.setCacheMode(WebSettings.LOAD_DEFAULT);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                for (String r : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r)) {
                        request.grant(new String[]{r});
                        return;
                    }
                }
            }
        });
    }

    private void loadListenPage() {
        webView.loadUrl(serverUrl);
    }

    private void startFloatingService() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            if (!android.provider.Settings.canDrawOverlays(this)) {
                Intent intent = new Intent(android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName()));
                startActivity(intent);
                Toast.makeText(this, "请开启悬浮窗权限", Toast.LENGTH_LONG).show();
            }
        }

        // 引导用户关闭电池优化，防止后台被杀
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            String pkg = getPackageName();
            android.os.PowerManager pm = getSystemService(android.os.PowerManager.class);
            if (pm != null && !pm.isIgnoringBatteryOptimizations(pkg)) {
                try {
                    Intent intent = new Intent(android.provider.Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                    intent.setData(Uri.parse("package:" + pkg));
                    startActivity(intent);
                    Toast.makeText(this, "请选择「允许」以保持后台运行", Toast.LENGTH_LONG).show();
                } catch (Exception e) {
                    // 部分 ROM 不支持此 Intent，忽略
                }
            }
        }

        Intent serviceIntent = new Intent(this, FloatingBubbleService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent);
        } else {
            startService(serviceIntent);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions,
                                           @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == PERMISSION_REQ) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                if (!serverUrl.isEmpty()) loadListenPage();
            }
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) webView.onResume();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (webView != null) webView.onPause();
    }

    @Override
    protected void onDestroy() {
        if (webView != null) webView.destroy();
        super.onDestroy();
    }
}
