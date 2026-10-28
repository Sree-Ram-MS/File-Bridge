#!/usr/bin/env python3
"""
FileBridge - share folders on your local network, with permission control
and a proper file explorer UI. A friendlier `python3 -m http.server`.

Runs on Ubuntu/Linux, Windows and macOS with plain Python 3.8+ (no pip installs).
Any device on the same network (Android, iPhone, laptops) connects from a browser.

Usage:
    python3 filebridge.py                 # share the current folder on port 8000
    python3 filebridge.py ~/Downloads     # share a specific folder
    python3 filebridge.py . --port 9000 --no-browser
"""
import argparse
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP = "FileBridge"
VERSION = "0.3.0"
CHUNK = 256 * 1024
STATE_DIR = os.path.join(os.path.expanduser("~"), ".filebridge")
LEVELS = {
    "read": {"read"},
    "upload": {"read", "upload"},
    "full": {"read", "upload", "full"},
    "owner": {"read", "upload", "full", "owner"},
}
PERM_LABEL = {"read": "View & download", "upload": "View & upload", "full": "Full access"}


class AuthError(Exception):
    pass


# --------------------------------------------------------------------------- state
def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


# --------------------------------------------------------------------------- QR code
# Pure-stdlib QR encoder, kept deliberately small: byte mode, error level M,
# versions 1-3 only. That covers every dashboard URL (http://<LAN IP>:<port>,
# at most 28 characters, well under the 42-byte version 3 limit) with no new
# dependency, so the packaged app still runs on plain Python 3.8+.
_QR_VERSIONS = {
    # version: (total codewords, EC codewords, data codewords, alignment centres)
    1: (26, 10, 16, []),
    2: (44, 16, 28, [6, 18]),
    3: (70, 26, 44, [6, 22]),
}
_QR_CAPACITY = {1: 14, 2: 26, 3: 42}


def _qr_gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_QR_EXP, _QR_LOG = _qr_gf_tables()


def _qr_mul(a, b):
    return 0 if a == 0 or b == 0 else _QR_EXP[_QR_LOG[a] + _QR_LOG[b]]


def _qr_rs_generator(degree):
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _qr_mul(c, _QR_EXP[i])
        poly = nxt
    return poly


def _qr_rs_remainder(data, degree):
    gen = _qr_rs_generator(degree)
    msg = list(data) + [0] * degree
    for i in range(len(data)):
        coef = msg[i]
        if coef:
            for j in range(len(gen)):
                msg[i + j] ^= _qr_mul(gen[j], coef)
    return msg[-degree:]


