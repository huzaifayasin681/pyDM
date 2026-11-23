// PyDM settings page: every change is saved immediately.
const $ = id => document.getElementById(id);
const DEFAULTS = { token: "", port: 6800, intercept: true, minSizeKB: 0, excludeSites: "",
  excludeTypes: "", videoButton: true };
const ask = msg => new Promise(res => chrome.runtime.sendMessage(msg, r => res(r || { error: "No response" })));

function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("show");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("show"), 1400);
}

async function check() {
  const pill = $("status"), sub = $("statusSub");
  pill.textContent = "Checking…"; pill.className = "pill";
  const s = await ask({ type: "status" });
  if (s.error) {
    pill.textContent = "Not connected"; pill.className = "pill err";
    sub.textContent = /not running/i.test(s.error)
      ? "PyDM isn't running. Open PyDM from your app menu, then click Reconnect." : s.error;
  } else {
    pill.textContent = "Connected"; pill.className = "pill ok";
    const active = s.jobs.filter(j => j.status === "downloading").length;
    sub.textContent = `${s.jobs.length} downloads in PyDM` + (active ? `, ${active} running` : "") +
      (s.online ? "" : " · no internet right now");
  }
  const c = await chrome.storage.local.get({ token: "" });
  $("token").value = c.token;
}

chrome.storage.local.get(DEFAULTS, c => {
  for (const [k, v] of Object.entries(c)) {
    const e = $(k); if (!e) continue;
    if (e.type === "checkbox") e.checked = v; else e.value = v;
  }
});

for (const k of Object.keys(DEFAULTS)) {
  const e = $(k); if (!e) continue;
  e.addEventListener(e.type === "checkbox" ? "change" : "input", () => {
    clearTimeout(e._t);
    e._t = setTimeout(() => {
      let v = e.type === "checkbox" ? e.checked : e.type === "number" ? Math.max(0, +e.value || 0) : e.value.trim();
      if (k === "port") v = Math.min(65535, v || 6800);
      chrome.storage.local.set({ [k]: v }, () => { toast("Saved"); if (k === "port" || k === "token") check(); });
    }, e.type === "checkbox" ? 0 : 500);
  });
}

$("pair").onclick = async () => {
  const r = await ask({ type: "pair" });
  if (r.error) toast("Couldn't connect"); else toast("Connected");
  check();
};

check();
