package com.sreeram.filebridge;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Environment;
import android.webkit.MimeTypeMap;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.zip.Deflater;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * FileBridge HTTP server: the same API and web pages as the desktop filebridge.py,
 * written with plain Java sockets so the APK stays tiny.
 */
public class Server {
    public static final String VERSION = "0.1.0";
    static final String[] PERMS = {"read", "upload", "full"};

    public interface Listener {
        void onAccessRequest(String name, String ip);
    }

    // ---------------------------------------------------------------- state
    static class Req {
        String id, name, ip, ua, status = "pending", token;
        double time;
    }

    static class Device {
        String id, name, ip, perm;
        double approvedAt, lastSeen;
    }

    private final Context ctx;
    private final SharedPreferences prefs;
    private final File devicesFile;
    private final SecureRandom rand = new SecureRandom();
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private final Map<String, Req> pending = new LinkedHashMap<>();
    private final Map<String, Device> devices = new LinkedHashMap<>(); // token -> device
    private final String joinHtml, explorerHtml, adminHtml;

    private ServerSocket serverSocket;
    private volatile boolean running;
    private volatile List<String> ownerIps = new ArrayList<>();
    volatile String root;
    volatile int port;
    Listener listener;

    public Server(Context context) throws IOException {
        ctx = context.getApplicationContext();
        prefs = ctx.getSharedPreferences("filebridge", Context.MODE_PRIVATE);
        devicesFile = new File(ctx.getFilesDir(), "devices.json");
        joinHtml = asset("web/join.html");
        explorerHtml = asset("web/explorer.html");
        adminHtml = asset("web/admin.html");
        String saved = prefs.getString("root", null);
        File dl = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
        root = canonical(saved != null && new File(saved).isDirectory() ? new File(saved) : dl);
        loadDevices();
    }

    private String asset(String name) throws IOException {
        try (InputStream in = ctx.getAssets().open(name)) {
            return new String(readAll(in, Integer.MAX_VALUE), StandardCharsets.UTF_8);
        }
    }

    public String hostname() {
        String n = prefs.getString("name", null);
        if (n != null && !n.trim().isEmpty()) return n;
        String m = Build.MODEL == null ? "Android" : Build.MODEL;
        String b = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER;
        if (!b.isEmpty() && !m.toLowerCase(Locale.ROOT).startsWith(b.toLowerCase(Locale.ROOT))) {
            m = Character.toUpperCase(b.charAt(0)) + b.substring(1) + " " + m;
        }
        return m;
    }

    public boolean isRunning() {
        return running;
    }

    public synchronized void start(int preferred) throws IOException {
        if (running) return;
        IOException last = null;
        for (int p = preferred; p < preferred + 20; p++) {
            try {
                ServerSocket ss = new ServerSocket();
                ss.setReuseAddress(true);
                ss.bind(new InetSocketAddress(p));
                serverSocket = ss;
                port = p;
                break;
            } catch (IOException e) {
                last = e;
            }
        }
        if (serverSocket == null) throw last != null ? last : new IOException("No free port");
        running = true;
        refreshIps();
        Thread t = new Thread(this::acceptLoop, "filebridge-accept");
        t.setDaemon(true);
        t.start();
    }

    public synchronized void stop() {
        running = false;
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        serverSocket = null;
    }

    private void acceptLoop() {
        ServerSocket ss = serverSocket;
        while (running && ss != null && !ss.isClosed()) {
            try {
                final Socket s = ss.accept();
                pool.execute(() -> handle(s));
            } catch (IOException e) {
                if (!running) break;
            }
        }
    }

