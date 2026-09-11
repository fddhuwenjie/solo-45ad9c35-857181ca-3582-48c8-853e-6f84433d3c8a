/* TLC 图像定量前端:原生 JS + Canvas,无框架依赖。 */
(() => {
"use strict";

// ---------------- 状态 ----------------
const S = {
  aid: null,
  analysis: null,
  geometry: null,      // {version, params, derived, previous_params}
  lanes: [],           // 当前几何版本的泳道(含 peaks)
  versions: [],
  mode: "geometry",    // geometry | lanes | peaks | results
  view: "original",    // original | corrected | background | signal
  geomStep: 0,
  draft: null,         // 几何草稿 {corners:[], baseline, front, scale:{p1,p2,mm}}
  imgCache: {}, img: null, imgW: 0, imgH: 0,
  selLane: null, selPeak: null,
  profile: null, profileMeta: null,
  proposed: null,      // 自动检测建议窗口
  results: null, flags: [],
  addLane: false, addPeak: false, split: false,
  drag: null,          // 拖拽上下文
};

const $ = (id) => document.getElementById(id);
const cv = $("cv"), ctx = cv.getContext("2d");
const pcv = $("profileCv"), pctx = pcv.getContext("2d");

// ---------------- API ----------------
async function api(path, opts = {}) {
  if (opts.json) {
    opts.method = opts.method || "POST";
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).description || msg; } catch (e) { /* ignore */ }
    alert("操作失败: " + msg);
    throw new Error(msg);
  }
  return r.json();
}

// ---------------- 载入与水合 ----------------
async function refreshList() {
  const list = await api("/api/analyses");
  const sel = $("analysisSelect");
  sel.innerHTML = '<option value="">打开分析…</option>' +
    list.map(a => `<option value="${a.id}">#${a.id} ${a.name}</option>`).join("");
  if (S.aid) sel.value = S.aid;
}

async function openAnalysis(aid) {
  const st = await api(`/api/analyses/${aid}`);
  S.aid = aid;
  S.analysis = st.analysis;
  S.geometry = st.geometry;
  S.lanes = st.lanes;
  S.versions = st.versions;
  S.selLane = null; S.selPeak = null; S.profile = null; S.proposed = null;
  S.results = null; S.flags = [];
  S.imgCache = {};
  S.draft = S.geometry ? JSON.parse(JSON.stringify(S.geometry.params))
                       : { corners: [], baseline: null, front: null, scale: { p1: null, p2: null, mm: 100 } };
  S.geomStep = S.geometry ? 0 : 0;
  setMode(S.geometry ? "lanes" : "geometry");
  setView(S.geometry ? "signal" : "original");
  $("emptyState").hidden = true;
  $("workspace").hidden = false;
  $("exportGroup").hidden = false;
  updateExports();
  updateGeomBadge();
  renderGeomPanel();
  if (S.geometry) refreshResults();
  refreshList();
}

function updateExports() {
  if (!S.aid) return;
  $("expPng").href = `/api/analyses/${S.aid}/export/annotated.png`;
  $("expCsv").href = `/api/analyses/${S.aid}/export/spots.csv`;
  $("expParams").href = `/api/analyses/${S.aid}/export/params.json`;
  $("expPrint").href = `/api/analyses/${S.aid}/print`;
}

function updateGeomBadge() {
  $("geomBadge").textContent = S.geometry ? `几何 v${S.geometry.version}` : "未标定";
}

// ---------------- 图像加载 ----------------
function imageURL(kind) {
  if (kind === "original") return `/api/analyses/${S.aid}/image`;
  return `/api/analyses/${S.aid}/preview/${kind}`;
}

function loadViewImage() {
  const kind = S.view;
  const apply = (im) => {
    S.img = im; S.imgW = im.naturalWidth; S.imgH = im.naturalHeight;
    fitCanvas(); render();
  };
  if (S.imgCache[kind]) { apply(S.imgCache[kind]); return; }
  const im = new Image();
  im.onload = () => { S.imgCache[kind] = im; apply(im); };
  im.onerror = () => alert("图像加载失败");
  im.src = imageURL(kind) + "?t=" + (S.geometry ? S.geometry.version : 0);
}

function fitCanvas() {
  const w = $("canvasWrap").clientWidth || 800;
  cv.width = w;
  cv.height = Math.round(w * S.imgH / S.imgW);
}

