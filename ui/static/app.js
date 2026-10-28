"use strict";

/* ---------- Thème (exécuté avant le rendu pour éviter le flash) ---------- */
const THEMES = ["system", "light", "dark"];
const THEME_LABEL = { system: "Thème : système", light: "Thème : clair", dark: "Thème : sombre" };
const mq = window.matchMedia("(prefers-color-scheme: dark)");
function store(k, v) { try { v === undefined ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) { /* stockage indisponible */ } }
function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function applyTheme(pref) {
  const eff = pref === "system" ? (mq.matches ? "dark" : "light") : pref;
  document.documentElement.dataset.theme = eff;
  document.documentElement.dataset.pref = pref;
}
let themePref = THEMES.includes(load("theme")) ? load("theme") : "system";
applyTheme(themePref);
mq.addEventListener("change", () => applyTheme(themePref));

document.addEventListener("DOMContentLoaded", () => {
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const svg = (paths, cls) => {
    const ns = "http://www.w3.org/2000/svg", s = document.createElementNS(ns, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); if (cls) s.setAttribute("class", cls);
    for (const d of paths) { const p = document.createElementNS(ns, "path"); p.setAttribute("d", d); s.appendChild(p); }
    return s;
  };
  const ICON_OK = ["m5 12.5 4.5 4.5L19 7.5"], ICON_KO = ["M7 7l10 10M17 7 7 17"];
  const ICON_TOR = ["M4 12h13", "m12 6 6 6-6 6"];
  const DEC = {
    allowed: { cls: "ok", label: "Autorisée", icon: ICON_OK },
    tor: { cls: "tor", label: "Via Tor", icon: ICON_TOR },
    denied: { cls: "ko", label: "Refusée", icon: ICON_KO },
  };
  let torSeen = false;
  const markTor = (on) => { if (on && !torSeen) { torSeen = true; document.body.classList.add("has-tor"); $("#top-title").textContent = "Hors whitelist"; } };
  const ICON_COPY = ["M9 9h10v10H9z", "M5 15V5h10"];

  const nf = new Intl.NumberFormat("fr-FR");
  const fmtBytes = (b) => b == null || b < 0 ? "–" : b < 1024 ? b + " o" : b < 1048576 ? (b / 1024).toFixed(1) + " Ko" : b < 1073741824 ? (b / 1048576).toFixed(1) + " Mo" : (b / 1073741824).toFixed(2) + " Go";
  const fmtDur = (ms) => ms == null || ms < 0 ? "–" : ms < 1000 ? ms + " ms" : ms < 60000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms / 60000) + " min";
  const fmtTime = (ts, withDate) => {
    const d = new Date(ts * 1000);
    const t = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return withDate ? d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " + t : t;
  };
  const ago = (ts) => {
    const s = Date.now() / 1000 - ts;
    return s < 60 ? "à l’instant" : s < 3600 ? `il y a ${Math.floor(s / 60)} min` : s < 86400 ? `il y a ${Math.floor(s / 3600)} h` : `il y a ${Math.floor(s / 86400)} j`;
  };

  /* ---------- État ---------- */
  const state = {
    range: load("range") || "24h",
    decision: "",
    profile: "",
    q: "",
    live: true,
    maxId: 0,
    minId: null,
    loading: false,
  };

  function toast(msg) {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 1800);
  }

  async function api(path, params) {
    const u = new URL(path, location.origin);
    for (const [k, v] of Object.entries(params || {})) if (v !== "" && v != null) u.searchParams.set(k, v);
    const r = await fetch(u, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(r.status + " " + r.statusText);
    return r.json();
  }
  const filters = () => ({ range: state.range, decision: state.decision, profile: state.profile, q: state.q });

  /* ---------- Contrôles ---------- */
  function bindSeg(id, key, persist) {
    const root = $(id);
    const sync = () => root.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === state[key])));
    root.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      state[key] = b.dataset.v; if (persist) store(key, state[key]); sync(); reload();
    });
    sync();
  }
  bindSeg("#range", "range", true);
  bindSeg("#decision", "decision");
  $("#profile").addEventListener("change", (e) => { state.profile = e.target.value; reload(); });
  let qTimer;
  $("#q").addEventListener("input", (e) => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = e.target.value.trim(); reload(); }, 250); });

  $("#live").addEventListener("click", () => {
    state.live = !state.live;
    $("#live").setAttribute("aria-pressed", String(state.live));
    $("#live .lbl").textContent = state.live ? "En direct" : "En pause";
    if (state.live) tick();
  });
  const themeBtn = $("#theme");
  const syncThemeBtn = () => { themeBtn.title = THEME_LABEL[themePref]; $("#theme-label").textContent = THEME_LABEL[themePref]; };
  themeBtn.addEventListener("click", () => {
    themePref = THEMES[(THEMES.indexOf(themePref) + 1) % THEMES.length];
    store("theme", themePref); applyTheme(themePref); syncThemeBtn(); renderChart(lastStats);
  });
  syncThemeBtn();

  /* ---------- Tableau ---------- */
  const tbody = $("#rows");
  const multiDay = () => ["7d", "30d"].includes(state.range);

  function rowFor(r, isNew) {
    const tr = el("tr", "r" + (isNew ? " new" : ""));
    tr.dataset.id = r.id;
    tr.appendChild(el("td", "time", fmtTime(r.ts, multiDay())));
    const dec = el("td"); const d = DEC[r.decision] || DEC.denied;
    if (r.decision === "tor") markTor(true);
    const pill = el("span", "pill " + d.cls);
    pill.appendChild(svg(d.icon)); pill.appendChild(document.createTextNode(d.label));
    dec.appendChild(pill); tr.appendChild(dec);
    const pt = el("td"); pt.appendChild(el("span", "tag" + (r.profile ? "" : " none"), r.profile || "aucun")); tr.appendChild(pt);
    tr.appendChild(el("td", "mono", r.client));
    tr.appendChild(el("td", "mono", r.method));
    const h = el("td", "host", r.host + (r.port && ![80, 443].includes(r.port) ? ":" + r.port : ""));
    h.title = r.url; tr.appendChild(h);
    const code = el("td", "num mono" + (r.http_status >= 400 || r.http_status === 0 ? " code-err" : ""), r.http_status ?? "–");
    tr.appendChild(code);
    tr.appendChild(el("td", "num", fmtBytes(r.bytes_out)));
    tr.appendChild(el("td", "num", fmtDur(r.duration_ms)));
    tr._data = r;
    return tr;
  }

  function detailFor(r) {
    const tr = el("tr", "detail"); const td = el("td"); td.colSpan = 9;
    const dl = el("dl", "kv");
    const add = (k, v) => { if (v === "" || v == null) return; dl.appendChild(el("dt", null, k)); dl.appendChild(el("dd", null, String(v))); };
    add("Date", new Date(r.ts * 1000).toLocaleString("fr-FR"));
    add("URL", r.url);
    add("Statut squid", r.squid_status + (r.http_status != null ? " / " + r.http_status : ""));
    add("IP serveur", r.server_ip);
    add("Envoyé / reçu", fmtBytes(r.bytes_in) + " / " + fmtBytes(r.bytes_out));
    add("User-Agent", r.user_agent);
    td.appendChild(dl); tr.appendChild(td); return tr;
  }

  tbody.addEventListener("click", (e) => {
    const tr = e.target.closest("tr.r"); if (!tr) return;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail")) next.remove();
    else tr.after(detailFor(tr._data));
  });

  function setEmpty() {
    const has = !!tbody.querySelector("tr.r");
    $("#empty").hidden = has;
    $("#count").textContent = has ? `${nf.format(tbody.querySelectorAll("tr.r").length)} affichées` : "";
  }

  async function loadRows(reset) {
    const PAGE = 100;
    const params = { ...filters(), limit: PAGE };
    if (!reset && state.minId) params.before_id = state.minId;
    const { items } = await api("/api/requests", params);
    if (reset) { tbody.replaceChildren(); state.maxId = 0; state.minId = null; }
    const frag = document.createDocumentFragment();
    for (const r of items) frag.appendChild(rowFor(r));
    tbody.appendChild(frag);
    if (items.length) {
      state.maxId = Math.max(state.maxId, items[0].id);
      state.minId = items[items.length - 1].id;
    }
    $("#more").hidden = items.length < PAGE;
    setEmpty();
  }
  $("#more").addEventListener("click", () => loadRows(false).catch(showErr));

  async function loadNew() {
    if (!state.maxId) return loadRows(true);
    const { items } = await api("/api/requests", { ...filters(), after_id: state.maxId, limit: 500 });
    if (!items.length) return;
    const frag = document.createDocumentFragment();
    for (const r of items) frag.appendChild(rowFor(r, true));
    tbody.prepend(frag);
    state.maxId = items[0].id;
    const rows = tbody.querySelectorAll("tr.r"); // borne la taille du DOM en direct
    if (rows.length > 1500) {
      for (let i = 1500; i < rows.length; i++) { const n = rows[i].nextElementSibling; if (n && n.classList.contains("detail")) n.remove(); rows[i].remove(); }
      state.minId = rows[1499]._data.id;
      $("#more").hidden = false;
    }
    setEmpty();
  }

  /* ---------- Statistiques ---------- */
  let lastStats = null;
  function renderStats(s) {
    lastStats = s;
    $("#k-total").textContent = nf.format(s.total);
    $("#k-ok").textContent = nf.format(s.allowed);
    $("#k-ko").textContent = nf.format(s.denied);
    markTor(s.tor > 0);
    $("#k-tor").textContent = nf.format(s.tor);
    $("#k-tor-pct").textContent = s.total ? `${(100 * s.tor / s.total).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} % du trafic` : "";
    $("#k-ko-pct").textContent = s.total ? `${(100 * s.denied / s.total).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} % du trafic` : "";
    $("#k-hosts").textContent = nf.format(s.hosts);
    renderChart(s);
    renderTopDenied(s.top_denied);
  }

  function renderTopDenied(list) {
    const ol = $("#top-denied"); ol.replaceChildren();
    if (!list.length) { ol.appendChild(el("li", "empty-li", "Aucun refus sur la période.")); return; }
    for (const t of list) {
      const li = el("li");
      const h = el("span", "h", t.host); h.title = t.host;
      const via = t.tor ? (t.tor === t.n ? " · via Tor" : ` · ${nf.format(t.tor)} via Tor`) : "";
      const sub = el("small", null, `${t.profiles || "aucun profil"}${via} · ${ago(t.last)}`); h.appendChild(sub);
      li.appendChild(h);
      li.appendChild(el("span", "n", nf.format(t.n)));
      const b = el("button", "copy"); b.type = "button"; b.title = "Copier la ligne de whitelist"; b.appendChild(svg(ICON_COPY));
      b.addEventListener("click", () => copy(t.host));
      li.appendChild(b);
      ol.appendChild(li);
    }
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast(`Copié : ${text}`); }
    catch (e) {
      const ta = el("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); toast(`Copié : ${text}`); } catch (e2) { toast(text); }
      ta.remove();
    }
  }

  /* ---------- Graphique : barres empilées autorisées / refusées ---------- */
  const NS = "http://www.w3.org/2000/svg";
  function sv(tag, attrs) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function niceMax(v) { if (v <= 4) return 4; const p = 10 ** Math.floor(Math.log10(v)); const m = v / p; return (m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }

  function renderChart(s) {
    if (!s) return;
    const box = $("#chart"); const tip = $("#tip");
    box.querySelector("svg")?.remove();
    const W = box.clientWidth - 32, H = box.clientHeight - 16;
    const padL = 36, padB = 20, padT = 6;
    const data = s.timeline, n = data.length;
    const max = niceMax(Math.max(1, ...data.map((d) => d.allowed + d.tor + d.denied)));
    const iw = W - padL, ih = H - padB - padT;
    const step = iw / n, bw = Math.max(2, step - Math.min(6, step * 0.3));
    const y = (v) => padT + ih - (v / max) * ih;
    const root = sv("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": "Requêtes autorisées et refusées dans le temps" });
    for (const f of [0.5, 1]) {
      root.appendChild(sv("line", { x1: padL, x2: W, y1: y(max * f), y2: y(max * f), class: "gridline" }));
      const t = sv("text", { x: padL - 8, y: y(max * f) + 4, "text-anchor": "end", class: "axis" }); t.textContent = nf.format(max * f); root.appendChild(t);
    }
    const z = sv("text", { x: padL - 8, y: y(0) + 4, "text-anchor": "end", class: "axis" }); z.textContent = "0"; root.appendChild(z);
    const long = s.bucket_seconds * n > 86400 * 1.5;
    const lab = (ts) => long ? new Date(ts * 1000).toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) : new Date(ts * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
    for (const i of [0, Math.floor(n / 4), Math.floor(n / 2), Math.floor(3 * n / 4), n - 1]) {
      const t = sv("text", { x: padL + i * step + step / 2, y: H - 4, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle", class: "axis" });
      t.textContent = lab(data[i].t + (i === n - 1 ? s.bucket_seconds : 0)); root.appendChild(t);
    }
    const r = Math.min(3, bw / 2);
    data.forEach((d, i) => {
      const x = padL + i * step + (step - bw) / 2;
      const g = sv("g", {});
      // Segment refusé en bas (ancré sur l'axe), autorisé au-dessus, séparés par 2 px.
      let base = y(0);
      const segs = [["b-ko", d.denied], ["b-tor", d.tor], ["b-ok", d.allowed]].filter(([, v]) => v > 0);
      segs.forEach(([cls, v], k) => {
        const h = Math.max(1.5, y(0) - y(v)) - (k > 0 ? 2 : 0);
        const top = base - h - (k > 0 ? 2 : 0);
        const isTop = k === segs.length - 1;
        const path = isTop && h > r
          ? `M${x},${base - (k > 0 ? 2 : 0)} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${base - (k > 0 ? 2 : 0)} Z`
          : `M${x},${base - (k > 0 ? 2 : 0)} V${top} H${x + bw} V${base - (k > 0 ? 2 : 0)} Z`;
        g.appendChild(sv("path", { d: path, class: cls }));
        base = top;
      });
      const hit = sv("rect", { x: padL + i * step, y: padT, width: step, height: ih, class: "hit" });
      hit.addEventListener("mouseenter", () => {
        tip.replaceChildren();
        const end = d.t + s.bucket_seconds;
        tip.appendChild(el("div", "t-h", `${lab(d.t)} → ${lab(end)}`));
        const rows = [["Autorisées", "ok", d.allowed]];
        if (torSeen) rows.push(["Via Tor", "tor", d.tor]);
        rows.push(["Refusées", "ko", d.denied]);
        for (const [lbl, cls, v] of rows) {
          const row = el("div", "t-r"); const l = el("span"); l.appendChild(el("i", "sw " + cls)); l.appendChild(document.createTextNode(lbl));
          row.appendChild(l); row.appendChild(el("b", null, nf.format(v))); tip.appendChild(row);
        }
        tip.hidden = false;
        const bx = box.getBoundingClientRect(), hx = hit.getBoundingClientRect();
        let left = hx.left - bx.left + hx.width / 2 + 10;
        if (left + 170 > bx.width) left = hx.left - bx.left - 170;
        tip.style.left = left + "px"; tip.style.top = "8px";
      });
      hit.addEventListener("mouseleave", () => (tip.hidden = true));
      g.appendChild(hit);
      root.appendChild(g);
    });
    root.appendChild(sv("line", { x1: padL, x2: W, y1: y(0), y2: y(0), class: "baseline" }));
    box.prepend(root);
  }
  let rsz; window.addEventListener("resize", () => { clearTimeout(rsz); rsz = setTimeout(() => renderChart(lastStats), 120); });

  /* ---------- Profils ---------- */
  async function loadProfiles() {
    const { profiles, unmatched_24h } = await api("/api/profiles");
    const sel = $("#profile"); const cur = sel.value;
    sel.replaceChildren(new Option("Tous les profils", ""));
    for (const p of profiles) sel.appendChild(new Option(p.name, p.name));
    sel.appendChild(new Option("(aucun profil)", "(aucun)"));
    sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "";

    const box = $("#profiles"); box.replaceChildren();
    for (const p of profiles) {
      const d = el("details"); const s = el("summary");
      const nm = el("span", "p-name", p.name + " ");
      if (p.fallback === "tor") { nm.appendChild(el("span", "tag tor", "hors whitelist → Tor")); markTor(true); }
      s.appendChild(nm);
      s.appendChild(el("span", "p-stats", `${nf.format(p.requests_24h)} req · ${p.fallback === "tor" ? nf.format(p.tor_24h) + " Tor · " : ""}${nf.format(p.denied_24h)} refus`));
      s.appendChild(el("span", "p-nets", p.networks.length ? p.networks.join(", ") : "tous les clients (mode legacy)"));
      d.appendChild(s);
      const body = el("div", "p-body");
      const list = (title, items) => {
        body.appendChild(el("h3", null, `${title} (${items.length})`));
        const ul = el("ul"); for (const it of items) ul.appendChild(el("li", null, it)); body.appendChild(ul);
      };
      list("Domaines", p.domains);
      if (p.ips.length) list("IP", p.ips);
      d.appendChild(body); box.appendChild(d);
    }
    if (unmatched_24h) box.appendChild(el("p", "p-warn", `${nf.format(unmatched_24h)} requête(s) sans profil sur 24 h (client hors réseaux déclarés).`));
  }

  /* ---------- Boucle ---------- */
  function showErr(e) { console.error(e); toast("Erreur : " + e.message); }
  async function reload() {
    try { await Promise.all([loadRows(true), api("/api/stats", filters()).then(renderStats)]); }
    catch (e) { showErr(e); }
  }
  let ticking = false, n = 0;
  async function tick() {
    if (!state.live || ticking || document.hidden) return;
    ticking = true;
    try {
      await loadNew();
      await api("/api/stats", filters()).then(renderStats);
      if (++n % 10 === 0) await loadProfiles();
    } catch (e) { console.error(e); }
    ticking = false;
  }
  setInterval(tick, 3000);
  document.addEventListener("visibilitychange", tick);

  loadProfiles().catch(showErr);
  reload();
});
