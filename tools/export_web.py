"""Copy the web UI (join page + file explorer) from filebridge.py into the Android app assets."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import filebridge  # noqa: E402

out = os.path.join(ROOT, "flutter_app", "assets", "web")
os.makedirs(out, exist_ok=True)
for name, html in (("join.html", filebridge.JOIN_HTML), ("explorer.html", filebridge.EXPLORER_HTML)):
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        f.write(html)
print("web UI exported to", out)