function toImg(e, canvas) {
  const r = canvas.getBoundingClientRect();
  return {
    x: (e.clientX - r.left) / r.width * (canvas === cv ? S.imgW : 1),
    y: (e.clientY - r.top) / r.height * (canvas === cv ? S.imgH : 1),
    px: e.clientX - r.left, py: e.clientY - r.top,
  };
}
const sx = (x) => x / S.imgW * cv.width;   // 图像坐标 -> 画布
const sy = (y) => y / S.imgH * cv.height;

// ---------------- 主画布渲染 ----------------
function render() {
  if (!S.img) return;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.drawImage(S.img, 0, 0, cv.width, cv.height);
  if (S.mode === "geometry") drawGeometryDraft();
  else if (S.view !== "original") {
    drawLanes();
    if (S.mode === "results") drawSpots();
    if (S.mode === "peaks") drawPeakWindows();
  } else {
    drawGeometryOnOriginal();
  }
}

function drawGeometryDraft() {
  const d = S.draft;
  ctx.lineWidth = 2; ctx.font = "13px sans-serif";
  if (d.corners.length) {
    ctx.strokeStyle = "#4f9cf9";
    ctx.beginPath();
    d.corners.forEach((p, i) => i ? ctx.lineTo(sx(p[0]), sy(p[1])) : ctx.moveTo(sx(p[0]), sy(p[1])));
    if (d.corners.length === 4) ctx.closePath();
    ctx.stroke();
    const names = ["左上", "右上", "右下", "左下"];
    d.corners.forEach((p, i) => {
      ctx.fillStyle = "#4f9cf9";
      ctx.beginPath(); ctx.arc(sx(p[0]), sy(p[1]), 5, 0, 7); ctx.fill();
      ctx.fillStyle = "#fff"; ctx.fillText(names[i], sx(p[0]) + 7, sy(p[1]) - 7);
    });
  }
  const mark = (p, color, label) => {
    if (!p) return;
    ctx.strokeStyle = color;
    ctx.beginPath();
    ctx.moveTo(sx(p[0]) - 30, sy(p[1])); ctx.lineTo(sx(p[0]) + 30, sy(p[1]));
    ctx.moveTo(sx(p[0]), sy(p[1]) - 8); ctx.lineTo(sx(p[0]), sy(p[1]) + 8);
    ctx.stroke();
    ctx.fillStyle = color; ctx.fillText(label, sx(p[0]) + 34, sy(p[1]) + 4);
  };
  mark(d.baseline, "#3c3", "基线");
  mark(d.front, "#e5534b", "溶剂前沿");
  if (d.scale.p1 && d.scale.p2) {
    ctx.strokeStyle = "#e6b93d";
    ctx.beginPath();
    ctx.moveTo(sx(d.scale.p1[0]), sy(d.scale.p1[1]));
    ctx.lineTo(sx(d.scale.p2[0]), sy(d.scale.p2[1]));
    ctx.stroke();
    ctx.fillStyle = "#e6b93d";
    ctx.fillText(`标尺 ${d.scale.mm} mm`, sx(d.scale.p2[0]) + 6, sy(d.scale.p2[1]));
  }
}

function drawGeometryOnOriginal() {
  // 结果/泳道/峰模式下对照原图时,叠加已标定几何
  if (!S.geometry) return;
  const p = S.geometry.params;
  ctx.strokeStyle = "rgba(79,156,249,.8)"; ctx.lineWidth = 2;
  ctx.beginPath();
  p.corners.forEach((c, i) => i ? ctx.lineTo(sx(c[0]), sy(c[1])) : ctx.moveTo(sx(c[0]), sy(c[1])));
  ctx.closePath(); ctx.stroke();
}

function laneColor(i) { return ["#4f9cf9", "#9c6ff0", "#3fbf7f", "#e6873d", "#e65d8f"][i % 5]; }

function drawLanes() {
  ctx.font = "12px sans-serif";
  S.lanes.forEach((l, i) => {
    const c = laneColor(i);
    ctx.fillStyle = c + (l.id === S.selLane ? "44" : "22");
    ctx.strokeStyle = c;
    ctx.lineWidth = l.id === S.selLane ? 2.5 : 1.2;
    ctx.fillRect(sx(l.x0), 0, sx(l.x1) - sx(l.x0), cv.height);
    ctx.strokeRect(sx(l.x0), 0, sx(l.x1) - sx(l.x0), cv.height);
    ctx.fillStyle = c;
    ctx.fillText(l.label || `L${l.id}`, sx(l.x0) + 3, 14);
  });
}

