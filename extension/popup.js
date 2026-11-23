// PyDM toolbar popup: status, takeover switch, page video, detected media, live downloads.
const $ = id => document.getElementById(id);
const ask = msg => new Promise(res => chrome.runtime.sendMessage(msg, r => res(r || { error: "No response" })));
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

const ICON = { video: "🎬", audio: "🎵", document: "📄", archive: "🗜️", program: "⚙️", image: "🖼️", other: "📦" };
const EXT = {
  video: "mp4 mkv webm avi mov m4v flv ts m3u8 mpd", audio: "mp3 m4a flac wav ogg opus aac",
  document: "pdf doc docx xls xlsx ppt pptx odt txt epub csv", archive: "zip rar 7z tar gz xz bz2 zst iso",
  program: "exe msi deb rpm appimage apk dmg sh run", image: "png jpg jpeg gif webp svg",
};
const cat = j => {
  const e = ((j.name || j.url.split("?")[0]).match(/\.(\w+)$/) || [])[1];
  for (const [k, v] of Object.entries(EXT)) if (e && v.split(" ").includes(e.toLowerCase())) return k;
  return j.kind === "video" ? (j.quality === "audio" ? "audio" : "video") : "other";
};
function hum(n) {
  n = +n || 0;
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (n < 1024 || u === "TB") return u === "B" ? n + " B" : n.toFixed(1) + " " + u;
    n /= 1024;
  }
}
const eta = s => s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.floor(s / 60) + "m " + Math.round(s % 60) + "s"
  : Math.floor(s / 3600) + "h " + Math.floor(s % 3600 / 60) + "m";
const nameOf = j => j.name || (() => {
  try { const u = new URL(j.url); return j.kind === "video" ? "Video from " + u.hostname.replace(/^www\./, "")
    : decodeURIComponent(u.pathname.split("/").pop()) || u.hostname; } catch (_) { return j.url; }
})();

