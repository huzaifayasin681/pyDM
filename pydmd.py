#!/usr/bin/env python3
"""pydmd - PyDM daemon: job queue, scheduler, clipboard monitor, video grabber,
local web GUI and a JSON API for the browser extension.

Run:  python3 pydmd.py            (GUI at http://127.0.0.1:6800/)
Config/token/jobs live in ~/.config/pydm/.

Security model: binds to 127.0.0.1 only; every /api call needs the secret
token (X-PyDM-Token); the Host header must be localhost (DNS-rebinding guard);
cross-origin calls are only allowed from browser-extension origins; only
http(s) URLs are accepted; clipboard URLs are queued as 'pending' and never
start without a click.
"""
import argparse
import http.server
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import tempfile
import uuid
from datetime import datetime

import pydm

CONF_DIR = os.path.expanduser("~/.config/pydm")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
JOBS_FILE = os.path.join(CONF_DIR, "jobs.json")
TOKEN_FILE = os.path.join(CONF_DIR, "token")
HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "download_dir": os.path.expanduser("~/Downloads"),
    "connections": 8,
    "max_parallel": 3,
    "limit": 0,                 # bytes/sec, 0 = unlimited
    "window": None,             # ["01:00", "07:00"] = only download in this window
    "clipboard": False,
    "organize": True,           # sort into Videos/, Music/, Documents/... folders
    "port": 6800,
}
CLIP_EXT = re.compile(
    r"\.(zip|7z|rar|tar|gz|xz|bz2|iso|exe|msi|deb|rpm|appimage|dmg|pdf|mp4|mkv|"
    r"webm|avi|mov|mp3|flac|wav|m4a|apk|bin|img)(\?.*)?$", re.I)
VIDEO_HOSTS = re.compile(
    r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|"
    r"twitch\.tv|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|x\.com|"
    r"twitter\.com|reddit\.com|v\.redd\.it|pinterest\.com|pin\.it|threads\.net|"
    r"snapchat\.com|likee\.video|bilibili\.com|streamable\.com|soundcloud\.com)/", re.I)
COOKIE_DIR = os.path.join(CONF_DIR, "cookies")
# Sub-folders of the download folder, by file type (like IDM's categories)
CATEGORY_DIRS = {
    "Videos": "mp4 mkv webm avi mov m4v flv wmv ts 3gp mpg mpeg",
    "Music": "mp3 m4a flac wav ogg opus aac wma",
    "Documents": "pdf doc docx xls xlsx ppt pptx odt ods odp txt rtf epub csv md",
    "Archives": "zip rar 7z tar gz tgz xz bz2 zst iso img",
    "Programs": "exe msi deb rpm appimage apk dmg sh run bin flatpak snap",
    "Images": "png jpg jpeg gif webp svg bmp heic avif",
}
EXT_DIR = {e: d for d, exts in CATEGORY_DIRS.items() for e in exts.split()}
MAX_AUTO_RETRIES = 60           # online failures; time spent offline doesn't count
# Errors that won't fix themselves by waiting
PERMANENT_ERR = re.compile(
    r"HTTP Error (40[0-79]|41\d|4[2-9][0-8])\b|Unsupported URL|is not a valid URL|"
    r"Private video|not available|has been removed|copyright|No space left|"
    r"Permission denied|Read-only file system|bad quality|only http", re.I)
# Errors caused by the network / server hiccups: retry automatically
TRANSIENT_ERR = re.compile(
    r"timed? ?out|temporar|connection (reset|refused|aborted|closed)|closed early|"
    r"network is unreachable|no route|name or service not known|name resolution|"
    r"getaddrinfo|nodename|Errno (-2|-3|101|103|104|110|111|113)|HTTP Error (408|429|5\d\d)|"
    r"remote end closed|IncompleteRead|segment \d+-\d+ failed|EOF occurred|ssl|"
    r"Unable to download|Got error|giving up after|did not get any data|"
    r"unexpected end|broken pipe|reset by peer|server ignored Range", re.I)