function drawPeakWindows() {
  const lane = S.lanes.find(l => l.id === S.selLane);
  if (!lane) return;
  lane.peaks.forEach(p => {
    ctx.fillStyle = p.id === S.selPeak ? "rgba(255,150,0,.35)" : "rgba(255,150,0,.18)";
    ctx.fillRect(sx(lane.x0), sy(p.y0), sx(lane.x1) - sx(lane.x0), sy(p.y1) - sy(p.y0));
  });
}

function drawSpots() {
  if (!S.results) return;
  const laneById = Object.fromEntries(S.lanes.map(l => [l.id, l]));
  const flagged = new Set(S.flags.filter(f => f.peak_id).map(f => f.peak_id));
  ctx.font = "bold 12px sans-serif";
  for (const s of S.results.spots) {
    const lane = laneById[s.lane_id];
    if (!lane) continue;
    const cx = sx((lane.x0 + lane.x1) / 2), cy = sy(s.center_y);
    ctx.strokeStyle = "#d0d"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, 5, 0, 7); ctx.stroke();
    ctx.fillStyle = "#d0d";
    ctx.fillText(`${lane.label || "L"}-${s.spot_no}`, sx(lane.x1) + 4, cy + 4);
    if (flagged.has(s.peak_id)) {
      ctx.fillStyle = "#e6b93d";
      ctx.beginPath();
      const tx = sx(lane.x0) + 4, ty = sy(s.y0) + 4;
      ctx.moveTo(tx, ty + 12); ctx.lineTo(tx + 6, ty); ctx.lineTo(tx + 12, ty + 12);
      ctx.closePath(); ctx.fill();
    }
  }
}

// ---------------- 几何面板与交互 ----------------
const GEOM_HINTS = [
  "第 1 步:依次点选板面四角(左上 → 右上 → 右下 → 左下),可拖动微调",
  "第 2 步:点选基线(点样线)上任意一点",
  "第 3 步:点选溶剂前沿上任意一点",
  "第 4 步:点选标尺两端点,并在右侧输入实际长度(mm)",
];

function renderGeomPanel() {
  document.querySelectorAll("#geomSteps li").forEach(li => {
    const st = +li.dataset.step;
    li.classList.toggle("current", st === S.geomStep);
    li.classList.toggle("done", st < S.geomStep);
  });
  $("scaleRow").hidden = S.geomStep !== 3;
  $("geomPrev").disabled = S.geomStep === 0;
  $("geomNext").hidden = S.geomStep === 3;
  $("geomSubmit").hidden = S.geomStep !== 3;
  $("geomResetNote").hidden = !S.geometry;
  $("hint").textContent = S.mode === "geometry" ? GEOM_HINTS[S.geomStep] : "";
  const d = S.geometry;
  $("geomDerived").textContent = d ? [
    `版本 v${d.version}  校正图 ${d.derived.width}×${d.derived.height} px`,
    `基线 y=${d.derived.baseline_y.toFixed(1)}  前沿 y=${d.derived.front_y.toFixed(1)}`,
    d.derived.px_per_mm ? `比例尺 ${d.derived.px_per_mm.toFixed(2)} px/mm` : "比例尺无效",
  ].join("\n") : "";
}

function geomReady() {
  const d = S.draft;
  return d.corners.length === 4 && d.baseline && d.front &&
         d.scale.p1 && d.scale.p2 && d.scale.mm > 0;
}

async function submitGeometry() {
  if (!geomReady()) { alert("请先完成四角、基线、前沿与标尺点选"); return; }
  if (S.geometry && !confirm("重设几何将使当前泳道/峰与定量结果失效,继续?")) return;
  const d = S.draft;
  const st = await api(`/api/analyses/${S.aid}/geometry`, { json: {
    corners: d.corners, baseline: d.baseline, front: d.front,
    scale: { p1: d.scale.p1, p2: d.scale.p2, mm: d.scale.mm },
  }});
  S.geometry = st.geometry; S.lanes = st.lanes; S.versions = st.versions;
  S.selLane = null; S.selPeak = null; S.profile = null; S.results = null;
  S.imgCache = {};   // 预览随几何版本变化,清缓存
  updateGeomBadge(); renderGeomPanel(); renderLaneList(); renderPeakList(); renderResults();
  setMode("lanes"); setView("signal");
}

