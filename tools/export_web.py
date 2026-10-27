"""Copy the web UI from filebridge.py into the Android app assets (run after changing the UI)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import filebridge  # noqa: E402

out = os.path.join(ROOT, "android", "app", "src", "main", "assets", "web")
os.makedirs(out, exist_ok=True)
pages = {"join.html": filebridge.JOIN_HTML, "explorer.html": filebridge.EXPLORER_HTML, "admin.html": filebridge.ADMIN_HTML}
for name, html in pages.items():
    with open(os.path.join(out, name), "w", encoding="utf-8") as f:
        f.write(html)
print("web UI exported to", out)
