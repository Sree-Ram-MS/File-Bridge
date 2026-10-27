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
| Android, iPhone, any browser | nothing to install | Open the address shown on the sharing device. |

## Branches

| Branch | Contents |
|---|---|
| `main` | Core app (`filebridge.py`, pure Python stdlib, no dependencies), tests, portable release |
| `ubuntu` | Debian/Ubuntu packaging (`.deb`, app-menu entry, icon) and its CI build |
| `windows` | Windows packaging (PyInstaller `.exe`) and its CI build |
| `android` | Android host app (planned) |

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

## Windows packaging (this branch)

On Windows with Python 3.8+:

```bat
packaging\windows\build.bat        :: -> dist\FileBridge.exe
```

`FileBridge.exe` is a single portable file. Double-click it: a console window shows the address, and the dashboard opens in your browser. Close the window to stop sharing. With no folder given it shares the last folder chosen in the dashboard, else your Downloads folder. Pushing to `windows` builds the exe in GitHub Actions and attaches it to the release.

SmartScreen may warn on first run because the exe isn't code-signed: choose *More info > Run anyway*.