// 几何点拖拽的命中检测(图像坐标,12px 屏幕容差)
function hitGeomPoint(im) {
  const d = S.draft, R = 12 / cv.width * S.imgW; // 12px 容差
  const pts = [];
  d.corners.forEach((c, i) => pts.push({ kind: "corner", idx: i, p: c }));
  if (d.baseline) pts.push({ kind: "baseline", p: d.baseline });
  if (d.front) pts.push({ kind: "front", p: d.front });
  if (d.scale.p1) pts.push({ kind: "scale1", p: d.scale.p1 });
  if (d.scale.p2) pts.push({ kind: "scale2", p: d.scale.p2 });
  for (const t of pts) {
    if (Math.hypot(t.p[0] - im.x, t.p[1] - im.y) < R) return t;
  }
  return null;
}

function onGeomClick(im) {
  const d = S.draft;
  if (S.geomStep === 0) {
    if (d.corners.length < 4) d.corners.push([im.x, im.y]);
  } else if (S.geomStep === 1) d.baseline = [im.x, im.y];
  else if (S.geomStep === 2) d.front = [im.x, im.y];
  else if (S.geomStep === 3) {
    if (!d.scale.p1 || (d.scale.p1 && d.scale.p2)) { d.scale.p1 = [im.x, im.y]; d.scale.p2 = null; }
    else d.scale.p2 = [im.x, im.y];
  }
  render();
}

// ---------------- 泳道 ----------------
function renderLaneList() {
  const ul = $("laneList");
  ul.innerHTML = "";
  S.lanes.forEach((l, i) => {
    const li = document.createElement("li");
    li.className = l.id === S.selLane ? "selected" : "";
    li.innerHTML = `<span class="grow">${l.label || "泳道 " + l.id} [${l.x0.toFixed(0)}, ${l.x1.toFixed(0)}] · ${l.peaks.length} 峰</span>`;
    const btn = document.createElement("button");
    btn.textContent = "删除";
    btn.onclick = (e) => { e.stopPropagation(); S.lanes.splice(i, 1); saveLanes(); };
    li.appendChild(btn);
    li.onclick = () => { S.selLane = l.id; if (S.mode === "peaks") loadProfile(); renderAll(); };
    ul.appendChild(li);
  });
}

async function saveLanes() {
  const r = await api(`/api/analyses/${S.aid}/lanes`, { json: {
    lanes: S.lanes.map(l => ({ id: l.id, x0: l.x0, x1: l.x1, label: l.label })),
  }});
  S.lanes = r.lanes;
  if (S.selLane && !S.lanes.some(l => l.id === S.selLane)) { S.selLane = null; S.profile = null; }
  renderAll(); refreshResults();
}

function laneEdgeHit(im) {
  const tol = 6 / cv.width * S.imgW;
  for (const l of S.lanes) {
    if (Math.abs(im.x - l.x0) < tol) return { lane: l, edge: "x0" };
    if (Math.abs(im.x - l.x1) < tol) return { lane: l, edge: "x1" };
  }
  return null;
}

// ---------------- 峰 / 密度曲线 ----------------
async function loadProfile() {
  if (!S.selLane) { S.profile = null; $("profileWrap").hidden = true; return; }
  const r = await api(`/api/analyses/${S.aid}/lanes/${S.selLane}/profile`);
  S.profile = r.profile;
  S.profileMeta = r;
  $("profileWrap").hidden = false;
  $("profileTitle").textContent =
    `泳道密度曲线(横轴:校正图 y / Rf 网格;绿线=基线,红线=前沿;噪声≈${r.noise.toFixed(2)})`;
  renderProfile();
}

function selLaneObj() { return S.lanes.find(l => l.id === S.selLane); }

function chartX(yPx) { return yPx / S.profileMeta.height * pcv.width; }
function chartY(v, vmax) { return pcv.height - 18 - (v / vmax) * (pcv.height - 34); }

