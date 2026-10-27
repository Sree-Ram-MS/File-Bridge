package com.sreeram.filebridge;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import java.util.List;

/** Foreground service that keeps the FileBridge server running while the app is in the background. */
public class BridgeService extends Service {
    static final String ACTION_STOP = "com.sreeram.filebridge.STOP";
    private static final String CH_RUN = "running";
    private static final String CH_REQ = "requests";
    private static final int ID_RUN = 1;

    static volatile Server server;
    static volatile String lastError;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private int reqId = 100;

    static void start(Context c) {
        Intent i = new Intent(c, BridgeService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, BridgeService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.createNotificationChannel(new NotificationChannel(CH_RUN, "Sharing status", NotificationManager.IMPORTANCE_LOW));
            nm.createNotificationChannel(new NotificationChannel(CH_REQ, "Access requests", NotificationManager.IMPORTANCE_HIGH));
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        goForeground("Starting…");
        new Thread(() -> {
            try {
                Server s = server;
                if (s == null) {
                    s = new Server(this);
                    final Server fs = s;
                    s.listener = (name, ip) -> notifyRequest(name, ip);
                    server = fs;
                }
                s.start(8000);
                lastError = null;
                List<String> urls = s.urls();
                goForeground(urls.isEmpty() ? "Port " + s.port + " · connect to Wi-Fi or hotspot" : urls.get(0));
            } catch (Exception e) {
                lastError = String.valueOf(e.getMessage());
                goForeground("Could not start: " + lastError);
            }
        }).start();
        acquireLocks();
        return START_STICKY;
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    @SuppressWarnings("deprecation")
    private Notification.Builder builder(String channel) {
        return Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, channel) : new Notification.Builder(this);
    }

    private void goForeground(String text) {
        PendingIntent stop = PendingIntent.getService(this, 1,
                new Intent(this, BridgeService.class).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE);
        Notification n = builder(CH_RUN)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("FileBridge is sharing")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(openApp())
                .addAction(new Notification.Action.Builder(null, "Stop sharing", stop).build())
                .build();
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_RUN, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(ID_RUN, n);
        }
    }

    private void notifyRequest(String name, String ip) {
        Notification n = builder(CH_REQ)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(name + " wants to connect")
                .setContentText(ip + " · tap to approve or deny")
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_ALL)
                .setContentIntent(openApp())
                .build();
        try {
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(reqId++, n);
        } catch (SecurityException ignored) {
            // notifications not allowed
        }
    }

    @SuppressWarnings("deprecation")
    private void acquireLocks() {
        try {
            if (wakeLock == null) {
                PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "FileBridge:server");
                wakeLock.acquire();
            }
            if (wifiLock == null) {
                WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
                wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "FileBridge:wifi");
                wifiLock.acquire();
            }
        } catch (Exception ignored) {
        }
    }

    @Override
    public void onDestroy() {
        Server s = server;
        if (s != null) s.stop();
        if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
        wakeLock = null;
        wifiLock = null;
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}
