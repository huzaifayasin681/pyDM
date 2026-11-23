#!/usr/bin/env python3
"""pydm - a multi-connection download manager for Linux (IDM-style).

Techniques implemented (all standard HTTP, written from scratch):
  * Probe with `Range: bytes=0-0` to learn size and range support.
  * Split the file into segments, one connection each, written with pwrite()
    into a pre-allocated `.part` file.
  * Dynamic segmentation: when a connection finishes, it takes over half of the
    largest remaining segment, so all connections stay busy to the end.
  * Resume: progress is saved to a `.pydm` sidecar file and validated against
    size/ETag/Last-Modified on restart.
  * Per-segment retries with exponential backoff, optional speed limit,
    live progress bar, and a queue of URLs.

Usage:
  pydm.py URL [URL ...] [-o NAME] [-d DIR] [-c 8] [--limit 2M]
  pydm.py -i urls.txt
Standard library only. Python 3.8+.
"""
import argparse
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 64 * 1024
MIN_SPLIT = 512 * 1024      # don't split segments smaller than this
STATE_EVERY = 2.0           # seconds between state saves
MAX_RETRIES = 8


def parse_size(s):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg]?)b?", s.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError("bad size: " + s)
    mult = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[m.group(2)]
    return int(float(m.group(1)) * mult)


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024


class Seg:
    __slots__ = ("start", "pos", "end", "active")

    def __init__(self, start, end, pos=None):
        self.start, self.end = start, end
        self.pos = start if pos is None else pos
        self.active = False

    @property
    def remaining(self):
        return self.end - self.pos + 1


class RateLimiter:
    """Shared token bucket (bytes/sec); limit=0 disables."""

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.next_t = time.monotonic()

    def wait(self, nbytes):
        if not self.limit:
            return
        with self.lock:
            now = time.monotonic()
            self.next_t = max(self.next_t, now) + nbytes / self.limit
            delay = self.next_t - now - 1.0   # allow ~1s burst
        if delay > 0:
            time.sleep(delay)