function renderProfile() {
  if (!S.profile) return;
  const w = $("profileWrap").clientWidth - 12;
  pcv.width = w; pcv.height = 240;
  const prof = S.profile, meta = S.profileMeta;
  const vmax = Math.max(...prof, 1) * 1.15;
  pctx.clearRect(0, 0, pcv.width, pcv.height);
  // Rf 网格
  pctx.strokeStyle = "#333"; pctx.fillStyle = "#778"; pctx.font = "10px sans-serif";
  for (let rf = 0; rf <= 1.001; rf += 0.2) {
    const yPx = meta.baseline_y - rf * (meta.baseline_y - meta.front_y);
    const x = chartX(yPx);
    pctx.beginPath(); pctx.moveTo(x, 0); pctx.lineTo(x, pcv.height - 18); pctx.stroke();
    pctx.fillText(rf.toFixed(1), x - 6, pcv.height - 6);
  }
  // 基线 / 前沿
  const hline = (y, c) => {
    pctx.strokeStyle = c; pctx.lineWidth = 1.5;
    pctx.beginPath(); pctx.moveTo(chartX(y), 0); pctx.lineTo(chartX(y), pcv.height - 18); pctx.stroke();
  };
  hline(meta.baseline_y, "#3c3"); hline(meta.front_y, "#e5534b");
  // 曲线
  pctx.strokeStyle = "#4f9cf9"; pctx.lineWidth = 1.5; pctx.beginPath();
  prof.forEach((v, i) => {
    const x = chartX(i), y = chartY(v, vmax);
    i ? pctx.lineTo(x, y) : pctx.moveTo(x, y);
  });
  pctx.stroke();
  // 建议窗口(虚线)
  if (S.proposed) {
    pctx.strokeStyle = "#3fbf7f"; pctx.setLineDash([4, 3]);
    for (const wn of S.proposed) {
      pctx.strokeRect(chartX(wn.y0), 6, chartX(wn.y1) - chartX(wn.y0), pcv.height - 24);
    }
    pctx.setLineDash([]);
  }
  // 积分窗口
  const lane = selLaneObj();
  if (lane) {
    for (const p of lane.peaks) {
      const x0 = chartX(p.y0), x1 = chartX(p.y1);
      pctx.fillStyle = p.id === S.selPeak ? "rgba(255,150,0,.35)" : "rgba(255,150,0,.18)";
      pctx.fillRect(x0, 6, x1 - x0, pcv.height - 24);
      pctx.fillStyle = "#e6873d";
      pctx.fillRect(x0 - 1.5, 6, 3, pcv.height - 24);
      pctx.fillRect(x1 - 1.5, 6, 3, pcv.height - 24);
    }
  }
}

async function savePeaks() {
  const lane = selLaneObj();
  if (!lane) return;
  const r = await api(`/api/analyses/${S.aid}/lanes/${lane.id}/peaks`, { json: {
    peaks: lane.peaks.map(p => ({ id: p.id, y0: p.y0, y1: p.y1, origin: p.origin || "manual" })),
  }});
  lane.peaks = r.peaks;
  renderAll(); refreshResults();
}

function peakEdgeHit(mx) {
  const lane = selLaneObj();
  if (!lane) return null;
  const tol = 6;
  for (const p of lane.peaks) {
    if (Math.abs(chartX(p.y0) - mx) < tol) return { peak: p, edge: "y0" };
    if (Math.abs(chartX(p.y1) - mx) < tol) return { peak: p, edge: "y1" };
  }
  return null;
}

function peakAt(mx) {
  const lane = selLaneObj();
  if (!lane) return null;
  const yPx = mx / pcv.width * S.profileMeta.height;
  return lane.peaks.find(p => yPx >= p.y0 && yPx <= p.y1) || null;
}

// ---------------- 结果与异常 ----------------
async function refreshResults() {
  if (!S.geometry) return;
  try {
    const r = await api(`/api/analyses/${S.aid}/results`);
    S.results = r; S.flags = r.flags;
  } catch (e) { S.results = null; S.flags = []; }
  renderResults(); renderFlags(); render();
}

function renderResults() {
  const tb = document.querySelector("#resultTable tbody");
  tb.innerHTML = "";
  if (!S.results) return;
  const flagByPeak = {};
  for (const f of S.flags) if (f.peak_id) (flagByPeak[f.peak_id] = flagByPeak[f.peak_id] || []).push(f);
  const spots = [...S.results.spots].sort((a, b) => a.lane_id - b.lane_id || a.spot_no - b.spot_no);
  for (const s of spots) {
    const tr = document.createElement("tr");
    const fs = flagByPeak[s.peak_id] || [];
    tr.innerHTML = `
      <td>${s.lane_label || "L" + s.lane_id}</td><td>${s.spot_no}</td>
      <td>${s.rf.toFixed(3)}</td><td>${s.center_y.toFixed(1)}</td>
      <td>${s.center_mm == null ? "—" : s.center_mm.toFixed(1)}</td>
      <td>${s.area.toFixed(0)}</td><td>${s.pct_lane.toFixed(1)}</td><td>${s.pct_plate.toFixed(1)}</td>
      <td class="${fs.length ? "flagged" : ""}">${fs.length ? "⚠" + fs.length : ""}</td>`;
    tb.appendChild(tr);
  }
}

