"""Run after `flutter create`: add permissions, app name and launcher icons to the Android project."""
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "android", "app", "src", "main", "AndroidManifest.xml")
RES = os.path.join(HERE, "android", "app", "src", "main", "res")
ICON = os.path.join(os.path.dirname(HERE), "assets", "icon.png")

PERMS = """    <uses-permission android:name="android.permission.INTERNET"/>
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE"/>
    <uses-permission android:name="android.permission.MANAGE_EXTERNAL_STORAGE"/>
    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" android:maxSdkVersion="32"/>
    <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" android:maxSdkVersion="29"/>
"""
QUERY = """        <intent>
            <action android:name="android.intent.action.VIEW"/>
            <data android:scheme="http"/>
        </intent>
        <intent>
            <action android:name="android.intent.action.VIEW"/>
            <data android:scheme="https"/>
        </intent>
"""

m = open(MANIFEST, encoding="utf-8").read()
if "MANAGE_EXTERNAL_STORAGE" not in m:
    m = m.replace("<application", PERMS + "    <application", 1)
m = re.sub(r'android:label="[^"]*"', 'android:label="FileBridge"', m, count=1)
if "requestLegacyExternalStorage" not in m:
    m = m.replace("<application", '<application\n        android:requestLegacyExternalStorage="true"', 1)
if "<queries>" in m:
    if 'android:scheme="http"' not in m:
        m = m.replace("<queries>", "<queries>\n" + QUERY, 1)
else:
    m = m.replace("</manifest>", "    <queries>\n" + QUERY + "    </queries>\n</manifest>")
open(MANIFEST, "w", encoding="utf-8").write(m)
print("manifest patched")

try:
    from PIL import Image
    img = Image.open(ICON).convert("RGBA")
    for folder, size in (("mipmap-mdpi", 48), ("mipmap-hdpi", 72), ("mipmap-xhdpi", 96),
                         ("mipmap-xxhdpi", 144), ("mipmap-xxxhdpi", 192)):
        d = os.path.join(RES, folder)
        os.makedirs(d, exist_ok=True)
        img.resize((size, size), Image.LANCZOS).save(os.path.join(d, "ic_launcher.png"))
    print("launcher icons written")
except ImportError:
    print("Pillow not installed; keeping default icon")