NET_PROBES = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443), ("208.67.222.222", 443))
MAX_COOKIES = 400
ALLOWED_HEADERS = {"cookie", "referer", "user-agent", "authorization"}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())            # survive power loss / crashes
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def valid_url(u):
    p = urllib.parse.urlparse(u)
    return p.scheme in ("http", "https") and bool(p.netloc) and "\n" not in u


def clean_cookies(cookies):
    """Validate cookies sent by the extension (chrome.cookies format)."""
    out = []
    if not isinstance(cookies, list):
        return out
    bad = re.compile(r"[\t\r\n]")
    for c in cookies[:MAX_COOKIES]:
        if not isinstance(c, dict):
            continue
        name, value, domain = (str(c.get(k) or "") for k in ("name", "value", "domain"))
        path = str(c.get("path") or "/")
        if not name or not domain or any(bad.search(x) for x in (name, value, domain, path)):
            continue
        try:
            exp = int(float(c.get("expirationDate") or 0))
        except (TypeError, ValueError):
            exp = 0
        out.append({"name": name, "value": value, "domain": domain, "path": path,
                    "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")),
                    "expirationDate": exp})
    return out


def write_cookie_file(cookies):
    """Write cookies as a Netscape cookies.txt (chmod 600) for yt-dlp."""
    if not cookies:
        return None
    os.makedirs(COOKIE_DIR, mode=0o700, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="c-", suffix=".txt", dir=COOKIE_DIR)
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            dom = c["domain"]
            f.write("\t".join([("#HttpOnly_" if c["httpOnly"] else "") + dom,
                                "TRUE" if dom.startswith(".") else "FALSE", c["path"],
                                "TRUE" if c["secure"] else "FALSE",
                                str(c["expirationDate"]), c["name"], c["value"]]) + "\n")
    os.chmod(path, 0o600)
    return path


def rm(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def ytdlp_common(headers, cookie_file):
    """yt-dlp options that make it look like the user's browser session."""
    cmd = []
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if h.get("referer"):
        cmd += ["--referer", h["referer"]]
    if h.get("user-agent"):
        cmd += ["--user-agent", h["user-agent"]]
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    return cmd


# Signed-in YouTube sessions often get a reduced format list from YouTube
# ("Requested format is not available"); public videos need no login at all.
COOKIES_LAST = re.compile(r"^https?://([\w-]+\.)*(youtube\.com|youtu\.be)/", re.I)
NEEDS_LOGIN = re.compile(r"sign in|log ?in|logged[ -]in|age[- ]restrict|confirm your age|members[- ]only|"
                         r"private video|not a bot|cookies|empty media response|rate-limit", re.I)
COOKIE_BROKE = re.compile(r"requested format is not available|no video formats found|"
                          r"only images are available|n challenge|po token", re.I)


def cookie_attempts(url, cookies):
    """Order in which to try yt-dlp: with the browser's cookies or without."""
    if not cookies:
        return [None]
    return [None, cookies] if COOKIES_LAST.match(url) else [cookies, None]


def should_try_next(err, used_cookies):
    """After a failed attempt, is the other cookie mode worth a try?"""
    return bool(COOKIE_BROKE.search(err)) if used_cookies else bool(NEEDS_LOGIN.search(err))


def ytdlp_error(lines):
    errs = [l for l in lines if l.startswith("ERROR")]
    msg = (errs or lines or ["yt-dlp failed"])[-1]
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"\s*Use --list-formats.*$", "", msg)
    if NEEDS_LOGIN.search(msg):
        msg = msg[:220] + "  (tip: log in to the site in your browser, then use the PyDM button there)"
    elif COOKIE_BROKE.search(msg):
        msg = msg[:220] + "  (the site didn't offer a downloadable format – try again later or update: ./install.sh)"
    return msg[:320]


def safe_title(t):
    """Turn a page/video title into a safe file name stem."""
    t = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", str(t or ""))
    t = re.sub(r"\s+", " ", t).strip(" .-_")
    return t[:150]


def is_online():
    for host in NET_PROBES:
        try:
            socket.create_connection(host, timeout=2).close()
            return True
        except OSError:
            continue
    return False