function renderFlags() {
  const ul = $("flagList");
  ul.innerHTML = "";
  $("panelFlags").hidden = !S.flags.length;
  for (const f of S.flags) {
    const li = document.createElement("li");
    li.className = "flag" + (f.level === "error" ? " error" : "");
    const scope = f.peak_id ? `(泳道 ${f.lane_id} / 峰 ${f.peak_id})`
                : f.lane_id ? `(泳道 ${f.lane_id})` : "(全局)";
    li.innerHTML = `<div class="msg">[${f.level === "error" ? "错误" : "警告"}] ${f.type} ${scope}<br>${f.message}</div>`;
    if (f.kept) {
      const d = document.createElement("div");
      d.className = "kept";
      d.textContent = `已保留,理由:${f.reason}`;
      const un = document.createElement("button");
      un.textContent = "撤销保留";
      un.onclick = async () => { await decide(f, false, ""); };
      d.appendChild(un);
      li.appendChild(d);
    } else if (f.level !== "error") {
      const form = document.createElement("form");
      const inp = document.createElement("input");
      inp.type = "text"; inp.placeholder = "保留该异常结果的理由(必填)";
      const btn = document.createElement("button");
      btn.textContent = "保留";
      form.append(inp, btn);
      form.onsubmit = async (e) => {
        e.preventDefault();
        if (!inp.value.trim()) { alert("必须注明处理理由"); return; }
        await decide(f, true, inp.value.trim());
      };
      li.appendChild(form);
    }
    ul.appendChild(li);
  }
}

async function decide(f, kept, reason) {
  await api(`/api/analyses/${S.aid}/flags/${encodeURIComponent(f.key)}/decision`,
            { json: { kept, reason } });
  refreshResults();
}

// ---------------- 版本留痕 ----------------
function showVersions() {
  const body = $("versionsBody");
  body.innerHTML = "";
  for (const v of S.versions) {
    const div = document.createElement("div");
    div.innerHTML = `<h4>v${v.version} ${v.is_current ? "(当前)" : "(已失效)"} · ${v.created_at}</h4>`;
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify({ 参数: v.params, 派生: v.derived, 调整前: v.previous_params || "—" }, null, 1);
    div.appendChild(pre);
    body.appendChild(div);
  }
  $("versionsDialog").showModal();
}

