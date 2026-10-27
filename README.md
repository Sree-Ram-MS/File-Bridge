# FileBridge

Share folders on your local network with permission control and a proper file explorer.
A friendlier replacement for `python3 -m http.server`.

- The sharing device shows its address (IP and port) on a dashboard.
- Anyone on the same network opens that address in a browser and requests access.
- The owner approves each device and picks what it may do: **View & download**, **View & upload**, or **Full access** (rename and delete too). Access can be changed or removed at any time.
- Explorer: grid/list view, breadcrumbs, search, sort, thumbnails, previews (images, video, audio, PDF, text/code), drag-and-drop upload of files and folders with progress, folder download as ZIP.
- Two-way: both people run FileBridge and approve each other.

## Download

Get the latest build from **Releases**:

| Platform | File | How to run |
|---|---|---|
| Windows 10/11 | `FileBridge-<version>-windows-x64.exe` | Double-click. Allow it on *Private networks* at the firewall prompt. |
| Ubuntu / Debian | `filebridge_<version>_all.deb` | `sudo apt install ./filebridge_<version>_all.deb`, then open **FileBridge** from the app menu or run `filebridge`. |
| Any OS with Python 3.8+ | `FileBridge-<version>-portable.zip` | `python3 filebridge.py ~/Downloads` |
| Android (to share from the phone) | `FileBridge-<version>-android.apk` | Install, allow *All files access* and notifications. Sharing keeps running in the background until you tap *Stop sharing*. |
| Any browser (to connect) | nothing to install | Open the address shown on the sharing device. |

## Branches

| Branch | Contents |
|---|---|
| `main` | Core app (`filebridge.py`, pure Python stdlib, no dependencies), tests, portable release |
| `ubuntu` | Debian/Ubuntu packaging (`.deb`, app-menu entry, icon) and its CI build |
| `windows` | Windows packaging (PyInstaller `.exe`) and its CI build |
| `android` | Android host app (plain Java, in `android/`) and its CI build |

Platform branches build automatically on push with GitHub Actions and attach their package to the release `v<version>`.

## Run from source

```bash
python3 filebridge.py                  # share the last-used folder (or current folder)
python3 filebridge.py ~/Downloads      # share a specific folder
python3 filebridge.py . --port 9000 --no-browser
python3 tests/test_api.py              # API smoke tests
```

The dashboard opens at `http://localhost:8000/admin`. Approved devices are remembered in `~/.filebridge/`.

## Notes

- Traffic is plain HTTP on your LAN, like `http.server`. Use it on trusted networks only.
- Ubuntu with ufw enabled: `sudo ufw allow 8000/tcp`.

## Android app (this branch)

`android/` is a small plain-Java app (no Flutter, no AndroidX). It runs the FileBridge server in a
foreground service, so sharing continues with the screen off, and shows the same owner dashboard
as the desktop in a WebView, plus a native folder picker, Share address and Stop sharing buttons.
Access requests arrive as notifications.

Build locally (JDK 17 + Android SDK):

```bash
python3 tools/export_web.py          # copy the web UI from filebridge.py into the app assets
gradle -p android assembleRelease    # android/app/build/outputs/apk/release/app-release.apk
```

Pushing to `android` builds the APK in GitHub Actions and attaches it to the release.
The APK is signed with a debug key, so installing a newer build may need uninstalling the old one first.
