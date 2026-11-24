"use strict";
/* Delhi bus crowding planner - front end (no build step). Data comes from /api/*. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // only used with static, trusted markup
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Request failed (${r.status})`);
  return r.json();
}
const fmt = (n, d = 0) => (n == null ? "–" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const debounce = (fn, ms = 180) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

/* ---------- crowding vocabulary ---------- */
const TIER = {
  "Seats Available": { c: "t0", short: "Seats available", color: "--seats", idx: 0 },
  "Standing Room":   { c: "t1", short: "Standing room",   color: "--standing", idx: 1 },
  "Crowded":         { c: "t2", short: "Crowded",         color: "--crowded", idx: 2 },
  "No estimate":     { c: "tn", short: "No estimate",     color: "--none", idx: -1 },
};
const TIER_BY_IDX = ["Seats Available", "Standing Room", "Crowded"];
const ICON = {
  t0: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5h5a2 2 0 0 1 2 2V9H6.5a2.5 2.5 0 0 1-2.5-2.5z" fill="currentColor"/><path d="M3 11h10M5 11v3M11 11v3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/></svg>',
  t1: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="3.2" r="1.8" fill="currentColor"/><path d="M8 5.6v4.4M5.4 7.4 8 6.4l2.6 1M8 10l-1.8 4M8 10l1.8 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" fill="none"/></svg>',
  t2: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="5" cy="4" r="1.6" fill="currentColor"/><circle cx="11" cy="4" r="1.6" fill="currentColor"/><path d="M2 13v-3.2a3 3 0 0 1 6 0V13M8 13v-3.2a3 3 0 0 1 6 0V13" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>',
  tn: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M6.3 6.2a1.8 1.8 0 1 1 2.6 1.6c-.6.3-.9.7-.9 1.3M8 11.4v.1" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>',
};
const pill = tier => { const t = TIER[tier] || TIER["No estimate"]; return h("span", { class: "pill " + t.c, html: ICON[t.c] + `<span>${t.short}</span>` }); };

/* ---------- tabs ---------- */
const loaded = {};
function showTab(name) {
  $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  $$(".page").forEach(p => (p.hidden = p.id !== "tab-" + name));
  history.replaceState(null, "", "#" + name);
  if (!loaded[name]) { loaded[name] = true; ({ plan: initPlan, routes: initRoutes, ops: initOps, evidence: initEvidence })[name](); }
  setTimeout(() => { mapPlan && mapPlan.resize(); mapRoute && mapRoute.resize(); }, 50);
}
$$(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

function seg(el, onChange) {
  el.addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", el).forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    onChange && onChange(b.dataset.v);
  });
  return () => $("button[aria-pressed=true]", el).dataset.v;
}