def resumable_by(folder, name, url):
    """True if folder/name has a resume state written for this same URL."""
    try:
        with open(os.path.join(folder, name + ".pydm")) as f:
            return json.load(f).get("url") == url
    except (OSError, ValueError, AttributeError):
        return False


def unique_name(folder, name, taken, url=None):
    """Pick a free file name: 'a.zip' -> 'a (1).zip' if a.zip exists."""
    stem, ext = os.path.splitext(name)
    if len(ext) > 10:
        stem, ext = name, ""
    n = 0
    while True:
        cand = name if n == 0 else "%s (%d)%s" % (stem, n, ext)
        path = os.path.join(folder, cand)
        if cand in taken:
            n += 1
            continue
        if url and not os.path.exists(path) and resumable_by(folder, cand, url):
            return cand                   # our own unfinished download: resume it
        if not any(os.path.exists(path + x) for x in ("", ".part", ".pydm")):
            return cand
        n += 1


def category_dir(name, kind="file", quality=None):
    """'movie.mkv' -> 'Videos', 'setup.exe' -> 'Programs', unknown -> 'Other'."""
    if kind == "video":
        return "Music" if quality == "audio" else "Videos"
    ext = os.path.splitext(name)[1][1:].lower()
    if ext in ("part", "pydm", "tmp"):
        ext = os.path.splitext(os.path.splitext(name)[0])[1][1:].lower()
    return EXT_DIR.get(ext, "Other")


