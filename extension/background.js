// PyDM extension background: takes over browser downloads, context menus,
// media sniffing, automatic pairing with the local PyDM service.
// Talks only to the local daemon on 127.0.0.1.

const MEDIA_RE = /\.(m3u8|mpd|mp4|webm|mkv|mov|mp3|m4a|flac|ogg|wav)(\?|$)/i;
const STREAM_RE = /\.(m3u8|mpd)(\?|$)/i;
// Sites where the page must go through yt-dlp (direct media URLs are fragments / signed)
const VIDEO_SITE = /(^|\.)(youtube\.com|youtu\.be|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|x\.com|twitter\.com|threads\.(net|com)|reddit\.com|vimeo\.com|dailymotion\.com|twitch\.tv|pinterest\.[a-z.]+|bilibili\.com|streamable\.com|snapchat\.com)$/i;
const MIN_MEDIA_BYTES = 300 * 1024;  // ignore UI sounds, thumbnails-as-video, tiny segments
const DEFAULTS = {
  token: "", port: 6800,
  intercept: true,        // take over every browser download
  minSizeKB: 0,           // leave smaller files to the browser
  excludeSites: "",       // one host per line
  excludeTypes: "",       // e.g. "jpg png"
  videoButton: true,      // in-page "Download video" button
};
const sniffed = new Map(); // tabId -> Map(url -> {url, kind, size})