class Download:
    def __init__(self, url, args):
        self.url = url
        self.args = args
        self.headers = {"User-Agent": args.user_agent}
        for h in args.header or []:
            k, _, v = h.partition(":")
            self.headers[k.strip()] = v.strip()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.segs = []
        self.size = None
        self.ranges = False
        self.etag = None
        self.error = None
        self.limiter = RateLimiter(args.limit)
        self.quiet = getattr(args, "quiet", False)
        self.log = print
        self.speed = 0.0
        self.final = None

    # ---------- network helpers ----------
    def _open(self, rng=None, url=None):
        h = dict(self.headers)
        if rng:
            h["Range"] = rng
        req = urllib.request.Request(url or self.url, headers=h)
        return urllib.request.urlopen(req, timeout=self.args.timeout)

    def probe(self):
        resp = self._open("bytes=0-0")
        try:
            self.final_url = resp.geturl()
            info = resp.headers
            cr = info.get("Content-Range", "")
            m = re.match(r"bytes\s+0-0/(\d+)", cr)
            if resp.status == 206 and m:
                self.size, self.ranges = int(m.group(1)), True
            else:
                cl = info.get("Content-Length")
                self.size = int(cl) if cl and cl.isdigit() else None
                self.ranges = False
            self.etag = info.get("ETag") or info.get("Last-Modified")
            self.disp = info.get("Content-Disposition", "")
        finally:
            resp.close()

    def filename(self):
        if self.args.output:
            return self.args.output
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", self.disp, re.I) or \
            re.search(r'filename="?([^";]+)"?', self.disp, re.I)
        name = urllib.parse.unquote(m.group(1).strip()) if m else \
            urllib.parse.unquote(os.path.basename(
                urllib.parse.urlparse(self.final_url).path))
        name = os.path.basename(name) or "download.bin"
        return name

    # ---------- state ----------
    def save_state(self):
        fd = getattr(self, "fd", None)
        if fd is not None:
            try:
                os.fdatasync(fd)
            except OSError:
                pass
        with self.lock:
            data = {"url": self.url, "size": self.size, "etag": self.etag,
                    "segs": [[s.start, s.pos, s.end] for s in self.segs]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def load_state(self):
        try:
            with open(self.state_path) as f:
                d = json.load(f)
            if d["size"] != self.size or d["etag"] != self.etag \
                    or not os.path.exists(self.part_path):
                return False
            segs = [Seg(a, c, b) for a, b, c in d["segs"]]
            if not segs or any(not (0 <= s.start <= s.pos <= s.end + 1 <= self.size)
                               for s in segs):
                return False                      # corrupt state: start over
            self.segs = segs
            return True
        except (OSError, ValueError, KeyError):
            return False

    # ---------- segment scheduling ----------
    def claim(self):
        """Hand out an idle segment, or split the biggest active one."""
        with self.lock:
            for s in self.segs:
                if not s.active and s.remaining > 0:
                    s.active = True
                    return s
            best = max((s for s in self.segs if s.active),
                       key=lambda s: s.remaining, default=None)
            if best and best.remaining >= 2 * MIN_SPLIT:
                mid = best.pos + best.remaining // 2
                new = Seg(mid, best.end)
                best.end = mid - 1
                new.active = True
                self.segs.append(new)
                return new
        return None

    def done_bytes(self):
        with self.lock:
            return sum(max(0, s.pos - s.start) for s in self.segs)

    # ---------- workers ----------
    def fetch(self, seg, fd):
        """Download seg until complete; returns on success, raises on error."""
        while seg.remaining > 0 and not self.stop.is_set():
            resp = self._open("bytes=%d-%d" % (seg.pos, seg.end), self.final_url)
            try:
                if resp.status != 206:
                    raise IOError("server ignored Range (status %d)" % resp.status)
                while seg.remaining > 0 and not self.stop.is_set():
                    data = resp.read(min(CHUNK, seg.remaining))
                    if not data:
                        break
                    data = data[:max(0, seg.remaining)]   # seg may have shrunk
                    if not data:
                        break
                    self.limiter.wait(len(data))
                    os.pwrite(fd, data, seg.pos)
                    seg.pos += len(data)
            finally:
                resp.close()
            if seg.remaining > 0 and not self.stop.is_set():
                raise IOError("connection closed early")

    def worker(self, fd):
        while not self.stop.is_set():
            seg = self.claim()
            if seg is None:
                return
            delay = 1.0
            for attempt in range(MAX_RETRIES):
                try:
                    self.fetch(seg, fd)
                    break
                except (OSError, urllib.error.URLError, IOError) as e:
                    if self.stop.wait(delay):
                        break
                    delay = min(delay * 2, 30)
                    if attempt == MAX_RETRIES - 1:
                        self.error = "segment %d-%d failed: %s" % (
                            seg.start, seg.end, e)
                        self.stop.set()
            with self.lock:
                seg.active = False

    def single_stream(self, fd):
        """Fallback for servers without Range support (no resume)."""
        resp = self._open(None, self.final_url)
        pos = 0
        try:
            while not self.stop.is_set():
                data = resp.read(CHUNK)
                if not data:
                    break
                self.limiter.wait(len(data))
                os.pwrite(fd, data, pos)
                pos += len(data)
                self.seg0.pos = self.seg0.start + pos
        finally:
            resp.close()
        self.seg0.end = pos - 1

    # ---------- main ----------
    def progress(self, t0):
        last, last_t, speed = self.done_bytes(), time.monotonic(), 0.0
        while not self.stop.is_set() and not self.finished.is_set():
            self.finished.wait(0.5)
            now, done = time.monotonic(), self.done_bytes()
            inst = (done - last) / max(now - last_t, 1e-6)
            speed = inst if speed == 0 else 0.7 * speed + 0.3 * inst
            last, last_t = done, now
            self.speed = speed
            if self.quiet:
                if now - self._last_save > STATE_EVERY and self.ranges:
                    self.save_state()
                    self._last_save = now
                continue
            if self.size:
                frac = done / self.size
                eta = (self.size - done) / speed if speed > 1 else 0
                bar = "#" * int(frac * 30)
                line = "\r[%-30s] %5.1f%%  %s/%s  %s/s  ETA %ds  conn:%d " % (
                    bar, frac * 100, human(done), human(self.size),
                    human(speed), eta, sum(s.active for s in self.segs))
            else:
                line = "\r%s  %s/s " % (human(done), human(speed))
            sys.stderr.write(line)
            sys.stderr.flush()
            if now - self._last_save > STATE_EVERY and self.ranges:
                self.save_state()
                self._last_save = now
        if not self.quiet:
            sys.stderr.write("\n")

    def run(self):
        self.probe()
        out_dir = self.args.dir
        os.makedirs(out_dir, exist_ok=True)
        final = os.path.join(out_dir, self.filename())
        self.part_path = final + ".part"
        self.state_path = final + ".pydm"
        self.final = final
        self.log("File: %s  size: %s  ranges: %s" % (
            final, human(self.size) if self.size else "unknown", self.ranges))
        if os.path.exists(final) and not self.args.force:
            self.log("Already exists (use --force to overwrite): " + final)
            return True

        resumed = self.ranges and self.size and self.load_state()
        if resumed:
            self.log("Resuming: %s already done" % human(self.done_bytes()))
        elif self.ranges and self.size:
            n = max(1, min(self.args.connections,
                           self.size // MIN_SPLIT or 1))
            step = self.size // n
            self.segs = [Seg(i * step, self.size - 1 if i == n - 1
                             else (i + 1) * step - 1) for i in range(n)]
        else:
            self.seg0 = Seg(0, (self.size or 1 << 62) - 1)
            self.segs = [self.seg0]

        fd = os.open(self.part_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.fd = fd
        try:
            if self.size and not resumed:
                os.ftruncate(fd, self.size)
            self.finished = threading.Event()
            self._last_save = time.monotonic()
            t0 = time.monotonic()
            prog = threading.Thread(target=self.progress, args=(t0,), daemon=True)
            prog.start()
            if self.ranges and self.size:
                ths = [threading.Thread(target=self.worker, args=(fd,), daemon=True)
                       for _ in range(self.args.connections)]
                for t in ths:
                    t.start()
                while any(t.is_alive() for t in ths):
                    for t in ths:
                        t.join(0.2)
            else:
                try:
                    self.single_stream(fd)
                except (OSError, urllib.error.URLError) as e:
                    self.error = str(e)
                    self.stop.set()
            self.finished.set()
            prog.join()
            if self.ranges and self.size:
                self.save_state()            # also flushes the data
        finally:
            self.fd = None
            os.close(fd)

        if self.stop.is_set():
            self.log("Stopped. %s" % (self.error or "Progress saved; rerun to resume."))
            return False
        if self.ranges and self.size and self.done_bytes() < self.size:
            self.save_state()
            self.log("Incomplete; rerun to resume.")
            return False
        if not (self.ranges and self.size):
            os.truncate(self.part_path, self.seg0.end + 1)
        os.replace(self.part_path, final)
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        self.log("Done in %.1fs -> %s" % (time.monotonic() - t0, final))
        return True


def main():
    ap = argparse.ArgumentParser(description="IDM-style multi-connection downloader")
    ap.add_argument("urls", nargs="*")
    ap.add_argument("-i", "--input", help="file with one URL per line")
    ap.add_argument("-o", "--output", help="output file name (single URL)")
    ap.add_argument("-d", "--dir", default=".", help="download directory")
    ap.add_argument("-c", "--connections", type=int, default=8)
    ap.add_argument("--limit", type=parse_size, default=0, help="e.g. 2M (bytes/s)")
    ap.add_argument("-H", "--header", action="append")
    ap.add_argument("--user-agent", default="Mozilla/5.0 (X11; Linux x86_64) pydm/1.0")
    ap.add_argument("--timeout", type=float, default=20)
    ap.add_argument("--force", action="store_true", help="overwrite existing file")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.input:
        with open(args.input) as f:
            urls += [l.strip() for l in f if l.strip() and not l.startswith("#")]
    if not urls:
        ap.error("no URLs given")
    if args.output and len(urls) > 1:
        ap.error("-o only works with a single URL")

    current = {}

    def on_sigint(sig, frm):
        d = current.get("d")
        if d:
            d.stop.set()
    signal.signal(signal.SIGINT, on_sigint)

    ok = True
    for u in urls:
        d = Download(u, args)
        current["d"] = d
        try:
            ok &= bool(d.run())
        except (urllib.error.URLError, OSError) as e:
            print("Error: %s: %s" % (u, e))
            ok = False
        if d.stop.is_set() and not d.error:
            break    # user pressed Ctrl+C: don't continue the queue
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
