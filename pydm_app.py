#!/usr/bin/env python3
"""PyDM desktop app (GTK 4 + libadwaita).

Front-end for the pydmd daemon (started automatically if needed): sidebar with
status/category filters, live download list, IDM-style progress windows,
"New download" dialog with automatic video-quality detection, preferences,
desktop notifications, drag-and-drop and keyboard shortcuts.

Closing the window leaves the daemon running (so the browser extension keeps
working); use the menu -> "Quit PyDM Completely" to stop it.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

SYNC = GObject.BindingFlags.SYNC_CREATE
BIDI = GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL

APP_ID = "local.pydm.app"
VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
CONF_DIR = os.path.expanduser("~/.config/pydm")
UI_FILE = os.path.join(CONF_DIR, "ui.json")
UI_DEFAULTS = {"popup": True, "notify": True, "background": True, "width": 980, "height": 640}
AUTOSTART = os.path.expanduser("~/.config/autostart/%s.desktop" % APP_ID)

VIDEO_HOSTS = re.compile(
    r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|"
    r"twitch\.tv|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|x\.com|"
    r"twitter\.com|reddit\.com|v\.redd\.it|pinterest\.com|pin\.it|threads\.net|"
    r"snapchat\.com|likee\.video|bilibili\.com|streamable\.com|soundcloud\.com)/", re.I)

CATEGORIES = {
    "video": ("Videos", "video-x-generic-symbolic",
              "mp4 mkv webm avi mov m4v flv wmv ts 3gp m3u8 mpd"),
    "audio": ("Music", "audio-x-generic-symbolic", "mp3 m4a flac wav ogg opus aac wma"),
    "document": ("Documents", "x-office-document-symbolic",
                 "pdf doc docx xls xlsx ppt pptx odt ods odp txt rtf epub csv md"),
    "archive": ("Archives", "package-x-generic-symbolic",
                "zip rar 7z tar gz tgz xz bz2 zst iso img"),
    "program": ("Programs", "application-x-executable-symbolic",
                "exe msi deb rpm appimage apk dmg sh run bin flatpak snap"),
    "image": ("Images", "image-x-generic-symbolic", "png jpg jpeg gif webp svg bmp heic avif"),
    "other": ("Other", "text-x-generic-symbolic", ""),
}
EXT_CAT = {e: c for c, (_, _, exts) in CATEGORIES.items() for e in exts.split()}

STATUS_FILTERS = [
    ("all", "All Downloads", "folder-download-symbolic", lambda j: True),
    ("active", "In Progress", "media-playback-start-symbolic",
     lambda j: j["status"] in ("downloading", "queued")),
    ("paused", "Paused", "media-playback-pause-symbolic",
     lambda j: j["status"] in ("paused", "pending")),
    ("done", "Completed", "object-select-symbolic", lambda j: j["status"] == "done"),
    ("error", "Failed", "dialog-warning-symbolic", lambda j: j["status"] == "error"),
]

CSS = b"""
.type-badge { border-radius: 12px; min-width: 42px; min-height: 42px; }
.type-badge image { -gtk-icon-size: 20px; }
.cat-video    { background: rgba(145, 65, 172, .16); color: #9141ac; }
.cat-audio    { background: rgba(230, 97, 0, .16);   color: #e66100; }
.cat-document { background: rgba(28, 113, 216, .16); color: #1c71d8; }
.cat-archive  { background: rgba(152, 106, 68, .18); color: #986a44; }
.cat-program  { background: rgba(38, 162, 105, .16); color: #26a269; }
.cat-image    { background: rgba(229, 165, 10, .18); color: #c88800; }
.cat-other    { background: rgba(119, 118, 123, .16); color: #77767b; }
.job-row { padding: 10px 14px; }
.job-title { font-weight: 600; }
.job-bar trough, .job-bar progress { min-height: 6px; border-radius: 3px; }
.job-bar.done progress   { background: #2ec27e; }
.job-bar.paused progress { background: #e5a50a; }
.job-bar.failed progress { background: #e01b24; }
.job-meta.failed { color: #e01b24; }
.pct { font-feature-settings: "tnum"; min-width: 3.5em; }
.count { font-size: smaller; font-feature-settings: "tnum"; opacity: .6; }
.sidebar-heading { font-size: smaller; font-weight: 700; opacity: .55; margin: 14px 12px 4px; }
.statusbar { padding: 4px 6px; }
.big-stat { font-size: 1.15em; font-weight: 700; font-feature-settings: "tnum"; }
.stat-cap { font-size: smaller; opacity: .6; }
"""


# --------------------------------------------------------------------------- helpers
def read_json(path, default):
    try:
        with open(path) as f:
            return dict(default, **json.load(f))
    except (OSError, ValueError):
        return dict(default)


def read_conf():
    return read_json(os.path.join(CONF_DIR, "config.json"), {})


def save_ui(ui):
    try:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(UI_FILE, "w") as f:
            json.dump(ui, f)
    except OSError:
        pass


class Api:
    def __init__(self):
        self.port = int(read_conf().get("port", 6800))

    def token(self):
        try:
            return open(os.path.join(CONF_DIR, "token")).read().strip()
        except OSError:
            return ""

    def call(self, path, body=None, timeout=100):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-PyDM-Token": self.token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise RuntimeError(msg)
        except urllib.error.URLError:
            raise RuntimeError("PyDM service is not running")

    def alive(self):
        try:
            self.call("/api/config", timeout=2)
            return True
        except Exception:
            return False

    def ensure_daemon(self):
        if self.alive():
            return True
        os.makedirs(CONF_DIR, exist_ok=True)
        unit = os.path.expanduser("~/.config/systemd/user/pydm.service")
        started = False
        if os.path.exists(unit):              # installed: let systemd own the daemon
            try:
                started = subprocess.run(["systemctl", "--user", "start", "pydm.service"],
                                         timeout=10).returncode == 0
            except (OSError, subprocess.SubprocessError):
                started = False
        if not started:
            log = open(os.path.join(CONF_DIR, "daemon.log"), "ab")
            subprocess.Popen([sys.executable, os.path.join(HERE, "pydmd.py")], cwd=HERE,
                             stdout=log, stderr=log, start_new_session=True)
        for _ in range(40):
            time.sleep(0.2)
            if self.alive():
                self.port = int(read_conf().get("port", self.port))
                return True
        return False


def bg(fn, cb=None):
    """Run fn in a thread; deliver (result, error) to cb on the GTK thread."""
    def work():
        try:
            res, err = fn(), None
        except Exception as e:
            res, err = None, str(e)
        if cb:
            GLib.idle_add(lambda: cb(res, err) and False)
    threading.Thread(target=work, daemon=True).start()


def hum(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024


def eta(sec):
    sec = int(sec)
    if sec < 60:
        return "%ds" % sec
    if sec < 3600:
        return "%dm %02ds" % (sec // 60, sec % 60)
    return "%dh %02dm" % (sec // 3600, sec % 3600 // 60)


def open_path(path):
    if path and os.path.exists(path):
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path), None)
        return True
    return False


def show_in_folder(path, folder):
    """Open the folder with the file selected when the file manager supports it."""
    if path and os.path.exists(path):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            bus.call_sync("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                          "org.freedesktop.FileManager1", "ShowItems",
                          GLib.Variant("(ass)", ([GLib.filename_to_uri(path)], "")),
                          None, Gio.DBusCallFlags.NONE, 2000, None)
            return
        except GLib.Error:
            pass
    open_path(folder)


def set_autostart(on):
    try:
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            exe = os.path.expanduser("~/.local/bin/pydm-gui")
            cmd = exe if os.path.exists(exe) else "%s %s" % (sys.executable, os.path.abspath(__file__))
            with open(AUTOSTART, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=PyDM\nExec=%s --background\n"
                        "Icon=%s\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n" % (cmd, APP_ID))
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)
    except OSError:
        pass


def job_path(cfg, j):
    if j.get("path"):
        return j["path"]
    name = os.path.basename(j.get("name") or "")
    sub = j.get("subdir", j.get("vdir", "")) or ""
    return os.path.join(cfg.get("download_dir", ""), sub, name) if name else ""


def display_name(j):
    if j.get("name"):
        return j["name"]
    p = urllib.parse.urlparse(j["url"])
    tail = urllib.parse.unquote(p.path.rstrip("/").rsplit("/", 1)[-1])
    if j.get("kind") == "video" or not tail:
        return "Video from " + p.netloc.removeprefix("www.")
    return tail


def category(j):
    name = j.get("name") or urllib.parse.urlparse(j["url"]).path
    ext = name.rsplit(".", 1)[-1].lower() if "." in name[-8:] else ""
    if ext in EXT_CAT:
        return EXT_CAT[ext]
    if j.get("kind") == "video":
        return "audio" if j.get("quality") == "audio" else "video"
    return "other"


def badge(cat, size=42):
    b = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
    b.add_css_class("type-badge")
    b.add_css_class("cat-" + cat)
    b.set_size_request(size, size)
    img = Gtk.Image(icon_name=CATEGORIES[cat][1], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                    hexpand=False)
    b.set_hexpand(False)
    img.set_size_request(size, size)
    b.append(img)
    return b


def lbl(text="", css=(), xalign=0.0, ellipsize=Pango.EllipsizeMode.END, **kw):
    l = Gtk.Label(label=text, xalign=xalign, **kw)
    if ellipsize:
        l.set_ellipsize(ellipsize)
    for c in ([css] if isinstance(css, str) else css):
        l.add_css_class(c)
    return l


def icon_button(icon, tip, cb, *css):
    b = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    b.add_css_class("circular")
    for c in css:
        b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


def describe(j, short=False):
    """One-line human status for a job."""
    st = j["status"]
    size, done, spd = j.get("size") or 0, j.get("done") or 0, j.get("speed") or 0
    if st == "downloading":
        parts = ["%s of %s" % (hum(done), hum(size)) if size else hum(done)]
        if spd:
            parts.append("%s/s" % hum(spd))
            if size > done:
                parts.append(eta((size - done) / spd) + " left")
        elif not done:
            parts.append("Connecting…")
        return " · ".join(parts)
    if st == "queued":
        if j.get("retry_note"):
            wait = j.get("start_at", 0) - time.time()
            done_txt = " · %s of %s saved" % (hum(done), hum(size)) if size and done else ""
            return j["retry_note"] + (" · next try in %s" % eta(wait) if wait > 1 else "") + done_txt
        if j.get("start_at", 0) > time.time():
            return "Scheduled for " + time.strftime("%H:%M, %d %b", time.localtime(j["start_at"]))
        return "Queued"
    if st == "paused":
        return "Paused" + (" · %s of %s" % (hum(done), hum(size)) if size else "")
    if st == "pending":
        return "Waiting to start (from clipboard)"
    if st == "done":
        return hum(size or done) + " · " + (j.get("note") or "Completed")
    if st == "error":
        return "Failed: " + (j.get("error") or "unknown error")
    return st


# --------------------------------------------------------------------------- job row
class JobRow(Gtk.ListBoxRow):
    def __init__(self, app, j):
        super().__init__(activatable=True)
        self.app, self.jid, self.j = app, j["id"], j
        self.add_css_class("job-row")
        self.cat = None
        outer = Gtk.Box(spacing=14)
        self.badge_slot = Gtk.Box(valign=Gtk.Align.CENTER, hexpand=False)
        outer.append(self.badge_slot)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True,
                      valign=Gtk.Align.CENTER)
        self.title = lbl(css="job-title", ellipsize=Pango.EllipsizeMode.MIDDLE)
        mid.append(self.title)
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self.bar.add_css_class("job-bar")
        mid.append(self.bar)
        meta = Gtk.Box(spacing=8)
        self.meta = lbl(css=("caption", "job-meta", "dim-label"), hexpand=True)
        self.pct = lbl(css=("caption", "dim-label", "pct"), xalign=1.0)
        meta.append(self.meta)
        meta.append(self.pct)
        mid.append(meta)
        outer.append(mid)

        acts = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        self.b_main = icon_button("media-playback-pause-symbolic", "Pause", self.on_main)
        self.b_open = Gtk.Button(label="Open", valign=Gtk.Align.CENTER, tooltip_text="Open file")
        self.b_open.add_css_class("flat")
        self.b_open.connect("clicked", lambda *_: self.on_open())
        self.b_dir = icon_button("folder-open-symbolic", "Show in folder",
                                 lambda: show_in_folder(self.path, app.cfg.get("download_dir")))
        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Copy Link", "row.copy")
        sec1.append("Open Source Page", "row.browse")
        sec2 = Gio.Menu()
        sec2.append("Remove from List", "row.remove")
        sec2.append("Delete File…", "row.delete")
        menu.append_section(None, sec1)
        menu.append_section(None, sec2)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                              tooltip_text="More", valign=Gtk.Align.CENTER)
        more.add_css_class("flat")
        more.add_css_class("circular")
        for w in (self.b_main, self.b_open, self.b_dir, more):
            acts.append(w)
        outer.append(acts)
        self.set_child(outer)

        ag = Gio.SimpleActionGroup()
        for name, cb in (("copy", lambda: app.copy_text(self.j["url"], "Link copied")),
                         ("browse", lambda: Gio.AppInfo.launch_default_for_uri(
                             (self.j.get("headers") or {}).get("Referer") or self.j["url"], None)),
                         ("remove", lambda: app.act(self.jid, "remove")),
                         ("delete", lambda: app.confirm_delete(self.j))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            ag.add_action(a)
        self.insert_action_group("row", ag)
        self.path = ""
        self.update(j)

    def on_main(self):
        st = self.j["status"]
        self.app.act(self.jid, "pause" if st in ("queued", "downloading") else "start")

    def on_open(self):
        if not open_path(self.path):
            self.app.toast("File not found – it may have been moved or deleted")

    def update(self, j):
        self.j = j
        self.path = job_path(self.app.cfg, j)
        st = j["status"]
        cat = category(j)
        if cat != self.cat:
            self.cat = cat
            if self.badge_slot.get_first_child():
                self.badge_slot.remove(self.badge_slot.get_first_child())
            self.badge_slot.append(badge(cat))
        self.title.set_text(display_name(j))
        self.set_tooltip_text(j["url"])
        size, done = j.get("size") or 0, j.get("done") or 0
        frac = 1.0 if st == "done" else (min(1.0, done / size) if size else 0)
        if st == "downloading" and not size:
            self.bar.pulse()
        else:
            self.bar.set_fraction(frac)
        self.bar.set_visible(st in ("downloading", "queued", "paused") and bool(size or st == "downloading"))
        for c, on in (("done", st == "done"),
                      ("paused", st in ("paused", "pending") or bool(j.get("retry_note"))),
                      ("failed", st == "error")):
            (self.bar.add_css_class if on else self.bar.remove_css_class)(c)
        (self.meta.add_css_class if st == "error" else self.meta.remove_css_class)("failed")
        self.meta.set_text(describe(j))
        self.pct.set_text("%d%%" % (frac * 100) if size and st != "done" else "")
        icon, tip = {"downloading": ("media-playback-pause-symbolic", "Pause"),
                     "queued": ("media-playback-pause-symbolic", "Pause"),
                     "error": ("view-refresh-symbolic", "Retry")}.get(
            st, ("media-playback-start-symbolic", "Resume" if st == "paused" else "Start"))
        self.b_main.set_icon_name(icon)
        self.b_main.set_tooltip_text(tip)
        self.b_main.set_visible(st != "done")
        self.b_open.set_visible(st == "done")
        self.b_dir.set_visible(st == "done")


# --------------------------------------------------------------------------- progress window
class ProgressWindow(Adw.Window):
    """IDM-style window shown when a download begins."""

    def __init__(self, app, j):
        super().__init__(title="PyDM", default_width=470, resizable=False)
        self.app, self.jid, self.j = app, j["id"], j
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        hb.add_css_class("flat")
        self.wtitle = Adw.WindowTitle(title="Downloading")
        hb.set_title_widget(self.wtitle)
        tv.add_top_bar(hb)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_start=20, margin_end=20, margin_bottom=20, margin_top=4)
        head = Gtk.Box(spacing=14)
        self.badge_slot = Gtk.Box()
        head.append(self.badge_slot)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
        self.name = lbl(css="heading", ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.name.set_max_width_chars(40)
        self.host = lbl(css=("caption", "dim-label"))
        tb.append(self.name)
        tb.append(self.host)
        head.append(tb)
        body.append(head)

        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("job-bar")
        body.append(self.bar)

        grid = Gtk.Grid(column_spacing=10, row_spacing=2, column_homogeneous=True)
        self.stats = {}
        for i, (k, cap) in enumerate((("done", "Downloaded"), ("size", "Size"),
                                      ("speed", "Speed"), ("eta", "Time left"))):
            v = lbl("—", "big-stat")
            grid.attach(v, i, 0, 1, 1)
            grid.attach(lbl(cap, "stat-cap"), i, 1, 1, 1)
            self.stats[k] = v
        body.append(grid)
        self.err = lbl(css=("caption", "error"), ellipsize=None, wrap=True, visible=False)
        body.append(self.err)

        row = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=4)
        self.b_pause = Gtk.Button(label="Pause")
        self.b_pause.add_css_class("pill")
        self.b_pause.connect("clicked", self.on_pause)
        self.b_dir = Gtk.Button(label="Show in Folder")
        self.b_dir.add_css_class("pill")
        self.b_dir.connect("clicked", lambda *_: show_in_folder(
            job_path(app.cfg, self.j), app.cfg.get("download_dir")))
        self.b_open = Gtk.Button(label="Open")
        self.b_open.add_css_class("pill")
        self.b_open.add_css_class("suggested-action")
        self.b_open.connect("clicked", lambda *_: (open_path(job_path(app.cfg, self.j)), self.close()))
        b_hide = Gtk.Button(label="Hide")
        b_hide.add_css_class("pill")
        b_hide.connect("clicked", lambda *_: self.close())
        for b in (b_hide, self.b_pause, self.b_dir, self.b_open):
            row.append(b)
        body.append(row)
        tv.set_content(body)
        self.set_content(tv)
        self.cat = None
        self.update(j)

    def on_pause(self, *_):
        st = self.j["status"]
        self.app.act(self.jid, "pause" if st in ("queued", "downloading") else "start")

    def update(self, j):
        self.j = j
        st = j["status"]
        cat = category(j)
        if cat != self.cat:
            self.cat = cat
            if self.badge_slot.get_first_child():
                self.badge_slot.remove(self.badge_slot.get_first_child())
            self.badge_slot.append(badge(cat, 48))
        self.name.set_text(display_name(j))
        self.host.set_text(urllib.parse.urlparse(j["url"]).netloc)
        size, done, spd = j.get("size") or 0, j.get("done") or 0, j.get("speed") or 0
        frac = 1.0 if st == "done" else (min(1.0, done / size) if size else 0)
        if st == "downloading" and not size:
            self.bar.pulse()
        else:
            self.bar.set_fraction(frac)
        for c, on in (("done", st == "done"), ("paused", st == "paused"), ("failed", st == "error")):
            (self.bar.add_css_class if on else self.bar.remove_css_class)(c)
        titles = {"downloading": "Downloading %d%%" % (frac * 100) if size else "Downloading",
                  "queued": "Waiting in queue", "paused": "Paused", "done": "Download complete",
                  "error": "Download failed", "pending": "Not started"}
        self.wtitle.set_title(titles.get(st, st))
        self.set_title(titles.get(st, st) + " – " + display_name(j))
        self.stats["done"].set_text(hum(done) if done else "—")
        self.stats["size"].set_text(hum(size) if size else "Unknown")
        self.stats["speed"].set_text("%s/s" % hum(spd) if spd and st == "downloading" else "—")
        self.stats["eta"].set_text(eta((size - done) / spd) if spd and size > done
                                   and st == "downloading" else ("Done" if st == "done" else "—"))
        self.err.set_visible(st == "error")
        self.err.set_text(j.get("error") or "")
        self.b_pause.set_visible(st in ("queued", "downloading", "paused", "error", "pending"))
        self.b_pause.set_label({"queued": "Pause", "downloading": "Pause",
                                "error": "Retry"}.get(st, "Resume"))
        self.b_open.set_visible(st == "done")
        self.b_dir.set_visible(st == "done")


# --------------------------------------------------------------------------- new download
class AddDialog(Adw.Dialog):
    def __init__(self, app, url=""):
        super().__init__(title="New Download", content_width=560)
        self.app = app
        self.qvals, self.check_id, self.checked_url, self.is_video = ["best"], 0, None, False
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        g1 = Adw.PreferencesGroup()
        self.url = Adw.EntryRow(title="Link (URL)")
        self.url.set_input_purpose(Gtk.InputPurpose.URL)
        self.url.connect("changed", self.on_url_changed)
        self.url.connect("entry-activated", lambda *_: self.on_add())
        paste = icon_button("edit-paste-symbolic", "Paste", self.paste)
        self.url.add_suffix(paste)
        g1.add(self.url)

        self.detect = Adw.ActionRow(title="Paste a link to a file or a video page",
                                    subtitle_lines=2)
        self.detect_icon = Gtk.Image(icon_name="globe-symbolic")
        self.detect.add_prefix(self.detect_icon)
        self.spinner = Adw.Spinner(visible=False)
        self.detect.add_suffix(self.spinner)
        self.look = Gtk.Button(label="Find Video", valign=Gtk.Align.CENTER, visible=False,
                               tooltip_text="Ask yt-dlp whether this page has a video")
        self.look.add_css_class("flat")
        self.look.connect("clicked", lambda *_: self.check(force=True))
        self.detect.add_suffix(self.look)
        g1.add(self.detect)

        self.quality = Adw.ComboRow(title="Quality", visible=False,
                                    model=Gtk.StringList.new(["Best available"]))
        g1.add(self.quality)
        page.add(g1)

        g2 = Adw.PreferencesGroup(title="Options")
        self.later = Adw.SwitchRow(title="Start Later", subtitle="Schedule this download")
        g2.add(self.later)
        self.when = Adw.ActionRow(title="Start at", visible=False)
        tbox = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        lt = time.localtime(time.time() + 3600)
        self.hh = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.mm = Gtk.SpinButton.new_with_range(0, 59, 1)
        for sb, v in ((self.hh, lt.tm_hour), (self.mm, 0)):
            sb.set_value(v)
            sb.set_numeric(True)
            sb.connect("output", self._two_digits)
            sb.set_orientation(Gtk.Orientation.VERTICAL)
        tbox.append(self.hh)
        tbox.append(lbl(":", "title-3"))
        tbox.append(self.mm)
        self.when.add_suffix(tbox)
        self.later.connect("notify::active", lambda *_: self.when.set_visible(self.later.get_active()))
        g2.add(self.when)
        folder = Adw.ActionRow(title="Save to", subtitle=GLib.markup_escape_text(
            app.cfg.get("download_dir", "") + ("  ·  sorted into folders by type"
                                               if app.cfg.get("organize", True) else "")))
        folder.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        g2.add(folder)
        page.add(g2)

        tv.set_content(page)
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=18,
                         margin_top=6, halign=Gtk.Align.CENTER)
        self.msg = lbl(css=("caption", "error"), xalign=0.5, ellipsize=None, wrap=True,
                       max_width_chars=60, visible=False)
        bottom.append(self.msg)
        self.ok = Gtk.Button(label="Download", sensitive=False)
        self.ok.add_css_class("pill")
        self.ok.add_css_class("suggested-action")
        self.ok.connect("clicked", lambda *_: self.on_add())
        bottom.append(self.ok)
        tv.add_bottom_bar(bottom)
        self.set_child(tv)
        self.set_default_widget(self.ok)

        if url:
            self.url.set_text(url)
        else:
            self.paste(only_if_url=True)

    @staticmethod
    def _two_digits(sb):
        sb.set_text("%02d" % sb.get_value_as_int())
        return True

    def paste(self, only_if_url=False):
        def got(cb, res):
            try:
                t = (cb.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if re.match(r"^https?://\S+$", t) or not only_if_url:
                self.url.set_text(t)
        Gdk.Display.get_default().get_clipboard().read_text_async(None, got)

    def on_url_changed(self, *_):
        u = self.url.get_text().strip()
        valid = bool(re.match(r"^https?://[^\s/]+\S*$", u))
        self.ok.set_sensitive(valid)
        self.msg.set_visible(False)
        self.quality.set_visible(False)
        self.is_video, self.checked_url = False, None
        self.check_id += 1
        self.spinner.set_visible(False)
        if not u:
            self._detect("globe-symbolic", "Paste a link to a file or a video page", "")
            self.look.set_visible(False)
            return
        if not valid:
            self._detect("dialog-warning-symbolic", "That doesn't look like a web link",
                         "Links start with http:// or https://")
            self.look.set_visible(False)
            return
        if VIDEO_HOSTS.match(u):
            cid = self.check_id
            GLib.timeout_add(450, lambda: (cid == self.check_id and self.check()) and False)
        else:
            name = urllib.parse.unquote(urllib.parse.urlparse(u).path.rsplit("/", 1)[-1]) or u
            self._detect("text-x-generic-symbolic", "File download",
                         GLib.markup_escape_text(name))
            self.look.set_visible(True)

    def _detect(self, icon, title, sub):
        self.detect_icon.set_from_icon_name(icon)
        self.detect.set_title(GLib.markup_escape_text(title))
        self.detect.set_subtitle(sub)

    def check(self, force=False):
        u = self.url.get_text().strip()
        cid = self.check_id
        self.look.set_visible(False)
        self.spinner.set_visible(True)
        self._detect("video-x-generic-symbolic", "Looking for video…",
                     GLib.markup_escape_text(urllib.parse.urlparse(u).netloc))

        def done(res, err):
            if cid != self.check_id:
                return
            self.spinner.set_visible(False)
            if err:
                if force:
                    self._detect("text-x-generic-symbolic", "No video found – will download as a file",
                                 GLib.markup_escape_text(err[:160]))
                else:
                    # known video site: still let yt-dlp try with the browser's cookies
                    self.is_video = True
                    self._detect("dialog-warning-symbolic", "Couldn't read video details",
                                 GLib.markup_escape_text(err[:200]))
                return
            self.is_video, self.checked_url = True, u
            self.qvals = ["best"] + [str(h) for h in res.get("heights") or []]
            names = ["Best available"] + ["%dp" % h for h in res.get("heights") or []]
            if res.get("audio"):
                self.qvals.append("audio")
                names.append("Audio only (MP3)")
            self.quality.set_model(Gtk.StringList.new(names))
            self.quality.set_selected(0)
            self.quality.set_visible(True)
            dur = res.get("duration")
            sub = ("🔴 LIVE  " if res.get("live") else "") + (
                time.strftime("%M:%S" if dur < 3600 else "%H:%M:%S", time.gmtime(dur))
                if dur else "")
            self._detect("video-x-generic-symbolic", res.get("title") or "Video",
                         GLib.markup_escape_text(sub.strip() or urllib.parse.urlparse(u).netloc))
        bg(lambda: self.app.api.call("/api/formats", {"url": u}), done)

    def on_add(self):
        u = self.url.get_text().strip()
        if not self.ok.get_sensitive():
            return
        start = 0
        if self.later.get_active():
            lt = time.localtime()
            start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, self.hh.get_value_as_int(),
                                 self.mm.get_value_as_int(), 0, 0, 0, -1))
            if start < time.time():
                start += 86400
        q = self.qvals[self.quality.get_selected()] if self.quality.get_visible() else "best"
        body = {"url": u, "start_at": start, "quality": q,
                "kind": "video" if self.is_video else ("file" if self.checked_url is None
                                                       and not VIDEO_HOSTS.match(u) else None)}
        self.ok.set_sensitive(False)

        def done(res, err):
            self.ok.set_sensitive(True)
            if err:
                self.msg.set_text(err)
                self.msg.set_visible(True)
            else:
                self.close()
                self.app.toast("Scheduled for " + time.strftime("%H:%M", time.localtime(start))
                               if start else "Download added")
                self.app.refresh()
        bg(lambda: self.app.api.call("/api/add", body), done)


# --------------------------------------------------------------------------- preferences
class Preferences(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(search_enabled=False)
        self.app = app
        c = app.cfg

        p1 = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        g = Adw.PreferencesGroup(title="Downloads")
        self.dir = c.get("download_dir", "")
        self.dir_row = Adw.ActionRow(title="Download Folder", subtitle=GLib.markup_escape_text(self.dir))
        self.dir_row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        choose = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.connect("clicked", self.choose_dir)
        self.dir_row.add_suffix(choose)
        g.add(self.dir_row)
        self.organize = Adw.SwitchRow(
            title="Sort into Folders by Type",
            subtitle="Videos, Music, Documents, Archives, Programs, Images and Other",
            active=c.get("organize", True))
        g.add(self.organize)
        tidy = Adw.ActionRow(title="Tidy Up Earlier Downloads",
                             subtitle="Move finished PyDM downloads into the type folders. "
                                      "Other files are never touched.")
        self.tidy_btn = Gtk.Button(label="Tidy Up", valign=Gtk.Align.CENTER)
        self.tidy_btn.connect("clicked", self.tidy)
        tidy.add_suffix(self.tidy_btn)
        g.add(tidy)
        p1.add(g)
        g = Adw.PreferencesGroup(title="Behaviour")
        self.popup = Adw.SwitchRow(title="Show Progress Window",
                                   subtitle="Open a small window when a download starts",
                                   active=app.ui["popup"])
        self.notify = Adw.SwitchRow(title="Notifications",
                                    subtitle="Notify when downloads finish or fail",
                                    active=app.ui["notify"])
        self.bgrun = Adw.SwitchRow(title="Keep Running in Background",
                                   subtitle="Closing the window keeps PyDM ready to show downloads "
                                            "from your browser",
                                   active=app.ui["background"])
        self.login = Adw.SwitchRow(title="Start at Login", subtitle="Start PyDM hidden when you log in",
                                   active=os.path.exists(AUTOSTART))
        self.clip = Adw.SwitchRow(title="Watch Clipboard",
                                  subtitle="Copied download links appear in the list, ready to start",
                                  active=bool(c.get("clipboard")))
        for w in (self.popup, self.notify, self.bgrun, self.login, self.clip):
            g.add(w)
        p1.add(g)
        self.add(p1)

        p2 = Adw.PreferencesPage(title="Connection", icon_name="network-transmit-receive-symbolic")
        g = Adw.PreferencesGroup(title="Speed",
                                 description="More connections usually download big files faster.")
        self.conn = Adw.SpinRow.new_with_range(1, 32, 1)
        self.conn.set_title("Connections per File")
        self.conn.set_value(c.get("connections", 8))
        self.par = Adw.SpinRow.new_with_range(1, 10, 1)
        self.par.set_title("Simultaneous Downloads")
        self.par.set_value(c.get("max_parallel", 3))
        self.lim = Adw.SpinRow.new_with_range(0, 1_000_000, 100)
        self.lim.set_title("Speed Limit")
        self.lim.set_subtitle("KB/s · 0 means unlimited")
        self.lim.set_value((c.get("limit") or 0) // 1024)
        for w in (self.conn, self.par, self.lim):
            g.add(w)
        p2.add(g)
        self.add(p2)

        p3 = Adw.PreferencesPage(title="Scheduler", icon_name="alarm-symbolic")
        g = Adw.PreferencesGroup(title="Download Window",
                                 description="Only download during these hours, e.g. at night. "
                                             "Downloads pause outside the window and resume automatically.")
        w = c.get("window") or None
        self.win_on = Adw.SwitchRow(title="Limit to a Time Window", active=bool(w))
        g.add(self.win_on)
        a, b = (w or ["01:00", "07:00"])
        self.w_from, self.f_h, self.f_m = self._time_row("From", a)
        self.w_to, self.t_h, self.t_m = self._time_row("Until", b)
        for r in (self.w_from, self.w_to):
            g.add(r)
            self.win_on.bind_property("active", r, "sensitive", SYNC)
        p3.add(g)
        self.add(p3)

        p4 = Adw.PreferencesPage(title="Browser", icon_name="web-browser-symbolic")
        g = Adw.PreferencesGroup(
            title="Browser Extension",
            description="Catches downloads and adds a “Download video” button on YouTube, TikTok, "
                        "Instagram and other sites. Works with Chrome, Chromium, Brave and Edge.")
        ext = os.path.join(HERE, "extension")
        r = Adw.ActionRow(title="1. Open chrome://extensions",
                          subtitle="Turn on Developer mode")
        cp = icon_button("edit-copy-symbolic", "Copy address",
                         lambda: app.copy_text("chrome://extensions", "Address copied"))
        r.add_suffix(cp)
        g.add(r)
        r = Adw.ActionRow(title="2. Load unpacked → choose this folder", subtitle=GLib.markup_escape_text(ext),
                          subtitle_selectable=True)
        r.add_suffix(icon_button("edit-copy-symbolic", "Copy path",
                                 lambda: app.copy_text(ext, "Folder path copied")))
        r.add_suffix(icon_button("folder-open-symbolic", "Open folder", lambda: open_path(ext)))
        g.add(r)
        r = Adw.ActionRow(title="3. Paste the pairing token in the extension’s Options",
                          subtitle="Keep it private – it lets apps add downloads")
        tk = Gtk.Button(label="Copy Token", valign=Gtk.Align.CENTER)
        tk.add_css_class("suggested-action")
        tk.connect("clicked", lambda *_: app.copy_text(app.api.token(), "Token copied"))
        r.add_suffix(tk)
        g.add(r)
        p4.add(g)
        self.add(p4)
        self.connect("closed", lambda *_: self.save())

    def _time_row(self, title, value):
        h, m = (int(x) for x in value.split(":"))
        row = Adw.ActionRow(title=title)
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        hh = Gtk.SpinButton.new_with_range(0, 23, 1)
        mm = Gtk.SpinButton.new_with_range(0, 59, 5)
        for sb, v in ((hh, h), (mm, m)):
            sb.set_value(v)
            sb.connect("output", AddDialog._two_digits)
        box.append(hh)
        box.append(lbl(":"))
        box.append(mm)
        row.add_suffix(box)
        return row, hh, mm

    def tidy(self, *_):
        self.tidy_btn.set_sensitive(False)

        def done(res, err):
            self.tidy_btn.set_sensitive(True)
            n = (res or {}).get("moved", 0)
            msg = err or ("Moved %d file%s into folders" % (n, "" if n == 1 else "s") if n
                          else "Everything is already tidy")
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg)))
            self.app.refresh()
        bg(lambda: self.app.api.call("/api/organize", {}), done)

    def choose_dir(self, *_):
        fd = Gtk.FileDialog(title="Choose Download Folder")
        if self.dir and os.path.isdir(self.dir):
            fd.set_initial_folder(Gio.File.new_for_path(self.dir))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.dir = f.get_path()
                self.dir_row.set_subtitle(GLib.markup_escape_text(self.dir))
        fd.select_folder(self.app.win, None, done)

    def save(self):
        ui = dict(popup=self.popup.get_active(), notify=self.notify.get_active(),
                  background=self.bgrun.get_active())
        ui_changed = any(self.app.ui.get(k) != v for k, v in ui.items())
        if ui_changed:
            self.app.ui.update(ui)
            save_ui(self.app.ui)
        if self.login.get_active() != os.path.exists(AUTOSTART):
            set_autostart(self.login.get_active())
            ui_changed = True
        fmt = lambda h, m: "%02d:%02d" % (h.get_value_as_int(), m.get_value_as_int())
        body = {"download_dir": self.dir, "connections": int(self.conn.get_value()),
                "max_parallel": int(self.par.get_value()),
                "limit": int(self.lim.get_value()) * 1024, "clipboard": self.clip.get_active(),
                "organize": self.organize.get_active(),
                "window": [fmt(self.f_h, self.f_m), fmt(self.t_h, self.t_m)]
                if self.win_on.get_active() else None}
        c = self.app.cfg
        if all(c.get(k, True if k == "organize" else None) == v
               for k, v in body.items() if k != "window") and \
                (c.get("window") or None) == body["window"]:
            if ui_changed:
                self.app.toast("Settings saved")
            return

        def done(res, err):
            if err:
                self.app.toast("Couldn't save settings: " + err)
            else:
                self.app.cfg = res
                self.app.toast("Settings saved")
        bg(lambda: self.app.api.call("/api/config", body), done)


# --------------------------------------------------------------------------- main window
class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PyDM",
                         default_width=app.ui["width"], default_height=app.ui["height"])
        self.app = app
        self.filter = "all"
        self.search = ""

        # ---- sidebar
        self.side_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.side_list.add_css_class("navigation-sidebar")
        self.side_rows, self.side_counts = {}, {}
        entries = [(k, t, i) for k, t, i, _ in STATUS_FILTERS] + \
                  [(k, v[0], v[1]) for k, v in CATEGORIES.items()]
        for key, title, icon in entries:
            r = Gtk.ListBoxRow()
            r.key = key
            b = Gtk.Box(spacing=12, margin_start=6, margin_end=6, margin_top=2, margin_bottom=2)
            b.append(Gtk.Image(icon_name=icon))
            b.append(lbl(title, hexpand=True))
            cnt = lbl("", "count", xalign=1.0)
            b.append(cnt)
            r.set_child(b)
            self.side_list.append(r)
            self.side_rows[key], self.side_counts[key] = r, cnt
        self.side_list.set_header_func(self._side_header)
        self.side_list.connect("row-selected", self.on_side)
        side_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroll.set_child(self.side_list)
        side_tv = Adw.ToolbarView()
        side_hb = Adw.HeaderBar()
        side_hb.set_title_widget(Adw.WindowTitle(title="PyDM"))
        side_tv.add_top_bar(side_hb)
        side_tv.set_content(side_scroll)
        disk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=14,
                       margin_end=14, margin_bottom=12, margin_top=6)
        self.disk_lbl = lbl("", ("caption", "dim-label"))
        self.disk_bar = Gtk.LevelBar(min_value=0, max_value=1)
        for off in ("low", "high", "full"):
            self.disk_bar.remove_offset_value(off)
        disk.append(self.disk_lbl)
        disk.append(self.disk_bar)
        side_tv.add_bottom_bar(disk)

        # ---- content
        self.title = Adw.WindowTitle(title="All Downloads")
        hb = Adw.HeaderBar()
        hb.set_title_widget(self.title)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="New Download (Ctrl+N)",
                         action_name="app.add")
        add.add_css_class("suggested-action")
        hb.pack_start(add)
        self.side_toggle = Gtk.ToggleButton(icon_name="sidebar-show-symbolic", visible=False,
                                            tooltip_text="Show Sidebar")
        hb.pack_start(self.side_toggle)
        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("Resume All", "app.resume-all")
        s1.append("Pause All", "app.pause-all")
        s1.append("Clear Completed", "app.clear-done")
        s2 = Gio.Menu()
        s2.append("Open Download Folder", "app.open-folder")
        s2.append("Preferences", "app.preferences")
        s2.append("Keyboard Shortcuts", "app.shortcuts")
        s2.append("About PyDM", "app.about")
        s3 = Gio.Menu()
        s3.append("Quit PyDM Completely", "app.quit-all")
        for s in (s1, s2, s3):
            menu.append_section(None, s)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                            tooltip_text="Main Menu")
        hb.pack_end(mb)
        self.search_btn = Gtk.ToggleButton(icon_name="system-search-symbolic",
                                           tooltip_text="Search (Ctrl+F)")
        hb.pack_end(self.search_btn)

        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search downloads", hexpand=True)
        clamp = Adw.Clamp(maximum_size=480, child=self.search_entry)
        self.search_bar.set_child(clamp)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_key_capture_widget(self)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled", BIDI)
        self.search_entry.connect("search-changed", self.on_search)

        self.banner = Adw.Banner(title="Connecting to the PyDM service…", button_label="Start Service")
        self.banner.connect("button-clicked", lambda *_: app.start_daemon())

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.set_filter_func(self._filter)
        self.list.set_sort_func(lambda a, b: (b.j["added"] > a.j["added"]) - (b.j["added"] < a.j["added"]))
        self.list.connect("row-activated", self.on_activate)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900, child=self.list,
                          margin_top=12, margin_bottom=18, margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(clamp)

        new_btn = Gtk.Button(label="New Download", halign=Gtk.Align.CENTER, action_name="app.add")
        new_btn.add_css_class("pill")
        new_btn.add_css_class("suggested-action")
        self.empty = Adw.StatusPage(icon_name="folder-download-symbolic", title="No Downloads",
                                    description="Add a link, drop one here, or use the "
                                                "“Download video” button in your browser.",
                                    child=new_btn, vexpand=True)
        self.no_match = Adw.StatusPage(icon_name="edit-find-symbolic", title="Nothing Here",
                                       description="No downloads match this view.", vexpand=True)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=150)
        self.stack.add_named(scroll, "list")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.no_match, "nomatch")

        status = Gtk.Box(spacing=12)
        status.add_css_class("statusbar")
        self.st_speed = lbl("", "caption")
        self.st_active = lbl("", ("caption", "dim-label"), hexpand=True)
        self.st_sched = lbl("", ("caption", "warning"))
        status.append(Gtk.Image(icon_name="network-receive-symbolic"))
        status.append(self.st_speed)
        status.append(self.st_active)
        status.append(self.st_sched)
        action_bar = Gtk.ActionBar()
        action_bar.pack_start(status)
        status.set_hexpand(True)

        content_tv = Adw.ToolbarView()
        content_tv.add_top_bar(hb)
        content_tv.add_top_bar(self.search_bar)
        content_tv.add_top_bar(self.banner)
        content_tv.set_content(self.stack)
        content_tv.add_bottom_bar(action_bar)
        self.toasts = Adw.ToastOverlay(child=content_tv)

        self.split = Adw.OverlaySplitView(
            sidebar=Adw.NavigationPage(title="PyDM", child=side_tv),
            content=Adw.NavigationPage(title="Downloads", child=self.toasts),
            min_sidebar_width=210, max_sidebar_width=260)
        self.split.bind_property("show-sidebar", self.side_toggle, "active", BIDI)
        self.set_content(self.split)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 700sp"))
        bp.add_setter(self.split, "collapsed", True)
        bp.add_setter(self.side_toggle, "visible", True)
        self.add_breakpoint(bp)

        drop = Gtk.DropTarget.new(str, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)

        self.side_list.select_row(self.side_rows["all"])
        self.connect("close-request", self.on_close)
        self.stack.set_visible_child_name("empty")

    def _side_header(self, row, before):
        if row.key == "video":
            h = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            h.append(lbl("CATEGORIES", "sidebar-heading"))
            row.set_header(h)
        else:
            row.set_header(None)

    def on_side(self, _lb, row):
        if not row:
            return
        self.filter = row.key
        title = next((t for k, t, _, _ in STATUS_FILTERS if k == row.key), None) \
            or CATEGORIES[row.key][0]
        self.title.set_title(title)
        self.list.invalidate_filter()
        self.update_views()
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def on_search(self, entry):
        self.search = entry.get_text().strip().lower()
        self.list.invalidate_filter()
        self.update_views()

    def matches(self, j):
        f = self.filter
        ok = next((fn(j) for k, _, _, fn in STATUS_FILTERS if k == f), None)
        if ok is None:
            ok = category(j) == f
        if ok and self.search:
            ok = self.search in (display_name(j) + " " + j["url"]).lower()
        return ok

    def _filter(self, row):
        return self.matches(row.j)

    def on_activate(self, _lb, row):
        if row.j["status"] == "done":
            row.on_open()
        else:
            self.app.show_progress(row.j)

    def on_drop(self, _t, value, _x, _y):
        url = (value or "").strip().splitlines()[0] if value else ""
        if re.match(r"^https?://", url):
            AddDialog(self.app, url).present(self)
            return True
        self.toast_msg("Drop a web link (http/https) to download it")
        return False

    def on_close(self, *_):
        w, h = self.get_default_size()
        self.app.ui.update(width=w, height=h)
        save_ui(self.app.ui)
        if self.app.ui.get("background", True):
            self.set_visible(False)          # keep running: popups/notifications still work
            self.app.keep_alive()
            return True
        return False

    def toast_msg(self, text, timeout=3):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        self.toasts.add_toast(t)

    def update_views(self):
        jobs = self.app.jobs
        if not jobs:
            self.stack.set_visible_child_name("empty")
        elif not any(self.matches(j) for j in jobs.values()):
            self.no_match.set_title("No Results" if self.search else "Nothing Here")
            self.stack.set_visible_child_name("nomatch")
        else:
            self.stack.set_visible_child_name("list")
        # counts
        for k, _, _, fn in STATUS_FILTERS:
            n = sum(1 for j in jobs.values() if fn(j))
            self.side_counts[k].set_text(str(n) if n else "")
        for k in CATEGORIES:
            n = sum(1 for j in jobs.values() if category(j) == k)
            self.side_counts[k].set_text(str(n) if n else "")
            self.side_rows[k].set_visible(bool(n) or k == self.filter)

    def update_status(self, res):
        jobs = self.app.jobs.values()
        active = [j for j in jobs if j["status"] == "downloading"]
        queued = sum(1 for j in jobs if j["status"] == "queued")
        speed = sum(j.get("speed") or 0 for j in active)
        self.st_speed.set_text("%s/s" % hum(speed) if speed else "Idle")
        parts = []
        if active:
            parts.append("%d downloading" % len(active))
        if queued:
            parts.append("%d queued" % queued)
        self.st_active.set_text(" · ".join(parts))
        self.title.set_subtitle(("%s/s · " % hum(speed) if speed else "") + (
            "%d active" % len(active) if active else "%d downloads" % len(self.app.jobs)))
        if not res.get("online", True):
            self.st_sched.set_text("⚠ No internet – downloads will resume automatically")
        else:
            self.st_sched.set_text("" if res.get("window_open", True)
                                   else "⏸ Outside scheduled download hours")
        d = self.app.cfg.get("download_dir")
        try:
            st = os.statvfs(d)
            free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
            self.disk_lbl.set_text("%s free of %s" % (hum(free), hum(total)))
            self.disk_bar.set_value(1 - free / total if total else 0)
        except (OSError, TypeError):
            self.disk_lbl.set_text("")


# --------------------------------------------------------------------------- application
class PyDM(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.api = Api()
        self.cfg = read_conf()
        self.ui = read_json(UI_FILE, UI_DEFAULTS)
        self.jobs, self.rows, self.popups = {}, {}, {}
        self.prev_status = None
        self.win = None
        self.polling = False
        self.connected = False
        self.pending_urls = []
        self.held = False
        self.background = "--background" in sys.argv

    # ---- lifecycle
    def do_startup(self):
        Adw.Application.do_startup(self)
        Gtk.Window.set_default_icon_name(APP_ID)
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS.decode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accels in (
                ("add", lambda: self.new_download(), ["<Control>n"]),
                ("preferences", lambda: Preferences(self).present(self.win), ["<Control>comma"]),
                ("about", self.about, []),
                ("shortcuts", self.shortcuts, ["<Control>question"]),
                ("open-folder", lambda: open_path(self.cfg.get("download_dir")), ["<Control>o"]),
                ("pause-all", lambda: self.bulk(("downloading", "queued"), "pause"), []),
                ("resume-all", lambda: self.bulk(("paused",), "start"), []),
                ("clear-done", lambda: self.bulk(("done",), "remove"), []),
                ("search", lambda: self.win.search_btn.set_active(True), ["<Control>f"]),
                ("close", lambda: self.win.close(), ["<Control>w", "<Control>q"]),
                ("quit-all", self.quit_all, [])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)
            if accels:
                self.set_accels_for_action("app." + name, accels)

    def keep_alive(self):
        if not self.held:
            self.hold()
            self.held = True

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self)
            self.start_daemon()
            GLib.timeout_add(1000, self.tick)
            if self.background:              # autostart: stay hidden until needed
                self.background = False
                self.keep_alive()
                return
        self.win.present()
        while self.pending_urls:
            self.new_download(self.pending_urls.pop(0))

    def do_open(self, files, _n, _hint):
        self.pending_urls += [f.get_uri() for f in files if f.get_uri().startswith("http")]
        self.activate()

    # ---- daemon
    def start_daemon(self):
        self.win.banner.set_title("Starting the PyDM service…")
        self.win.banner.set_revealed(True)

        def done(ok, err):
            if ok:
                self.cfg = read_conf() or self.cfg
                bg(lambda: self.api.call("/api/config"),
                   lambda r, e: r and setattr(self, "cfg", r))
                self.tick()
            else:
                self.win.banner.set_title("Couldn't start the PyDM service – see ~/.config/pydm/daemon.log")
        bg(self.api.ensure_daemon, done)

    def tick(self):
        if not self.polling:
            self.polling = True
            bg(lambda: self.api.call("/api/jobs", timeout=5), self.on_jobs)
        return True

    def refresh(self):
        GLib.timeout_add(150, lambda: self.tick() and False)

    def on_jobs(self, res, err):
        self.polling = False
        if err or not res:
            if self.connected or not self.win.banner.get_revealed():
                self.win.banner.set_title("The PyDM service is not running")
            self.win.banner.set_revealed(True)
            self.connected = False
            return
        if not self.connected:
            self.connected = True
            self.win.banner.set_revealed(False)
        jobs = {j["id"]: j for j in res["jobs"]}
        first = self.prev_status is None
        prev = self.prev_status or {}
        for jid, j in jobs.items():
            row = self.rows.get(jid)
            if not row:
                row = self.rows[jid] = JobRow(self, j)
                self.win.list.append(row)
            elif row.j != j:
                row.update(j)
            if not first:
                was = prev.get(jid)
                if was is None and j["status"] != "pending" and self.ui["popup"]:
                    self.show_progress(j)
                if was not in (None, "done") and j["status"] == "done":
                    self.finished(j, True)
                elif was not in (None, "error") and j["status"] == "error":
                    self.finished(j, False)
            if jid in self.popups:
                self.popups[jid].update(j)
        for jid in list(self.rows):
            if jid not in jobs:
                self.win.list.remove(self.rows.pop(jid))
                p = self.popups.pop(jid, None)
                if p:
                    p.close()
        changed = set(jobs) != set(self.jobs) or any(
            self.jobs.get(k, {}).get("status") != j["status"] for k, j in jobs.items())
        self.jobs = jobs
        self.prev_status = {k: j["status"] for k, j in jobs.items()}
        if changed:
            self.win.list.invalidate_filter()
            self.win.list.invalidate_sort()
        self.win.update_views()
        self.win.update_status(res)

    # ---- actions
    def act(self, jid, action):
        def done(_r, err):
            if err:
                self.toast(err)
            self.refresh()
        bg(lambda: self.api.call("/api/jobs/%s/%s" % (jid, action), {}), done)

    def bulk(self, states, action):
        ids = [k for k, j in self.jobs.items() if j["status"] in states]
        if not ids:
            return self.toast("Nothing to do")

        def run():
            for i in ids:
                self.api.call("/api/jobs/%s/%s" % (i, action), {})
        bg(run, lambda r, e: (self.toast(e) if e else None, self.refresh()))

    def new_download(self, url=""):
        if self.win:
            AddDialog(self, url).present(self.win)

    def show_progress(self, j):
        p = self.popups.get(j["id"])
        if p:
            return p.present()
        p = ProgressWindow(self, j)
        p.connect("close-request", lambda *_: self.popups.pop(j["id"], None) and False)
        self.popups[j["id"]] = p
        p.present()

    def finished(self, j, ok):
        name = display_name(j)
        if self.win.is_active():
            self.toast(("Finished: " if ok else "Failed: ") + name)
        if not self.ui["notify"] or self.win.is_active():
            return
        n = Gio.Notification.new("Download complete" if ok else "Download failed")
        n.set_body(name if ok else "%s\n%s" % (name, (j.get("error") or "")[:120]))
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_default_action("app.open-folder")
        self.send_notification("job-" + j["id"], n)

    def toast(self, text):
        if self.win:
            self.win.toast_msg(text)

    def copy_text(self, text, msg):
        Gdk.Display.get_default().get_clipboard().set(text)
        self.toast(msg)

    def confirm_delete(self, j):
        path = job_path(self.cfg, j)
        d = Adw.AlertDialog(heading="Delete File?",
                            body="“%s” will be removed from the list and deleted from disk. "
                                 "This can't be undone." % display_name(j))
        d.add_response("cancel", "Cancel")
        d.add_response("delete", "Delete")
        d.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        def resp(_d, r):
            if r != "delete":
                return
            self.act(j["id"], "remove")

            def rm_files():
                time.sleep(1.0)          # let the runner stop and release the file
                n = 0
                for p in (path, path + ".part", path + ".pydm"):
                    if p and os.path.isfile(p):
                        os.remove(p)
                        n += 1
                return n
            bg(rm_files, lambda n, e: self.toast(e or ("File deleted" if n else "Removed from list")))
        d.connect("response", resp)
        d.present(self.win)

    def about(self):
        Adw.AboutDialog(application_name="PyDM", application_icon=APP_ID, version=VERSION,
                        developer_name="PyDM",
                        comments="Fast multi-connection download manager with video grabbing "
                                 "for YouTube, TikTok, Instagram and 1000+ sites.",
                        license_type=Gtk.License.MIT_X11).present(self.win)

    def shortcuts(self):
        d = Adw.Dialog(title="Keyboard Shortcuts", content_width=420)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        for k, t in (("Ctrl+N", "New download (uses a copied link)"),
                     ("Ctrl+F", "Search"), ("Ctrl+O", "Open download folder"),
                     ("Ctrl+,", "Preferences"), ("Ctrl+W", "Close window (keeps running)"),
                     ("Drag & drop", "Drop a link on the window to download it")):
            r = Adw.ActionRow(title=t)
            r.add_suffix(lbl(k, ("dim-label", "monospace")))
            g.add(r)
        page.add(g)
        tv.set_content(page)
        d.set_child(tv)
        d.present(self.win)

    def quit_gui(self):
        for p in list(self.popups.values()):
            p.destroy()
        if self.win:
            self.win.destroy()
        self.quit()

    def quit_all(self):
        d = Adw.AlertDialog(heading="Quit PyDM Completely?",
                            body="Running downloads will pause and the browser extension "
                                 "will stop working until you open PyDM again.")
        d.add_response("cancel", "Cancel")
        d.add_response("quit", "Quit")
        d.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "quit" and bg(
            lambda: self.api.call("/api/quit", {}, timeout=5), lambda *_: self.quit_gui()))
        d.present(self.win)


if __name__ == "__main__":
    sys.exit(PyDM().run([a for a in sys.argv if a != "--background"]))