def hhmm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = dict(DEFAULTS, **load_json(CONF_FILE, {}))
        self.jobs = {j["id"]: j for j in load_json(JOBS_FILE, [])}
        for j in self.jobs.values():
            if j["status"] == "downloading":
                j["status"] = "queued"
            j["speed"] = 0
        self.runners = {}
        self.dirty = True
        self.seen_clip = ""
        self.video_ok = shutil.which("yt-dlp") is not None
        self.fmt_cache = {}         # url -> (time, result)
        self.online = True
        for f in os.listdir(COOKIE_DIR) if os.path.isdir(COOKIE_DIR) else []:
            rm(os.path.join(COOKIE_DIR, f))   # leftovers from a crash

    # ---- persistence ----
    def save(self):
        with self.lock:
            save_json(JOBS_FILE, list(self.jobs.values()))
            save_json(CONF_FILE, self.cfg)
            self.dirty = False

    # ---- job ops ----
    def add(self, url, kind=None, start_at=None, headers=None, status="queued",
            quality=None, cookies=None, title=None):
        if not valid_url(url):
            raise ValueError("only http(s) URLs are accepted")
        if kind is None:
            kind = "video" if VIDEO_HOSTS.match(url) else "file"
        if kind == "video" and not self.video_ok:
            raise ValueError("yt-dlp is not installed (pip install yt-dlp)")
        quality = str(quality or "best")
        if quality not in ("best", "audio") and not re.fullmatch(r"\d{3,4}", quality):
            raise ValueError("bad quality")
        hdr = {k: v for k, v in (headers or {}).items()
               if k.lower() in ALLOWED_HEADERS and "\n" not in str(v)
               and "\r" not in str(v)}
        job = {"id": uuid.uuid4().hex[:10], "url": url, "kind": kind,
               "status": status, "name": "", "size": 0, "done": 0, "speed": 0,
               "error": "", "start_at": start_at or 0, "added": time.time(),
               "quality": quality, "title": safe_title(title),
               "headers": hdr,
               "cookies": clean_cookies(cookies) if kind == "video" else []}
        with self.lock:
            self.jobs[job["id"]] = job
            self.dirty = True
        return job

    def action(self, jid, act):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                raise KeyError(jid)
            if act == "start":
                if j["status"] in ("pending", "paused", "error") or (
                        j["status"] == "queued" and j.get("retry_note")):
                    j["status"], j["error"], j["start_at"] = "queued", "", 0
                    j["retries"], j["retry_note"] = 0, ""
            elif act == "pause":
                self._stop(jid)
                if j["status"] in ("queued", "downloading"):
                    j["status"], j["retry_note"] = "paused", ""
            elif act == "remove":
                self._stop(jid)
                del self.jobs[jid]
            self.dirty = True

    def _stop(self, jid):
        r = self.runners.get(jid)
        if r:
            r["stop"]()

    # ---- runners ----
    def _start(self, j):
        j["status"] = "downloading"
        j["error"] = ""
        j["retry_note"] = ""
        target = self._run_video if j["kind"] == "video" else self._run_file
        t = threading.Thread(target=target, args=(j,), daemon=True)
        t.start()

    def _finish(self, j, ok, err=""):
        online = True if ok else is_online()       # network check outside the lock
        with self.lock:
            self.runners.pop(j["id"], None)
            j["speed"] = 0
            if j["status"] == "paused":        # user paused / window closed
                pass
            elif j["status"] == "queued":
                pass
            elif ok:
                j["status"] = "done"
                j["done"] = j["size"] or j["done"]
                j["cookies"] = []            # don't keep session cookies around
                j["retry_note"], j["retries"] = "", 0
            else:
                err = err or "failed"
                self._maybe_retry(j, err, online)
            self.dirty = True

    def _maybe_retry(self, j, err, online):
        """Network drop / server hiccup -> requeue with backoff; resumes where it left off."""
        self.online = online
        transient = (not online) or (TRANSIENT_ERR.search(err) and not PERMANENT_ERR.search(err))
        n = j.get("retries", 0)
        if transient and n < MAX_AUTO_RETRIES:
            if online:
                n += 1
            delay = min(300, 5 * 2 ** min(n, 6)) if online else 15
            j["retries"] = n
            j["status"], j["error"] = "queued", ""
            j["start_at"] = time.time() + delay
            j["retry_note"] = ("Waiting for internet connection…" if not online else
                               "Connection problem – retrying (attempt %d)" % n)
            j["last_error"] = err[-200:]
        else:
            j["status"], j["error"], j["retry_note"] = "error", err, ""

    def _run_file(self, j):
        c = self.cfg
        args = argparse.Namespace(
            output=None, dir=c["download_dir"], connections=int(c["connections"]),
            limit=int(c["limit"]), header=[], user_agent=pydm_ua(j), timeout=20,
            force=False, quiet=True)
        args.header = ["%s: %s" % (k, v) for k, v in j["headers"].items()
                       if k.lower() != "user-agent"]
        d = pydm.Download(j["url"], args)
        d.log = lambda *a: None
        orig = d.filename

        def pick_name():
            # decided once per job so retries resume into the same file
            root = c["download_dir"]
            if j.get("fname") and "subdir" not in j:
                j["subdir"] = ""               # started by an older version: stays in root
            if not j.get("fname"):
                base = orig()
                if j.get("title"):            # name the file after the page's title
                    base = j["title"] + (os.path.splitext(base)[1] or "")[:8]
                if resumable_by(root, base, j["url"]) or not c.get("organize", True):
                    sub = ""                   # unfinished download already in root: resume it
                else:
                    sub = category_dir(base)
                os.makedirs(os.path.join(root, sub), exist_ok=True)
                with self.lock:
                    taken = {x.get("fname") for x in self.jobs.values()
                             if x is not j and x["status"] != "done"
                             and x.get("subdir", "") == sub}
                    j["fname"] = unique_name(os.path.join(root, sub), base, taken, j["url"])
                    j["subdir"] = sub
                    self.dirty = True
            return os.path.join(j.get("subdir", ""), j["fname"])
        d.filename = pick_name
        self.runners[j["id"]] = {"stop": d.stop.set}
        running = threading.Event()

        def upd():
            while not running.wait(0.5):
                try:
                    j["size"] = d.size or 0
                    j["done"] = d.done_bytes() if d.segs else 0
                    j["speed"] = int(d.speed)
                    if d.final:
                        j["name"] = os.path.basename(d.final)
                        j["path"] = d.final
                except Exception:
                    pass
        threading.Thread(target=upd, daemon=True).start()
        try:
            ok = d.run()
            if d.final:
                j["name"] = os.path.basename(d.final)
                j["path"] = d.final
            if ok and d.size:
                j["size"] = j["done"] = d.size
            self._finish(j, ok, d.error or "")
        except Exception as e:                           # network/probe errors
            self._finish(j, False, str(e))
        finally:
            running.set()

    def _run_video(self, j):
        root = self.cfg["download_dir"]
        if "vdir" not in j:
            # an unfinished download from an older version keeps its place (so it resumes)
            old = j.get("name") and any(os.path.exists(os.path.join(root, j["name"] + x))
                                        for x in ("", ".part", ".ytdl"))
            j["vdir"] = "" if old or not self.cfg.get("organize", True) else \
                category_dir("", "video", j.get("quality"))
        folder = os.path.join(root, j["vdir"])
        os.makedirs(folder, exist_ok=True)
        q = j.get("quality", "best")
        tag = " (%sp)" % q if q not in ("best", "audio") else ""   # 480p and best don't collide
        out = os.path.join(folder, "%(title).150B [%(id)s]" + tag + ".%(ext)s")
        if j.get("title") and not VIDEO_HOSTS.match(j["url"]):
            # stream URLs (m3u8/mpd) have no useful title of their own
            out = os.path.join(folder, j["title"].replace("%", "%%") + tag + ".%(ext)s")
        cmd = ["yt-dlp", "--newline", "--no-playlist", "--no-mtime", "-o", out,
               "--continue", "--retries", "15", "--fragment-retries", "30",
               "--retry-sleep", "http:exp=1:30", "--retry-sleep", "fragment:exp=1:30",
               "--socket-timeout", "30", "--file-access-retries", "5",
               "--concurrent-fragments", "4",
               "--progress-template",
               "download:PYDM|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|"
               "%(progress.total_bytes_estimate)s|%(progress.speed)s"]
        q = j.get("quality", "best")
        if q == "audio":
            cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3"]
        elif q != "best":
            cmd += ["-f", "bv*[format_note^=%sp]+ba/bv*[height<=%s]+ba/b[height<=%s]/bv*+ba/b"
                    % (q, q, q), "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bv*+ba/b", "--merge-output-format", "mp4"]
        if q != "audio":
            # same resolution, but prefer H.264/AAC: plays in every video player
            cmd += ["-S", "res,fps,vcodec:h264,acodec:m4a"]
        if self.cfg["limit"]:
            cmd += ["--limit-rate", str(int(self.cfg["limit"]))]
        base_cmd = cmd
        attempts = cookie_attempts(j["url"], j.get("cookies"))
        for n, ck in enumerate(attempts):
            res = self._ytdlp_once(j, base_cmd, ck)
            if res is None:
                return                                   # couldn't start yt-dlp
            rc, tail, final, skipped = res
            err = ytdlp_error(tail) if rc else ""
            if rc == 0 or j["status"] != "downloading" or n == len(attempts) - 1 \
                    or not should_try_next(err, bool(ck)):
                break
            tail = (tail + ["[pydm] retrying %s cookies" % ("without" if ck else "with")])
        if rc == 0 and not final and j.get("name"):
            final = os.path.join(folder, j["name"])  # single-stream
        if rc == 0 and final:
            j["name"] = os.path.basename(final)
            j["path"] = final
            try:
                j["size"] = j["done"] = os.path.getsize(final)
            except OSError:
                pass
            if skipped:
                j["note"] = "Already downloaded - skipped"
        self._finish(j, rc == 0, err)

    def _ytdlp_once(self, j, base_cmd, cookies):
        cookie_file = write_cookie_file(cookies)
        cmd = base_cmd + ytdlp_common(j["headers"], cookie_file) + ["--", j["url"]]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            rm(cookie_file)
            self._finish(j, False, str(e))
            return None
        # SIGINT lets yt-dlp/ffmpeg finalize a partially recorded live stream
        self.runners[j["id"]] = {"stop": lambda: p.send_signal(signal.SIGINT)}
        tail, final, skipped = [], "", False
        for line in p.stdout:
            line = line.strip()
            if line.startswith("PYDM|"):
                _, dl, tot, est, spd = (line.split("|") + [""] * 5)[:5]
                num = lambda x: int(float(x)) if re.fullmatch(r"[\d.]+", x or "") else 0
                j["done"], j["size"] = num(dl), num(tot) or num(est)
                j["speed"] = num(spd)
                continue
            tail = (tail + [line])[-30:]
            # The final file is announced by the merger / audio extractor; a
            # plain "Destination" is only an intermediate stream.
            m = (re.search(r'\[Merger\] Merging formats into "(.+)"$', line)
                 or re.search(r"\[ExtractAudio\] Destination: (.+)$", line))
            if m:
                final = m.group(1)
            m = re.search(r"\[download\] (.+) has already been downloaded", line)
            if m:
                final, skipped = m.group(1), True
            m = re.search(r"\[download\] Destination: (.+)$", line)
            if m and not final:
                j["name"] = os.path.basename(m.group(1))
        rc = p.wait()
        rm(cookie_file)
        return rc, tail, final, skipped

    def organize_existing(self):
        """Move PyDM's own finished downloads from the download folder into
        type folders. Only files listed as completed jobs are touched."""
        root = self.cfg["download_dir"]
        moved = 0
        with self.lock:
            jobs = [j for j in self.jobs.values() if j["status"] == "done" and j.get("name")]
        for j in jobs:
            src = j.get("path") or os.path.join(root, j["name"])
            if os.path.dirname(os.path.abspath(src)) != os.path.abspath(root) or not os.path.isfile(src):
                continue                       # already sorted, moved, or deleted
            sub = category_dir(j["name"], j.get("kind"), j.get("quality"))
            if j.get("kind") == "video" and category_dir(j["name"]) == "Music":
                sub = "Music"
            dest_dir = os.path.join(root, sub)
            os.makedirs(dest_dir, exist_ok=True)
            name = unique_name(dest_dir, j["name"], set())
            try:
                os.rename(src, os.path.join(dest_dir, name))
            except OSError:
                continue
            with self.lock:
                j.update(name=name, path=os.path.join(dest_dir, name), subdir=sub, vdir=sub)
                self.dirty = True
            moved += 1
        return moved

    def formats(self, url, headers=None, cookies=None):
        """Ask yt-dlp which qualities a video offers (no download)."""
        if not valid_url(url):
            raise ValueError("only http(s) URLs are accepted")
        if not self.video_ok:
            raise ValueError("yt-dlp is not installed")
        hit = self.fmt_cache.get(url)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        hdr = {k: v for k, v in (headers or {}).items()
               if k.lower() in ALLOWED_HEADERS and "\n" not in str(v)}
        attempts = cookie_attempts(url, clean_cookies(cookies))
        for n, ck in enumerate(attempts):
            cookie_file = write_cookie_file(ck)
            try:
                p = subprocess.run(["yt-dlp", "-J", "--no-playlist", "--no-warnings"]
                                   + ytdlp_common(hdr, cookie_file) + ["--", url],
                                   capture_output=True, text=True, timeout=90)
            except subprocess.TimeoutExpired:
                raise ValueError("timed out reading video info")
            finally:
                rm(cookie_file)
            if p.returncode == 0:
                break
            err = ytdlp_error(p.stderr.strip().splitlines())
            if n == len(attempts) - 1 or not should_try_next(err, bool(ck)):
                raise ValueError(err)
        info = json.loads(p.stdout)
        fm = info.get("formats") or []
        labels, raw = set(), set()
        for f in fm:
            if not f.get("height") or f.get("vcodec") in (None, "none"):
                continue
            # YouTube labels a 1920x804 widescreen stream "1080p": use the site's label
            m = re.match(r"(\d{3,4})p", str(f.get("format_note") or ""))
            (labels.add(int(m.group(1))) if m else raw.add(f["height"]))
        heights = sorted(labels or raw, reverse=True)
        res = {"title": info.get("title") or (info.get("description") or "")[:80],
               "live": bool(info.get("is_live")),
               "duration": info.get("duration"), "heights": heights,
               "audio": any(f.get("acodec") not in (None, "none") for f in fm)}
        if len(self.fmt_cache) > 200:
            self.fmt_cache.clear()
        self.fmt_cache[url] = (time.time(), res)
        return res

    # ---- scheduler loop ----
    def in_window(self):
        w = self.cfg.get("window")
        if not w:
            return True
        now = datetime.now()
        n, a, b = now.hour * 60 + now.minute, hhmm(w[0]), hhmm(w[1])
        return a <= n < b if a <= b else (n >= a or n < b)

    def net_loop(self):
        """Watch the connection; when it comes back, resume waiting downloads at once."""
        while True:
            waiting = [j for j in list(self.jobs.values())
                       if j["status"] == "queued" and j.get("retry_note")]
            if waiting or not self.online:
                online = is_online()
                if online and not self.online:
                    with self.lock:
                        for j in waiting:
                            j["start_at"] = 0
                            j["retry_note"] = "Connection restored – resuming"
                        self.dirty = True
                elif not online:
                    with self.lock:
                        for j in waiting:
                            j["retry_note"] = "Waiting for internet connection…"
                            j["start_at"] = max(j["start_at"], time.time() + 5)
                self.online = online
            time.sleep(5)

    def loop(self):
        last_save = 0
        while True:
            time.sleep(1)
            with self.lock:
                allowed = self.in_window()
                if not allowed:
                    for jid in list(self.runners):
                        j = self.jobs.get(jid)
                        if j and j["status"] == "downloading":
                            j["status"] = "queued"         # resume when window opens
                            self._stop(jid)
                else:
                    running = sum(1 for j in self.jobs.values()
                                  if j["status"] == "downloading")
                    for j in sorted(self.jobs.values(), key=lambda x: x["added"]):
                        if running >= int(self.cfg["max_parallel"]):
                            break
                        if (j["status"] == "queued" and j["start_at"] <= time.time()
                                and j["id"] not in self.runners):
                            self._start(j)
                            running += 1
                if self.dirty and time.time() - last_save > 2:
                    self.save()
                    last_save = time.time()

    # ---- clipboard monitor ----
    def clip_text(self):
        for cmd in (["wl-paste", "-n"], ["xclip", "-o", "-selection", "clipboard"],
                    ["xsel", "-ob"]):
            if shutil.which(cmd[0]):
                try:
                    return subprocess.run(cmd, capture_output=True, text=True,
                                          timeout=2).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    return ""
        return ""

    def clip_loop(self):
        while True:
            time.sleep(1.5)
            if not self.cfg.get("clipboard"):
                continue
            t = self.clip_text()
            if (t and t != self.seen_clip and len(t) < 2000 and "\n" not in t
                    and valid_url(t) and (CLIP_EXT.search(t) or VIDEO_HOSTS.match(t))):
                self.seen_clip = t
                try:
                    self.add(t, status="pending")
                except ValueError:
                    pass
            elif t:
                self.seen_clip = t


