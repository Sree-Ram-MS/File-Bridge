"""Transfers + Close app test: python3 tests/test_transfers.py (no dependencies)

Covers what test_api.py does not: an in-progress transfer is visible as
Sending/Receiving, and the dashboard Close app button (/api/admin/shutdown)
actually stops the server.
"""
import json, os, sys, tempfile, threading, time, urllib.error, urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import filebridge as fb

fb.STATE_DIR = tempfile.mkdtemp()
root = tempfile.mkdtemp()
big = os.path.join(root, "big.bin")
with open(big, "wb") as f:
    f.write(os.urandom(20 * 1024 * 1024))
fb.STATE = fb.State(root, 8766)
fb.Handler.is_owner = lambda self: True  # owner-only server for this test
srv = fb.Server(("127.0.0.1", 8766), fb.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8766"


def req(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, headers={"X-FB": "1", "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# In-progress: start a download in a thread, poll /api/transfers while it runs.
# The client reads slowly so the server stays in Sending long enough to see.
seen_sending = []
def dl():
    with urllib.request.urlopen(B + "/dl/big.bin", timeout=30) as resp:
        while resp.read(65536):
            time.sleep(0.01)

t = threading.Thread(target=dl, daemon=True)
t.start()
for _ in range(100):
    trs = json.loads(req("/api/transfers")[1])["transfers"]
    if any(x["direction"] == "send" and x["status"] == "sending" and x["name"] == "big.bin" for x in trs):
        seen_sending = trs
        break
    time.sleep(0.05)
t.join(timeout=10)
trs = json.loads(req("/api/transfers")[1])["transfers"]
assert seen_sending, "download was never visible as Sending"
assert any(x["direction"] == "send" and x["status"] == "sent" and x["size"] == 20 * 1024 * 1024 for x in trs), trs

# Close app: POST /api/admin/shutdown, then the server must stop responding.
assert req("/api/admin/shutdown", {}, method="POST")[0] == 200
time.sleep(1.0)
try:
    req("/api/transfers")
    raise AssertionError("server still responding after shutdown")
except (urllib.error.URLError, ConnectionError, OSError):
    pass
print("all transfer tests passed")