/* ---------- map ---------- */
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
function makeMap(id) {
  const style = `https://basemaps.cartocdn.com/gl/${dark() ? "dark-matter" : "positron"}-gl-style/style.json`;
  let m;
  try { m = new maplibregl.Map({ container: id, style, center: [77.2, 28.62], zoom: 10.2, attributionControl: { compact: true } }); }
  catch (e) { m = new maplibregl.Map({ container: id, style: { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": css("--none-bg") } }] }, center: [77.2, 28.62], zoom: 10 }); }
  m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  m.on("error", () => {});
  return m;
}
function lineLayers(m, srcId) {
  if (m.getSource(srcId)) return;
  m.addSource(srcId, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  m.addLayer({ id: srcId + "-casing", type: "line", source: srcId, filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": "#ffffff", "line-width": 9, "line-opacity": .85 }, layout: { "line-cap": "round", "line-join": "round" } });
  const base = { "line-color": ["get", "color"], "line-width": 6 }, lay = { "line-cap": "round", "line-join": "round" };
  m.addLayer({ id: srcId + "-line", type: "line", source: srcId, filter: ["all", ["==", ["geometry-type"], "LineString"], ["==", ["get", "unknown"], 0]], paint: base, layout: lay });
  m.addLayer({ id: srcId + "-dash", type: "line", source: srcId, filter: ["all", ["==", ["geometry-type"], "LineString"], ["==", ["get", "unknown"], 1]], paint: { ...base, "line-dasharray": [1, 1.4] }, layout: { "line-cap": "butt", "line-join": "round" } });
  m.addLayer({ id: srcId + "-pts", type: "circle", source: srcId, filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 7, "circle-color": "#1f2a44", "circle-stroke-color": "#fff", "circle-stroke-width": 3 } });
}
function setLines(m, srcId, features) {
  const go = () => { lineLayers(m, srcId); m.getSource(srcId).setData({ type: "FeatureCollection", features });
    const pts = features.flatMap(f => f.geometry.type === "LineString" ? f.geometry.coordinates : [f.geometry.coordinates]);
    if (pts.length) { const b = pts.reduce((bb, p) => bb.extend(p), new maplibregl.LngLatBounds(pts[0], pts[0])); m.fitBounds(b, { padding: 50, maxZoom: 14, duration: 500 }); } };
  m.loaded() ? go() : m.once("load", go);
}
const tierHex = t => css((TIER[t] || TIER["No estimate"]).color);
function legend(el) { el.replaceChildren(...["Seats Available", "Standing Room", "Crowded", "No estimate"].map(t => h("span", {}, h("i", { style: `background:${tierHex(t)}` }), TIER[t].short))); }

/* ---------- autocomplete ---------- */
function autocomplete(input, list, fetcher, onPick) {
  let items = [], idx = -1;
  const close = () => { list.hidden = true; idx = -1; };
  const draw = () => { list.replaceChildren(...items.map((it, i) => h("li", { role: "option", "aria-selected": String(i === idx), onmousedown: e => { e.preventDefault(); pick(i); } }, it.label)));
    list.hidden = !items.length; };
  const pick = i => { onPick(items[i]); close(); };
  const run = debounce(async () => { const q = input.value.trim(); if (q.length < 2) return close(); try { items = await fetcher(q); idx = -1; draw(); } catch { close(); } });
  input.addEventListener("input", run);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", e => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { idx = Math.min(items.length - 1, idx + 1); draw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = Math.max(0, idx - 1); draw(); e.preventDefault(); }
    else if (e.key === "Enter" && idx >= 0) { pick(idx); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
}

/* =====================================================================
   PLAN
   ===================================================================== */
let mapPlan, planDay = () => "weekday", planMode = () => "balanced", selected = 0, lastPlan = null;
const MODE_HINT = { fastest: "shortest trip, crowding shown", balanced: "trade a few minutes for a calmer bus", comfortable: "strongly prefer a calmer bus" };

function initPlan() {
  mapPlan = makeMap("map-plan"); legend($("#legend-plan"));
  planDay = seg($("#day"));
  planMode = seg($("#mode"), m => { $("#mode-hint").textContent = MODE_HINT[m]; });
  $("#mode-hint").textContent = MODE_HINT.balanced;
  for (const [id, sid] of [["from", "from-s"], ["to", "to-s"]])
    autocomplete($("#" + id), $("#" + sid), async q => (await api("/api/places?q=" + encodeURIComponent(q))).map(p => ({ label: p.name, value: p.name })), it => { $("#" + id).value = it.value; });
  $("#swap").addEventListener("click", () => { const a = $("#from"), b = $("#to"); [a.value, b.value] = [b.value, a.value]; });
  $("#plan-form").addEventListener("submit", e => { e.preventDefault(); runPlan(); });
  $("#preset").addEventListener("click", () => { $("#from").value = "Anand Vihar"; $("#to").value = "Connaught Place"; $("#time").value = "18:00";
    $$("#day button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === "weekday"))); runPlan(); });
  const q = new URLSearchParams(location.search);
  if (q.get("from") && q.get("to")) { $("#from").value = q.get("from"); $("#to").value = q.get("to"); $("#time").value = q.get("time") || "18:00"; runPlan(); }
  else $("#plan-out").append(h("div", { class: "empty" }, "Pick where you are going and when. You will see each bus option with how crowded it usually is, so you can choose speed or comfort."));
}

async function runPlan() {
  const from = $("#from").value.trim(), to = $("#to").value.trim(), time = $("#time").value || "18:00";
  const out = $("#plan-out");
  if (!from || !to) { out.replaceChildren(h("div", { class: "errbox" }, "Enter both a starting point and a destination.")); return; }
  $("#go").disabled = true; $("#go").textContent = "Searching…";
  out.replaceChildren(h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
  try {
    const p = await api(`/api/plan?origin=${encodeURIComponent(from)}&dest=${encodeURIComponent(to)}&time=${time}&day=${planDay()}&mode=${planMode()}`);
    lastPlan = p; selected = 0; renderPlan(p);
  } catch (e) { out.replaceChildren(h("div", { class: "errbox" }, "Could not plan that trip: " + e.message)); }
  finally { $("#go").disabled = false; $("#go").textContent = "Find buses"; }
}

function ribbon(opt) {
  const parts = opt.legs.flatMap(l => l.stretches);
  const total = parts.reduce((s, x) => s + Math.max(.5, x.dur || 1), 0) || 1;
  return h("div", { class: "ribbon", role: "img", "aria-label": "Crowding along the trip, start to end" },
    parts.map(s => h("i", { class: "r" + (TIER[s.tier] || TIER["No estimate"]).c.slice(1), style: `flex:${Math.max(.5, s.dur || 1) / total}`, title: `${s.from_stop} → ${s.to_stop}: ${TIER[s.tier]?.short}` })));
}
function busiest(opt) {
  let best = null;
  for (const l of opt.legs) for (const s of l.stretches) if (s.load != null && (!best || s.load > best.load)) best = s;
  return best;
}
function renderPlan(p) {
  const out = $("#plan-out"); out.replaceChildren();
  if (!p.options.length) {
    out.append(h("div", { class: "empty" }, `No bus found from "${p.origin}" to "${p.dest}" on a ${p.day.replace("_", " / ")}. Try a nearby, larger stop or area name.`)); drawPlanMap(null); return;
  }
  p.options.forEach((o, i) => {
    const t = TIER[o.overall_tier] || TIER["No estimate"], bt = TIER[o.board_tier] || TIER["No estimate"], bz = busiest(o);
    const card = h("article", { class: `opt ${t.c}`, tabindex: "0", role: "button", "aria-pressed": String(i === selected), "aria-label": `${o.option_label}, ${o.total_minutes} minutes, ${t.short}` },
      h("div", { class: "opt-top" }, h("span", { class: "opt-label" }, o.option_label), pill(o.overall_tier)),
      h("div", { class: "times" }, `${o.departure_time} → ${o.arrival_time}`, h("small", {}, `${Math.round(o.total_minutes)} min${o.wait_minutes >= 1 ? ` (wait ${Math.round(o.wait_minutes)})` : ""}`)),
      ribbon(o),
      h("div", { class: "meta" },
        h("span", {}, "At your boarding stop: ", h("b", {}, bt.short)),
        bz && o.trust_level !== "Low" ? h("span", {}, `Busiest stretch ≈ ${Math.round(bz.load)} people${bz.p90 ? ` (usually up to ${Math.round(bz.p90)})` : ""}`)
          : h("span", {}, "Too few tickets for a reliable headcount"),
        o.transfers ? h("span", {}, `${o.transfers} change`) : h("span", {}, "Direct")),
      h("div", { class: "legs" }, o.legs.map(l => h("div", {}, h("b", {}, "Bus " + l.route_key.replace(/(UP|DOWN)$/i, "")), ` · ${l.board_stop} → ${l.alight_stop} · ${Math.round(l.minutes)} min`))),
      h("div", { class: "note" }, o.summary_note),
      h("div", { class: "meta" }, h("span", { class: "trust " + o.trust_level }, "Evidence: " + o.trust_level), h("span", {}, o.confidence_text)));
    const sel = () => { selected = i; $$(".opt").forEach((c, j) => c.setAttribute("aria-pressed", String(j === i))); drawPlanMap(o); };
    card.addEventListener("click", sel); card.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sel(); } });
    out.append(card);
  });
  if (p.timeline) out.append(renderTimeline(p));
  out.append(h("p", { class: "sub" }, `Crowding is the typical level for this day type; the number of people is a model estimate scaled from tickets (about 1 in 40 riders appear in the data). ${p.latency_ms} ms.`));
  drawPlanMap(p.options[selected]);
}
function renderTimeline(p) {
  const wrap = h("div", { class: "card" }, h("h3", {}, "Best time to leave"), h("p", { class: "sub" }, "How crowded the bus you would catch is, if you left earlier or later. Tap a time to search it."));
  const row = h("div", { class: "timeline" });
  p.timeline.forEach(t => {
    const tt = TIER[t.tier] || TIER["No estimate"];
    const el = h("button", { class: `tl ${tt.c} ${t.offset === 0 ? "now" : ""} ${t.trust === "Low" ? "low" : ""}`, style: "background:none;border:0;padding:0",
      title: t.tier ? `${t.time}: ${t.route ? "bus " + t.route.replace(/(UP|DOWN)$/i, "") : ""} · ${tt.short} · ${Math.round(t.total_minutes)} min${t.trust === "Low" ? " · thin evidence" : ""}` : `${t.time}: no direct service found`,
      onclick: () => { $("#time").value = t.time; runPlan(); } }, h("i"), t.time);
    row.append(el);
  });
  wrap.append(row); return wrap;
}
function drawPlanMap(o) {
  if (!mapPlan) return;
  if (!o) return setLines(mapPlan, "journey", []);
  const feats = [];
  for (const l of o.legs) for (const s of l.stretches) if (s.coords && s.coords.length > 1 && s.coords[0][0])
    feats.push({ type: "Feature", properties: { color: tierHex(s.tier), unknown: s.tier === "No estimate" ? 1 : 0 }, geometry: { type: "LineString", coordinates: s.coords.map(c => [c[1], c[0]]) } });
  const first = o.legs[0].stretches[0]?.coords?.[0], last = o.legs.at(-1).stretches.at(-1)?.coords?.at(-1);
  for (const c of [first, last]) if (c) feats.push({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [c[1], c[0]] } });
  setLines(mapPlan, "journey", feats);
}

/* =====================================================================
   ROUTES
   ===================================================================== */
let mapRoute, rDay = () => "weekday", rData = null, rKey = null;
function initRoutes() {
  mapRoute = makeMap("map-route"); legend($("#legend-route"));
  rDay = seg($("#rday"), () => rKey && loadRoute(rKey));
  autocomplete($("#route-q"), $("#route-s"), async q => (await api("/api/routes?q=" + encodeURIComponent(q))).map(r => ({ label: r.route_key, value: r.route_key })), it => { $("#route-q").value = it.value; loadRoute(it.value); });
  $("#rtime").addEventListener("input", () => { updateTimeLabel(); drawRouteMap(); drawHeat(); });
  $("#route-info").hidden = true;
  $("#route-q").value = "73UP"; loadRoute("73UP");
}
const binLabel = b => `${String(Math.floor(b / 2)).padStart(2, "0")}:${b % 2 ? "30" : "00"}`;
function updateTimeLabel() { $("#rtime-v").textContent = binLabel(+$("#rtime").value); }
async function loadRoute(key) {
  rKey = key;
  try { rData = await api(`/api/route/${encodeURIComponent(key)}?day=${rDay()}`); }
  catch (e) { $("#route-info").hidden = false; $("#route-info").replaceChildren(h("div", { class: "errbox" }, e.message)); rData = null; return; }
  const info = $("#route-info"); info.hidden = false;
  info.replaceChildren(h("h3", {}, "Bus " + key), h("p", { class: "sub" }, `${rData.segments[0].from_name} → ${rData.segments.at(-1).to_name}`),
    h("div", { class: "meta" }, h("span", {}, `${rData.segments.length} stretches`), h("span", {}, `Seats ${Math.round(rData.seat_cap)} · crush ${Math.round(rData.max_cap)}`)),
    rData.kappa_capped ? h("p", { class: "note" }, "Heavy ticket use on this route: loads are capped at a physical limit, so read ‘Crowded’ as a floor rather than an exact count.") : null);
  updateTimeLabel(); drawRouteMap(); drawHeat();
}
function cell(seg, bin) { const c = rData.cells[`${seg}:${bin}`]; return c ? { load: c[0], p10: c[1], p90: c[2], tier: c[3], pc: c[4], tickets: c[5], trust: c[6] } : null; }
function drawRouteMap() {
  if (!rData) return; const bin = +$("#rtime").value;
  const feats = rData.segments.map(s => { const c = cell(s.seg_seq, bin);
    return { type: "Feature", properties: { color: c ? tierHex(TIER_BY_IDX[c.tier]) : tierHex("No estimate"), unknown: c ? 0 : 1 }, geometry: { type: "LineString", coordinates: [[s.from_lon, s.from_lat], [s.to_lon, s.to_lat]] } }; });
  setLines(mapRoute, "rroute", feats);
}
function drawHeat() {
  if (!rData) return;
  const cv = $("#heat"), n = rData.segments.length, cw = 15, lm = 34, tm = 18, rh = Math.max(3, Math.min(14, Math.floor(420 / n))), dpr = devicePixelRatio || 1;
  const W = lm + 48 * cw, H = tm + n * rh;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const cols = [css("--seats"), css("--standing"), css("--crowded")], none = css("--none-bg"), ink = css("--ink-2");
  g.font = "11px system-ui"; g.fillStyle = ink; g.textAlign = "left";
  for (let hr = 0; hr < 24; hr += 3) g.fillText(String(hr).padStart(2, "0"), lm + hr * 2 * cw, 11);
  rData.segments.forEach((s, i) => { for (let b = 0; b < 48; b++) { const c = cell(s.seg_seq, b);
    g.globalAlpha = c ? [.4, .75, 1][c.trust] : 1; g.fillStyle = c ? cols[c.tier] : none; g.fillRect(lm + b * cw, tm + i * rh, cw - 1, rh - (rh > 5 ? 1 : 0)); } });
  g.globalAlpha = 1; g.strokeStyle = css("--ink"); g.lineWidth = 2; const sb = +$("#rtime").value; g.strokeRect(lm + sb * cw - .5, tm - 1, cw, n * rh + 2);
  g.fillStyle = ink; g.textAlign = "right"; g.fillText("start", lm - 4, tm + 10); g.fillText("end", lm - 4, tm + n * rh - 2);
  cv.onmousemove = cv.onclick = e => { const r = cv.getBoundingClientRect(), x = e.clientX - r.left - lm, y = e.clientY - r.top - tm, b = Math.floor(x / cw), i = Math.floor(y / rh), tip = $("#heat-tip");
    if (b < 0 || b > 47 || i < 0 || i >= n) { tip.hidden = true; return; }
    const s = rData.segments[i], c = cell(s.seg_seq, b); tip.hidden = false; tip.style.left = Math.min(innerWidth - 270, e.clientX + 12) + "px"; tip.style.top = e.clientY + 14 + "px";
    tip.textContent = `${s.from_name} → ${s.to_name}\n${binLabel(b)}–${binLabel(b + 1 > 47 ? 0 : b + 1)}: ` + (c ? `${TIER[TIER_BY_IDX[c.tier]].short}, ≈${Math.round(c.load)} people (usually ${Math.round(c.p10)}–${Math.round(c.p90)}), ${c.tickets} tickets, evidence ${["Low", "Medium", "High"][c.trust]}` : "no scheduled bus"); tip.style.whiteSpace = "pre-line"; };
  cv.onmouseleave = () => ($("#heat-tip").hidden = true);
}

/* =====================================================================
   OPERATORS
   ===================================================================== */
let oDay = () => "weekday", oSel = null;
function initOps() { oDay = seg($("#oday"), loadOps);
  $("#ops-caution").textContent = "Loads are model estimates. Where tickets imply more than 1.5× crush capacity on a route, the estimate is capped, so treat those rows as ‘severely overloaded’ rather than an exact headcount. Relief assumes demand stays the same when a bus is added.";
  loadOps(); }
async function loadOps() {
  const day = oDay();
  const [top, heat, under] = await Promise.all([api(`/api/operator/top?day=${day}&limit=20`), api(`/api/operator/heat?day=${day}`), api(`/api/operator/underused?day=${day}`)]);
  const head = ["Route", "Hour", "Busiest stretch", "Peak", "Buses/h", "Relief", "After +1 bus", "Evidence"];
  const t = h("table", {}, h("thead", {}, h("tr", {}, head.map((x, i) => h("th", { class: i >= 3 && i <= 5 ? "num" : "" }, x)))),
    h("tbody", {}, top.map(r => { const tr = h("tr", { class: "pick", tabindex: "0", onclick: () => pickOps(r, tr) },
      h("td", {}, h("b", {}, r.route_key)), h("td", {}, `${String(r.hour).padStart(2, "0")}:00`), h("td", {}, `${r.peak_from_name} → ${r.peak_to_name}`),
      h("td", { class: "num" }, fmt(r.peak_load)), h("td", { class: "num" }, fmt(r.trips_hr, 0)), h("td", { class: "num" }, fmt(r.relief_rider_min)),
      h("td", {}, pill(r.tier_plus1)), h("td", {}, h("span", { class: "trust " + r.trust_level }, r.trust_level)));
      tr.addEventListener("keydown", e => e.key === "Enter" && tr.click()); return tr; })));
  $("#ops-top").replaceWith(Object.assign(t, { id: "ops-top" }));
  if (top[0]) pickOps(top[0], $("#ops-top tbody tr"));
  drawOpsHeat(heat);
  const u = h("table", { id: "ops-under" }, h("thead", {}, h("tr", {}, ["Route", "Hour", "Buses/h", "Busiest-stretch load", "Seats", "Evidence"].map(x => h("th", {}, x)))),
    h("tbody", {}, under.map(r => h("tr", {}, h("td", {}, h("b", {}, r.route_key)), h("td", {}, `${String(r.hour).padStart(2, "0")}:00`), h("td", { class: "num" }, fmt(r.trips_hr)), h("td", { class: "num" }, fmt(r.peak_load)), h("td", { class: "num" }, fmt(r.seat_cap)), h("td", {}, r.trust_level)))));
  $("#ops-under").replaceWith(u);
}
async function pickOps(r, tr) {
  $$("#ops-top tr.sel").forEach(x => x.classList.remove("sel")); tr && tr.classList.add("sel"); oSel = r;
  const box = $("#whatif"), render = async n => {
    const w = await api(`/api/operator/whatif?route_key=${encodeURIComponent(r.route_key)}&day=${oDay()}&hour=${r.hour}&extra_buses=${n}`);
    box.replaceChildren(h("h3", {}, `Bus ${w.route_key}, ${String(w.hour).padStart(2, "0")}:00`), h("p", { class: "sub" }, `Busiest stretch: ${w.peak_from} → ${w.peak_to}`),
      h("div", { class: "field" }, h("label", {}, `Extra buses per hour: ${n} (now ${fmt(w.trips_hr)})`), h("input", { type: "range", min: 1, max: 5, value: n, oninput: e => render(+e.target.value) })),
      h("div", { class: "meta" }, pill(w.tier_now), h("span", {}, "→"), pill(w.tier_after)),
      h("p", {}, `Peak load ≈ ${fmt(w.peak_load_now)} → ≈ ${fmt(w.peak_load_after)} people per bus.`), h("p", { class: "sub" }, w.assumption));
  };
  render(1);
}
function drawOpsHeat(d) {
  const cw = 26, rh = 20, lm = 90, tm = 18, W = lm + 24 * cw, H = tm + d.routes.length * rh, ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", W); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Overload by route and hour");
  const add = (tag, a, txt) => { const e = document.createElementNS(ns, tag); for (const k in a) e.setAttribute(k, a[k]); if (txt) e.textContent = txt; svg.append(e); return e; };
  const max = Math.max(1, ...d.cells.map(c => c[2])), col = css("--crowded"), ink = css("--ink-2");
  for (let hr = 0; hr < 24; hr += 2) add("text", { x: lm + hr * cw + cw / 2, y: 11, "text-anchor": "middle", "font-size": 10, fill: ink }, String(hr).padStart(2, "0"));
  d.routes.forEach((r, i) => add("text", { x: lm - 6, y: tm + i * rh + 14, "text-anchor": "end", "font-size": 11, fill: css("--ink") }, r));
  add("rect", { x: lm, y: tm, width: 24 * cw, height: d.routes.length * rh, fill: css("--none-bg"), rx: 4 });
  d.cells.forEach(([r, hr, ov, pk, tier]) => { const i = d.routes.indexOf(r); if (i < 0 || ov <= 0) return;
    const e = add("rect", { x: lm + hr * cw + 1, y: tm + i * rh + 1, width: cw - 2, height: rh - 2, rx: 3, fill: col, "fill-opacity": (.15 + .85 * Math.sqrt(ov / max)).toFixed(2) });
    e.append(Object.assign(document.createElementNS(ns, "title"), { textContent: `${r} at ${String(hr).padStart(2, "0")}:00: peak ≈ ${Math.round(pk)} people, ${fmt(ov)} overload rider-min` })); });
  $("#ops-heat").replaceChildren(svg);
}

/* =====================================================================
   EVIDENCE
   ===================================================================== */
async function initEvidence() {
  const root = $("#evidence"); root.replaceChildren(h("div", { class: "skeleton" }));
  let ev; try { ev = await api("/api/evidence"); } catch (e) { root.replaceChildren(h("div", { class: "errbox" }, e.message)); return; }
  const E = ev.eval, M = ev.model, V = ev.validation, MT = ev.match, root2 = h("div", { class: "ev" });
  const get = (model, bucket, k) => E.comparison_weekday.find(r => r.model === model && r.bucket === bucket)?.[k];
  const models = [...new Set(E.comparison_weekday.map(r => r.model))];
  const label = m => m.startsWith("v8") ? "This model (v8)" : m.startsWith("v7") ? "Previous model (v7)" : m;
  const bestM = models.find(m => m.startsWith("v8"));
  root2.append(h("div", { class: "kpis" },
    kpi(fmt(2559899), "tickets used (1–31 Oct 2026)"), kpi((MT?.match_performance?.matched_pax_pct ?? 93.6).toFixed(1) + "%", "of passengers matched to a bus stop sequence"),
    kpi(fmt(M?.routes_total), "bus routes covered"), kpi(fmt(get(bestM, "all", "n")), "held-out route-stretch-times per test week")));
  root2.append(h("h2", {}, "Does it predict a week it has never seen?"),
    h("p", {}, "We hide one whole week, fit the model on the other weeks, then compare its predicted riders per bus with what the hidden week actually recorded. This is repeated for four weeks. Lower deviance and higher rank correlation are better."));
  root2.append(h("div", { class: "card" }, h("h3", {}, "Rank agreement with the held-out week (Spearman), cells with ≥30 tickets"), models.map(m => barRow(label(m), get(m, "30-99", "spearman"), 1, m === bestM)),
    h("h3", { style: "margin-top:14px" }, "Prediction error (Poisson deviance, lower is better), all cells"), models.map(m => barRow(label(m), get(m, "all", "deviance"), Math.max(...models.map(x => get(x, "all", "deviance"))), m === bestM, 3, true))));
  const v7m = models.find(m => m.startsWith("v7"));
  root2.append(h("p", { class: "sub" }, `Honest reading: against the previous model the gain in point accuracy is modest (deviance ${get(v7m, "all", "deviance").toFixed(3)} → ${get(bestM, "all", "deviance").toFixed(3)}, rank agreement on busy cells ${get(v7m, "30-99", "spearman").toFixed(2)} → ${get(bestM, "30-99", "spearman").toFixed(2)}) and the three-level tier agrees with the held-out week about as often (${pc(get(v7m, "all", "tier_agree"))} vs ${pc(get(bestM, "all", "tier_agree"))}). The larger gains are that busy cells are no longer over-smoothed and that the stated ranges are now trustworthy (next section). The raw cell rate has the same rank agreement but is ${Math.round(100 * (1 - get(bestM, "all", "deviance") / get(models.find(m => m.startsWith("B1")), "all", "deviance")))}% worse in deviance because it cannot handle thin cells. Route-hour averages (B0) lose the stretch detail and are clearly worse.`));
  const cov = E.coverage;
  root2.append(h("h2", {}, "Are the stated ranges trustworthy?"), h("p", {}, "A ‘usual range’ is only useful if about 80% of real weeks fall inside it. The previous model’s ranges were far too narrow on busy routes; these are calibrated per amount of evidence."),
    table(["Tickets behind the estimate", "Previous model: weeks inside range", "This model: weeks inside range"], Object.keys(cov.v8_selected_by_bucket).map(b => [b, pc(cov.v7_phi1_by_bucket[b]), pc(cov.v8_selected_by_bucket[b].coverage)]), "Target ≈ 80%. Weeks inside the 10–90% range."));
  const rel = E.split_half_reliability;
  root2.append(h("h2", {}, "How much evidence is enough?"), h("p", {}, "Splitting the weeks into two halves and comparing the two independent estimates shows how repeatable an estimate is. This is why the app labels evidence High (30+ tickets), Medium (10–29) or Low."),
    table(["Tickets", "Cells", "Agreement between halves (Spearman)", "Full-data reliability"], Object.entries(rel).filter(([k]) => k !== "all").map(([k, v]) => [k, fmt(v.cells), v.split_half_spearman.toFixed(2), v.spearman_brown_full.toFixed(2)])));
  const abl = E.ablation_weekday.filter(r => r.bucket === "all");
  root2.append(h("h2", {}, "What each design choice contributes"), table(["Variant", "Deviance (all)", "Spearman (all)"], abl.map(r => [label(r.model).replace(/^v8 a=.*smooth=1$/, "This model + temporal smoothing"), r.deviance.toFixed(3), r.spearman.toFixed(3)])),
    h("p", { class: "sub" }, "Temporal smoothing and a single city-wide prior both made held-out predictions worse, so they are not used."));
  if (V?.checks) { root2.append(h("h2", {}, "Checks against known patterns"));
    V.checks.forEach(c => root2.append(h("div", { class: "card", style: "margin-bottom:10px" }, h("h3", {}, `${c.id} · ${c.title}`, " ", h("span", { class: "chip" }, c.status)), h("p", { class: "sub" }, c.summary),
      c.metrics ? table(["Measure", "Value"], Object.entries(c.metrics).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v)])) : null))); }
  const m = M || {};
  root2.append(h("h2", {}, "Where the estimates are least reliable"), h("ul", {},
    h("li", {}, `Ticket sample: tickets are roughly 1 in 40 riders. ${pc(m.weekday_trust_share?.Low)} of weekday route-stretch-times have Low evidence and show wide ranges.`),
    h("li", {}, `Absolute headcount rests on a scale factor (κ = ${m.kappa_global}) from published city ridership. On ${m.routes_kappa_capped} routes (${pc(m.share_tagged_pax_on_capped_routes)} of ticketed passengers) tickets would imply loads far above physical capacity, so we cap them at ${m.capacity_ceiling_factor}× crush capacity. That cap lowers implied city ridership to ${pc(m.implied_daily_weekday_ridership_vs_published)} of the published figure, which shows ticket capture is not uniform across routes. Rankings are reliable; exact headcounts on those routes are not.`),
    h("li", {}, "Weekends: only about four to five days per type, and the schedule file lists the same trips every day, so weekend frequency is a weekday assumption."),
    h("li", {}, "Bus size: every route uses a 12 m default (38 seats, 75 crush). The 9 m mapping needs the operator’s fleet list."),
    h("li", {}, "Schedules, not live operation: buses that never ran or ran bunched are not reflected. Late night and low-frequency feeder routes have very few tickets."),
    h("li", {}, "October only: festivals, monsoon and other seasons are not covered; 2 and 20 October were treated as holidays.")));
  root2.append(h("h2", {}, "Method in brief"), h("p", {}, "Each ticket becomes passenger-presence on every stretch between where the rider boarded and where they likely got off (with a 15/70/15% allowance for getting off a stop early or late). Presence is summed by route, stretch, day type and half hour, divided by the buses scheduled there, and smoothed toward similar route-hours with a Gamma-Poisson model. A calibrated week-to-week range comes from a negative-binomial layer. Counts are scaled to riders with a city-wide factor, capped by physical capacity, and mapped to seats / standing / crowded."));
  root.replaceChildren(root2);
}
const pc = v => (v == null ? "–" : (v * 100).toFixed(v < .1 ? 1 : 0) + "%");
const kpi = (b, s) => h("div", { class: "kpi" }, h("b", {}, b), h("span", {}, s));
function barRow(name, v, max, win, d = 2, rev = false) { return h("div", { class: "bar" + (win ? " win" : "") }, h("span", {}, name), h("div", { class: "t" }, h("i", { style: `width:${Math.max(2, 100 * v / max)}%` })), h("b", {}, v == null ? "–" : v.toFixed(d))); }
function table(head, rows, cap) { return h("div", { class: "card scroll" }, h("table", {}, h("thead", {}, h("tr", {}, head.map(x => h("th", {}, x)))), h("tbody", {}, rows.map(r => h("tr", {}, r.map(c => h("td", {}, c)))))), cap ? h("p", { class: "sub", style: "margin:8px 0 0" }, cap) : null); }

/* ---------- boot ---------- */
const startTab = (location.hash || "#plan").slice(1); showTab(["plan","routes","ops","evidence"].includes(startTab) ? startTab : "plan");

addEventListener("hashchange", () => { const t = location.hash.slice(1); if (["plan","routes","ops","evidence"].includes(t)) showTab(t); });