function describe(j) {
  const { status: st, size = 0, done = 0, speed = 0 } = j;
  if (st === "downloading") {
    const p = [size ? `${hum(done)} of ${hum(size)}` : hum(done)];
    if (speed) { p.push(hum(speed) + "/s"); if (size > done) p.push(eta((size - done) / speed) + " left"); }
    else if (!done) p.push("Connecting…");
    return p.join(" · ");
  }
  if (st === "queued") return j.retry_note || (j.start_at * 1000 > Date.now()
    ? "Scheduled for " + new Date(j.start_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "Queued");
  if (st === "paused") return "Paused" + (size ? ` · ${hum(done)} of ${hum(size)}` : "");
  if (st === "pending") return "Waiting to start";
  if (st === "done") return hum(size || done) + " · Completed";
  if (st === "error") return "Failed: " + (j.error || "unknown error");
  return st;
}

function flashMsg(text, ok) {
  const m = $("msg");
  m.textContent = text; m.className = "msg show " + (ok ? "ok" : "err");
  clearTimeout(flashMsg.t); flashMsg.t = setTimeout(() => m.className = "msg", 3500);
}

async function sendJob(payload, label) {
  const r = await ask({ type: "send", ...payload });
  if (r.error) flashMsg(r.error, false); else { flashMsg("Added to PyDM: " + label, true); refresh(); }
}

// ---------------------------------------------------------------- downloads list
const rows = new Map();
function renderJobs(jobs) {
  const list = $("list");
  const shown = jobs.slice().sort((a, b) => {
    const rank = j => ({ downloading: 0, queued: 1, paused: 2, pending: 2, error: 3, done: 4 }[j.status] ?? 5);
    return rank(a) - rank(b) || b.added - a.added;
  }).slice(0, 12);
  $("clear").hidden = !jobs.some(j => j.status === "done");
  if (!shown.length) { list.replaceChildren(el("div", "empty", "No downloads yet")); rows.clear(); return; }
  if (list.querySelector(".empty")) list.replaceChildren();
  const keep = new Set(shown.map(j => j.id));
  for (const [id, r] of rows) if (!keep.has(id)) { r.root.remove(); rows.delete(id); }
  shown.forEach((j, i) => {
    let r = rows.get(j.id);
    if (!r) {
      r = { root: el("div", "item row") };
      r.badge = el("div", "badge");
      const mid = el("div", "grow");
      r.name = el("div", "name ell"); r.bar = el("div", "bar"); r.fill = el("i");
      r.bar.append(r.fill); r.meta = el("div", "muted small ell");
      mid.append(r.name, r.bar, r.meta);
      r.btn = el("button", "icon-btn");
      r.root.append(r.badge, mid, r.btn);
      rows.set(j.id, r);
    }
    if (list.children[i] !== r.root) list.insertBefore(r.root, list.children[i] || null);
    r.badge.textContent = ICON[cat(j)];
    r.name.textContent = nameOf(j); r.name.title = j.url;
    const frac = j.status === "done" ? 1 : j.size ? Math.min(1, j.done / j.size) : 0;
    r.fill.style.width = (frac * 100).toFixed(1) + "%";
    r.bar.className = "bar" + (j.status === "done" ? " done" : j.status === "error" ? " err"
      : (j.status === "paused" || j.retry_note) ? " paused" : (j.status === "downloading" && !j.size) ? " indet" : "");
    r.bar.hidden = j.status === "done" || j.status === "pending" || (j.status === "error" && !j.done);
    r.meta.textContent = describe(j);
    r.meta.style.color = j.status === "error" ? "var(--err)" : "";
    const act = { downloading: ["pause", "⏸", "Pause"], queued: ["pause", "⏸", "Pause"],
      paused: ["start", "▶", "Resume"], pending: ["start", "▶", "Start"], error: ["start", "↻", "Retry"] }[j.status];
    r.btn.hidden = !act;
    if (act) {
      r.btn.textContent = act[1]; r.btn.title = act[2];
      r.btn.onclick = async () => { const x = await ask({ type: "job", id: j.id, action: act[0] }); if (x.error) flashMsg(x.error); refresh(); };
    }
  });
}

let jobsCache = [];
async function refresh() {
  const s = await ask({ type: "status" });
  const pill = $("status");
  if (s.error) {
    pill.textContent = /not running/i.test(s.error) ? "Not running" : "Error";
    pill.className = "pill err"; pill.title = s.error;
    $("setup").hidden = false; $("list").replaceChildren(el("div", "empty", "PyDM isn't running"));
    return;
  }
  $("setup").hidden = true;
  const active = s.jobs.filter(j => j.status === "downloading");
  const speed = active.reduce((a, j) => a + (j.speed || 0), 0);
  if (!s.online) { pill.textContent = "Offline"; pill.className = "pill warn"; pill.title = "No internet – downloads resume automatically"; }
  else { pill.textContent = active.length ? `${hum(speed)}/s` : "Connected"; pill.className = "pill ok"; pill.title = ""; }
  jobsCache = s.jobs;
  renderJobs(s.jobs);
}

// ---------------------------------------------------------------- media label
function mediaLabel(i) {
  const u = i.url.split("?")[0].toLowerCase();
  const q = (i.url.match(/(?:^|[^\d])(2160|1440|1080|720|540|480|360|240)p?(?:[^\d]|$)/i) || [])[1];
  const kind = /\.m3u8$/.test(u) ? "HLS stream" : /\.mpd$/.test(u) ? "DASH stream" :
    /\.(mp3|m4a|flac|ogg|wav)$/.test(u) ? "Audio" : (u.match(/\.(\w{2,4})$/) || [, "file"])[1].toUpperCase();
  const parts = [(kind === "Audio" ? "Audio" : "Video") + (q ? " " + q + "p" : ""), kind];
  if (i.size) parts.push(hum(i.size));
  return parts.join(" · ");
}

// ---------------------------------------------------------------- init
(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const c = await chrome.storage.local.get({ intercept: true, port: 6800 });
  $("intercept").checked = c.intercept;
  const takeSub = () => $("takeSub").textContent = $("intercept").checked
    ? "Every download goes to PyDM" : "Off – Chrome handles downloads itself";
  takeSub();
  $("intercept").onchange = () => { chrome.storage.local.set({ intercept: $("intercept").checked }); takeSub(); };
  const openOpts = () => chrome.runtime.openOptionsPage();
  $("opts").onclick = openOpts; $("opts2").onclick = openOpts;
  $("openApp").onclick = () => chrome.tabs.create({ url: `http://127.0.0.1:${c.port}/` });
  $("retry").onclick = async () => { await ask({ type: "pair" }); refresh(); };
  $("pauseAll").onclick = async () => {
    for (const j of jobsCache.filter(j => ["downloading", "queued"].includes(j.status)))
      await ask({ type: "job", id: j.id, action: "pause" });
    refresh();
  };
  $("clear").onclick = async () => {
    for (const j of jobsCache.filter(j => j.status === "done")) await ask({ type: "job", id: j.id, action: "remove" });
    refresh();
  };

  refresh();
  setInterval(refresh, 1000);

  // Page video (only on web pages)
  if (tab && /^https?:/i.test(tab.url || "")) {
    const res = await ask({ type: "resolve-tab", tabId: tab.id });
    const pageUrl = (res && res.url) || tab.url;
    const isVideoPage = !!(res && res.url) || /youtube|youtu\.be|tiktok|instagram|facebook|vimeo|dailymotion|twitch|x\.com|twitter|reddit/i.test(tab.url);
    const items = await ask({ type: "list", tabId: tab.id });
    const media = Array.isArray(items) ? items.sort((a, b) => (b.kind === "video") - (a.kind === "video") || (b.size || 0) - (a.size || 0)) : [];
    const title = (tab.title || "").replace(/\s+[|\-–—]\s+[^|–—]{2,30}$/, "").slice(0, 150);
    if (isVideoPage) {
      $("pageSec").hidden = false;
      $("vtitle").textContent = tab.title || pageUrl;
      $("quick").onclick = () => sendJob({ url: pageUrl, kind: "video", referrer: tab.url, quality: "best" }, "best quality");
      $("quals").onclick = async () => {
        $("qspin").hidden = false; $("quals").disabled = true;
        const f = await ask({ type: "formats", url: pageUrl });
        $("qspin").hidden = true; $("quals").disabled = false;
        if (f.error) return flashMsg(f.error, false);
        if (f.title) $("vtitle").textContent = (f.live ? "🔴 LIVE · " : "") + f.title;
        const chips = $("chips"); chips.replaceChildren(); chips.hidden = false;
        const chip = (label, q) => { const b = el("button", "chip", label); b.onclick = () => sendJob({ url: pageUrl, kind: "video", referrer: tab.url, quality: q }, label); chips.append(b); };
        chip("Best", "best");
        (f.heights || []).forEach(h => chip(h + "p", String(h)));
        if (f.audio) chip("Audio (MP3)", "audio");
      };
    }
    if (media.length) {
      $("mediaSec").hidden = false; $("mcount").textContent = media.length;
      const box = $("media");
      media.slice(0, 15).forEach(i => {
        const row = el("div", "item row");
        const lab = el("div", "grow ell", mediaLabel(i)); lab.title = i.url;
        const b = el("button", "icon-btn", "⬇"); b.title = "Download";
        b.onclick = () => sendJob({ url: i.url, kind: i.kind, referrer: tab.url, title }, mediaLabel(i));
        row.append(el("div", "badge", i.kind === "video" ? "📺" : "🎬"), lab, b);
        box.append(row);
      });
    }
  }
})();
