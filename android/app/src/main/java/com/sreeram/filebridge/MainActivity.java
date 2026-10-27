package com.sreeram.filebridge;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONObject;

/** Shows the FileBridge owner dashboard (served by the local server) in a WebView. */
public class MainActivity extends Activity {
    private static final int REQ_TREE = 7;
    private static final int REQ_PERMS = 8;

    private WebView web;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean loaded;
    private boolean askedAllFiles;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        web = new WebView(this);
        setContentView(web);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        web.addJavascriptInterface(new Bridge(), "FBNative");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                String host = u.getHost();
                if ("127.0.0.1".equals(host) || "localhost".equals(host)) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));
                } catch (Exception e) {
                    toast("No browser found");
                }
                return true;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (url != null && url.endsWith("/admin")) view.evaluateJavascript(INJECT, null);
            }
        });
        web.setDownloadListener((url, ua, cd, mime, len) -> {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
            } catch (Exception e) {
                toast("No app can open this file");
            }
        });
        askPermissions();
        BridgeService.start(this);
        waitForServer(0);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (loaded) web.evaluateJavascript("typeof refresh==='function'&&refresh()", null);
    }

    private void waitForServer(int tries) {
        Server s = BridgeService.server;
        if (s != null && s.isRunning()) {
            web.loadUrl("http://127.0.0.1:" + s.port + "/admin");
            loaded = true;
            return;
        }
        if (BridgeService.lastError != null && tries > 10) {
            web.loadData("<body style='font-family:sans-serif;padding:30px'>FileBridge could not start: "
                    + android.text.Html.escapeHtml(BridgeService.lastError) + "</body>", "text/html", "UTF-8");
            return;
        }
        ui.postDelayed(() -> waitForServer(tries + 1), 150);
    }

    // ------------------------------------------------------------ permissions
    private void askPermissions() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERMS);
        }
        if (Build.VERSION.SDK_INT >= 30) {
            if (!Environment.isExternalStorageManager() && !askedAllFiles) {
                askedAllFiles = true;
                new AlertDialog.Builder(this)
                        .setTitle("Allow file access")
                        .setMessage("FileBridge needs \"All files access\" so the devices you approve can browse, download and upload files in the folder you share.")
                        .setPositiveButton("Open settings", (d, w) -> {
                            try {
                                startActivity(new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                        Uri.parse("package:" + getPackageName())));
                            } catch (Exception e) {
                                startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                            }
                        })
                        .setNegativeButton("Later", null)
                        .show();
            }
        } else if (checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE,
                    Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_PERMS);
        }
    }

    // ------------------------------------------------------------ folder picker
    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_TREE || resultCode != RESULT_OK || data == null || data.getData() == null) return;
        String path = treeToPath(data.getData());
        Server s = BridgeService.server;
        if (path == null || s == null) {
            toast("That folder can't be shared. Pick one on the phone storage or SD card.");
            return;
        }
        try {
            s.setRoot(path);
            web.evaluateJavascript("(function(){var r=document.getElementById('root');if(r)r.value="
                    + JSONObject.quote(path) + ";typeof refresh==='function'&&refresh();typeof toast==='function'&&toast('Now sharing '+"
                    + JSONObject.quote(path) + ")})()", null);
        } catch (Exception e) {
            toast(e.getMessage());
        }
    }

    static String treeToPath(Uri tree) {
        try {
            String id = DocumentsContract.getTreeDocumentId(tree); // e.g. "primary:Download" or "1A2B-3C4D:Music"
            int c = id.indexOf(':');
            String vol = c >= 0 ? id.substring(0, c) : id;
            String rel = c >= 0 ? id.substring(c + 1) : "";
            String base = "primary".equalsIgnoreCase(vol)
                    ? Environment.getExternalStorageDirectory().getPath()
                    : "/storage/" + vol;
            return rel.isEmpty() ? base : base + "/" + rel;
        } catch (Exception e) {
            return null;
        }
    }

    private void toast(String m) {
        Toast.makeText(this, m, Toast.LENGTH_LONG).show();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else moveTaskToBack(true); // keep sharing in the background
    }

    /** Called from the dashboard page. */
    class Bridge {
        @JavascriptInterface
        public void pickFolder() {
            ui.post(() -> {
                try {
                    startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE), REQ_TREE);
                } catch (Exception e) {
                    toast("No folder picker on this phone. Type the path instead.");
                }
            });
        }

        @JavascriptInterface
        public void stopSharing() {
            ui.post(() -> {
                BridgeService.stop(MainActivity.this);
                finishAndRemoveTask();
            });
        }

        @JavascriptInterface
        public void share(String text) {
            ui.post(() -> {
                Intent i = new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text);
                startActivity(Intent.createChooser(i, "Share address"));
            });
        }
    }

    /** Adds phone-only buttons to the shared dashboard page. */
    private static final String INJECT = "(function(){if(window.__fbn)return;window.__fbn=1;"
            + "var s=document.getElementById('setroot');"
            + "if(s){var b=document.createElement('button');b.textContent='Browse…';b.style.marginLeft='6px';"
            + "b.onclick=function(){FBNative.pickFolder()};s.parentNode.insertBefore(b,s.nextSibling);}"
            + "var f=document.getElementById('foot');"
            + "var bar=document.createElement('div');bar.style.cssText='display:flex;gap:8px;justify-content:center;margin:18px 0 6px;flex-wrap:wrap';"
            + "var sh=document.createElement('button');sh.textContent='Share address';"
            + "sh.onclick=function(){var u=(typeof S!=='undefined'&&S&&S.ips&&S.ips.length)?('http://'+S.ips[0]+':'+S.port):location.origin;FBNative.share(u)};"
            + "var st=document.createElement('button');st.textContent='Stop sharing';st.className='btn-danger';"
            + "st.onclick=function(){FBNative.stopSharing()};"
            + "bar.appendChild(sh);bar.appendChild(st);"
            + "if(f&&f.parentNode)f.parentNode.insertBefore(bar,f);else document.body.appendChild(bar);"
            + "})()";
}
