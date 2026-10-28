#!/usr/bin/env python3
"""FileBridge desktop window for Ubuntu/Debian.

Runs the FileBridge server in the background and shows the dashboard in its
own window (GTK + WebKit), so there is no terminal and no browser tab.
Closing the window stops sharing. Launching again focuses the open window.
"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("FILEBRIDGE_PACKAGED", "1")
import filebridge as fb  # noqa: E402

APP_ID = "com.sreeram.FileBridge"


def start_server(base_port=8000):
    folder = fb.default_folder()
    if not os.path.isdir(folder):
        folder = os.path.expanduser("~")
    for port in range(base_port, base_port + 20):
        try:
            fb.STATE = fb.State(folder, port)
            httpd = fb.Server(("0.0.0.0", port), fb.Handler)
        except OSError:
            continue
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, port, thread
    raise SystemExit("FileBridge: no free port between %d and %d" % (base_port, base_port + 19))


def run_without_window(httpd, port, thread):
    # Fallback when GTK/WebKit bindings are missing: open the browser instead.
    # The thread ends when the dashboard's Close app button stops the server,
    # so waiting on it (instead of forever) lets that button exit the app too.
    webbrowser.open("http://localhost:%d/admin" % port)
    try:
        thread.join()
    except KeyboardInterrupt:
        httpd.shutdown()


def main():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gio, GLib, Gtk, WebKit2
    except (ImportError, ValueError):
        httpd, port, thread = start_server()
        return run_without_window(httpd, port, thread)

    GLib.set_prgname("filebridge")
    GLib.set_application_name("FileBridge")
    state = {"httpd": None, "port": None, "win": None}
    downloads = os.path.join(GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                             or os.path.expanduser("~/Downloads"))

    def unique_path(name):
        name = os.path.basename(name) or "download"
        base, ext = os.path.splitext(name)
        path, n = os.path.join(downloads, name), 1
        while os.path.exists(path):
            path = os.path.join(downloads, "%s (%d)%s" % (base, n, ext))
            n += 1
        return path

    def notify(app, title, body):
        note = Gio.Notification.new(title)
        note.set_body(body)
        app.send_notification(None, note)

    def on_download(ctx, download, app):
        def decide(dl, suggested):
            os.makedirs(downloads, exist_ok=True)
            path = unique_path(suggested)
            dl.set_allow_overwrite(False)
            dl.set_destination(GLib.filename_to_uri(path, None))
            dl._fb_path = path
            return True

        def finished(dl):
            path = getattr(dl, "_fb_path", None)
            if path and os.path.exists(path):
                notify(app, "Download complete", os.path.basename(path) + " saved to Downloads")

        download.connect("decide-destination", decide)
        download.connect("finished", finished)

    def on_permission(view, request):
        if isinstance(request, WebKit2.NotificationPermissionRequest):
            request.allow()
            return True
        return False

    def new_view(app, related=None):
        view = WebKit2.WebView.new_with_related_view(related) if related else WebKit2.WebView()
        view.connect("permission-request", on_permission)
        view.connect("create", lambda v, action: open_child(app, v, action))
        return view

    def open_child(app, parent_view, action):
        # window.open (e.g. "connect to another device") opens in its own app window
        child = new_view(app, parent_view)
        win = Gtk.ApplicationWindow(application=app, title="FileBridge")
        win.set_default_size(1100, 760)
        win.set_icon_name("filebridge")
        win.add(child)
        child.connect("ready-to-show", lambda v: win.show_all())
        child.connect("close", lambda v: win.destroy())
        child.connect("notify::title", lambda v, p: win.set_title(v.get_title() or "FileBridge"))
        return child

    def on_close(win, event, app):
        state["quitting"] = True
        httpd = state["httpd"]
        if httpd:
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        app.quit()
        return False

    def on_activate(app):
        if state["win"]:
            state["win"].present()
            return
        state["httpd"], state["port"], thread = start_server()
        # If the dashboard's Close app button stops the server, close the
        # window too instead of leaving it on a dead dashboard.
        def _watch():
            thread.join()
            if not state.get("quitting"):
                GLib.idle_add(app.quit)
        threading.Thread(target=_watch, daemon=True).start()
        ctx = WebKit2.WebContext.get_default()
        ctx.connect("download-started", on_download, app)

        win = Gtk.ApplicationWindow(application=app, title="FileBridge")
        win.set_default_size(1180, 800)
        win.set_icon_name("filebridge")
        view = new_view(app)
        view.load_uri("http://localhost:%d/admin" % state["port"])
        win.add(view)
        win.connect("delete-event", on_close, app)
        win.show_all()
        state["win"] = win

    app = Gtk.Application(application_id=APP_ID)
    app.connect("activate", on_activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main() or 0)