    public List<String> refreshIps() {
        List<String> out = new ArrayList<>();
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InetAddress a : Collections.list(ni.getInetAddresses())) {
                    if (a instanceof Inet4Address && !a.isLoopbackAddress()) out.add(a.getHostAddress());
                }
            }
        } catch (Exception ignored) {
        }
        Collections.sort(out, (a, b) -> Boolean.compare(!a.startsWith("192.168."), !b.startsWith("192.168.")));
        ownerIps = out;
        return out;
    }

    public List<String> urls() {
        List<String> u = new ArrayList<>();
        for (String ip : ownerIps) u.add("http://" + ip + ":" + port);
        return u;
    }

    public void setRoot(String path) throws IOException {
        File f = new File(path);
        if (!f.isDirectory()) throw new HttpError(400, "That folder doesn't exist");
        root = canonical(f);
        prefs.edit().putString("root", root).apply();
    }

    // ---------------------------------------------------------------- devices store
    private synchronized void loadDevices() {
        try (InputStream in = new FileInputStream(devicesFile)) {
            JSONObject j = new JSONObject(new String(readAll(in, 8 << 20), StandardCharsets.UTF_8));
            java.util.Iterator<String> it = j.keys();
            while (it.hasNext()) {
                String tok = it.next();
                JSONObject o = j.getJSONObject(tok);
                Device d = new Device();
                d.id = o.optString("id");
                d.name = o.optString("name");
                d.ip = o.optString("ip");
                d.perm = validPerm(o.optString("perm")) ? o.optString("perm") : "read";
                d.approvedAt = o.optDouble("approved_at", 0);
                d.lastSeen = o.optDouble("last_seen", 0);
                devices.put(tok, d);
            }
        } catch (Exception ignored) {
        }
    }

    private synchronized void saveDevices() {
        try {
            JSONObject j = new JSONObject();
            for (Map.Entry<String, Device> e : devices.entrySet()) {
                Device d = e.getValue();
                j.put(e.getKey(), new JSONObject().put("id", d.id).put("name", d.name).put("ip", d.ip)
                        .put("perm", d.perm).put("approved_at", d.approvedAt).put("last_seen", d.lastSeen));
            }
            try (OutputStream out = new FileOutputStream(devicesFile)) {
                out.write(j.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    // ---------------------------------------------------------------- helpers
    static class HttpError extends IOException {
        final int code;

        HttpError(int code, String msg) {
            super(msg);
            this.code = code;
        }
    }

    static class Request {
        String method, path, query = "", remoteIp;
        Map<String, String> headers = new HashMap<>();
        Map<String, String> params = new HashMap<>();
        InputStream body;
        long contentLength = -1;

        String header(String k) {
            return headers.get(k.toLowerCase(Locale.ROOT));
        }

        String param(String k) {
            String v = params.get(k);
            return v == null ? "" : v;
        }

        String cookie(String name) {
            String c = header("cookie");
            if (c == null) return null;
            for (String part : c.split(";")) {
                int eq = part.indexOf('=');
                if (eq > 0 && part.substring(0, eq).trim().equals(name)) return part.substring(eq + 1).trim();
            }
            return null;
        }
    }

    static boolean validPerm(String p) {
        for (String x : PERMS) if (x.equals(p)) return true;
        return false;
    }

    static boolean allows(String perm, String action) {
        if (perm == null) return false;
        switch (perm) {
            case "owner":
                return true;
            case "full":
                return !action.equals("owner");
            case "upload":
                return action.equals("read") || action.equals("upload");
            case "read":
                return action.equals("read");
            default:
                return false;
        }
    }

    private String token(int bytes) {
        byte[] b = new byte[bytes];
        rand.nextBytes(b);
        return android.util.Base64.encodeToString(b, android.util.Base64.URL_SAFE | android.util.Base64.NO_PADDING | android.util.Base64.NO_WRAP);
    }

    private String hex(int bytes) {
        byte[] b = new byte[bytes];
        rand.nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format(Locale.ROOT, "%02x", x));
        return sb.toString();
    }

    static String canonical(File f) throws IOException {
        return f.getCanonicalPath();
    }

    static byte[] readAll(InputStream in, int max) throws IOException {
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) {
            bo.write(buf, 0, n);
            if (bo.size() > max) throw new HttpError(400, "Request too large");
        }
        return bo.toByteArray();
    }

    static String decode(String s) {
        try {
            return URLDecoder.decode(s.replace("+", "%2B"), "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    static String decodeQuery(String s) {
        try {
            return URLDecoder.decode(s, "UTF-8");
        } catch (Exception e) {
            return s;
        }
    }

    static String encode(String s) {
        try {
            return URLEncoder.encode(s, "UTF-8").replace("+", "%20");
        } catch (Exception e) {
            return s;
        }
    }

    static String cleanName(String n) throws HttpError {
        n = n == null ? "" : n.trim();
        for (char ch : "<>:\"|?*".toCharArray()) n = n.replace(ch, '_');
        if (n.isEmpty() || n.equals(".") || n.equals("..") || n.contains("/") || n.contains("\\")
                || n.indexOf('\0') >= 0 || n.length() > 255) {
            throw new HttpError(400, "Invalid name");
        }
        return n;
    }

    static File uniquePath(File f) {
        if (!f.exists()) return f;
        String name = f.getName();
        int dot = name.lastIndexOf('.');
        String base = dot > 0 ? name.substring(0, dot) : name;
        String ext = dot > 0 ? name.substring(dot) : "";
        int i = 1;
        File c;
        do {
            c = new File(f.getParentFile(), base + " (" + i + ")" + ext);
            i++;
        } while (c.exists());
        return c;
    }

    File safePath(String rel) throws IOException {
        String r = rel == null ? "" : rel.replace('\\', '/');
        while (r.startsWith("/")) r = r.substring(1);
        while (r.endsWith("/")) r = r.substring(0, r.length() - 1);
        String base = root;
        File full = new File(r.isEmpty() ? base : base + "/" + r);
        String c = full.getCanonicalPath();
        if (!c.equals(base) && !c.startsWith(base.endsWith("/") ? base : base + "/")) {
            throw new HttpError(403, "Outside the shared folder");
        }
        return new File(c);
    }

    private boolean isOwner(Request r) {
        String ip = r.remoteIp;
        return ip.startsWith("127.") || ip.equals("::1") || ip.equals("0:0:0:0:0:0:0:1") || ownerIps.contains(ip);
    }

    private String cookieName() {
        return "fb_token_" + port;
    }

    private synchronized Device device(Request r) {
        String tok = r.cookie(cookieName());
        Device d = tok == null ? null : devices.get(tok);
        if (d != null) {
            d.lastSeen = System.currentTimeMillis() / 1000.0;
            d.ip = r.remoteIp;
        }
        return d;
    }

    private String perm(Request r) {
        if (isOwner(r)) return "owner";
        Device d = device(r);
        return d == null ? null : d.perm;
    }

    private void require(Request r, String action) throws HttpError {
        String p = perm(r);
        if (p == null) throw new HttpError(401, "Not connected. Request access first.");
        if (!allows(p, action)) throw new HttpError(403, "Your access level doesn't allow this");
    }

    private void requireOwner(Request r) throws HttpError {
        if (!isOwner(r)) throw new HttpError(403, "Only the sharing device can do this");
    }

    private void csrf(Request r) throws HttpError {
        if (!"1".equals(r.header("x-fb"))) throw new HttpError(403, "Missing request header");
    }

    private JSONObject readJson(Request r) throws IOException {
        if (r.contentLength > (1 << 20)) throw new HttpError(400, "Request too large");
        String s = r.contentLength > 0 ? new String(readAll(r.body, 1 << 20), StandardCharsets.UTF_8) : "";
        if (s.trim().isEmpty()) return new JSONObject();
        try {
            return new JSONObject(s);
        } catch (JSONException e) {
            throw new HttpError(400, "Bad JSON");
        }
    }

    // ---------------------------------------------------------------- responses
    private static void writeHead(OutputStream out, int code, String type, long length, Map<String, String> extra) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("HTTP/1.1 ").append(code).append(' ').append(reason(code)).append("\r\n");
        sb.append("Server: FileBridge/").append(VERSION).append("\r\n");
        sb.append("Connection: close\r\n");
        if (type != null) sb.append("Content-Type: ").append(type).append("\r\n");
        if (length >= 0) sb.append("Content-Length: ").append(length).append("\r\n");
        if (extra != null) for (Map.Entry<String, String> e : extra.entrySet()) {
            sb.append(e.getKey()).append(": ").append(e.getValue()).append("\r\n");
        }
        sb.append("\r\n");
        out.write(sb.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static String reason(int code) {
        switch (code) {
            case 200: return "OK";
            case 204: return "No Content";
            case 206: return "Partial Content";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 409: return "Conflict";
            case 411: return "Length Required";
            case 416: return "Range Not Satisfiable";
            default: return code >= 500 ? "Server Error" : "OK";
        }
    }

    private static void sendBytes(OutputStream out, int code, String type, byte[] body, Map<String, String> extra) throws IOException {
        Map<String, String> h = new LinkedHashMap<>();
        h.put("Cache-Control", "no-store");
        if (extra != null) h.putAll(extra);
        writeHead(out, code, type, body.length, h);
        out.write(body);
        out.flush();
    }

    private static void json(OutputStream out, int code, JSONObject o, Map<String, String> extra) throws IOException {
        sendBytes(out, code, "application/json", o.toString().getBytes(StandardCharsets.UTF_8), extra);
    }

    private static void json(OutputStream out, JSONObject o) throws IOException {
        json(out, 200, o, null);
    }

    private static void html(OutputStream out, int code, String s) throws IOException {
        sendBytes(out, code, "text/html; charset=utf-8", s.getBytes(StandardCharsets.UTF_8), null);
    }

    private static JSONObject ok() {
        try {
            return new JSONObject().put("ok", true);
        } catch (JSONException e) {
            return new JSONObject();
        }
    }

    // ---------------------------------------------------------------- connection
    private void handle(Socket s) {
        OutputStream out = null;
        boolean headSent = false;
        try {
            s.setSoTimeout(60000);
            InputStream in = new BufferedInputStream(s.getInputStream(), 65536);
            out = new BufferedOutputStream(s.getOutputStream(), 65536);
            Request r = parse(in);
            if (r == null) return;
            InetAddress ra = s.getInetAddress();
            r.remoteIp = ra == null ? "" : ra.getHostAddress();
            if (r.remoteIp.startsWith("::ffff:")) r.remoteIp = r.remoteIp.substring(7);
            try {
                route(r, out);
            } catch (HttpError e) {
                err(out, e.code, e.getMessage());
            } catch (FileNotFoundException e) {
                err(out, 404, "Not found");
            } catch (JSONException e) {
                err(out, 400, "Bad JSON");
            } catch (IOException e) {
                // client went away or disk error mid-response; best effort
                err(out, 500, "I/O error: " + e.getMessage());
            } catch (Exception e) {
                err(out, 500, "Server error: " + e);
            }
            out.flush();
        } catch (Exception ignored) {
        } finally {
            try {
                s.close();
            } catch (IOException ignored) {
            }
        }
    }

    private void err(OutputStream out, int code, String msg) {
        try {
            json(out, code, new JSONObject().put("error", msg == null ? "Error" : msg), null);
        } catch (Exception ignored) {
        }
    }

    private Request parse(InputStream in) throws IOException {
        ByteArrayOutputStream head = new ByteArrayOutputStream();
        int state = 0, b;
        while ((b = in.read()) != -1) {
            head.write(b);
            if (head.size() > 65536) throw new HttpError(400, "Headers too large");
            if (b == '\r' && (state == 0 || state == 2)) state++;
            else if (b == '\n' && (state == 1 || state == 3)) state++;
            else state = 0;
            if (state == 4) break;
        }
        if (state != 4) return null;
        String[] lines = new String(head.toByteArray(), StandardCharsets.ISO_8859_1).split("\r\n");
        String[] rl = lines[0].split(" ");
        if (rl.length < 2) return null;
        Request r = new Request();
        r.method = rl[0].toUpperCase(Locale.ROOT);
        String target = rl[1];
        int qi = target.indexOf('?');
        r.path = qi >= 0 ? target.substring(0, qi) : target;
        r.query = qi >= 0 ? target.substring(qi + 1) : "";
        for (String kv : r.query.split("&")) {
            if (kv.isEmpty()) continue;
            int eq = kv.indexOf('=');
            String k = decodeQuery(eq >= 0 ? kv.substring(0, eq) : kv);
            String v = eq >= 0 ? decodeQuery(kv.substring(eq + 1)) : "";
            if (!r.params.containsKey(k)) r.params.put(k, v);
        }
        for (int i = 1; i < lines.length; i++) {
            int c = lines[i].indexOf(':');
            if (c > 0) {
                String k = lines[i].substring(0, c).trim().toLowerCase(Locale.ROOT);
                String v = new String(lines[i].substring(c + 1).trim().getBytes(StandardCharsets.ISO_8859_1), StandardCharsets.UTF_8);
                r.headers.put(k, v);
            }
        }
        String cl = r.header("content-length");
        if (cl != null) {
            try {
                r.contentLength = Long.parseLong(cl.trim());
            } catch (NumberFormatException ignored) {
            }
        }
        r.body = new Limited(in, Math.max(0, r.contentLength));
        return r;
    }

    /** Reads at most n bytes of the request body. */
    static class Limited extends InputStream {
        private final InputStream in;
        private long left;

        Limited(InputStream in, long n) {
            this.in = in;
            this.left = n;
        }

        @Override
        public int read() throws IOException {
            if (left <= 0) return -1;
            int b = in.read();
            if (b >= 0) left--;
            return b;
        }

        @Override
        public int read(byte[] b, int off, int len) throws IOException {
            if (left <= 0) return -1;
            int n = in.read(b, off, (int) Math.min(len, left));
            if (n > 0) left -= n;
            return n;
        }
    }

    // ---------------------------------------------------------------- routes
    private void route(Request r, OutputStream out) throws Exception {
        String p = r.path;
        switch (r.method) {
            case "GET":
            case "HEAD":
                get(r, out, p);
                return;
            case "POST":
                post(r, out, p);
                return;
            case "PUT":
                csrf(r);
                if (!p.equals("/api/upload")) throw new HttpError(404, "Not found");
                require(r, "upload");
                upload(r, out);
                return;
            default:
                throw new HttpError(404, "Not found");
        }
    }

    private void get(Request r, OutputStream out, String p) throws Exception {
        if (p.equals("/") || p.equals("/index.html")) {
            html(out, 200, perm(r) != null ? explorerHtml : joinHtml);
            return;
        }
        if (p.equals("/admin")) {
            if (!isOwner(r)) {
                html(out, 403, "<!doctype html><meta name=viewport content='width=device-width'><body style='font-family:sans-serif;padding:40px;text-align:center'>The dashboard only opens on the sharing device itself.</body>");
                return;
            }
            html(out, 200, adminHtml);
            return;
        }
        if (p.equals("/favicon.ico")) {
            writeHead(out, 204, null, 0, null);
            return;
        }
        if (p.equals("/api/info")) {
            json(out, new JSONObject().put("app", "FileBridge").put("version", VERSION).put("hostname", hostname()));
            return;
        }
        if (p.equals("/api/whoami")) {
            String pm = perm(r);
            Device d = device(r);
            String rn = new File(root).getName();
            json(out, new JSONObject().put("perm", pm == null ? JSONObject.NULL : pm).put("owner", "owner".equals(pm))
                    .put("name", d == null ? JSONObject.NULL : d.name).put("hostname", hostname())
                    .put("root_name", rn.isEmpty() ? root : rn));
            return;
        }
        if (p.equals("/api/request-status")) {
            synchronized (this) {
                Req q = pending.get(r.param("id"));
                if (q == null) {
                    json(out, new JSONObject().put("status", "unknown"));
                    return;
                }
                if (q.status.equals("approved")) {
                    pending.remove(q.id);
                    Map<String, String> h = new HashMap<>();
                    h.put("Set-Cookie", cookieName() + "=" + q.token + "; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax");
                    json(out, 200, new JSONObject().put("status", "approved"), h);
                    return;
                }
                if (q.status.equals("denied")) pending.remove(q.id);
                json(out, new JSONObject().put("status", q.status));
                return;
            }
        }
        if (p.equals("/api/admin/state")) {
            requireOwner(r);
            List<String> ips = refreshIps();
            double now = System.currentTimeMillis() / 1000.0;
            JSONArray pend = new JSONArray();
            JSONArray devs = new JSONArray();
            synchronized (this) {
                for (Req q : pending.values()) {
                    if (!q.status.equals("pending")) continue;
                    pend.put(new JSONObject().put("id", q.id).put("name", q.name).put("ip", q.ip).put("ua", q.ua).put("time", q.time));
                }
                List<Device> list = new ArrayList<>(devices.values());
                Collections.sort(list, (a, b) -> Double.compare(b.lastSeen, a.lastSeen));
                for (Device d : list) {
                    devs.put(new JSONObject().put("id", d.id).put("name", d.name).put("ip", d.ip).put("perm", d.perm)
                            .put("approved_at", d.approvedAt).put("last_seen", d.lastSeen).put("online", now - d.lastSeen < 40));
                }
            }
            json(out, new JSONObject().put("app", "FileBridge").put("version", VERSION).put("root", root).put("port", port)
                    .put("ips", new JSONArray(ips)).put("hostname", hostname()).put("pending", pend).put("devices", devs));
            return;
        }
        if (p.equals("/api/list")) {
            require(r, "read");
            File dir = safePath(r.param("path"));
            if (!dir.isDirectory()) throw new HttpError(404, "Not found");
            JSONArray arr = new JSONArray();
            File[] kids = dir.listFiles();
            if (kids != null) for (File f : kids) {
                boolean isDir = f.isDirectory();
                arr.put(new JSONObject().put("name", f.getName()).put("dir", isDir).put("size", isDir ? 0 : f.length())
                        .put("mtime", f.lastModified() / 1000.0));
            }
            json(out, new JSONObject().put("path", r.param("path")).put("entries", arr));
            return;
        }
        if (p.startsWith("/dl/")) {
            require(r, "read");
            sendFile(r, out, decode(p.substring(4)), "1".equals(r.param("inline")));
            return;
        }
        if (p.equals("/api/zip")) {
            require(r, "read");
            sendZip(r, out);
            return;
        }
        throw new HttpError(404, "Not found");
    }

    private void post(Request r, OutputStream out, String p) throws Exception {
        csrf(r);
        JSONObject b = readJson(r);
        if (p.equals("/api/request-access")) {
            if (isOwner(r)) {
                json(out, new JSONObject().put("status", "owner"));
                return;
            }
            String name = b.optString("name", "").trim();
            if (name.isEmpty()) name = "Unknown device";
            if (name.length() > 60) name = name.substring(0, 60);
            String id;
            synchronized (this) {
                for (Req q : pending.values()) {
                    if (q.ip.equals(r.remoteIp) && q.status.equals("pending")) {
                        q.name = name;
                        json(out, new JSONObject().put("id", q.id));
                        return;
                    }
                }
                int waiting = 0;
                for (Req q : pending.values()) if (q.status.equals("pending")) waiting++;
                if (waiting >= 30) throw new HttpError(403, "Too many waiting requests");
                Req q = new Req();
                q.id = id = token(12);
                q.name = name;
                q.ip = r.remoteIp;
                String ua = r.header("user-agent");
                q.ua = ua == null ? "" : ua.substring(0, Math.min(200, ua.length()));
                q.time = System.currentTimeMillis() / 1000.0;
                pending.put(id, q);
            }
            Listener l = listener;
            if (l != null) l.onAccessRequest(name, r.remoteIp);
            json(out, new JSONObject().put("id", id));
            return;
        }
        if (p.equals("/api/leave")) {
            String tok = r.cookie(cookieName());
            synchronized (this) {
                if (tok != null && devices.remove(tok) != null) saveDevices();
            }
            Map<String, String> h = new HashMap<>();
            h.put("Set-Cookie", cookieName() + "=; Path=/; Max-Age=0");
            json(out, 200, ok(), h);
            return;
        }
        if (p.startsWith("/api/admin/")) {
            requireOwner(r);
            String act = p.substring("/api/admin/".length());
            synchronized (this) {
                switch (act) {
                    case "approve": {
                        Req q = pending.get(b.optString("id"));
                        String perm = validPerm(b.optString("perm")) ? b.optString("perm") : "read";
                        if (q == null || !q.status.equals("pending")) throw new HttpError(404, "Not found");
                        String tok = token(24);
                        Device d = new Device();
                        d.id = hex(4);
                        d.name = q.name;
                        d.ip = q.ip;
                        d.perm = perm;
                        d.approvedAt = d.lastSeen = System.currentTimeMillis() / 1000.0;
                        devices.put(tok, d);
                        q.status = "approved";
                        q.token = tok;
                        saveDevices();
                        break;
                    }
                    case "deny": {
                        Req q = pending.get(b.optString("id"));
                        if (q != null) q.status = "denied";
                        break;
                    }
                    case "revoke":
                    case "set-perm": {
                        String hit = null;
                        for (Map.Entry<String, Device> e : devices.entrySet()) {
                            if (e.getValue().id.equals(b.optString("id"))) hit = e.getKey();
                        }
                        if (hit == null) throw new HttpError(404, "Not found");
                        if (act.equals("revoke")) {
                            devices.remove(hit);
                        } else {
                            if (!validPerm(b.optString("perm"))) throw new HttpError(400, "Unknown permission");
                            devices.get(hit).perm = b.optString("perm");
                        }
                        saveDevices();
                        break;
                    }
                    case "set-root":
                        setRoot(b.optString("path", ""));
                        break;
                    default:
                        throw new HttpError(404, "Not found");
                }
            }
            json(out, ok());
            return;
        }
        if (p.equals("/api/mkdir")) {
            require(r, "upload");
            File dest = new File(safePath(b.optString("path", "")), cleanName(b.optString("name", "")));
            if (dest.exists()) throw new HttpError(409, "Something with that name already exists");
            if (!dest.mkdir()) throw new HttpError(400, "Could not create the folder");
            json(out, ok());
            return;
        }
        if (p.equals("/api/rename")) {
            require(r, "full");
            File src = safePath(b.optString("path", ""));
            if (src.getPath().equals(root)) throw new HttpError(403, "Can't rename the shared folder");
            if (!src.exists()) throw new HttpError(404, "Not found");
            File dest = new File(src.getParentFile(), cleanName(b.optString("name", "")));
            if (dest.exists()) throw new HttpError(409, "Something with that name already exists");
            if (!src.renameTo(dest)) throw new HttpError(400, "Could not rename");
            json(out, ok());
            return;
        }
        if (p.equals("/api/delete")) {
            require(r, "full");
            JSONArray arr = b.optJSONArray("paths");
            if (arr != null) for (int i = 0; i < arr.length(); i++) {
                File f = safePath(arr.optString(i, ""));
                if (f.getPath().equals(root)) throw new HttpError(403, "Can't delete the shared folder");
                deleteTree(f);
            }
            json(out, ok());
            return;
        }
        throw new HttpError(404, "Not found");
    }

    private static void deleteTree(File f) throws HttpError {
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) deleteTree(k);
        }
        if (f.exists() && !f.delete()) throw new HttpError(400, "Could not delete " + f.getName());
    }

    private void upload(Request r, OutputStream out) throws Exception {
        File folder = safePath(r.param("path"));
        List<String> parts = new ArrayList<>();
        for (String x : r.param("name").replace('\\', '/').split("/")) {
            if (!x.isEmpty()) parts.add(cleanName(x));
        }
        if (parts.isEmpty()) throw new HttpError(400, "Missing file name");
        if (r.contentLength < 0) throw new HttpError(411, "Missing Content-Length");
        File destDir = folder;
        for (int i = 0; i < parts.size() - 1; i++) destDir = new File(destDir, parts.get(i));
        String dc = destDir.getCanonicalPath();
        if (!dc.equals(root) && !dc.startsWith(root + "/")) throw new HttpError(403, "Outside the shared folder");
        if (!destDir.isDirectory() && !destDir.mkdirs()) throw new HttpError(400, "Could not create the folder");
        File dest;
        File tmp;
        synchronized (this) {
            dest = uniquePath(new File(destDir, parts.get(parts.size() - 1)));
            tmp = new File(dest.getPath() + ".fbpart");
        }
        long got = 0;
        try {
            try (OutputStream fo = new BufferedOutputStream(new FileOutputStream(tmp), 1 << 16)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = r.body.read(buf)) > 0) {
                    fo.write(buf, 0, n);
                    got += n;
                }
            }
            if (got != r.contentLength) throw new HttpError(400, "Upload incomplete");
            if (!tmp.renameTo(dest)) throw new HttpError(400, "Could not save the file");
        } finally {
            if (tmp.exists()) tmp.delete();
        }
        json(out, new JSONObject().put("ok", true).put("name", dest.getName()));
    }

    private static String mime(String name) {
        int dot = name.lastIndexOf('.');
        String ext = dot >= 0 ? name.substring(dot + 1).toLowerCase(Locale.ROOT) : "";
        String m = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext);
        if (m == null) {
            switch (ext) {
                case "txt": case "log": case "md": case "csv": case "py": case "js": case "json": case "xml": case "sh":
                    return "text/plain; charset=utf-8";
                case "mkv":
                    return "video/x-matroska";
                default:
                    return "application/octet-stream";
            }
        }
        return m.startsWith("text/") ? m + "; charset=utf-8" : m;
    }

    private void sendFile(Request r, OutputStream out, String rel, boolean inline) throws Exception {
        File f = safePath(rel);
        if (!f.isFile()) throw new HttpError(404, "Not found");
        long size = f.length(), start = 0, end = size - 1;
        int code = 200;
        String rng = r.header("range");
        if (rng != null && rng.startsWith("bytes=")) {
            String spec = rng.substring(6).split(",")[0].trim();
            int dash = spec.indexOf('-');
            if (dash >= 0) {
                String a = spec.substring(0, dash).trim(), z = spec.substring(dash + 1).trim();
                try {
                    if (a.isEmpty() && !z.isEmpty()) {
                        start = Math.max(0, size - Long.parseLong(z));
                        code = 206;
                    } else if (!a.isEmpty()) {
                        start = Long.parseLong(a);
                        end = z.isEmpty() ? size - 1 : Math.min(Long.parseLong(z), size - 1);
                        code = 206;
                    }
                } catch (NumberFormatException ignored) {
                    code = 200;
                    start = 0;
                    end = size - 1;
                }
                if (code == 206 && (start >= size || start > end)) {
                    Map<String, String> h = new HashMap<>();
                    h.put("Content-Range", "bytes */" + size);
                    writeHead(out, 416, null, 0, h);
                    return;
                }
            }
        }
        long len = size == 0 ? 0 : end - start + 1;
        Map<String, String> h = new LinkedHashMap<>();
        h.put("Accept-Ranges", "bytes");
        h.put("Content-Disposition", (inline ? "inline" : "attachment") + "; filename*=UTF-8''" + encode(f.getName()));
        if (code == 206) h.put("Content-Range", "bytes " + start + "-" + end + "/" + size);
        writeHead(out, code, mime(f.getName()), len, h);
        if (r.method.equals("HEAD") || len == 0) return;
        try (RandomAccessFile raf = new RandomAccessFile(f, "r")) {
            raf.seek(start);
            byte[] buf = new byte[1 << 16];
            long left = len;
            while (left > 0) {
                int n = raf.read(buf, 0, (int) Math.min(buf.length, left));
                if (n <= 0) break;
                out.write(buf, 0, n);
                left -= n;
            }
        }
        out.flush();
    }

    private void sendZip(Request r, OutputStream out) throws Exception {
        File dir = safePath(r.param("path"));
        if (!dir.isDirectory()) throw new HttpError(404, "Not found");
        String name = dir.getName().isEmpty() ? "shared" : dir.getName();
        Map<String, String> h = new HashMap<>();
        h.put("Content-Disposition", "attachment; filename*=UTF-8''" + encode(name) + ".zip");
        writeHead(out, 200, "application/zip", -1, h);
        ZipOutputStream zo = new ZipOutputStream(out);
        zo.setLevel(Deflater.BEST_SPEED);
        addToZip(zo, dir, name);
        zo.finish();
        out.flush();
    }

    private static void addToZip(ZipOutputStream zo, File dir, String prefix) throws IOException {
        File[] kids = dir.listFiles();
        if (kids == null) return;
        byte[] buf = new byte[1 << 16];
        for (File f : kids) {
            String entry = prefix + "/" + f.getName();
            if (f.isDirectory()) {
                addToZip(zo, f, entry);
            } else if (f.isFile()) {
                try (InputStream in = new FileInputStream(f)) {
                    ZipEntry ze = new ZipEntry(entry);
                    ze.setTime(f.lastModified());
                    zo.putNextEntry(ze);
                    int n;
                    while ((n = in.read(buf)) > 0) zo.write(buf, 0, n);
                    zo.closeEntry();
                } catch (FileNotFoundException ignored) {
                    // unreadable file: skip it
                }
            }
        }
    }
}
