// PyDM in-page button: shows "Download video" over a visible <video> (like IDM),
// opens a quality menu, and sends the choice to the PyDM desktop app.
// On feed sites (TikTok, Instagram, X, Facebook...) it works out the URL of the
// post the video belongs to, because the page URL is just the feed.
(() => {
  if (window.__pydmInjected) return;
  window.__pydmInjected = true;

  // ---------- figure out the post URL for a <video> ----------
  const POST_LINK = [
    /^https?:\/\/(www\.)?tiktok\.com\/@[^/?#]*\/(video|photo)\/\d+/i,
    /^https?:\/\/(www\.)?instagram\.com\/([\w.]+\/)?(p|reel|reels|tv)\/[\w-]+/i,
    /^https?:\/\/(www\.)?(x|twitter)\.com\/[^/]+\/status\/\d+/i,
    /^https?:\/\/(www\.|m\.|web\.)?facebook\.com\/(reel\/\d+|watch\/?\?v=\d+|[^/]+\/videos\/[\w.-]+|share\/[rv]\/\w+)/i,
    /^https?:\/\/(www\.)?threads\.(net|com)\/@[^/]+\/post\/[\w-]+/i,
    /^https?:\/\/(www\.)?reddit\.com\/r\/[^/]+\/comments\/\w+/i,
    /^https?:\/\/(www\.)?pinterest\.[a-z.]+\/pin\/\d+/i,
    /^https?:\/\/(www\.)?youtube\.com\/(shorts\/[\w-]+|watch\?v=[\w-]+)/i,
  ];
  const isPost = u => POST_LINK.some(r => r.test(u || ""));
  const norm = u => {
    try {
      const x = new URL(u, location.href);
      x.hash = "";
      // instagram.com/reels/ID -> /reel/ID ; drop tracking params
      x.pathname = x.pathname.replace(/^\/([\w.]+\/)?reels\//, "/reel/");
      if (/instagram\.com|tiktok\.com|x\.com|twitter\.com|threads\./.test(x.hostname)) x.search = "";
      return x.href;
    } catch (_) { return u; }
  };

  function tiktokFromIds(v) {
    // TikTok feed players live in <div id="xgwrapper-N-<videoId>">
    for (let e = v, i = 0; e && i < 12; e = e.parentElement, i++) {
      const m = (e.id || "").match(/(\d{17,21})$/);
      if (m) {
        const a = e.closest("article, [data-e2e*='item']");
        const user = a && [...a.querySelectorAll("a[href*='/@']")]
          .map(x => (x.getAttribute("href").match(/\/@([^/?#]+)/) || [])[1]).find(Boolean);
        return `https://www.tiktok.com/@${user || "_"}/video/${m[1]}`;
      }
    }
    return null;
  }

  function postUrlFor(v) {
    const here = norm(location.href);
    // A single-post page: the page URL is right.
    if (isPost(here) && document.querySelectorAll("video").length <= 2) return here;
    if (/tiktok\.com$/.test(location.hostname)) {
      const t = tiktokFromIds(v);
      if (t) return t;
    }
    // Walk up from the video; the first ancestor that contains a post link is
    // (almost always) the post card the video belongs to.
    for (let e = v && v.parentElement, i = 0; e && e !== document.body && i < 25; e = e.parentElement, i++) {
      const links = [...e.querySelectorAll("a[href]")].map(a => norm(a.href)).filter(isPost);
      if (links.length) {
        // Prefer links that look like the post itself (status/video/reel), most common first
        const count = {};
        links.forEach(l => count[l] = (count[l] || 0) + 1);
        return Object.keys(count).sort((a, b) => count[b] - count[a])[0];
      }
    }
    if (isPost(here)) return here;
    return location.href;
  }

  // ---------- UI ----------
  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;width:0;height:0;";
  const root = host.attachShadow({ mode: "closed" });
  root.innerHTML = `
  <style>
    .btn{position:fixed;display:none;font:600 13px system-ui,sans-serif;color:#fff;background:#2563eb;
      border:0;border-radius:6px;padding:7px 12px;cursor:pointer;box-shadow:0 2px 8px #0006;opacity:.92;
      transition:opacity .15s}
    .btn:hover{opacity:1;background:#1d4ed8}
    .x{margin-left:8px;opacity:.7}
    .panel{position:fixed;display:none;width:290px;max-height:380px;overflow:auto;background:#1b2029;color:#e6e9ef;
      border:1px solid #3a4254;border-radius:8px;padding:10px;font:13px system-ui,sans-serif;box-shadow:0 6px 24px #000a}
    .panel h4{margin:0 0 6px;font-size:13px}.t{color:#98a2b3;font-size:12px;margin:6px 0;word-break:break-word}
    .opt{display:block;width:100%;text-align:left;margin:3px 0;padding:6px 8px;border-radius:5px;border:1px solid #3a4254;
      background:#242b38;color:#e6e9ef;cursor:pointer;font:inherit;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .opt:hover{background:#2f3a4d}.main{background:#2563eb;border-color:#2563eb;font-weight:600}.main:hover{background:#1d4ed8}
    .msg{color:#f87171;font-size:12px;margin-top:6px;word-break:break-word}.ok{color:#34d399}
    .spin{display:inline-block;width:10px;height:10px;border:2px solid #98a2b3;border-top-color:transparent;
      border-radius:50%;animation:s .7s linear infinite;vertical-align:-1px;margin-right:6px}
    @keyframes s{to{transform:rotate(360deg)}}
  </style>
  <button class="btn" id="b"><span id="bl">Download video</span><span class="x" id="x" title="Hide on this page">✕</span></button>
  <div class="panel" id="p"></div>`;
  const $ = id => root.getElementById(id);
  const btn = $("b"), panel = $("p");
  let current = null, dismissed = false, hovered = null, lastCtx = null, enabled = true;
  try {
    chrome.storage.local.get({ videoButton: true }, c => { enabled = c.videoButton; });
    chrome.storage.onChanged.addListener(ch => { if (ch.videoButton) enabled = ch.videoButton.newValue; });
  } catch (_) {}

  addEventListener("mouseover", e => { if (e.target instanceof HTMLVideoElement) hovered = e.target; }, true);
  addEventListener("contextmenu", e => { lastCtx = e.target; }, true);

  const visible = v => {
    const r = v.getBoundingClientRect();
    const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    return r.width >= 160 && r.height >= 120 && w > r.width * 0.5 && h > r.height * 0.5;
  };
  const area = v => v.clientWidth * v.clientHeight;
  // Prefer the playing video, then the one under the mouse, then the biggest visible one.
  function pick() {
    const vids = [...document.querySelectorAll("video")].filter(v => !v.ended && visible(v));
    if (!vids.length) return null;
    const playing = vids.filter(v => !v.paused && v.readyState >= 2).sort((a, b) => area(b) - area(a));
    if (playing.length) return playing[0];
    if (hovered && vids.includes(hovered)) return hovered;
    return vids.filter(v => v.readyState >= 1).sort((a, b) => area(b) - area(a))[0] || null;
  }

  function place() {
    const v = pick();
    if (!v || dismissed || !enabled) {
      btn.style.display = "none";
      if (!panel.dataset.keep) panel.style.display = "none";
      return;
    }
    if (panel.dataset.keep && current && current !== v) return; // menu open: stay put
    current = v;
    const r = v.getBoundingClientRect();
    const top = Math.max(4, Math.min(innerHeight - 40, r.top + 10));
    btn.style.display = "block";
    btn.style.left = Math.max(4, Math.min(innerWidth - 190, r.right - 190)) + "px";
    btn.style.top = top + "px";
    panel.style.left = Math.max(4, Math.min(innerWidth - 300, r.right - 300)) + "px";
    panel.style.top = Math.max(4, Math.min(innerHeight - 200, top + 38)) + "px";
  }

  const ask = msg => new Promise(res => {
    try { chrome.runtime.sendMessage(msg, r => res(r || { error: chrome.runtime.lastError ? "Extension was reloaded - refresh this page" : "no response" })); }
    catch (_) { res({ error: "Extension was reloaded - refresh this page" }); }
  });
  const clear = () => { while (panel.firstChild) panel.removeChild(panel.firstChild); };
  const add = (tag, cls, text, parent = panel) => {
    const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text;
    parent.append(e); return e;
  };
  const closePanel = () => { delete panel.dataset.keep; panel.style.display = "none"; };

  async function send(label, payload) {
    clear(); add("h4", "", "Sending to PyDM...");
    const r = await ask({ type: "send", referrer: location.href, ...payload });
    clear();
    add("h4", "", r.error ? "Could not add" : "Added to PyDM ✓");
    add("div", r.error ? "msg" : "t ok", r.error || label);
    setTimeout(closePanel, r.error ? 5000 : 1800);
  }

  // Title for the saved file: the page's video title, without the " | Site name" tail
  function pageTitle() {
    const og = document.querySelector('meta[property="og:title"], meta[name="twitter:title"]');
    let t = (og && og.content) || document.title || "";
    t = t.replace(/\s+[|\-–—]\s+[^|–—]{2,30}$/, "").trim();   // "Movie - SiteName" -> "Movie"
    return t.slice(0, 150);
  }

  // Human label for a detected media URL: "Video 1080p · HLS stream · 512 MB"
  function mediaLabel(i) {
    const u = i.url.split("?")[0].toLowerCase();
    const q = (i.url.match(/(?:^|[^\d])(2160|1440|1080|720|540|480|360|240)p?(?:[^\d]|$)/i) || [])[1];
    const kind = /\.m3u8$/.test(u) ? "HLS stream" : /\.mpd$/.test(u) ? "DASH stream" :
      /\.(mp3|m4a|flac|ogg|wav)$/.test(u) ? "Audio" : (u.match(/\.(\w{2,4})$/) || [, "file"])[1].toUpperCase();
    const parts = [(/^audio/i.test(kind) ? "Audio" : "Video") + (q ? " " + q + "p" : ""), kind];
    if (i.size) parts.push(i.size >= 1073741824 ? (i.size / 1073741824).toFixed(2) + " GB"
      : (i.size / 1048576).toFixed(1) + " MB");
    return parts.join(" · ");
  }

  async function openMenu() {
    if (panel.style.display === "block") return closePanel();
    panel.dataset.keep = "1"; panel.style.display = "block";
    const url = postUrlFor(current);
    const title = pageTitle();
    clear();
    add("h4", "", "Download video");
    // Instant options: no need to wait for yt-dlp to list qualities
    const top = add("div", "");
    add("button", "opt main", "⬇ Best quality", top).onclick = () => send("Best quality", { url, kind: "video", quality: "best" });
    add("button", "opt", "♪ Audio only (mp3)", top).onclick = () => send("Audio only", { url, kind: "video", quality: "audio" });
    const info = add("div", "t");
    add("span", "spin", "", info); info.append("Reading qualities...");
    const box = add("div", "");

    const direct = current && /^https?:/i.test(current.currentSrc || "") ? current.currentSrc : null;
    const [fmt, sniffed] = await Promise.all([ask({ type: "formats", url }), ask({ type: "list" })]);
    if (panel.style.display !== "block" || !box.isConnected) return;

    const extra = [...(direct ? [{ url: direct }] : []), ...(Array.isArray(sniffed) ? sniffed : [])];
    const seen = new Set();
    const media = extra.filter(i => !seen.has(i.url) && seen.add(i.url))
      // streams first (they hold every quality), then the biggest files
      .sort((a, b) => (b.kind === "video") - (a.kind === "video") || (b.size || 0) - (a.size || 0));
    const mediaButtons = (parent, main) => media.slice(0, 8).forEach((i, n) => {
      const b = add("button", "opt" + (main && n === 0 ? " main" : ""), (main && n === 0 ? "⬇ " : "") + mediaLabel(i), parent);
      b.title = i.url;
      b.onclick = () => send(title || mediaLabel(i), { url: i.url, kind: i.kind, title });
    });

    if (!fmt.error) {
      info.textContent = (fmt.live ? "LIVE: " : "") + (fmt.title || new URL(url).hostname);
      (fmt.heights || []).forEach(h => add("button", "opt", h + "p", box).onclick =
        () => send(h + "p", { url, kind: "video", quality: String(h) }));
      if (media.length) { add("div", "t", "Other media on this page:", box); mediaButtons(box, false); }
      return;
    }

    // yt-dlp can't read this page: offer what the browser actually loaded instead
    const unsupported = /unsupported url|no video formats|unable to extract/i.test(fmt.error);
    if (media.length) {
      top.remove();
      info.textContent = title ? "“" + title + "”" : "";
      mediaButtons(box, true);
      add("div", "t", unsupported
        ? "This site can't be read directly, so PyDM will download the video stream your browser is playing."
        : "Couldn't read the page (" + fmt.error.slice(0, 120) + "). Downloading the detected stream instead.", box);
    } else if (unsupported) {
      top.remove();
      info.textContent = "";
      add("div", "t", "This site isn't supported directly. Play the video for a few seconds " +
        "(skip any ads), then click Download video again – PyDM will catch the stream.", box);
    } else {
      info.textContent = "";
      add("div", "msg", fmt.error, box);
    }
  }

  btn.addEventListener("click", e => {
    if (e.target.id === "x") { dismissed = true; closePanel(); place(); } else openMenu();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePanel(); });

  // Popup / context menu ask: "what is the video post on this page?"
  chrome.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg.type !== "resolve") return;
    let v = null;
    if (msg.fromContext && lastCtx) {
      // Sites put overlays on top of the <video>; look around the clicked element
      for (let e = lastCtx, i = 0; e && e.nodeType === 1 && i < 8 && !v; e = e.parentElement, i++)
        v = e instanceof HTMLVideoElement ? e : e.querySelector("video");
    }
    v = v || pick();
    reply({ url: v ? postUrlFor(v) : (isPost(norm(location.href)) ? norm(location.href) : null) });
  });

  document.documentElement.append(host);
  setInterval(place, 300);
  addEventListener("scroll", place, { passive: true, capture: true });
  addEventListener("resize", place);
})();
