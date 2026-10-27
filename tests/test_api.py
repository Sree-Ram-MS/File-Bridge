"""API smoke test: python3 tests/test_api.py  (no dependencies)"""
import io, json, os, sys, tempfile, threading, urllib.error, urllib.request, http.cookiejar, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import filebridge as fb

fb.STATE_DIR = tempfile.mkdtemp()
root = tempfile.mkdtemp()
os.makedirs(root + "/docs")
open(root + "/docs/a.txt", "w").write("hello world")
fb.STATE = fb.State(root, 8765)
fb.Handler.is_owner = lambda self: self.headers.get("X-Guest") != "1"  # fake guests
srv = fb.Server(("127.0.0.1", 8765), fb.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8765"
guest = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
owner = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def req(op, path, body=None, method=None, is_guest=True, raw=None, hdr=None):
    h = {"X-FB": "1", "Content-Type": "application/json"}
    if is_guest:
        h["X-Guest"] = "1"
    h.update(hdr or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    try:
        r = op.open(urllib.request.Request(B + path, data=data, headers=h, method=method))
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


G = lambda *a, **k: req(guest, *a, **k)
O = lambda *a, **k: req(owner, *a, is_guest=False, **k)
assert G("/api/list")[0] == 401
assert b"Request access" in G("/")[1]
assert G("/api/admin/state")[0] == 403
rid = json.loads(G("/api/request-access", {"name": "Phone"})[1])["id"]
assert O("/api/admin/approve", {"id": rid}, hdr={"X-FB": "0"})[0] == 403
assert O("/api/admin/approve", {"id": rid, "perm": "read"})[0] == 200
assert json.loads(G("/api/request-status?id=" + rid)[1])["status"] == "approved"
assert G("/api/list")[0] == 200
assert G("/dl/docs/a.txt")[1] == b"hello world"
assert G("/dl/docs/a.txt", hdr={"Range": "bytes=0-4"}) == (206, b"hello")
assert G("/api/list?path=../..")[0] == 403
assert G("/api/upload?path=docs&name=b.txt", raw=b"abc", method="PUT")[0] == 403
dev = json.loads(O("/api/admin/state")[1])["devices"][0]
O("/api/admin/set-perm", {"id": dev["id"], "perm": "upload"})
assert json.loads(G("/api/upload?path=docs&name=sub%2Fb.txt", raw=b"abc", method="PUT")[1])["name"] == "b.txt"
assert json.loads(G("/api/upload?path=docs&name=sub%2Fb.txt", raw=b"abc", method="PUT")[1])["name"] == "b (1).txt"
assert G("/api/delete", {"paths": ["docs/a.txt"]})[0] == 403
O("/api/admin/set-perm", {"id": dev["id"], "perm": "full"})
assert G("/api/mkdir", {"path": "", "name": "New"})[0] == 200
assert G("/api/rename", {"path": "New", "name": "Renamed"})[0] == 200
assert G("/api/delete", {"paths": [""]})[0] == 403
s, z = G("/api/zip?path=docs")
assert sorted(zipfile.ZipFile(io.BytesIO(z)).namelist()) == ["docs/a.txt", "docs/sub/b (1).txt", "docs/sub/b.txt"]
assert O("/api/admin/revoke", {"id": dev["id"]})[0] == 200
assert G("/api/list")[0] == 401
srv.shutdown()
print("all API tests passed")