// ---------------- 模式与视图 ----------------
function setMode(m) {
  S.mode = m;
  document.querySelectorAll("#modeTabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === m));
  $("panelGeometry").hidden = m !== "geometry";
  $("panelLanes").hidden = !(m === "lanes" || m === "peaks");
  $("panelPeaks").hidden = m !== "peaks";
  $("panelResults").hidden = !(m === "results" || m === "peaks");
  if (m === "geometry") { setView("original"); renderGeomPanel(); }
  else if (S.view === "original" && m !== "results") setView("signal");
  const hints = {
    lanes: "点击“添加泳道”后在图上横向拖出泳道;拖动泳道边缘调整宽度,点击选中。",
    peaks: "点击泳道查看密度曲线;拖动曲线上的积分边界,或用“拆分峰”处理共洗脱。",
    results: "逐斑点 Rf / 中心 / 面积 / 相对含量;异常需注明理由后方可保留。",
    geometry: GEOM_HINTS[S.geomStep],
  };
  $("hint").textContent = hints[m] || "";
  if (m === "peaks" && S.selLane) loadProfile();
  else if (m !== "peaks") { $("profileWrap").hidden = true; }
  renderAll();
}

function setView(v) {
  S.view = v;
  document.querySelectorAll("#viewTabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.view === v));
  if (S.aid) loadViewImage();
}

function renderAll() {
  renderGeomPanel(); renderLaneList(); renderPeakList(); render();
}

function renderPeakList() {
  const ul = $("peakList");
  ul.innerHTML = "";
  const lane = selLaneObj();
  if (!lane) return;
  lane.peaks.forEach((p, i) => {
    const li = document.createElement("li");
    li.className = p.id === S.selPeak ? "selected" : "";
    li.innerHTML = `<span class="grow">峰 ${i + 1}:y [${p.y0.toFixed(0)}, ${p.y1.toFixed(0)}] · ${p.origin}</span>`;
    li.onclick = () => { S.selPeak = p.id; renderPeakList(); renderProfile(); render(); };
    ul.appendChild(li);
  });
}

// ---------------- 画布事件 ----------------
cv.addEventListener("pointerdown", (e) => {
  const im = toImg(e, cv);
  cv.setPointerCapture(e.pointerId);
  if (S.mode === "geometry") {
    const hit = hitGeomPoint(im);
    if (hit) { S.drag = { kind: "geom", hit }; return; }
    onGeomClick(im);
  } else if (S.mode === "lanes") {
    if (S.addLane) { S.drag = { kind: "newlane", x0: im.x, x1: im.x }; return; }
    const edge = laneEdgeHit(im);
    if (edge) { S.drag = { kind: "laneedge", ...edge }; return; }
    const lane = S.lanes.find(l => im.x >= l.x0 && im.x <= l.x1);
    if (lane) { S.selLane = lane.id; renderAll(); }
  } else if (S.mode === "peaks") {
    const lane = S.lanes.find(l => im.x >= l.x0 && im.x <= l.x1);
    if (lane && lane.id !== S.selLane) { S.selLane = lane.id; loadProfile(); renderAll(); }
  }
});

cv.addEventListener("pointermove", (e) => {
  if (!S.drag) return;
  const im = toImg(e, cv);
  const d = S.drag;
  if (d.kind === "geom") {
    const p = [im.x, im.y];
    const dr = S.draft;
    if (d.hit.kind === "corner") dr.corners[d.hit.idx] = p;
    else if (d.hit.kind === "baseline") dr.baseline = p;
    else if (d.hit.kind === "front") dr.front = p;
    else if (d.hit.kind === "scale1") dr.scale.p1 = p;
    else if (d.hit.kind === "scale2") dr.scale.p2 = p;
    render();
  } else if (d.kind === "newlane") {
    d.x1 = im.x; render();
    ctx.strokeStyle = "#4f9cf9";
    ctx.strokeRect(sx(Math.min(d.x0, d.x1)), 0, Math.abs(sx(d.x1) - sx(d.x0)), cv.height);
  } else if (d.kind === "laneedge") {
    d.lane[d.edge] = Math.max(0, Math.min(S.imgW, im.x));
    render();
  }
});

cv.addEventListener("pointerup", async () => {
  const d = S.drag; S.drag = null;
  if (!d) return;
  if (d.kind === "newlane") {
    S.addLane = false; $("btnAddLane").classList.remove("active");
    const x0 = Math.min(d.x0, d.x1), x1 = Math.max(d.x0, d.x1);
    if (x1 - x0 > 5) {
      S.lanes.push({ id: null, x0, x1, label: `L${S.lanes.length + 1}`, peaks: [] });
      await saveLanes();
    }
  } else if (d.kind === "laneedge") {
    if (d.lane.x1 < d.lane.x0) [d.lane.x0, d.lane.x1] = [d.lane.x1, d.lane.x0];
    await saveLanes();
  }
});

// 密度曲线画布事件
pcv.addEventListener("pointerdown", (e) => {
  if (!S.profile) return;
  const r = pcv.getBoundingClientRect();
  const mx = e.clientX - r.left;
  pcv.setPointerCapture(e.pointerId);
  const yPx = mx / pcv.width * S.profileMeta.height;
  const lane = selLaneObj();
  if (!lane) return;
  if (S.split) {
    const p = peakAt(mx);
    if (p && yPx - p.y0 > 3 && p.y1 - yPx > 3) {
      const a = { ...p, y1: yPx, id: undefined, origin: "split" };
      const b = { ...p, y0: yPx, id: undefined, origin: "split" };
      lane.peaks = lane.peaks.filter(q => q.id !== p.id).concat([a, b]);
      lane.peaks.sort((q1, q2) => q1.y0 - q2.y0);
      S.split = false; $("btnSplit").classList.remove("active");
      savePeaks();
    }
    return;
  }
  if (S.addPeak) {
    S.drag = { kind: "newpeak", y0: yPx, y1: yPx };
    return;
  }
  const edge = peakEdgeHit(mx);
  if (edge) { S.drag = { kind: "peakedge", ...edge }; return; }
  const p = peakAt(mx);
  S.selPeak = p ? p.id : null;
  renderPeakList(); renderProfile();
});

pcv.addEventListener("pointermove", (e) => {
  if (!S.drag || !S.profile) return;
  const r = pcv.getBoundingClientRect();
  const yPx = Math.max(0, Math.min(S.profileMeta.height,
    (e.clientX - r.left) / pcv.width * S.profileMeta.height));
  const d = S.drag;
  if (d.kind === "peakedge") {
    d.peak[d.edge] = yPx;
    if (d.peak.y1 < d.peak.y0) {  // 交换,保持 y0<y1
      const t = d.peak.y0; d.peak.y0 = d.peak.y1; d.peak.y1 = t;
      d.edge = d.edge === "y0" ? "y1" : "y0";
    }
    renderProfile();
  } else if (d.kind === "newpeak") {
    d.y1 = yPx; renderProfile();
    pctx.fillStyle = "rgba(255,150,0,.25)";
    const x0 = chartX(Math.min(d.y0, d.y1)), x1 = chartX(Math.max(d.y0, d.y1));
    pctx.fillRect(x0, 6, x1 - x0, pcv.height - 24);
  }
});

pcv.addEventListener("pointerup", () => {
  const d = S.drag; S.drag = null;
  if (!d) return;
  if (d.kind === "peakedge") savePeaks();
  else if (d.kind === "newpeak") {
    S.addPeak = false; $("btnAddPeak").classList.remove("active");
    const y0 = Math.min(d.y0, d.y1), y1 = Math.max(d.y0, d.y1);
    if (y1 - y0 > 3) {
      selLaneObj().peaks.push({ id: null, y0, y1, origin: "manual" });
      savePeaks();
    }
  }
});

// ---------------- 控件绑定 ----------------
$("btnNew").onclick = () => $("fileInput").click();
$("fileInput").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  const r = await fetch("/api/analyses", { method: "POST", body: fd });
  if (!r.ok) { alert("上传失败"); return; }
  const st = await r.json();
  e.target.value = "";
  openAnalysis(st.analysis.id);
};
$("btnSample").onclick = async () => {
  const st = await api("/api/analyses/sample", { method: "POST" });
  openAnalysis(st.analysis.id);
};
$("analysisSelect").onchange = (e) => { if (e.target.value) openAnalysis(+e.target.value); };

document.querySelectorAll("#modeTabs button").forEach(b =>
  b.onclick = () => {
    if (!S.geometry && b.dataset.mode !== "geometry") { alert("请先完成几何标定"); return; }
    setMode(b.dataset.mode);
  });
document.querySelectorAll("#viewTabs button").forEach(b =>
  b.onclick = () => {
    if (!S.geometry && b.dataset.view !== "original") { alert("请先完成几何标定"); return; }
    setView(b.dataset.view);
  });

$("geomPrev").onclick = () => { S.geomStep = Math.max(0, S.geomStep - 1); renderGeomPanel(); };
$("geomNext").onclick = () => { S.geomStep = Math.min(3, S.geomStep + 1); renderGeomPanel(); };
$("geomSubmit").onclick = submitGeometry;
$("scaleMm").oninput = (e) => { S.draft.scale.mm = parseFloat(e.target.value) || 0; };

$("btnAddLane").onclick = () => {
  S.addLane = !S.addLane;
  $("btnAddLane").classList.toggle("active", S.addLane);
};

$("btnAuto").onclick = async () => {
  if (!S.selLane) { alert("先选择泳道"); return; }
  const r = await api(`/api/analyses/${S.aid}/lanes/${S.selLane}/autodetect`, { json: {} });
  S.proposed = r.windows;
  $("btnApplyProposed").hidden = !r.windows.length;
  if (!r.windows.length) alert("未检出峰,可手动添加");
  renderProfile();
};
$("btnApplyProposed").onclick = async () => {
  const lane = selLaneObj();
  if (!lane || !S.proposed) return;
  lane.peaks = S.proposed.map(w => ({ id: null, y0: w.y0, y1: w.y1, origin: "auto" }));
  S.proposed = null;
  $("btnApplyProposed").hidden = true;
  await savePeaks();
};
$("btnAddPeak").onclick = () => {
  S.addPeak = !S.addPeak;
  $("btnAddPeak").classList.toggle("active", S.addPeak);
};
$("btnSplit").onclick = () => {
  S.split = !S.split;
  $("btnSplit").classList.toggle("active", S.split);
  $("hint").textContent = S.split ? "在曲线上点击要拆分的峰内部位置,将其一分为二。" : "";
};
$("btnDelPeak").onclick = () => {
  const lane = selLaneObj();
  if (!lane || !S.selPeak) return;
  lane.peaks = lane.peaks.filter(p => p.id !== S.selPeak);
  S.selPeak = null;
  savePeaks();
};
$("btnRecompute").onclick = refreshResults;
$("btnVersions").onclick = showVersions;

// 删除峰按钮可用性
setInterval(() => { $("btnDelPeak").disabled = !S.selPeak; }, 300);

window.addEventListener("resize", () => { if (S.img) { fitCanvas(); render(); renderProfile(); } });

refreshList();
})();