const cfg = () => chrome.storage.local.get(DEFAULTS);
const hostOf = u => { try { return new URL(u).hostname; } catch (_) { return ""; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const quiet = () => void chrome.runtime.lastError;

// ---------------------------------------------------------------- daemon API
async function pair(port) {
  // The PyDM page on 127.0.0.1 embeds the token; only extensions (not web
  // pages) may read it, because the daemon checks the Origin header.
  const r = await fetch(`http://127.0.0.1:${port}/`, { cache: "no-store" });
  const m = (await r.text()).match(/const TOKEN="([\w-]+)"/);
  if (!m) throw new Error("Couldn't pair with PyDM");
  await chrome.storage.local.set({ token: m[1] });
  return m[1];
}

async function api(path, body, retry = true) {
  const c = await cfg();
  let token = c.token;
  try {
    if (!token) token = await pair(c.port);
    const r = await fetch(`http://127.0.0.1:${c.port}${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-PyDM-Token": token },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (r.status === 401 && retry) {          // token changed (reinstall): pair again
      await chrome.storage.local.set({ token: "" });
      return api(path, body, false);
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  } catch (e) {
    if (e instanceof TypeError) throw new Error("PyDM is not running. Open PyDM from your app menu.");
    throw e;
  }
}

async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map(c => ({ name: c.name, value: c.value, domain: c.domain, path: c.path,
      secure: c.secure, httpOnly: c.httpOnly, expirationDate: c.expirationDate || 0 }));
  } catch (_) { return []; }
}

async function send(url, { kind, referrer, quality, title } = {}) {
  if (!kind && VIDEO_SITE.test(hostOf(url)) && !MEDIA_RE.test(url)) kind = "video";
  const headers = { "User-Agent": navigator.userAgent };
  if (referrer) headers.Referer = referrer;
  const cookies = await cookiesFor(url);
  if (kind === "video") return api("/api/add", { url, kind, headers, quality, cookies, title });
  if (cookies.length) headers.Cookie = cookies.map(k => `${k.name}=${k.value}`).join("; ");
  return api("/api/add", { url, kind, headers, quality, title });
}

async function formats(url) {
  return api("/api/formats", { url, headers: { "User-Agent": navigator.userAgent, Referer: url },
    cookies: await cookiesFor(url) });
}

// ---------------------------------------------------------------- feedback
function flash(msg, ok) {
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#16a34a" : "#dc2626" });
  chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
  chrome.action.setTitle({ title: "PyDM: " + msg });
  setTimeout(() => { chrome.action.setBadgeText({ text: "" }); chrome.action.setTitle({ title: "PyDM" }); }, 4000);
}
function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icons/icon128.png", title, message,
    priority: 0 }, quiet);
}

// ---------------------------------------------------------------- menus
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: "link", title: "Download with PyDM", contexts: ["link", "audio", "image"] });
    chrome.contextMenus.create({ id: "video", title: "Download this video with PyDM", contexts: ["video"] });
    chrome.contextMenus.create({ id: "page", title: "Grab video from this page (PyDM)", contexts: ["page", "frame"] });
    chrome.contextMenus.create({ id: "sep", type: "separator", contexts: ["action"] });
    chrome.contextMenus.create({ id: "toggle", title: "Take over browser downloads", type: "checkbox",
      checked: true, contexts: ["action"] });
  });
  cfg().then(c => chrome.contextMenus.update("toggle", { checked: c.intercept }, quiet));
  cfg().then(c => c.token || pair(c.port).catch(() => {}));   // pair automatically
});

chrome.storage.onChanged.addListener(ch => {
  if (ch.intercept) chrome.contextMenus.update("toggle", { checked: ch.intercept.newValue }, quiet);
});

function resolve(tabId, frameId, fromContext) {
  return new Promise(res => {
    try {
      chrome.tabs.sendMessage(tabId, { type: "resolve", fromContext }, { frameId: frameId || 0 },
        r => { quiet(); res(r && r.url); });
    } catch (_) { res(null); }
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === "toggle") return chrome.storage.local.set({ intercept: info.checked });
  const ref = tab && tab.url;
  try {
    if (info.menuItemId === "link") {
      await send(info.linkUrl || info.srcUrl, { referrer: ref });
    } else if (info.menuItemId === "video") {
      const src = info.srcUrl || "";
      // blob:/MSE players (TikTok, Instagram, YouTube...) -> download the post with yt-dlp
      if (/^https?:/i.test(src) && !VIDEO_SITE.test(hostOf(info.pageUrl || ""))) {
        await send(src, { referrer: ref, title: tab && tab.title });
      } else {
        const post = tab && await resolve(tab.id, info.frameId, true);
        await send(post || info.pageUrl, { kind: "video", referrer: ref });
      }
    } else {
      const post = tab && await resolve(tab.id, info.frameId, false);
      await send(post || info.frameUrl || info.pageUrl, { kind: "video", referrer: ref });
    }
    flash("Added to PyDM", true);
  } catch (e) { flash(e.message); notify("PyDM", e.message); }
});

// ---------------------------------------------------------------- download takeover
function skipReason(item, url, c) {
  if (!c.intercept) return "off";
  if (item.byExtensionId === chrome.runtime.id) return "ours";       // our own hand-back
  if (!/^https?:/i.test(url)) return "scheme";                         // blob:, data:, file:
  if (item.state && item.state !== "in_progress") return "state";
  const host = hostOf(url);
  const sites = c.excludeSites.split(/[\s,]+/).filter(Boolean).map(s => s.replace(/^\*?\.?/, "").toLowerCase());
  if (sites.some(s => host === s || host.endsWith("." + s))) return "site";
  const ext = ((item.filename || new URL(url).pathname).match(/\.(\w{1,8})$/) || [])[1];
  const types = c.excludeTypes.toLowerCase().split(/[\s,.]+/).filter(Boolean);
  if (ext && types.includes(ext.toLowerCase())) return "type";
  if (c.minSizeKB && item.totalBytes > 0 && item.totalBytes < c.minSizeKB * 1024) return "small";
  return null;
}

const takeovers = new Set();
chrome.downloads.onCreated.addListener(async item => {
  const c = await cfg();
  const url = item.finalUrl || item.url;
  if (skipReason(item, url, c) || takeovers.has(item.id)) return;
  takeovers.add(item.id);
  // Hold the browser's copy until PyDM proves it can download the file.
  chrome.downloads.pause(item.id, quiet);
  let job;
  try {
    job = await send(url, { referrer: item.referrer, kind: "file" });
  } catch (e) {
    chrome.downloads.resume(item.id, quiet);     // PyDM not running: browser keeps it
    takeovers.delete(item.id);
    return;
  }
  const handBack = async why => {
    chrome.downloads.resume(item.id, quiet);
    api(`/api/jobs/${job.id}/remove`, {}).catch(() => {});
    notify("Downloading in Chrome instead", why);
  };
  const accept = () => new Promise(done => chrome.downloads.search({ id: item.id }, ([it] = []) => {
    quiet();
    if (it && it.state === "complete") {
      // Chrome already finished it (pause wasn't possible): don't download twice
      api(`/api/jobs/${job.id}/remove`, {}).catch(() => {});
    } else {
      chrome.downloads.cancel(item.id, () => { quiet(); chrome.downloads.erase({ id: item.id }, quiet); });
      flash("Sent to PyDM", true);
    }
    done();
  }));
  // Watch the job for up to 25 s: cancel Chrome's copy once data flows.
  try {
    for (let t = 0; t < 25; t++) {
      await sleep(1000);
      const res = await api("/api/jobs");
      const j = (res.jobs || []).find(x => x.id === job.id);
      if (!j) { chrome.downloads.resume(item.id, quiet); break; }        // user removed it
      if (j.status === "error" || j.retry_note) { await handBack(j.error || j.last_error || "PyDM couldn't fetch this file."); break; }
      if (j.status === "done" || (j.status === "downloading" && (j.done > 0 || j.size > 0))
          || (j.status === "queued" && !j.retry_note && t >= 2) || j.status === "paused" || t === 24) {
        await accept(); break;
      }
    }
  } catch (_) { await accept(); }
  takeovers.delete(item.id);
});

// ---------------------------------------------------------------- media sniffer
function cleanMediaUrl(u) {
  try {
    const x = new URL(u);
    // Instagram/Facebook CDN serve byte ranges of the full file: drop the range
    if (/fbcdn\.net|cdninstagram\.com/.test(x.hostname)) {
      x.searchParams.delete("bytestart"); x.searchParams.delete("byteend");
    }
    return x.href;
  } catch (_) { return u; }
}

chrome.webRequest.onHeadersReceived.addListener(d => {
  if (d.tabId < 0 || d.statusCode >= 400) return;
  const h = Object.fromEntries((d.responseHeaders || []).map(x => [x.name.toLowerCase(), x.value || ""]));
  const ctype = h["content-type"] || "";
  const isMedia = MEDIA_RE.test(d.url) || /^(video|audio)\//i.test(ctype) || /mpegurl|dash\+xml/i.test(ctype);
  if (!isMedia) return;
  if (/[?&](range|rn|sq)=|\/range\/|\.ts(\?|$)|\/seg[-_]?\d+|init\.mp4/i.test(d.url)) return; // segments
  const stream = STREAM_RE.test(d.url) || /mpegurl|dash\+xml/i.test(ctype);
  const cr = (h["content-range"] || "").match(/\/(\d+)$/);
  const size = cr ? +cr[1] : +(h["content-length"] || 0);
  if (!stream && size && size < MIN_MEDIA_BYTES) return;
  const url = cleanMediaUrl(d.url);
  if (!sniffed.has(d.tabId)) sniffed.set(d.tabId, new Map());
  const m = sniffed.get(d.tabId);
  if (m.has(url)) return;
  m.set(url, { url, kind: stream ? "video" : undefined, size });
  if (m.size > 50) m.delete(m.keys().next().value);
  chrome.action.setBadgeBackgroundColor({ tabId: d.tabId, color: "#2563eb" });
  chrome.action.setBadgeText({ tabId: d.tabId, text: String(m.size) });
}, { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other"] }, ["responseHeaders"]);

chrome.tabs.onRemoved.addListener(id => sniffed.delete(id));
chrome.tabs.onUpdated.addListener((id, ch) => {
  // full page loads reset the list; SPA navigations (TikTok/Instagram scrolling) keep it
  if (ch.status === "loading" && ch.url) { sniffed.delete(id); chrome.action.setBadgeText({ tabId: id, text: "" }); }
});

// ---------------------------------------------------------------- messages (popup, content, options)
const handlers = {
  list: (msg, sender) => [...(sniffed.get(msg.tabId != null ? msg.tabId : sender.tab && sender.tab.id)
    || new Map()).values()].reverse(),
  formats: msg => formats(msg.url),
  send: msg => send(msg.url, { kind: msg.kind, referrer: msg.referrer, quality: msg.quality, title: msg.title })
    .then(() => ({ ok: true })),
  "resolve-tab": msg => resolve(msg.tabId, 0, false).then(url => ({ url })),
  status: () => api("/api/jobs").then(r => ({ ok: true, online: r.online, jobs: r.jobs, window_open: r.window_open })),
  job: msg => api(`/api/jobs/${msg.id}/${msg.action}`, {}),
  pair: async () => { const c = await cfg(); await pair(c.port); return api("/api/jobs").then(() => ({ ok: true })); },
};
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const h = handlers[msg.type];
  if (!h) return;
  Promise.resolve().then(() => h(msg, sender)).then(reply, e => reply({ error: e.message }));
  return true;
});