def pydm_ua(j):
    return j["headers"].get("User-Agent") or j["headers"].get("user-agent") or \
        "Mozilla/5.0 (X11; Linux x86_64) pydm/1.0"


def make_handler(mgr, token, port):
    host_ok = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "pydmd"

        def log_message(self, *a):
            pass

        def _origin_ok(self):
            o = self.headers.get("Origin")
            return (o is None or o in ("http://127.0.0.1:%d" % port,
                                       "http://localhost:%d" % port)
                    or o.startswith(("chrome-extension://", "moz-extension://")))

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            o = self.headers.get("Origin")
            if o and o.startswith(("chrome-extension://", "moz-extension://")):
                self.send_header("Access-Control-Allow-Origin", o)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self):
            if self.headers.get("Host") not in host_ok or not self._origin_ok():
                self._send(403, {"error": "forbidden"})
                return False
            return True

        def do_OPTIONS(self):
            if not self._guard():
                return
            self.send_response(204)
            o = self.headers.get("Origin", "")
            if o.startswith(("chrome-extension://", "moz-extension://")):
                self.send_header("Access-Control-Allow-Origin", o)
                self.send_header("Access-Control-Allow-Headers",
                                 "Content-Type, X-PyDM-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.end_headers()

        def do_GET(self):
            if not self._guard():
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                html = open(os.path.join(HERE, "ui.html"), "rb").read()
                self._send(200, html.replace(b"__TOKEN__", token.encode()),
                           "text/html; charset=utf-8")
            elif not self._auth():
                return
            elif path == "/api/jobs":
                with mgr.lock:
                    jobs = [{k: v for k, v in j.items() if k != "cookies"}
                            for j in sorted(mgr.jobs.values(), key=lambda j: j["added"])]
                    self._send(200, {"jobs": jobs,
                                     "video": mgr.video_ok, "online": mgr.online,
                                     "window_open": mgr.in_window()})
            elif path == "/api/config":
                self._send(200, mgr.cfg)
            elif path == "/api/formats":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                try:
                    self._send(200, mgr.formats((q.get("url") or [""])[0]))
                except ValueError as e:
                    self._send(400, {"error": str(e)})
            else:
                self._send(404, {"error": "not found"})

        def _auth(self):
            if not secrets.compare_digest(self.headers.get("X-PyDM-Token", ""), token):
                self._send(401, {"error": "bad token"})
                return False
            return True

        def do_POST(self):
            if not self._guard() or not self._auth():
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/add":
                    j = mgr.add(body.get("url", ""), body.get("kind"),
                                float(body.get("start_at") or 0),
                                body.get("headers"), quality=body.get("quality"),
                                cookies=body.get("cookies"), title=body.get("title"))
                    self._send(200, {k: v for k, v in j.items() if k != "cookies"})
                elif path == "/api/formats":
                    self._send(200, mgr.formats(body.get("url", ""), body.get("headers"),
                                                body.get("cookies")))
                elif path == "/api/organize":
                    self._send(200, {"moved": mgr.organize_existing()})
                elif path == "/api/quit":
                    self._send(200, {"ok": True})
                    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
                elif path == "/api/config":
                    self._set_config(body)
                    self._send(200, mgr.cfg)
                else:
                    m = re.fullmatch(r"/api/jobs/(\w+)/(start|pause|remove)", path)
                    if not m:
                        return self._send(404, {"error": "not found"})
                    mgr.action(m.group(1), m.group(2))
                    self._send(200, {"ok": True})
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {"error": str(e)})

        def _set_config(self, b):
            c = {}
            if "download_dir" in b:
                d = os.path.abspath(os.path.expanduser(str(b["download_dir"])))
                os.makedirs(d, exist_ok=True)
                c["download_dir"] = d
            for k, lo, hi in (("connections", 1, 32), ("max_parallel", 1, 10),
                              ("limit", 0, 1 << 40)):
                if k in b:
                    c[k] = max(lo, min(hi, int(b[k])))
            if "clipboard" in b:
                c["clipboard"] = bool(b["clipboard"])
            if "organize" in b:
                c["organize"] = bool(b["organize"])
            if "window" in b:
                w = b["window"]
                if w:
                    hhmm(w[0]), hhmm(w[1])           # validate
                    w = [w[0], w[1]]
                c["window"] = w or None
            with mgr.lock:
                mgr.cfg.update(c)
                mgr.dirty = True

    return H


def main():
    ap = argparse.ArgumentParser(description="PyDM daemon")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    os.makedirs(CONF_DIR, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_urlsafe(24))
        os.chmod(TOKEN_FILE, 0o600)
    token = open(TOKEN_FILE).read().strip()
    # a parent shell may have left SIGINT ignored; make shutdown signals reliable
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    mgr = Manager()
    port = a.port or int(mgr.cfg["port"])
    os.makedirs(mgr.cfg["download_dir"], exist_ok=True)
    threading.Thread(target=mgr.loop, daemon=True).start()
    threading.Thread(target=mgr.clip_loop, daemon=True).start()
    threading.Thread(target=mgr.net_loop, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                          make_handler(mgr, token, port))
    print("PyDM running: http://127.0.0.1:%d/   (token: %s)" % (port, TOKEN_FILE))
    if not mgr.video_ok:
        print("note: yt-dlp not found - video grabbing disabled")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with mgr.lock:
            for jid in list(mgr.runners):
                if mgr.jobs.get(jid, {}).get("status") == "downloading":
                    mgr.jobs[jid]["status"] = "queued"   # resume on next start
                mgr._stop(jid)
        time.sleep(1)
        mgr.save()


if __name__ == "__main__":
    main()