def _qr_format_bits(mask):
    # Error level M = 00, followed by the 3 mask bits, BCH + 0x5412 mask.
    data = mask  # (0b00 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _qr_build_matrix(text):
    data = text.encode("utf-8")
    version = next((v for v in (1, 2, 3) if len(data) <= _QR_CAPACITY[v]), None)
    if version is None:
        raise ValueError("Text too long for QR code")
    total, ec_len, data_len, align = _QR_VERSIONS[version]
    bits = []
    bits.extend([0, 1, 0, 0])  # byte mode
    for i in range(7, -1, -1):
        bits.append((len(data) >> i) & 1)
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    bits.extend([0] * min(4, data_len * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    codewords = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        codewords.append(v)
    pads = (0xEC, 0x11)
    pi = 0
    while len(codewords) < data_len:
        codewords.append(pads[pi % 2])
        pi += 1
    all_cw = codewords + _qr_rs_remainder(codewords, ec_len)
    all_bits = []
    for cw in all_cw:
        for i in range(7, -1, -1):
            all_bits.append((cw >> i) & 1)

    size = 17 + 4 * version
    mat = [[None] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def set_func(r, c, v):
        mat[r][c] = v
        func[r][c] = True

    finder = [[1, 1, 1, 1, 1, 1, 1], [1, 0, 0, 0, 0, 0, 1], [1, 0, 1, 1, 1, 0, 1],
              [1, 0, 1, 1, 1, 0, 1], [1, 0, 1, 1, 1, 0, 1], [1, 0, 0, 0, 0, 0, 1],
              [1, 1, 1, 1, 1, 1, 1]]
    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):
        for r in range(7):
            for c in range(7):
                set_func(r0 + r, c0 + c, finder[r][c])
        # One white separator row and column around each finder pattern.
        if r0 == 0:  # top finders: separator row below
            for c in range(8):
                set_func(7, c0 + c if c0 == 0 else size - 8 + c, 0)
        else:  # bottom-left: separator row above
            for c in range(8):
                set_func(size - 8, c, 0)
        if c0 == 0:  # left finders: separator column to the right
            start = 0 if r0 == 0 else size - 8
            for r in range(8):
                set_func(start + r, 7, 0)
        else:  # top-right: separator column to the left
            for r in range(8):
                set_func(r, size - 8, 0)
    for i in range(8, size - 8):
        set_func(6, i, 1 if i % 2 == 0 else 0)
        set_func(i, 6, 1 if i % 2 == 0 else 0)
    align_pat = [[1, 1, 1, 1, 1], [1, 0, 0, 0, 1], [1, 0, 1, 0, 1],
                 [1, 0, 0, 0, 1], [1, 1, 1, 1, 1]]
    for r in align:
        for c in align:
            if (r <= 8 and c <= 8) or (r <= 8 and c >= size - 9) or (r >= size - 9 and c <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    set_func(r + dr, c + dc, align_pat[dr + 2][dc + 2])
    # Reserve format-info cells and the dark module; values are filled later.
    for c in range(9):
        if c != 6:
            set_func(8, c, 0)
    for r in range(9):
        if r != 6:
            set_func(r, 8, 0)
    for c in range(8):
        set_func(8, size - 8 + c, 0)
    for r in range(7):
        set_func(size - 7 + r, 8, 0)
    set_func(4 * version + 9, 8, 1)

    # Data placement, zig-zag from the bottom right.
    i = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        for vert in range(size):
            row = size - 1 - vert if upward else vert
            for j in range(2):
                cc = col - j
                if not func[row][cc]:
                    mat[row][cc] = all_bits[i] if i < len(all_bits) else 0
                    i += 1
        upward = not upward
        col -= 2

    def mask_ok(mask, r, c):
        if mask == 0:
            return (r + c) % 2 == 0
        if mask == 1:
            return r % 2 == 0
        if mask == 2:
            return c % 3 == 0
        if mask == 3:
            return (r + c) % 3 == 0
        if mask == 4:
            return (r // 2 + c // 3) % 2 == 0
        if mask == 5:
            return (r * c) % 2 + (r * c) % 3 == 0
        if mask == 6:
            return ((r * c) % 2 + (r * c) % 3) % 2 == 0
        return ((r + c) % 2 + (r * c) % 3) % 2 == 0

    def penalty(m):
        score = 0
        for r in range(size):
            run = 1
            for c in range(1, size):
                if m[r][c] == m[r][c - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
        for c in range(size):
            run = 1
            for r in range(1, size):
                if m[r][c] == m[r - 1][c]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            if run >= 5:
                score += 3 + run - 5
        for r in range(size - 1):
            for c in range(size - 1):
                if m[r][c] == m[r + 1][c] == m[r][c + 1] == m[r + 1][c + 1]:
                    score += 3
        for line in ([m[r] for r in range(size)] + [[m[r][c] for r in range(size)] for c in range(size)]):
            for j in range(len(line) - 10):
                seg = line[j:j + 11]
                if seg == [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0] or seg == [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]:
                    score += 40
        dark = sum(sum(row) for row in m)
        # floor(|dark% - 50| / 5) * 10, with dark% = dark * 100 / size**2.
        score += (abs(dark * 20 - size * size * 10) // (size * size)) * 10
        return score

    best = None
    for mask in range(8):
        trial = [row[:] for row in mat]
        for r in range(size):
            for c in range(size):
                if not func[r][c] and mask_ok(mask, r, c):
                    trial[r][c] ^= 1
        fmt = _qr_format_bits(mask)
        # Format info placement (ISO 18004, section 7.9): the vertical
        # copy beside the top-left finder carries bits 0-7 top to bottom
        # (skipping the timing row), the horizontal copy carries bits
        # 14-7 left to right (skipping the timing column), and the two
        # second copies mirror them at the other finders.
        for bit, row in enumerate((0, 1, 2, 3, 4, 5, 7, 8)):
            trial[row][8] = (fmt >> bit) & 1
        for bit, col in zip(range(7, 15), (8, 7, 5, 4, 3, 2, 1, 0)):
            trial[8][col] = (fmt >> bit) & 1
        for bit in range(8):
            trial[8][size - 1 - bit] = (fmt >> bit) & 1
        # Bottom-left copy: bits 8-14 only; the cell just above it
        # (row size-8, column 8) is the dark module, set earlier.
        for bit in range(8, 15):
            trial[size - 15 + bit][8] = (fmt >> bit) & 1
        sc = penalty(trial)
        if best is None or sc < best[0]:
            best = (sc, trial)
    return best[1]


def qr_svg(text):
    mat = _qr_build_matrix(text)
    size = len(mat)
    total = size + 8  # 4-module quiet zone on each side
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" shape-rendering="crispEdges">' % (total, total),
             '<rect width="%d" height="%d" fill="white"/>' % (total, total), '<path fill="black" d="']
    for r in range(size):
        for c in range(size):
            if mat[r][c]:
                parts.append("M%d %dh1v1h-1z" % (c + 4, r + 4))
    parts.append('"/></svg>')
    return "".join(parts)


class State:
    def __init__(self, root, port):
        self.root = os.path.realpath(root)
        self.port = port
        self.lock = threading.Lock()
        self.pending = {}   # request id -> info
        self.devices = {}   # token -> info
        self.transfers = []  # transfer dicts, newest last; see add_transfer
        self.owner_ips = set(local_ips())
        self.file = os.path.join(STATE_DIR, "devices.json")
        self.transfers_file = os.path.join(STATE_DIR, "transfers.json")
        self.load()

    def load(self):
        try:
            with open(self.file, "r", encoding="utf-8") as f:
                self.devices = json.load(f)
        except (OSError, ValueError):
            self.devices = {}
        try:
            with open(self.transfers_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # A previous run may have been killed mid-transfer; never show a
            # stale transfer as still in progress after a restart.
            for t in loaded:
                if t.get("status") in ("sending", "receiving"):
                    t["status"] = "failed"
            self.transfers = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self.transfers = []

    def save_transfers(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.transfers_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.transfers[-500:], f, indent=1)
            os.replace(tmp, self.transfers_file)
        except OSError:
            pass

    def add_transfer(self, direction, name, size, device_id, device_name):
        # direction: "send" (this device sends a file out) or "receive"
        # (a device uploads a file in). Status starts as the in-progress
        # form and is updated to "sent"/"received"/"failed" on completion.
        t = {"id": secrets.token_hex(6), "direction": direction, "name": name,
             "size": size, "bytes": 0, "status": "sending" if direction == "send" else "receiving",
             "device_id": device_id, "device": device_name,
             "started": time.time(), "updated": time.time()}
        with self.lock:
            self.transfers.append(t)
            self.transfers = self.transfers[-500:]
        self.save_transfers()
        return t

    def update_transfer(self, tid, **kw):
        with self.lock:
            for t in self.transfers:
                if t["id"] == tid:
                    t.update(kw)
                    t["updated"] = time.time()
                    break
        self.save_transfers()

    def finish_transfer(self, tid, status):
        self.update_transfer(tid, status=status)

    def save(self):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.devices, f, indent=1)
            os.replace(tmp, self.file)
        except OSError as e:
            print("  ! could not save devices:", e)


STATE = None  # set in main()


def load_config():
    try:
        with open(os.path.join(STATE_DIR, "config.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except OSError:
        pass


def default_folder():
    """Folder to share when none is given on the command line."""
    last = load_config().get("root")
    if last and os.path.isdir(last):
        return last
    if getattr(sys, "frozen", False) or os.environ.get("FILEBRIDGE_PACKAGED"):
        dl = os.path.join(os.path.expanduser("~"), "Downloads")
        return dl if os.path.isdir(dl) else os.path.expanduser("~")
    return os.getcwd()


def clean_name(n):
    n = (n or "").strip()
    if os.name == "nt":
        for ch in '<>:"|?*':
            n = n.replace(ch, "_")
        n = n.rstrip(". ")
    if not n or n in (".", "..") or "/" in n or "\\" in n or "\x00" in n or len(n) > 255:
        raise ValueError("Invalid name")
    return n


def unique_path(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    # ---- helpers
    @property
    def cookie_name(self):
        return f"fb_token_{STATE.port}"

    def client_ip(self):
        ip = self.client_address[0]
        return ip[7:] if ip.startswith("::ffff:") else ip

    def is_owner(self):
        ip = self.client_ip()
        return ip.startswith("127.") or ip == "::1" or ip in STATE.owner_ips

    def token(self):
        c = cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        return c[self.cookie_name].value if self.cookie_name in c else None

    def device(self):
        tok = self.token()
        if not tok:
            return None, None
        with STATE.lock:
            d = STATE.devices.get(tok)
            if d:
                d["last_seen"] = time.time()
                d["ip"] = self.client_ip()
            return tok, d

    def perm(self):
        if self.is_owner():
            return "owner"
        _, d = self.device()
        return d["perm"] if d else None

    def transfer_device(self):
        # (device_id, display name) for the current requester, for transfers.
        if self.is_owner():
            return "owner", "Owner (this device)"
        _, d = self.device()
        if d:
            return d["id"], d["name"]
        return None, self.client_ip()

    def require(self, action):
        p = self.perm()
        if not p:
            raise AuthError()
        if action not in LEVELS[p]:
            raise PermissionError("Your access level doesn't allow this")
        return p

    def require_owner(self):
        if not self.is_owner():
            raise PermissionError("Only the sharing device can do this")

    def csrf(self):
        if self.headers.get("X-FB") != "1":
            raise PermissionError("Missing request header")

    def safe_path(self, rel):
        rel = (rel or "").replace("\\", "/").strip("/")
        full = os.path.realpath(os.path.join(STATE.root, rel))
        if os.path.commonpath([STATE.root, full]) != STATE.root:
            raise PermissionError("Outside the shared folder")
        return full

    def send_json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            raise ValueError("Request too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise ValueError("Bad JSON")

    def dispatch(self, fn):
        try:
            fn()
        except AuthError:
            self.err(401, "Not connected. Request access first.")
        except PermissionError as e:
            self.err(403, str(e) or "Permission denied")
        except FileNotFoundError:
            self.err(404, "Not found")
        except FileExistsError:
            self.err(409, "Something with that name already exists")
        except IsADirectoryError:
            self.err(400, "That is a folder")
        except ValueError as e:
            self.err(400, str(e))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa
            traceback.print_exc()
            self.err(500, f"Server error: {e}")

    def err(self, code, msg):
        try:
            self.send_json({"error": msg}, code)
        except Exception:
            pass

    # ---- verbs
    def do_GET(self):
        self.dispatch(self._get)

    def do_POST(self):
        self.dispatch(self._post)

    def do_PUT(self):
        self.dispatch(self._put)

    # ---- GET routes
    def _get(self):
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        q = urllib.parse.parse_qs(u.query)

        def qp(k, d=""):
            return q.get(k, [d])[0]

        if p in ("/", "/index.html"):
            return self.send_html(EXPLORER_HTML if self.perm() else JOIN_HTML)
        if p == "/admin":
            if not self.is_owner():
                return self.send_html(page("Not allowed", "<div class='center'><div class='card'>The dashboard only opens on the sharing device itself.</div></div>", ""), 403)
            return self.send_html(ADMIN_HTML)
        if p == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if p == "/api/info":
            return self.send_json({"app": APP, "version": VERSION, "hostname": socket.gethostname()})
        if p == "/api/whoami":
            perm = self.perm()
            _, d = self.device()
            return self.send_json({
                "perm": perm, "owner": perm == "owner",
                "name": d["name"] if d else None,
                "hostname": socket.gethostname(),
                "root_name": os.path.basename(STATE.root) or STATE.root,
            })
        if p == "/api/request-status":
            rid = qp("id")
            with STATE.lock:
                r = STATE.pending.get(rid)
                if not r:
                    return self.send_json({"status": "unknown"})
                if r["status"] == "approved":
                    STATE.pending.pop(rid, None)
                    ck = f"{self.cookie_name}={r['token']}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                    return self.send_json({"status": "approved"}, headers={"Set-Cookie": ck})
                if r["status"] == "denied":
                    STATE.pending.pop(rid, None)
                return self.send_json({"status": r["status"]})
        if p == "/api/admin/state":
            self.require_owner()
            STATE.owner_ips = set(local_ips())
            now = time.time()
            with STATE.lock:
                pend = [{k: r[k] for k in ("id", "name", "ip", "ua", "time")}
                        for r in STATE.pending.values() if r["status"] == "pending"]
                devs = [{"id": d["id"], "name": d["name"], "ip": d.get("ip", ""), "perm": d["perm"],
                         "approved_at": d.get("approved_at", 0), "last_seen": d.get("last_seen", 0),
                         "online": now - d.get("last_seen", 0) < 40}
                        for d in STATE.devices.values()]
            return self.send_json({"app": APP, "version": VERSION, "root": STATE.root, "port": STATE.port,
                                   "ips": sorted(STATE.owner_ips, key=lambda x: (not x.startswith("192.168."), x)),
                                   "hostname": socket.gethostname(), "pending": pend,
                                   "devices": sorted(devs, key=lambda d: -d["last_seen"])})
        if p == "/qr.svg":
            # Owner-only: the QR just encodes a link the dashboard already
            # shows, but only generate it for this server's own LAN address.
            self.require_owner()
            raw = qp("u")
            u = urllib.parse.urlsplit(raw)
            allowed = set(local_ips()) | set(STATE.owner_ips)
            if (u.scheme != "http" or u.hostname not in allowed or u.port != STATE.port
                    or u.path not in ("", "/") or u.query or u.fragment or u.username or u.password):
                raise ValueError("Not a FileBridge address for this device")
            body = qr_svg(f"http://{u.hostname}:{STATE.port}").encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if p == "/api/transfers":
            self.require("read")
            if self.is_owner():
                items = list(STATE.transfers)
            else:
                _, d = self.device()
                did = d["id"] if d else None
                items = [t for t in STATE.transfers if t.get("device_id") == did]
            return self.send_json({"transfers": list(reversed(items))[:200]})

        # ---- everything below needs access
        if p == "/api/list":
            self.require("read")
            full = self.safe_path(qp("path"))
            if not os.path.isdir(full):
                raise FileNotFoundError()
            out = []
            with os.scandir(full) as it:
                for e in it:
                    try:
                        isdir = e.is_dir()
                        st = e.stat()
                    except OSError:
                        continue
                    out.append({"name": e.name, "dir": isdir, "size": 0 if isdir else st.st_size, "mtime": st.st_mtime})
            return self.send_json({"path": qp("path"), "entries": out})
        if p.startswith("/dl/"):
            self.require("read")
            return self.send_file(urllib.parse.unquote(p[4:]), qp("inline") == "1")
        if p == "/api/zip":
            self.require("read")
            return self.send_zip(qp("path"))
        self.err(404, "Not found")

    def send_file(self, rel, inline):
        full = self.safe_path(rel)
        if not os.path.isfile(full):
            raise FileNotFoundError()
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            s, _, e = rng[6:].split(",")[0].strip().partition("-")
            try:
                if s == "":
                    start = max(0, size - int(e))
                else:
                    start = int(s)
                    end = min(int(e), size - 1) if e else size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            except ValueError:
                start, end = 0, size - 1
        length = max(0, end - start + 1)
        fname = urllib.parse.quote(os.path.basename(full))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{fname}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        # Previews (inline) are not transfers; real downloads are.
        tr = None
        if not inline:
            did, dname = self.transfer_device()
            tr = STATE.add_transfer("send", os.path.basename(full), length, did, dname)
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
                    if tr is not None:
                        tr["bytes"] += len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            if tr is not None:
                STATE.finish_transfer(tr["id"], "failed")
            return
        if tr is not None:
            tr["bytes"] = length
            STATE.finish_transfer(tr["id"], "sent")

    def send_zip(self, rel):
        full = self.safe_path(rel)
        if not os.path.isdir(full):
            raise FileNotFoundError()
        name = os.path.basename(full) or "shared"
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for dirpath, _dirs, files in os.walk(full):
                    for fn in files:
                        fp = os.path.join(dirpath, fn)
                        real = os.path.realpath(fp)
                        if os.path.commonpath([STATE.root, real]) != STATE.root:
                            continue
                        try:
                            z.write(fp, os.path.join(name, os.path.relpath(fp, full)))
                        except OSError:
                            pass
            size = tmp.tell()
            tmp.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}.zip")
            self.end_headers()
            did, dname = self.transfer_device()
            tr = STATE.add_transfer("send", name + ".zip", size, did, dname)
            try:
                while True:
                    chunk = tmp.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    tr["bytes"] += len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                STATE.finish_transfer(tr["id"], "failed")
                return
            tr["bytes"] = size
            STATE.finish_transfer(tr["id"], "sent")

    # ---- POST routes
    def _post(self):
        self.csrf()
        p = urllib.parse.urlsplit(self.path).path
        b = self.read_json()

        if p == "/api/request-access":
            if self.is_owner():
                return self.send_json({"status": "owner"})
            name = (str(b.get("name") or "").strip() or "Unknown device")[:60]
            ip = self.client_ip()
            with STATE.lock:
                for r in STATE.pending.values():
                    if r["ip"] == ip and r["status"] == "pending":
                        r["name"] = name
                        return self.send_json({"id": r["id"]})
                if sum(1 for r in STATE.pending.values() if r["status"] == "pending") >= 30:
                    raise PermissionError("Too many waiting requests")
                rid = secrets.token_urlsafe(12)
                STATE.pending[rid] = {"id": rid, "name": name, "ip": ip, "time": time.time(), "status": "pending",
                                      "ua": (self.headers.get("User-Agent") or "")[:200]}
            print(f"\a  -> Access request from '{name}' ({ip}). Approve it in the dashboard.")
            return self.send_json({"id": rid})

        if p == "/api/leave":
            tok, d = self.device()
            if d:
                with STATE.lock:
                    STATE.devices.pop(tok, None)
                    STATE.save()
            ck = f"{self.cookie_name}=; Path=/; Max-Age=0"
            return self.send_json({"ok": True}, headers={"Set-Cookie": ck})

        if p.startswith("/api/admin/"):
            self.require_owner()
            act = p[len("/api/admin/"):]
            if act == "shutdown":
                # Close app: stop sharing and stop the server process (or,
                # for the packaged desktop apps, the app window via its
                # server-thread watcher). Reply first, then shut down so
                # the response actually reaches the dashboard.
                def _stop():
                    time.sleep(0.3)
                    try:
                        self.server.shutdown()
                    except Exception:
                        pass
                threading.Thread(target=_stop, daemon=True).start()
                print("  Stopped via dashboard. Nobody can reach your files now.")
                return self.send_json({"ok": True})
            with STATE.lock:
                if act == "approve":
                    r = STATE.pending.get(b.get("id"))
                    perm = b.get("perm") if b.get("perm") in PERM_LABEL else "read"
                    if not r or r["status"] != "pending":
                        raise FileNotFoundError()
                    tok = secrets.token_urlsafe(24)
                    STATE.devices[tok] = {"id": secrets.token_hex(4), "name": r["name"], "ip": r["ip"], "perm": perm,
                                          "approved_at": time.time(), "last_seen": time.time()}
                    r["status"], r["token"] = "approved", tok
                    STATE.save()
                    print(f"  ok Approved '{r['name']}' ({PERM_LABEL[perm]})")
                elif act == "deny":
                    r = STATE.pending.get(b.get("id"))
                    if r:
                        r["status"] = "denied"
                elif act in ("revoke", "set-perm"):
                    hit = [t for t, d in STATE.devices.items() if d["id"] == b.get("id")]
                    if not hit:
                        raise FileNotFoundError()
                    if act == "revoke":
                        STATE.devices.pop(hit[0])
                    else:
                        if b.get("perm") not in PERM_LABEL:
                            raise ValueError("Unknown permission")
                        STATE.devices[hit[0]]["perm"] = b["perm"]
                    STATE.save()
                elif act == "set-root":
                    path = os.path.realpath(os.path.expanduser(str(b.get("path") or "")))
                    if not os.path.isdir(path):
                        raise ValueError("That folder doesn't exist")
                    STATE.root = path
                    cfg = load_config()
                    cfg["root"] = path
                    save_config(cfg)
                    print(f"  ok Now sharing {path}")
                else:
                    raise FileNotFoundError()
            return self.send_json({"ok": True})

        if p == "/api/mkdir":
            self.require("upload")
            parent = self.safe_path(b.get("path"))
            dest = os.path.join(parent, clean_name(b.get("name")))
            os.mkdir(dest)
            return self.send_json({"ok": True})
        if p == "/api/rename":
            self.require("full")
            src = self.safe_path(b.get("path"))
            if src == STATE.root:
                raise PermissionError("Can't rename the shared folder")
            dest = os.path.join(os.path.dirname(src), clean_name(b.get("name")))
            if os.path.exists(dest):
                raise FileExistsError()
            os.rename(src, dest)
            return self.send_json({"ok": True})
        if p == "/api/delete":
            self.require("full")
            for rel in b.get("paths") or []:
                full = self.safe_path(rel)
                if full == STATE.root:
                    raise PermissionError("Can't delete the shared folder")
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
            return self.send_json({"ok": True})
        self.err(404, "Not found")

    # ---- PUT (upload, streamed)
    def _put(self):
        self.csrf()
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/api/upload":
            return self.err(404, "Not found")
        self.require("upload")
        q = urllib.parse.parse_qs(u.query)
        folder = self.safe_path(q.get("path", [""])[0])
        parts = [clean_name(x) for x in q.get("name", [""])[0].replace("\\", "/").split("/") if x not in ("",)]
        if not parts:
            raise ValueError("Missing file name")
        dest_dir = os.path.join(folder, *parts[:-1])
        if os.path.commonpath([STATE.root, os.path.realpath(dest_dir)]) != STATE.root:
            raise PermissionError("Outside the shared folder")
        os.makedirs(dest_dir, exist_ok=True)
        dest = unique_path(os.path.join(dest_dir, parts[-1]))
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise ValueError("Missing Content-Length")
        tmp = dest + ".fbpart"
        left = length
        did, dname = self.transfer_device()
        tr = STATE.add_transfer("receive", "/".join(parts), length, did, dname)
        try:
            with open(tmp, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
                    tr["bytes"] += len(chunk)
            if left:
                raise ConnectionResetError()
            os.replace(tmp, dest)
        except Exception:
            STATE.finish_transfer(tr["id"], "failed")
            raise
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        tr["bytes"] = length
        STATE.finish_transfer(tr["id"], "received")
        return self.send_json({"ok": True, "name": os.path.basename(dest)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


# --------------------------------------------------------------------------- UI
CSS = r"""
:root{--bg:#0e1015;--panel:#161920;--panel2:#1d212a;--line:#2a2f3b;--text:#e9ebf1;--muted:#8a92a6;--accent:#6b8bff;--accent2:#5272f5;--ok:#36c98a;--warn:#f2a93b;--err:#f0505f;--r:14px}
@media (prefers-color-scheme:light){:root{--bg:#f4f5f9;--panel:#fff;--panel2:#eef0f5;--line:#dfe2ea;--text:#151821;--muted:#666f84}}
*{box-sizing:border-box}[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,sans-serif}
a{color:var(--accent);text-decoration:none}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--text);padding:8px 14px;border-radius:10px;transition:border-color .15s,background .15s;white-space:nowrap}
button:hover{border-color:var(--accent)}
.btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn-primary:hover{background:var(--accent2)}
.btn-danger{color:var(--err)}.btn-danger-fill{background:var(--err);border-color:var(--err);color:#fff}
.btn-ghost{background:transparent;border-color:transparent}.btn-ghost:hover{background:var(--panel2)}
input,select{font:inherit;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:10px;padding:8px 12px;outline:none;min-width:0}
input:focus,select:focus{border-color:var(--accent)}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--bg);border-bottom:1px solid var(--line)}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px;letter-spacing:.2px}
.logo i{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#6b8bff,#a66bff);display:grid;place-items:center;color:#fff;font-style:normal}
.chip{padding:3px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);color:var(--muted);font-size:12px;white-space:nowrap}
.chip.ok{color:var(--ok);border-color:color-mix(in srgb,var(--ok) 40%,var(--line))}
.spacer{flex:1}.muted{color:var(--muted)}.full{width:100%}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:20px}
.center{min-height:100vh;display:grid;place-items:center;padding:20px}
.row-end{display:flex;justify-content:flex-end;gap:8px;margin-top:16px}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;z-index:60;padding:16px}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:20px;width:min(420px,100%)}
.modal h3{margin:0 0 8px;font-size:17px}.modal input{margin-top:8px}
.tmodal{width:min(680px,100%);max-height:85vh;display:flex;flex-direction:column}
.tmodal h3{margin:0 0 12px}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.tabs button{padding:6px 12px;border-radius:999px}.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.tlist{overflow:auto;flex:1;min-height:120px}
.trow{display:grid;grid-template-columns:28px 1fr auto;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;margin-bottom:8px;background:var(--panel2)}
.trow .tnm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .tmeta{color:var(--muted);font-size:12px}
.trow .tst{font-size:12px;font-weight:600;text-transform:capitalize}
.trow .tst.sending,.trow .tst.receiving{color:var(--warn)}.trow .tst.sent,.trow .tst.received{color:var(--ok)}.trow .tst.failed{color:var(--err)}
.tbar{height:4px;background:var(--panel);border-radius:4px;margin-top:6px;overflow:hidden;grid-column:2/3}.tbar i{display:block;height:100%;background:var(--accent)}
#toasts{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);display:flex;flex-direction:column;gap:8px;z-index:90}
.toast{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--ok);padding:10px 16px;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.3);animation:up .2s}
.toast.err{border-left-color:var(--err)}
@keyframes up{from{transform:translateY(10px);opacity:0}}
.spin{width:18px;height:18px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp 0.8s linear infinite;display:inline-block;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
"""

COMMON_JS = r"""
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,opts={}){opts.headers=Object.assign({'X-FB':'1'},opts.headers||{});const r=await fetch(url,opts);let j={};try{j=await r.json()}catch(_){}if(r.status===401&&!url.includes('request')){location.href='/';throw new Error('Not connected')}if(!r.ok)throw new Error(j.error||r.statusText);return j}
const post=(u,b)=>api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
function toast(msg,type){const t=document.createElement('div');t.className='toast '+(type||'');t.textContent=msg;$('#toasts').appendChild(t);setTimeout(()=>t.remove(),3200)}
function copy(text){const ok=()=>toast('Copied: '+text);if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(ok);return}const ta=document.createElement('textarea');ta.value=text;ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok()}catch(_){prompt('Copy this:',text)}ta.remove()}
function fmtSize(n){if(n<1024)return n+' B';const u=['KB','MB','GB','TB'];let i=-1;do{n/=1024;i++}while(n>=1024&&i<u.length-1);return n.toFixed(n<10?1:0)+' '+u[i]}
function fmtDate(t){const d=new Date(t*1000);return d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})+', '+d.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}
function ago(t){const s=Math.max(0,Date.now()/1000-t);if(s<60)return 'just now';if(s<3600)return Math.floor(s/60)+' min ago';if(s<86400)return Math.floor(s/3600)+' h ago';return Math.floor(s/86400)+' d ago'}
function dialog(o){return new Promise(res=>{const bg=document.createElement('div');bg.className='modal-bg';const hasIn=o.input!==undefined;bg.innerHTML=`<div class="modal"><h3>${esc(o.title)}</h3>${o.text?`<p class="muted">${esc(o.text)}</p>`:''}${hasIn?`<input class="full" value="${esc(o.input)}" placeholder="${esc(o.placeholder||'')}">`:''}<div class="row-end"><button class="cancel">Cancel</button><button class="okb ${o.danger?'btn-danger-fill':'btn-primary'}">${esc(o.ok||'OK')}</button></div></div>`;document.body.appendChild(bg);const inp=bg.querySelector('input');const done=v=>{bg.remove();res(v)};bg.querySelector('.cancel').onclick=()=>done(null);bg.querySelector('.okb').onclick=()=>done(inp?inp.value.trim():true);bg.onclick=e=>{if(e.target===bg)done(null)};if(inp){inp.focus();const d=o.input.lastIndexOf('.');inp.setSelectionRange(0,d>0?d:o.input.length);inp.onkeydown=e=>{if(e.key==='Enter')done(inp.value.trim());if(e.key==='Escape')done(null)}}})}
// ---- Transfers menu: sent / sending, received / receiving (and failed)
let TFILTER='all',TTIMER=null;
function tdir(t){return t.direction==='send'?'&#8593;':'&#8595;'}
function tlabel(t){return t.direction==='send'?(t.status==='sent'?'Sent':t.status==='sending'?'Sending':'Failed'):(t.status==='received'?'Received':t.status==='receiving'?'Receiving':'Failed')}
async function openTransfers(){
  closeTransfers();
  const bg=document.createElement('div');bg.className='modal-bg';bg.id='tmodal-bg';
  bg.innerHTML=`<div class="modal tmodal"><h3>Transfers</h3><div class="tabs" id="ttabs">${['all','sending','sent','receiving','received','failed'].map(f=>`<button data-f="${f}" class="${f===TFILTER?'on':''}">${f[0].toUpperCase()+f.slice(1)}</button>`).join('')}</div><div class="tlist" id="tlist"><p class="muted">Loading…</p></div><div class="row-end"><button id="tclose">Close</button></div></div>`;
  document.body.appendChild(bg);
  const render=async()=>{try{const j=await api('/api/transfers');const list=j.transfers.filter(t=>TFILTER==='all'||t.status===TFILTER);const el=$('#tlist');if(!el)return;if(!list.length){el.innerHTML='<p class="muted">No transfers yet. Downloads appear as Sent, uploads as Received, while they are still running they show as Sending / Receiving.</p>';return}el.innerHTML=list.map(t=>{const pct=t.size?Math.min(100,Math.floor(t.bytes/t.size*100)):0;return `<div class="trow"><span>${tdir(t)}</span><div><div class="tnm">${esc(t.name)}</div><div class="tmeta">${esc(t.device||'')} · ${fmtSize(t.bytes)} of ${fmtSize(t.size)} · ${fmtDate(t.started)}</div>${(t.status==='sending'||t.status==='receiving')?`<div class="tbar"><i style="width:${pct}%"></i></div>`:''}</div><span class="tst ${t.status}">${tlabel(t)}</span></div>`}).join('')}catch(e){const el=$('#tlist');if(el)el.innerHTML='<p class="muted">Could not load transfers.</p>'}};
  bg.querySelector('#ttabs').onclick=e=>{const b=e.target.closest('button');if(!b)return;TFILTER=b.dataset.f;bg.querySelectorAll('#ttabs button').forEach(x=>x.classList.toggle('on',x===b));render()};
  bg.querySelector('#tclose').onclick=closeTransfers;bg.onclick=e=>{if(e.target===bg)closeTransfers()};
  await render();TTIMER=setInterval(render,2000);
}
function closeTransfers(){if(TTIMER){clearInterval(TTIMER);TTIMER=null}const bg=document.getElementById('tmodal-bg');if(bg)bg.remove()}
async function closeApp(){if(!await dialog({title:'Close FileBridge?',text:'Sharing stops immediately. Nobody can reach your files until you start it again.',ok:'Close app',danger:true}))return;try{await post('/api/admin/shutdown')}catch(_){}window.__fbClosed=true;document.body.innerHTML='<div class="center"><div class="card" style="text-align:center"><h2>FileBridge is closed</h2><p class="muted">Sharing has stopped. Start FileBridge again to share this folder.</p></div></div>';try{window.close()}catch(_){}}
"""

LOGO = '<div class="logo"><i>&#8644;</i>FileBridge</div>'


def page(title, body, script, extra_css=""):
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}{extra_css}</style></head><body>{body}"
            f"<div id='toasts'></div><script>{COMMON_JS}{script}</script></body></html>")


# ---------------- join page (guest, not yet approved)
JOIN_CSS = r"""
.join{width:min(420px,100%);text-align:center;padding:32px 26px}
.join .logo{justify-content:center;font-size:20px;margin-bottom:18px}.join .logo i{width:40px;height:40px;font-size:20px}
.join h2{margin:0 0 6px;font-size:20px}.join input{width:100%;margin:18px 0 12px;padding:12px 14px;text-align:center}
.join button{width:100%;padding:12px}.wait{margin-top:18px;display:flex;gap:10px;align-items:center;justify-content:center}
"""
JOIN_BODY = f"""<div class="center"><div class="card join">{LOGO}
<h2>Connect to <span id="host">this device</span></h2>
<p class="muted">Ask for access. The owner approves you on their screen and picks what you can do.</p>
<div id="form"><input id="name" maxlength="60" placeholder="Your name or device name"><button class="btn-primary" id="go">Request access</button></div>
<div id="wait" class="wait" hidden><span class="spin"></span><span>Waiting for approval…</span></div>
<p id="msg" class="muted"></p></div></div>"""
JOIN_JS = r"""
fetch('/api/info').then(r=>r.json()).then(i=>{$('#host').textContent=i.hostname});
const ua=navigator.userAgent;const guess=/Android/i.test(ua)?'Android phone':/iPhone|iPad/i.test(ua)?'iPhone':/Windows/i.test(ua)?'Windows PC':/Mac/i.test(ua)?'Mac':/Linux/i.test(ua)?'Linux PC':'My device';
$('#name').value=localStorage.fbName||guess;
async function ask(){const name=$('#name').value.trim()||guess;localStorage.fbName=name;try{const r=await post('/api/request-access',{name});if(r.status==='owner'){location.reload();return}localStorage.fbReq=r.id;poll()}catch(e){$('#msg').textContent=e.message}}
async function poll(){$('#form').hidden=true;$('#wait').hidden=false;$('#msg').textContent='';const id=localStorage.fbReq;try{const r=await (await fetch('/api/request-status?id='+encodeURIComponent(id))).json();if(r.status==='approved'){localStorage.removeItem('fbReq');location.reload();return}if(r.status==='denied'||r.status==='unknown'){localStorage.removeItem('fbReq');$('#form').hidden=false;$('#wait').hidden=true;$('#msg').textContent=r.status==='denied'?'The owner declined your request.':'Request expired. Ask again.';return}}catch(_){}setTimeout(poll,1500)}
$('#go').onclick=ask;$('#name').onkeydown=e=>{if(e.key==='Enter')ask()};
if(localStorage.fbReq)poll();
"""
JOIN_HTML = page("FileBridge · Connect", JOIN_BODY, JOIN_JS, JOIN_CSS)

# ---------------- explorer
EXP_CSS = r"""
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:14px 20px;border-bottom:1px solid var(--line)}
#crumbs{display:flex;flex-wrap:wrap;align-items:center;gap:4px;font-size:15px;font-weight:600;flex:1;min-width:200px}
#crumbs a{color:var(--text);padding:4px 8px;border-radius:8px}#crumbs a:hover{background:var(--panel2)}#crumbs a:last-child{color:var(--accent)}
#crumbs .sep{color:var(--muted)}
#search{width:200px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}.seg button{border:0;border-radius:0;padding:8px 11px;background:var(--panel2)}.seg button.on{background:var(--accent);color:#fff}
main{padding:18px 20px 90px}
#items.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
#items.grid .item{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:10px;cursor:pointer;transition:border-color .15s,transform .15s;user-select:none}
#items.grid .item:hover{border-color:var(--accent);transform:translateY(-2px)}
.thumb{height:104px;border-radius:10px;background:var(--panel2);display:grid;place-items:center;overflow:hidden;margin-bottom:8px}
.thumb img{width:100%;height:100%;object-fit:cover}.thumb svg{width:64px;height:64px}
.nm{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meta{color:var(--muted);font-size:12px}
.more{position:absolute;top:14px;right:14px;padding:2px 8px;font-size:16px;line-height:1.2;opacity:0;background:var(--panel);}
.item:hover .more,.more:focus{opacity:1}
@media (hover:none){.more{opacity:1}}
.ficon{width:52px;height:64px;border-radius:6px 16px 6px 6px;background:color-mix(in srgb,var(--c) 18%,var(--panel));border:2px solid var(--c);display:grid;place-items:end center;padding-bottom:8px}
.ficon span{background:var(--c);color:#fff;font-size:10px;font-weight:700;padding:1px 5px;border-radius:4px;letter-spacing:.3px}
#items.list{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
#items.list .lhead,#items.list .item{display:grid;grid-template-columns:1fr 100px 190px 44px;align-items:center;gap:12px;padding:8px 14px}
#items.list .lhead{color:var(--muted);font-size:12px;border-bottom:1px solid var(--line);text-transform:uppercase;letter-spacing:.5px}
#items.list .item{cursor:pointer;border-bottom:1px solid var(--line);position:relative}#items.list .item:last-child{border-bottom:0}#items.list .item:hover{background:var(--panel2)}
#items.list .more{position:static;opacity:1;background:transparent;border-color:transparent}
.nmcell{display:flex;align-items:center;gap:12px;min-width:0}.sicon{flex:none;width:30px;display:grid;place-items:center}.sicon svg{width:28px;height:28px}
.sicon .ficon{width:24px;height:30px;border-radius:3px 7px 3px 3px;border-width:1.5px;padding-bottom:3px}.sicon .ficon span{font-size:7px;padding:0 2px}
@media (max-width:640px){#items.list .lhead,#items.list .item{grid-template-columns:1fr 70px 44px}#items.list .item>span:nth-child(3),#items.list .lhead span:nth-child(3){display:none}#search{width:100%;order:9}#items.grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr))}.toolbar{padding:12px}main{padding:12px 12px 90px}.hide-sm{display:none}}
.empty{text-align:center;padding:70px 20px;color:var(--muted)}.empty b{display:block;font-size:40px;margin-bottom:6px}
#status{position:fixed;left:0;bottom:0;padding:8px 20px;color:var(--muted);font-size:12px;background:var(--bg);border-top-right-radius:10px}
.menu{position:fixed;z-index:70;min-width:180px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px;box-shadow:0 12px 40px rgba(0,0,0,.35)}
.menu button{display:block;width:100%;text-align:left;border:0;background:transparent;padding:8px 12px;border-radius:8px}.menu button:hover{background:var(--panel2)}.menu .danger{color:var(--err)}
#drop{position:fixed;inset:0;z-index:80;background:color-mix(in srgb,var(--accent) 22%,transparent);border:3px dashed var(--accent);display:grid;place-items:center;font-size:22px;font-weight:600;pointer-events:none}
#up{position:fixed;right:18px;bottom:18px;width:min(360px,calc(100vw - 36px));z-index:40;background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.35);overflow:hidden}
#up .uh{display:flex;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);font-weight:600}#up .uh button{padding:2px 8px}
#ulist{max-height:260px;overflow:auto}.uj{padding:8px 14px;border-bottom:1px solid var(--line);font-size:13px}.uj .bar{height:4px;background:var(--panel2);border-radius:4px;margin-top:6px;overflow:hidden}.uj .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}
.uj.done .bar i{background:var(--ok)}.uj.err .bar i{background:var(--err);width:100%!important}.uj .row{display:flex;gap:8px}.uj .row .nm{flex:1}
#pv{position:fixed;inset:0;z-index:65;background:rgba(5,6,10,.92);display:flex;flex-direction:column}
.pv-top{display:flex;align-items:center;gap:10px;padding:12px 16px;color:#fff}.pv-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.pv-top button{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.15);color:#fff}
.pv-body{flex:1;display:flex;align-items:center;min-height:0;padding:0 10px 16px;gap:8px}.pv-media{flex:1;height:100%;display:grid;place-items:center;min-width:0}
.pv-media img,.pv-media video{max-width:100%;max-height:100%;border-radius:8px}.pv-media iframe{width:100%;height:100%;border:0;border-radius:8px;background:#fff}
.pv-media pre{width:100%;height:100%;overflow:auto;margin:0;background:#11131a;color:#dfe3ee;padding:16px;border-radius:10px;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.nav{font-size:28px;padding:8px 14px;background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.15);color:#fff}
"""
EXP_BODY = f"""<div class="topbar">{LOGO}<span class="chip hide-sm" id="hostchip"></span><span class="chip ok" id="permchip"></span><span class="spacer"></span>
<button id="transfers">Transfers</button>
<a id="dash" hidden href="/admin"><button>Dashboard</button></a><button id="closeapp" class="btn-danger" hidden>Close app</button><button id="leave" class="btn-ghost" hidden>Disconnect</button></div>
<div class="toolbar"><nav id="crumbs"></nav>
<input id="search" type="search" placeholder="Search this folder">
<select id="sort" title="Sort"><option value="name">Name</option><option value="date">Newest</option><option value="size">Largest</option><option value="type">Type</option></select>
<div class="seg"><button id="vgrid" title="Grid">&#9638;</button><button id="vlist" title="List">&#9776;</button></div>
<button id="hid" class="btn-ghost hide-sm" title="Show hidden files">.files</button>
<button id="mk" hidden>New folder</button>
<button id="upf" class="btn-primary" hidden>Upload</button><button id="upd" class="hide-sm" hidden>Upload folder</button>
<input type="file" id="fin" multiple hidden><input type="file" id="din" webkitdirectory hidden></div>
<main><div id="items" class="grid"></div></main><div id="status"></div>
<div id="drop" hidden>Drop to upload here</div>
<div id="up" hidden><div class="uh"><span id="utitle" style="flex:1">Uploads</span><button id="uclose" class="btn-ghost">&#10005;</button></div><div id="ulist"></div></div>
<div id="pv" hidden></div>"""
EXP_JS = r"""
let ME=null,CUR='',ITEMS=[],VIS=[],VIEW=localStorage.fbView||'grid',SORT=localStorage.fbSort||'name',SHOW_HIDDEN=false,FILTER='',MENU=null,PV=null;
const FOLDER='<svg viewBox="0 0 24 24"><path fill="#f3b43f" d="M2.5 6.5A2.5 2.5 0 0 1 5 4h4.2l2.3 2.3H19a2.5 2.5 0 0 1 2.5 2.5v8.7A2.5 2.5 0 0 1 19 20H5a2.5 2.5 0 0 1-2.5-2.5z"/><path fill="#f8c95f" d="M2.5 9.5A1.5 1.5 0 0 1 4 8h16a1.5 1.5 0 0 1 1.5 1.5v8A2.5 2.5 0 0 1 19 20H5a2.5 2.5 0 0 1-2.5-2.5z"/></svg>';
const KINDS={image:'jpg jpeg png gif webp bmp svg ico avif',video:'mp4 webm mkv mov avi m4v 3gp',audio:'mp3 wav ogg m4a flac aac opus',pdf:'pdf',archive:'zip rar 7z tar gz bz2 xz tgz',code:'py js ts jsx tsx html css scss json xml yml yaml sh bat ps1 c cpp h java kt dart go rs php rb sql md csv ini conf toml env',text:'txt log',doc:'doc docx odt rtf',sheet:'xls xlsx ods',slide:'ppt pptx odp',apk:'apk exe deb msi appimage dmg'};
const COLORS={image:'#2fbf7f',video:'#ef4f5f',audio:'#a66bff',pdf:'#e5484d',archive:'#f2a93b',code:'#6b8bff',text:'#8a92a6',doc:'#2f7cf6',sheet:'#1f9d55',slide:'#f97316',apk:'#14b8a6',other:'#8a92a6'};
const KMAP={};for(const k in KINDS)for(const e of KINDS[k].split(' '))KMAP[e]=k;
const ext=n=>{const i=n.lastIndexOf('.');return i>0?n.slice(i+1).toLowerCase():''};
const kind=n=>KMAP[ext(n)]||'other';
const PREVIEW=new Set(['image','video','audio','pdf','code','text']);
const enc=p=>p.split('/').map(encodeURIComponent).join('/');
const join=(a,b)=>a?a+'/'+b:b;
function can(a){const p=ME&&ME.perm;if(p==='owner'||p==='full')return true;if(a==='upload')return p==='upload';return a==='read'&&!!p}
const PERMS={owner:'Owner · full access',full:'Full access',upload:'View & upload',read:'View & download'};
const hashPath=()=>{try{return decodeURIComponent(location.hash.replace(/^#\/?/,''))}catch(_){return ''}};
const go=p=>{location.hash='#/'+enc(p)};

async function init(){
  ME=await api('/api/whoami');if(!ME.perm){location.href='/';return}
  $('#hostchip').textContent='on '+ME.hostname;$('#permchip').textContent=PERMS[ME.perm];
  $('#dash').hidden=!ME.owner;$('#leave').hidden=ME.owner;$('#closeapp').hidden=!ME.owner;
  $('#transfers').onclick=openTransfers;$('#closeapp').onclick=closeApp;
  for(const id of ['#mk','#upf','#upd'])$(id).hidden=!can('upload');
  setView(VIEW);$('#sort').value=SORT;
  window.addEventListener('hashchange',load);load();
  setInterval(async()=>{try{const m=await api('/api/whoami');if(!m.perm)location.reload();else if(m.perm!==ME.perm)location.reload()}catch(_){}},15000);
}
async function load(){
  CUR=hashPath();FILTER='';$('#search').value='';
  try{const j=await api('/api/list?path='+encodeURIComponent(CUR));ITEMS=j.entries;render()}
  catch(e){toast(e.message,'err');if(CUR)go('')}
}
function icon(it,big){
  if(it.dir)return FOLDER;const k=kind(it.name);
  if(big&&k==='image')return `<img loading="lazy" src="/dl/${enc(join(CUR,it.name))}?inline=1" alt="">`;
  return `<div class="ficon" style="--c:${COLORS[k]}"><span>${esc((ext(it.name)||'file').slice(0,4).toUpperCase())}</span></div>`;
}
function itemHTML(it,i){
  if(VIEW==='grid')return `<div class="item" data-i="${i}"><div class="thumb">${icon(it,true)}</div><div class="nm" title="${esc(it.name)}">${esc(it.name)}</div><div class="meta">${it.dir?'Folder':fmtSize(it.size)}</div><button class="more" data-i="${i}" title="More">&#8943;</button></div>`;
  return `<div class="item" data-i="${i}"><span class="nmcell"><span class="sicon">${icon(it,false)}</span><span class="nm" title="${esc(it.name)}">${esc(it.name)}</span></span><span class="meta">${it.dir?'—':fmtSize(it.size)}</span><span class="meta">${fmtDate(it.mtime)}</span><button class="more" data-i="${i}" title="More">&#8943;</button></div>`;
}
function render(){
  const parts=CUR?CUR.split('/'):[];let acc='';
  let bc=`<a href="#/">&#8962; ${esc(ME.root_name)}</a>`;
  for(const p of parts){acc=join(acc,p);bc+=`<span class="sep">›</span><a href="#/${enc(acc)}">${esc(p)}</a>`}
  $('#crumbs').innerHTML=bc;
  const f=FILTER.toLowerCase();
  const v=ITEMS.filter(x=>(SHOW_HIDDEN||!x.name.startsWith('.'))&&(!f||x.name.toLowerCase().includes(f)));
  const cmpName=(a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true,sensitivity:'base'});
  const cmp={name:cmpName,size:(a,b)=>b.size-a.size||cmpName(a,b),date:(a,b)=>b.mtime-a.mtime,type:(a,b)=>ext(a.name).localeCompare(ext(b.name))||cmpName(a,b)}[SORT];
  v.sort((a,b)=>(b.dir-a.dir)||cmp(a,b));VIS=v;
  const box=$('#items');box.className=VIEW;
  if(!v.length){box.className='';box.innerHTML=`<div class="empty"><b>${f?'&#128269;':'&#128194;'}</b>${f?'Nothing matches “'+esc(FILTER)+'”':'This folder is empty'}${!f&&can('upload')?'<br>Drop files here or tap Upload':''}</div>`}
  else box.innerHTML=(VIEW==='list'?'<div class="lhead"><span>Name</span><span>Size</span><span>Modified</span><span></span></div>':'')+v.map(itemHTML).join('');
  const total=v.filter(x=>!x.dir).reduce((s,x)=>s+x.size,0);
  $('#status').textContent=`${v.length} item${v.length===1?'':'s'} · ${fmtSize(total)}`;
}
function setView(m){VIEW=m;localStorage.fbView=m;$('#vgrid').classList.toggle('on',m==='grid');$('#vlist').classList.toggle('on',m==='list');if(ME)render()}
const fileUrl=it=>'/dl/'+enc(join(CUR,it.name));
function download(it){const a=document.createElement('a');a.href=it.dir?'/api/zip?path='+encodeURIComponent(join(CUR,it.name)):fileUrl(it);a.download=it.dir?it.name+'.zip':it.name;document.body.appendChild(a);a.click();a.remove();if(it.dir)toast('Zipping “'+it.name+'”… download starts shortly')}
function openItem(it){if(it.dir)go(join(CUR,it.name));else if(PREVIEW.has(kind(it.name)))preview(it);else download(it)}
function closeMenu(){if(MENU){MENU.remove();MENU=null}}
function openMenu(it,x,y){
  closeMenu();const acts=[];
  if(it.dir)acts.push(["Open",()=>openItem(it)],['Download as ZIP',()=>download(it)]);
  else{if(PREVIEW.has(kind(it.name)))acts.push(['Preview',()=>preview(it)]);acts.push(['Download',()=>download(it)])}
  acts.push(['Copy link',()=>copy(location.origin+(it.dir?'/#/'+enc(join(CUR,it.name)):fileUrl(it)))]);
  if(can('full'))acts.push(['Rename',()=>renameItem(it)],['Delete',()=>deleteItem(it),'danger']);
  const m=document.createElement('div');m.className='menu';
  m.innerHTML=acts.map((a,i)=>`<button data-k="${i}" class="${a[2]||''}">${a[0]}</button>`).join('');
  m.onclick=e=>{e.stopPropagation();const b=e.target.closest('button');if(!b)return;closeMenu();acts[+b.dataset.k][1]()};
  document.body.appendChild(m);MENU=m;
  const L=Math.max(8,Math.min(x,innerWidth-m.offsetWidth-8)),T=Math.max(8,Math.min(y,innerHeight-m.offsetHeight-8));
  m.style.left=L+'px';m.style.top=T+'px';
}
async function renameItem(it){const n=await dialog({title:'Rename',input:it.name,ok:'Rename'});if(!n||n===it.name)return;try{await post('/api/rename',{path:join(CUR,it.name),name:n});toast('Renamed');load()}catch(e){toast(e.message,'err')}}
async function deleteItem(it){const y=await dialog({title:'Delete “'+it.name+'”?',text:it.dir?'The folder and everything in it will be deleted from the sharing device.':'This file will be deleted from the sharing device.',ok:'Delete',danger:true});if(!y)return;try{await post('/api/delete',{paths:[join(CUR,it.name)]});toast('Deleted');load()}catch(e){toast(e.message,'err')}}
async function newFolder(){const n=await dialog({title:'New folder',input:'',placeholder:'Folder name',ok:'Create'});if(!n)return;try{await post('/api/mkdir',{path:CUR,name:n});load()}catch(e){toast(e.message,'err')}}

// ---- preview
function preview(it){const list=VIS.filter(x=>!x.dir&&PREVIEW.has(kind(x.name)));PV={list,i:list.indexOf(it)};showPV()}
function showPV(){
  const it=PV.list[PV.i],url=fileUrl(it)+'?inline=1',k=kind(it.name),multi=PV.list.length>1;let body;
  if(k==='image')body=`<img src="${url}" alt="">`;else if(k==='video')body=`<video src="${url}" controls autoplay playsinline></video>`;
  else if(k==='audio')body=`<audio src="${url}" controls autoplay></audio>`;else if(k==='pdf')body=`<iframe src="${url}"></iframe>`;else body='<pre>Loading…</pre>';
  const pv=$('#pv');pv.innerHTML=`<div class="pv-top"><div class="pv-title">${esc(it.name)} <span style="opacity:.6;font-weight:400">· ${fmtSize(it.size)}${multi?` · ${PV.i+1} of ${PV.list.length}`:''}</span></div><button id="pvdl">Download</button><button id="pvx">&#10005;</button></div><div class="pv-body">${multi?'<button class="nav" id="pvp">‹</button>':''}<div class="pv-media">${body}</div>${multi?'<button class="nav" id="pvn">›</button>':''}</div>`;
  pv.hidden=false;$('#pvx').onclick=closePV;$('#pvdl').onclick=()=>download(it);
  if(multi){$('#pvp').onclick=()=>step(-1);$('#pvn').onclick=()=>step(1)}
  if(k==='code'||k==='text'){const lim=262144;fetch(url,{headers:{Range:'bytes=0-'+(lim-1)}}).then(r=>r.arrayBuffer()).then(b=>{if(!PV||PV.list[PV.i]!==it)return;const pre=$('#pv pre');if(pre)pre.textContent=new TextDecoder().decode(b)+(it.size>lim?'\n\n… preview shows the first 256 KB. Download for the full file.':'')})}
}
function step(d){PV.i=(PV.i+d+PV.list.length)%PV.list.length;showPV()}
function closePV(){$('#pv').hidden=true;$('#pv').innerHTML='';PV=null}

// ---- uploads
let UQ=[],UACTIVE=0,UTOTAL=0,UDONE=0;
function enqueue(list){
  if(!list.length)return;if(!can('upload')){toast('You don\'t have upload permission','err');return}
  for(const {file,rel} of list){const el=document.createElement('div');el.className='uj';el.innerHTML=`<div class="row"><span class="nm">${esc(rel)}</span><span class="meta st">${fmtSize(file.size)}</span></div><div class="bar"><i></i></div>`;$('#ulist').prepend(el);UQ.push({file,rel,dir:CUR,el});UTOTAL++}
  $('#up').hidden=false;utitle();pump();
}
function utitle(){$('#utitle').textContent=UDONE<UTOTAL?`Uploading ${UDONE} of ${UTOTAL}`:`Uploaded ${UTOTAL} file${UTOTAL===1?'':'s'}`}
function pump(){while(UACTIVE<3&&UQ.length){const j=UQ.shift();UACTIVE++;send(j).then(()=>{UACTIVE--;UDONE++;utitle();if(!UACTIVE&&!UQ.length)load();pump()})}}
function send(j){return new Promise(res=>{const x=new XMLHttpRequest();const bar=j.el.querySelector('i'),st=j.el.querySelector('.st');
  x.open('PUT','/api/upload?path='+encodeURIComponent(j.dir)+'&name='+encodeURIComponent(j.rel));x.setRequestHeader('X-FB','1');
  x.upload.onprogress=e=>{if(e.lengthComputable){const p=e.loaded/e.total*100;bar.style.width=p+'%';st.textContent=Math.floor(p)+'%'}};
  x.onload=()=>{if(x.status<300){j.el.classList.add('done');bar.style.width='100%';st.textContent='Done'}else{let m='Failed';try{m=JSON.parse(x.responseText).error}catch(_){}j.el.classList.add('err');st.textContent=m}res()};
  x.onerror=()=>{j.el.classList.add('err');st.textContent='Network error';res()};x.send(j.file)})}
async function entriesToFiles(items){
  const out=[];const ents=[...items].map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  const walk=async(en,pre)=>{if(en.isFile){const f=await new Promise((r,j)=>en.file(r,j));out.push({file:f,rel:pre+f.name})}
    else if(en.isDirectory){const rd=en.createReader();let b;do{b=await new Promise((r,j)=>rd.readEntries(r,j));for(const e of b)await walk(e,pre+en.name+'/')}while(b.length)}};
  for(const e of ents)await walk(e,'');return out;
}

// ---- events
$('#items').addEventListener('click',e=>{const m=e.target.closest('.more');if(m){e.stopPropagation();const r=m.getBoundingClientRect();openMenu(VIS[+m.dataset.i],r.right-180,r.bottom+4);return}const el=e.target.closest('.item');if(el)openItem(VIS[+el.dataset.i])});
$('#items').addEventListener('contextmenu',e=>{const el=e.target.closest('.item');if(!el)return;e.preventDefault();openMenu(VIS[+el.dataset.i],e.clientX,e.clientY)});
document.addEventListener('click',closeMenu);window.addEventListener('scroll',closeMenu,true);
$('#search').oninput=e=>{FILTER=e.target.value;render()};
$('#sort').onchange=e=>{SORT=e.target.value;localStorage.fbSort=SORT;render()};
$('#vgrid').onclick=()=>setView('grid');$('#vlist').onclick=()=>setView('list');
$('#hid').onclick=()=>{SHOW_HIDDEN=!SHOW_HIDDEN;$('#hid').classList.toggle('on',SHOW_HIDDEN);$('#hid').style.color=SHOW_HIDDEN?'var(--accent)':'';render()};
$('#mk').onclick=newFolder;$('#upf').onclick=()=>$('#fin').click();$('#upd').onclick=()=>$('#din').click();
$('#fin').onchange=e=>{enqueue([...e.target.files].map(f=>({file:f,rel:f.name})));e.target.value=''};
$('#din').onchange=e=>{enqueue([...e.target.files].map(f=>({file:f,rel:f.webkitRelativePath||f.name})));e.target.value=''};
$('#uclose').onclick=()=>{if(UDONE<UTOTAL)return toast('Uploads still running');$('#up').hidden=true;$('#ulist').innerHTML='';UTOTAL=UDONE=0};
$('#leave').onclick=async()=>{const y=await dialog({title:'Disconnect?',text:'You will need to request access again to come back.',ok:'Disconnect',danger:true});if(!y)return;await post('/api/leave');location.href='/'};
let dragN=0;const hasFiles=e=>e.dataTransfer&&[...e.dataTransfer.types].includes('Files');
window.addEventListener('dragenter',e=>{if(!hasFiles(e)||!can('upload'))return;e.preventDefault();dragN++;$('#drop').hidden=false});
window.addEventListener('dragleave',e=>{if(!hasFiles(e))return;if(--dragN<=0){dragN=0;$('#drop').hidden=true}});
window.addEventListener('dragover',e=>{if(hasFiles(e)&&can('upload'))e.preventDefault()});
window.addEventListener('drop',async e=>{if(!hasFiles(e))return;e.preventDefault();dragN=0;$('#drop').hidden=true;if(!can('upload'))return;const it=e.dataTransfer.items;const list=(it&&it.length&&it[0].webkitGetAsEntry)?await entriesToFiles(it):[...e.dataTransfer.files].map(f=>({file:f,rel:f.name}));enqueue(list)});
document.addEventListener('keydown',e=>{if(PV){if(e.key==='Escape')closePV();if(e.key==='ArrowRight'&&PV.list.length>1)step(1);if(e.key==='ArrowLeft'&&PV.list.length>1)step(-1);return}if(e.key==='Escape')closeMenu();if(e.key==='Backspace'&&CUR&&document.activeElement.tagName!=='INPUT'){go(CUR.split('/').slice(0,-1).join('/'))}});
init();
"""
EXPLORER_HTML = page("FileBridge · Files", EXP_BODY, EXP_JS, EXP_CSS)

# ---------------- owner dashboard
ADM_CSS = r"""
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 60px;display:grid;gap:16px}
.hero{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
@media (max-width:760px){.hero{grid-template-columns:1fr}}
h2{margin:0 0 4px;font-size:15px}.sub{color:var(--muted);font-size:13px;margin:0 0 14px}
.url{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:8px}
.url code{flex:1;font:600 22px/1.2 ui-monospace,Menlo,Consolas,monospace;color:var(--accent);word-break:break-all}
.qr{display:flex;gap:14px;align-items:center;margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--panel2)}
.qr img{width:150px;height:150px;flex:none;background:#fff;padding:6px;border-radius:8px}
.qr .qtxt{font-size:13px;color:var(--muted)}
.qr .qtxt b{display:block;color:var(--text);font-size:14px;margin-bottom:4px}
.kv{display:flex;gap:8px;align-items:center;margin-top:8px}.kv input{flex:1}
.list .it{display:flex;align-items:center;gap:12px;padding:12px 0;border-top:1px solid var(--line);flex-wrap:wrap}.list .it:first-child{border-top:0}
.av{width:38px;height:38px;border-radius:50%;background:var(--panel2);display:grid;place-items:center;font-weight:700;color:var(--accent);flex:none;position:relative}
.dot{position:absolute;right:0;bottom:0;width:11px;height:11px;border-radius:50%;border:2px solid var(--panel);background:var(--muted)}.dot.on{background:var(--ok)}
.who{flex:1;min-width:150px}.who b{display:block}.who span{color:var(--muted);font-size:12px}
.req{background:color-mix(in srgb,var(--warn) 10%,var(--panel));border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.badge{background:var(--warn);color:#111;border-radius:999px;padding:0 8px;font-size:12px;font-weight:700;margin-left:6px}
.empty{color:var(--muted);padding:6px 0}
"""
ADM_BODY = f"""<div class="topbar">{LOGO}<span class="chip ok">&#9679; Sharing on</span><span class="spacer"></span><button id="transfers">Transfers</button><a href="/"><button class="btn-primary">Open my files</button></a><button id="closeapp" class="btn-danger">Close app</button></div>
<div class="wrap">
<div class="hero">
 <div class="card"><h2>Connect from another device</h2><p class="sub">On the same Wi-Fi or office network, open this address in any browser (Android, Windows, Ubuntu, iPhone).</p>
  <div id="urls"></div><div id="qr"></div><p class="muted" style="margin:6px 0 0;font-size:12px">Device name <b id="host"></b> · port <b id="port"></b></p></div>
 <div class="card"><h2>Shared folder</h2><p class="sub">Approved devices only see what's inside this folder.</p>
  <div class="kv"><input id="root" spellcheck="false"><button id="setroot">Change</button></div>
  <h2 style="margin-top:20px">Open someone else's files</h2><p class="sub">If a colleague is running FileBridge too, enter their address.</p>
  <div class="kv"><input id="peer" placeholder="192.168.1.20:8000"><button id="goPeer" class="btn-primary">Connect</button></div></div>
</div>
<div class="card req" id="reqcard" hidden><h2>Access requests<span class="badge" id="reqn"></span></h2><p class="sub">Choose what each device may do, then approve.</p><div class="list" id="pending"></div></div>
<div class="card"><h2>Approved devices</h2><p class="sub">Change access or remove a device at any time. Changes apply instantly.</p><div class="list" id="devices"></div></div>
<p class="muted" style="text-align:center;font-size:12px" id="foot"></p>
</div>"""
ADM_JS = r"""
let S=null,sigP='',sigD='',seen=new Set(),first=true;
const PL={read:'View & download',upload:'View & upload',full:'Full access'};
const sel=(v,attr)=>`<select ${attr}>${Object.entries(PL).map(([k,l])=>`<option value="${k}"${k===v?' selected':''}>${l}</option>`).join('')}</select>`;
const initial=n=>esc((n||'?').trim().charAt(0).toUpperCase());
const dev=ua=>/Android/i.test(ua)?'Android':/iPhone|iPad/i.test(ua)?'iOS':/Windows/i.test(ua)?'Windows':/Mac OS/i.test(ua)?'macOS':/Linux/i.test(ua)?'Linux':'Browser';
async function refresh(){if(window.__fbClosed)return;try{S=await api('/api/admin/state');draw();$('#foot').textContent='FileBridge '+S.version}catch(e){if(window.__fbClosed)return;const f=$('#foot');if(f)f.textContent='Lost connection to FileBridge. Is it still running?'}}
function draw(){
  $('#host').textContent=S.hostname;$('#port').textContent=S.port;
  const urls=S.ips.map(ip=>`http://${ip}:${S.port}`);
  const uh=urls.length?urls.map(u=>`<div class="url"><code>${esc(u)}</code><button data-copy="${esc(u)}">Copy</button></div>`).join(''):'<div class="url"><code style="color:var(--warn);font-size:15px">No network found. Connect to Wi-Fi or LAN.</code></div>';
  if($('#urls').dataset.h!==uh){$('#urls').innerHTML=uh;$('#urls').dataset.h=uh}
  const qh=urls.length?`<div class="qr"><img alt="QR code for ${esc(urls[0])}" src="/qr.svg?u=${encodeURIComponent(urls[0])}"><div class="qtxt"><b>Scan to connect</b>Point a phone camera at this code to open ${esc(urls[0])} — no typing needed.</div></div>`:'';
  if($('#qr').dataset.h!==qh){$('#qr').innerHTML=qh;$('#qr').dataset.h=qh}
  if(document.activeElement!==$('#root'))$('#root').value=S.root;
  const sp=JSON.stringify(S.pending.map(r=>[r.id,r.name]));
  if(sp!==sigP){sigP=sp;$('#reqcard').hidden=!S.pending.length;$('#reqn').textContent=S.pending.length;
    $('#pending').innerHTML=S.pending.map(r=>`<div class="it"><div class="av">${initial(r.name)}</div><div class="who"><b>${esc(r.name)}</b><span>${esc(r.ip)} · ${dev(r.ua)} · asked ${ago(r.time)}</span></div>${sel('read',`id="p_${r.id}"`)}<button class="btn-primary" data-ok="${r.id}">Approve</button><button class="btn-danger" data-no="${r.id}">Deny</button></div>`).join('');
    for(const r of S.pending)if(!seen.has(r.id)){seen.add(r.id);if(!first&&window.Notification&&Notification.permission==='granted')new Notification('FileBridge: access request',{body:r.name+' ('+r.ip+') wants to connect'})}}
  const sd=JSON.stringify(S.devices.map(d=>[d.id,d.name,d.perm,d.online,d.ip]));
  if(sd!==sigD){sigD=sd;$('#devices').innerHTML=S.devices.length?S.devices.map(d=>`<div class="it"><div class="av">${initial(d.name)}<i class="dot ${d.online?'on':''}"></i></div><div class="who"><b>${esc(d.name)}</b><span>${esc(d.ip)} · ${d.online?'online now':'last seen '+ago(d.last_seen)}</span></div>${sel(d.perm,`data-perm="${d.id}"`)}<button class="btn-danger" data-rm="${d.id}">Remove</button></div>`).join(''):'<div class="empty">No devices yet. Share your address and approve requests as they come in.</div>'}
  document.title=(S.pending.length?`(${S.pending.length}) `:'')+'FileBridge Dashboard';first=false;
}
document.addEventListener('click',async e=>{const t=e.target;try{
  if(t.dataset.copy)copy(t.dataset.copy);
  if(t.dataset.ok){await post('/api/admin/approve',{id:t.dataset.ok,perm:$('#p_'+t.dataset.ok).value});toast('Approved');sigP='';refresh()}
  if(t.dataset.no){await post('/api/admin/deny',{id:t.dataset.no});sigP='';refresh()}
  if(t.dataset.rm){if(!await dialog({title:'Remove this device?',text:'It loses access immediately and must request again.',ok:'Remove',danger:true}))return;await post('/api/admin/revoke',{id:t.dataset.rm});toast('Removed');refresh()}
}catch(err){toast(err.message,'err')}});
document.addEventListener('change',async e=>{const id=e.target.dataset&&e.target.dataset.perm;if(!id)return;try{await post('/api/admin/set-perm',{id,perm:e.target.value});toast('Access updated');refresh()}catch(err){toast(err.message,'err')}});
$('#setroot').onclick=async()=>{try{await post('/api/admin/set-root',{path:$('#root').value});$('#root').blur();toast('Now sharing '+$('#root').value);refresh()}catch(e){toast(e.message,'err')}};
$('#transfers').onclick=openTransfers;$('#closeapp').onclick=closeApp;
$('#root').onkeydown=e=>{if(e.key==='Enter')$('#setroot').click()};
$('#goPeer').onclick=()=>{let v=$('#peer').value.trim();if(!v)return;if(!/^https?:\/\//.test(v))v='http://'+v;if(!/:\d+(\/|$)/.test(v.replace(/^https?:\/\//,'')))v=v.replace(/\/?$/,'')+':8000';window.open(v,'_blank')};
$('#peer').onkeydown=e=>{if(e.key==='Enter')$('#goPeer').click()};
if(window.Notification&&Notification.permission==='default')document.addEventListener('click',()=>Notification.requestPermission(),{once:true});
refresh();setInterval(refresh,2000);
"""
ADMIN_HTML = page("FileBridge Dashboard", ADM_BODY, ADM_JS, ADM_CSS)


# --------------------------------------------------------------------------- main
def main():
    global STATE
    ap = argparse.ArgumentParser(description="Share a folder on your local network with permission control.")
    ap.add_argument("folder", nargs="?", default=None,
                    help="folder to share (default: last folder chosen in the dashboard, else the current folder)")
    ap.add_argument("-p", "--port", type=int, default=8000, help="port to listen on (default 8000)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    ap.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    args = ap.parse_args()

    folder = os.path.expanduser(args.folder) if args.folder else default_folder()
    if not os.path.isdir(folder):
        sys.exit(f"Folder not found: {folder}")

    httpd, port = None, args.port
    for port in range(args.port, args.port + 20):
        try:
            STATE = State(folder, port)
            httpd = Server(("0.0.0.0", port), Handler)
            break
        except OSError:
            continue
    if not httpd:
        sys.exit(f"No free port between {args.port} and {args.port + 19}")

    ips = local_ips()
    print(f"\n  {APP} {VERSION} is running")
    print(f"  Sharing    {STATE.root}")
    for ip in ips or ["<no network>"]:
        print(f"  Connect    http://{ip}:{port}")
    print(f"  Dashboard  http://localhost:{port}/admin   (approve devices here)")
    print("  Press Ctrl+C to stop.\n")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{port}/admin")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Nobody can reach your files now.")
    except Exception:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            input("Press Enter to close...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
