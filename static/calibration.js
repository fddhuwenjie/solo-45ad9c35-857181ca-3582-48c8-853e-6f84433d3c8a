/* 校准曲线与含量反算前端:泳道角色标注、斑点绑定、联动校准图/残差、三模型切换、
   问题定位、版本留痕(过期可查)与导出。计算全部在 Python 端完成。 */
(() => {
"use strict";

const S = {
  aid: null,          // 当前板
  cid: null,          // 当前校准
  st: null,           // /api/calibrations/<id> 状态
  img: null,          // 校正图
  imgVersion: null,
  selectedLane: null, // 板面定位高亮
  saveTimer: null,
};

const $ = (id) => document.getElementById(id);
const ROLES = [["standard", "标准"], ["blank", "空白"], ["unknown", "未知样"]];
const ROLE_COLOR = { standard: "#3182bd", blank: "#9aa3ad", unknown: "#e67e22" };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (x, n = 4) => (x == null || isNaN(x)) ? "—" :
  (Math.abs(x) >= 1000 || (Math.abs(x) < 0.001 && x !== 0) ? x.toExponential(3) : (+x.toFixed(n))).toString();

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
    throw new Error(msg);
  }
  return r.json();
}

// ---------------- 板/校准列表 ----------------
async function refreshAnalyses() {
  const list = await api("/api/analyses");
  const states = await Promise.all(list.map(a =>
    api(`/api/analyses/${a.id}`).then(st => ({ a, st })).catch(() => null)));
  S.plates = (states || []).filter(x => x && x.st.geometry &&
    x.st.lanes.some(l => l.peaks.length > 0))
    .map(x => ({ id: x.a.id, name: x.a.name }));
  $("newAnalysis").innerHTML = S.plates.length
    ? '<option value="">选择已定量的板…</option>' +
      S.plates.map(p => `<option value="${p.id}">#${p.id} ${esc(p.name)}</option>`).join("")
    : '<option value="">(没有已定量的板,请先完成单板定量)</option>';
}

async function refreshCalList(selectId = S.aid) {
  const sel = $("calSelect");
  if (!selectId) {
    sel.innerHTML = '<option value="">打开校准…</option>';
    return;
  }
  const list = await api(`/api/calibrations/by-analysis/${selectId}`);
  sel.innerHTML = '<option value="">打开校准…</option>' +
    list.map(k => `<option value="${k.id}">#${k.id} ${esc(k.target_name || k.name)}` +
      `(${k.n_standard}标准/${k.n_version}版)</option>`).join("");
  if (S.cid) sel.value = S.cid;
}

$("newAnalysis").onchange = async (e) => {
  S.aid = +e.target.value || null;
  S.cid = null;
  $("workspace").hidden = true; $("calExports").hidden = true;
  await refreshCalList(S.aid);
};
$("calSelect").onchange = (e) => { if (e.target.value) openCal(+e.target.value); };

$("btnNewCal").onclick = async () => {
  const aid = +$("newAnalysis").value;
  const name = $("newName").value.trim();
  const target_name = $("newTarget").value.trim();
  if (!aid) { alert("请先选择一块已定量的板"); return; }
  if (!name || !target_name) { alert("请填写校准名称与目标成分"); return; }
  const rfRaw = $("newRf").value;
  try {
    const st = await api(`/api/analyses/${aid}/calibrations`, { json: {
      name, target_name,
      target_rf: rfRaw === "" ? null : +rfRaw,
      conc_unit: $("newConcUnit").value || "ng/uL",
      vol_unit: $("newVolUnit").value || "uL",
    }});
    S.aid = aid;
    await refreshCalList(aid);
    await openCal(st.calibration.id);
  } catch (e) { alert("创建失败: " + e.message); }
};

// ---------------- 打开/重载 ----------------
async function openCal(cid) {
  S.cid = cid;
  await reload();
  $("emptyState").hidden = true;
  $("workspace").hidden = false;
}

async function reload() {
  S.st = await api(`/api/calibrations/${S.cid}`);
  const cal = S.st.calibration;
  S.aid = cal.analysis_id;
  await loadImage();
  renderAll();
  $("calExports").hidden = cal.current_version == null;
  if (cal.current_version != null) {
    for (const [k, tail] of [["expSamples", "samples.csv"], ["expModel", "model.json"],
                             ["expFig", "figure.png"]]) {
      $(k).href = `/api/calibrations/${S.cid}/versions/${cal.current_version}/${tail}`;
    }
  }
  await refreshCalList(S.aid);
}

function loadImage() {
  return new Promise((res) => {
    const v = S.st.analysis.geometry_version;
    if (S.img && S.imgVersion === v) return res();
    const im = new Image();
    im.onload = () => { S.img = im; S.imgVersion = v; res(); };
    im.onerror = () => res();
    im.src = `/api/analyses/${S.aid}/preview/corrected?t=${v}`;
  });
}

// ---------------- 标注提交(防抖,实时评估) ----------------
function payload(model) {
  // orphan(几何重建后失效的旧泳道)在显式清除角色时才从服务端删除
  const removed = S.st.lanes
    .filter(l => l.orphan && !l.role)
    .map(l => l.id);
  return {
    model: model || S.st.calibration.model,
    lanes: S.st.lanes.filter(l => l.role && !l.orphan).map(l => {
      const a = l.annotation || {};
      return {
        lane_id: l.id, role: l.role, peak_id: a.peak_id ?? null,
        concentration: a.concentration ?? null, volume: a.volume ?? null,
        dilution: a.dilution ?? null, excluded: !!a.excluded,
        exclude_reason: a.exclude_reason || "",
      };
    }),
    removed_lane_ids: removed,
  };
}

function scheduleSave(model) {
  clearTimeout(S.saveTimer);
  $("hint").textContent = "计算中…";
  S.saveTimer = setTimeout(() => doEvaluate(model), 250);
}

async function doEvaluate(model) {
  try {
    S.st = await api(`/api/calibrations/${S.cid}/evaluate`,
                     { json: payload(model) });
    renderAll();
  } catch (e) {
    $("hint").textContent = "操作失败: " + e.message;
  }
}

// ---------------- 泳道标注表 ----------------
function renderLaneTable() {
  const st = S.st, cal = st.calibration;
  $("hConcUnit").textContent = `(${cal.conc_unit})`;
  $("hVolUnit").textContent = `(${cal.vol_unit})`;
  const issueByLane = {};
  for (const i of st.evaluation.issues) {
    for (const lid of i.lane_ids)
      (issueByLane[lid] = issueByLane[lid] || []).push(i);
  }
  const tb = document.querySelector("#laneTable tbody");
  tb.innerHTML = "";
  st.lanes.forEach(l => {
    const a = l.annotation || { role: null };
    const role = l.role;
    const tr = document.createElement("tr");
    tr.dataset.lane = l.id;
    if (l.orphan) tr.style.color = "var(--dim)";
    else if (S.selectedLane === l.id) tr.classList.add("selected");
    if (!l.orphan && (issueByLane[l.id] || []).some(i => i.level === "error")) tr.style.color = "var(--err)";
    else if (!l.orphan && issueByLane[l.id]) tr.style.color = "var(--warn)";

    const tdName = document.createElement("td");
    tdName.style.textAlign = "left";
    tdName.innerHTML = `<b>${esc(l.label || "L" + l.id)}</b> <span class="dim">#${l.id}</span>` +
      (l.orphan ? ' <span class="warn" title="几何重设/泳道重建后该泳道已不存在">来源已失效</span>' : "");
    tdName.style.cursor = "pointer";
    tdName.onclick = () => { if (!l.orphan) { S.selectedLane = l.id; drawPlate(); renderLaneTable(); } };

    const tdRole = document.createElement("td");
    tdRole.appendChild(selectFor(
      [["", "—"], ...ROLES], role || "",
      (v) => { setAnnot(l.id, { role: v || null }); scheduleSave(); }));

    const tdSpot = document.createElement("td");
    if (role) {
      const opts = [["", "— 未绑定 —"],
        ...l.spots.map(s => [s.peak_id,
          `#${s.spot_no} Rf${s.rf.toFixed(3)} A${s.area.toFixed(0)} (峰${s.peak_id})`])];
      tdSpot.appendChild(selectFor(opts, a.peak_id ?? "", (v) => {
        setAnnot(l.id, { peak_id: v === "" ? null : +v });
        scheduleSave();
      }));
      if (a.suggestion && a.peak_id !== a.suggestion.peak_id) {
        const b = document.createElement("button");
        b.textContent = "建议";
        b.title = `目标 Rf 附近候选:#${a.suggestion.spot_no} Rf${a.suggestion.rf.toFixed(3)}`;
        b.style.marginLeft = "4px";
        b.onclick = () => { setAnnot(l.id, { peak_id: a.suggestion.peak_id }); scheduleSave(); };
        tdSpot.appendChild(b);
      }
    } else tdSpot.innerHTML = '<span class="dim">—</span>';

    const tdConc = numCell(role === "standard", a.concentration,
      (v) => { setAnnot(l.id, { concentration: v }); scheduleSave(); });
    const tdVol = numCell(role === "standard" || role === "unknown", a.volume,
      (v) => { setAnnot(l.id, { volume: v }); scheduleSave(); });
    const tdDil = numCell(role === "unknown", a.dilution,
      (v) => { setAnnot(l.id, { dilution: v }); scheduleSave(); });

    const tdArea = document.createElement("td");
    tdArea.textContent = a.bound_spot ? fmt(a.bound_spot.area, 1) : "—";
    const tdRf = document.createElement("td");
    tdRf.textContent = a.bound_spot ? fmt(a.bound_spot.rf, 3) : "—";

    const tdEx = document.createElement("td");
    if (role === "standard") {
      const wrap = document.createElement("div");
      wrap.className = "row wrap"; wrap.style.margin = "0";
      const ck = document.createElement("input");
      ck.type = "checkbox"; ck.checked = !!a.excluded;
      ck.title = "排除该标准点(必须填写理由)";
      ck.onchange = () => { setAnnot(l.id, { excluded: ck.checked }); scheduleSave(); };
      const rs = document.createElement("input");
      rs.type = "text"; rs.value = a.exclude_reason || "";
      rs.placeholder = "排除理由"; rs.style.width = "110px";
      rs.onchange = () => { setAnnot(l.id, { exclude_reason: rs.value }); scheduleSave(); };
      wrap.append(ck, rs);
      tdEx.appendChild(wrap);
    } else tdEx.textContent = "—";

    const tdStatus = document.createElement("td");
    const probs = issueByLane[l.id] || [];
    if (a.peak_id && a.binding_valid === false) {
      tdStatus.innerHTML = '<span class="warn">绑定斑点已失效(来源已变更)</span>';
    } else if (probs.length) {
      tdStatus.innerHTML = probs.map(p =>
        `<div class="${p.level === "error" ? "" : "dim"}" style="color:${p.level === "error"
          ? "var(--err)" : "var(--warn)"}">${p.level === "error" ? "✕" : "⚠"} ${esc(p.message)}</div>`).join("");
    } else if (role === "standard" && a.excluded) {
      tdStatus.innerHTML = '<span class="dim">已排除</span>';
    } else tdStatus.innerHTML = '<span style="color:#5c6">✔</span>';
    tdStatus.style.textAlign = "left";

    tr.append(tdName, tdRole, tdSpot, tdConc, tdVol, tdDil, tdArea, tdRf, tdEx, tdStatus);
    tb.appendChild(tr);
  });
  if (st.missing_lanes.length) {
    const note = document.createElement("div");
    note.className = "warn";
    note.style.marginTop = "6px";
    note.textContent = `泳道 ${st.missing_lanes.join(", ")} 在当前几何版本中已不存在(板来源已重建);` +
      "把对应角色改回“—”即可清除失效标注。";
    tb.parentElement.parentElement.appendChild(note);
  }
}

function setAnnot(laneId, patch) {
  const l = S.st.lanes.find(x => x.id === laneId);
  if (!l.role && patch.role === undefined) return;
  if (patch.role !== undefined) {
    l.role = patch.role;
    l.annotation = l.annotation || { role: patch.role };
    l.annotation.role = patch.role;
    return;
  }
  l.annotation = l.annotation || {};
  Object.assign(l.annotation, patch);
}

function selectFor(opts, value, onchange) {
  const sel = document.createElement("select");
  sel.style.maxWidth = "200px";
  sel.innerHTML = opts.map(([v, t]) =>
    `<option value="${v}" ${String(v) === String(value) ? "selected" : ""}>${esc(t)}</option>`).join("");
  sel.onchange = () => onchange(sel.value);
  return sel;
}

function numCell(enabled, value, onchange) {
  const td = document.createElement("td");
  if (!enabled) { td.textContent = "—"; return td; }
  const inp = document.createElement("input");
  inp.type = "number"; inp.step = "any"; inp.min = "0";
  inp.value = value ?? ""; inp.style.width = "86px";
  inp.onchange = () => onchange(inp.value === "" ? null : +inp.value);
  td.appendChild(inp);
  return td;
}

// ---------------- 板面定位图 ----------------
function drawPlate() {
  const cv = $("plateCv"), ctx = cv.getContext("2d");
  if (!S.img) return;
  const w = cv.parentElement.clientWidth - 4;
  const k = w / S.img.naturalWidth;
  cv.width = w; cv.height = S.img.naturalHeight * k;
  ctx.drawImage(S.img, 0, 0, cv.width, cv.height);
  const d = S.st.derived;
  if (d) {
    ctx.strokeStyle = "rgba(30,160,60,.9)"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, d.baseline_y * k); ctx.lineTo(cv.width, d.baseline_y * k); ctx.stroke();
    ctx.strokeStyle = "rgba(220,40,40,.9)";
    ctx.beginPath(); ctx.moveTo(0, d.front_y * k); ctx.lineTo(cv.width, d.front_y * k); ctx.stroke();
  }
  const issueLanes = new Set();
  for (const i of S.st.evaluation.issues)
    if (i.level === "error") i.lane_ids.forEach(x => issueLanes.add(x));
  for (const l of S.st.lanes) {
    if (!l.role) continue;
    const col = ROLE_COLOR[l.role];
    ctx.fillStyle = col + "26";
    ctx.fillRect(l.x0 * k, 0, (l.x1 - l.x0) * k, cv.height);
    ctx.strokeStyle = issueLanes.has(l.id) ? "#e5534b" : col;
    ctx.lineWidth = issueLanes.has(l.id) ? 3 : 1.2;
    ctx.strokeRect(l.x0 * k, 0, (l.x1 - l.x0) * k, cv.height);
    ctx.fillStyle = col; ctx.font = "bold 12px sans-serif";
    ctx.fillText(l.label || "L" + l.id, l.x0 * k + 3, 14);
    const a = l.annotation;
    if (a && a.bound_spot) {
      const cx = (l.x0 + l.x1) / 2 * k, cy = a.bound_spot.center_y * k;
      ctx.strokeStyle = col; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(cx, cy, 7, 0, 7); ctx.stroke();
      ctx.fillStyle = col;
      ctx.fillText(`#${a.bound_spot.spot_no}`, cx + 9, cy - 6);
    }
    if (S.selectedLane === l.id) {
      ctx.strokeStyle = "#fff"; ctx.setLineDash([5, 4]); ctx.lineWidth = 2;
      ctx.strokeRect(l.x0 * k - 1, 0, (l.x1 - l.x0) * k + 2, cv.height);
      ctx.setLineDash([]);
    }
  }
}
$("plateCv").addEventListener("click", (e) => {
  if (!S.img) return;
  const r = e.target.getBoundingClientRect();
  const x = (e.clientX - r.left) / (S.img.naturalWidth / r.width);
  const hit = S.st.lanes.find(l => x >= l.x0 && x <= l.x1);
  if (hit) { S.selectedLane = hit.id; drawPlate(); renderLaneTable();
    document.querySelector(`#laneTable tr[data-lane="${hit.id}"]`)?.scrollIntoView(
      { behavior: "smooth", block: "center" }); }
});

// ---------------- 校准图 + 残差(联动) ----------------
function drawCalibration() {
  const ev = S.st.evaluation, cal = S.st.calibration;
  const cv = $("calCv"), ctx = cv.getContext("2d");
  const W = cv.parentElement.clientWidth - 4, H = 460;
  cv.width = W; cv.height = H;
  const M = { l: 64, r: 220, t: 30, b: 44 };
  ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, W, H);
  const pts = ev.points, fit = ev.fit, samples = ev.samples;
  const xs = pts.map(p => p.amount).concat(samples.filter(s => s.amount_back != null).map(s => s.amount_back)).concat([0]);
  const ys = pts.map(p => p.response).concat(samples.map(s => s.response || 0));
  if (xs.length < 2) {
    ctx.fillStyle = "#9aa3ad"; ctx.font = "14px sans-serif";
    ctx.fillText("标准点不足,尚不能成线", M.l, M.t + 20);
    drawResiduals(); return;
  }
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const ymax = Math.max(...ys) * 1.12;
  const X = (v) => M.l + (v - xmin) / (xmax - xmin || 1) * (W - M.l - M.r);
  const Y = (v) => H - M.b - v / (ymax || 1) * (H - M.t - M.b);

  ctx.strokeStyle = "#2a2f37"; ctx.fillStyle = "#778"; ctx.font = "10px sans-serif";
  for (let i = 0; i <= 4; i++) {
    const gx = M.l + i * (W - M.l - M.r) / 4;
    const gy = M.t + i * (H - M.t - M.b) / 4;
    ctx.beginPath(); ctx.moveTo(gx, M.t); ctx.lineTo(gx, H - M.b); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(M.l, gy); ctx.lineTo(W - M.r, gy); ctx.stroke();
    ctx.fillText((xmin + i * (xmax - xmin) / 4).toFixed(3), gx - 14, H - M.b + 14);
    ctx.fillText((ymax - i * ymax / 4).toFixed(2), 6, gy + 3);
  }
  ctx.fillStyle = "#9aa3ad";
  ctx.fillText(`点样量 (${cal.conc_unit}·${cal.vol_unit})`, M.l, H - 10);
  ctx.save(); ctx.translate(14, M.t + 90); ctx.rotate(-Math.PI / 2);
  ctx.fillText("响应(斑点面积)", 0, 0); ctx.restore();

  // 工作范围底色
  if (ev.range) {
    ctx.fillStyle = "rgba(49,130,189,.10)";
    ctx.fillRect(X(ev.range[0]), M.t, X(ev.range[1]) - X(ev.range[0]), H - M.t - M.b);
  }
  // 拟合线
  if (fit && ev.range) {
    const line = (x0, x1, dash) => {
      ctx.strokeStyle = "#e5534b"; ctx.lineWidth = 2; ctx.setLineDash(dash);
      ctx.beginPath();
      ctx.moveTo(X(x0), Y(fit.intercept + fit.slope * x0));
      ctx.lineTo(X(x1), Y(fit.intercept + fit.slope * x1)); ctx.stroke();
      ctx.setLineDash([]);
    };
    line(Math.max(xmin, ev.range[0]), Math.min(xmax, ev.range[1]), []);
    if (xmin < ev.range[0]) line(xmin, ev.range[0], [5, 4]);
    if (xmax > ev.range[1]) line(ev.range[1], xmax, [5, 4]);
  }
  // 标准散点 + 来源标签
  pts.forEach(p => {
    const cx = X(p.amount), cy = Y(p.response);
    ctx.strokeStyle = "#3182bd"; ctx.fillStyle = "#3182bd"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(cx, cy, 4, 0, 7); ctx.fill();
    ctx.font = "10px sans-serif";
    ctx.fillText(`${p.lane_label}-${p.spot_no ?? "?"}(峰${p.peak_id})`, cx + 6, cy - 5);
  });
  ev.excluded_points.forEach(p => {
    if (p.amount == null) return;
    const cx = X(p.amount), cy = Y(p.response);
    ctx.strokeStyle = "#9aa3ad"; ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(cx - 4, cy - 4); ctx.lineTo(cx + 4, cy + 4);
    ctx.moveTo(cx - 4, cy + 4); ctx.lineTo(cx + 4, cy - 4); ctx.stroke();
    ctx.fillStyle = "#9aa3ad";
    ctx.fillText(`${p.lane_label} 已排除`, cx + 6, cy + 12);
  });
  // 未知样投影
  samples.forEach(s => {
    if (s.amount_back == null || !s.response) return;
    const cx = X(s.amount_back), cy = Y(s.response);
    const col = s.status === "ok" ? "#2ecc71" : "#e6b93d";
    ctx.strokeStyle = col + "88"; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(M.l, cy); ctx.lineTo(cx, cy);
    ctx.moveTo(cx, H - M.b); ctx.lineTo(cx, cy); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col;
    ctx.beginPath();
    ctx.moveTo(cx, cy - 6); ctx.lineTo(cx - 5, cy + 4); ctx.lineTo(cx + 5, cy + 4);
    ctx.closePath(); ctx.fill();
    ctx.font = "bold 10px sans-serif";
    ctx.fillText(`${s.lane_label} ${s.status === "ok" ? "" : "(" + s.status + ")"}`, cx + 6, cy + 12);
  });
  // 图例
  let ly = M.t + 6;
  const legend = [["#3182bd", "标准点(标签=泳道-斑点#/峰ID)"],
                  ["#9aa3ad", "排除点(须填理由)"],
                  ["#2ecc71", "未知样(范围内)"],
                  ["#e6b93d", "未知样(范围外/异常)"],
                  ["#e5534b", "拟合线(实线=工作范围)"]];
  ctx.font = "11px sans-serif";
  legend.forEach(([c, t]) => {
    ctx.fillStyle = c; ctx.fillRect(W - M.r + 6, ly - 8, 10, 10);
    ctx.fillStyle = "#c9ced6";
    wrapText(t, W - M.r + 22, ly, M.r - 24, 12);
    ly += 30;
  });
  drawResiduals();

  function drawResiduals() {
    const rc = $("resCv"), rctx = rc.getContext("2d");
    const rW = W, rH = 150;
    rc.width = rW; rc.height = rH;
    rctx.fillStyle = "#14161a"; rctx.fillRect(0, 0, rW, rH);
    if (!pts.length || pts.some(p => p.residual == null)) {
      rctx.fillStyle = "#9aa3ad"; rctx.font = "12px sans-serif";
      rctx.fillText("残差:成线后显示", M.l, 20);
      return;
    }
    const mm = Math.max(...pts.map(p => Math.abs(p.residual)), 1e-9);
    const zy = rH / 2 + 6;
    rctx.strokeStyle = "#3a4048";
    rctx.beginPath(); rctx.moveTo(M.l, zy); rctx.lineTo(rW - M.r, zy); rctx.stroke();
    rctx.fillStyle = "#9aa3ad"; rctx.font = "10px sans-serif";
    rctx.fillText("残差(实测-拟合)", M.l, 14);
    pts.forEach(p => {
      const cx = X(p.amount), ry = zy - p.residual / mm * (rH / 2 - 16);
      rctx.strokeStyle = "#3182bd"; rctx.lineWidth = 5;
      rctx.beginPath(); rctx.moveTo(cx, zy); rctx.lineTo(cx, ry); rctx.stroke();
      rctx.fillStyle = "#c9ced6";
      rctx.fillText(`${p.resid_pct == null ? "" : p.resid_pct.toFixed(1) + "%"}`, cx - 10,
                    p.residual >= 0 ? ry - 4 : ry + 12);
    });
  }
}

function wrapText(text, x, y, maxW, lh) {
  // 简单按字符折行(图例用,短文本)
  const ctx = $("calCv").getContext("2d");
  let line = "", yy = y;
  for (const ch of text) {
    if (ctx.measureText(line + ch).width > maxW && line) {
      ctx.fillText(line, x, yy); line = ch; yy += lh;
    } else line += ch;
  }
  ctx.fillText(line, x, yy);
}

// ---------------- 模型结果 / 样品表 ----------------
function renderModel() {
  const st = S.st, ev = st.evaluation, cal = st.calibration;
  $("calBadge").textContent =
    `#${cal.id} ${cal.target_name || cal.name} · ${cal.conc_unit} · ${ev.model_label}`;
  // 模型选择按钮
  const mp = $("modelPick");
  mp.innerHTML = "";
  st.models.forEach(m => {
    const b = document.createElement("button");
    b.textContent = m.label;
    if (m.kind === cal.model) b.classList.add("primary");
    b.onclick = () => { if (cal.model !== m.kind) scheduleSave(m.kind); };
    mp.appendChild(b);
  });

  const fit = ev.fit;
  let html = "";
  if (fit && ev.range) {
    const eq = ev.model === "linear_zero"
      ? `y = ${fmt(fit.slope, 6)}·x`
      : `y = ${fmt(fit.slope, 6)}·x ${fit.intercept >= 0 ? "+" : "−"} ${fmt(Math.abs(fit.intercept), 4)}`;
    html += `<div class="kv" style="color:var(--fg);font-size:14px">${eq}</div>`;
    html += `<table style="margin-top:6px"><tbody><tr>` +
      ["R²", "斜率 b", "截距 a", "标准点数 n", "工作范围(点样量)", "工作范围(点样液浓度)*"]
        .map(t => `<th>${t}</th>`).join("") + "</tr><tr>" +
      [fmt(fit.r2, 5), fmt(fit.slope, 6), fmt(fit.intercept, 4), ev.points.length,
       `${fmt(ev.range[0], 4)} ~ ${fmt(ev.range[1], 4)} ${cal.conc_unit}·${cal.vol_unit}`,
       sampleConcRange(ev, cal)].map(v => `<td>${v}</td>`).join("") + "</tr></tbody></table>";
    html += `<div class="dim" style="margin-top:4px">* 按各未知样进样体积换算:点样量 ÷ 进样体积。</div>`;
  }
  const errs = ev.issues.filter(i => i.level === "error");
  const warns = ev.issues.filter(i => i.level === "warning");
  if (ev.blocked) {
    html += `<div class="flag error" style="margin-top:8px"><b>✕ 存在阻断性问题,暂不形成含量结论:</b>` +
      errs.filter(i => ev.blocker_types.includes(i.type)).map(issueHtml).join("") + `</div>`;
  }
  if (errs.filter(i => !ev.blocker_types.includes(i.type)).length) {
    html += errs.filter(i => !ev.blocker_types.includes(i.type)).map(issueHtml).join("");
  }
  if (warns.length) html += warns.map(issueHtml).join("");
  if (!ev.issues.length) html += `<div style="color:#5c6;margin-top:6px">✔ 质控通过。</div>`;
  $("modelBody").innerHTML = html;
  $("modelKv").textContent = fit && ev.range
    ? `方程与工作范围基于当前已纳入的 ${ev.points.length} 个标准点实时计算;点“保存当前模型为新版本”后留痕可导出。`
    : "尚未成线:至少需要 2 个已绑定、有浓度/体积且未被排除的标准点。";
  $("fitHint").textContent = ev.blocked
    ? "阻断性问题未消除前不能保存模型版本。" : "";
  $("btnFit").disabled = ev.blocked;
  $("hint").textContent =
    `标准 ${ev.points.length} 点(排除 ${ev.excluded_points.length})·` +
    `未知样 ${ev.samples.length} 个·` + (ev.blocked ? "存在阻断性问题" :
    (fit ? `R²=${fmt(fit.r2, 4)}` : "未成线"));
  // 问题定位:点击消息定位泳道
  $("modelBody").querySelectorAll("[data-lane]").forEach(el => {
    el.onclick = () => {
      S.selectedLane = +el.dataset.lane;
      drawPlate(); renderLaneTable();
      document.querySelector(`#laneTable tr[data-lane="${el.dataset.lane}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    };
  });
}

function issueHtml(i) {
  const locate = i.lane_ids.length
    ? ` <a data-lane="${i.lane_ids[0]}" style="color:var(--accent);cursor:pointer">[定位泳道]</a>` : "";
  return `<div class="flag ${i.level === "error" ? "error" : ""}" style="margin-top:6px">` +
    `<span class="msg">${i.level === "error" ? "✕" : "⚠"} ${esc(i.message)}${locate}</span></div>`;
}

function sampleConcRange(ev, cal) {
  const vols = new Set(ev.samples.filter(s => s.volume > 0).map(s => s.volume));
  if (vols.size !== 1) return "(各样品体积不一)";
  const v = [...vols][0];
  return `${fmt(ev.range[0] / v, 4)} ~ ${fmt(ev.range[1] / v, 4)} ${cal.conc_unit}`;
}

function renderSamples() {
  const ev = S.st.evaluation, cal = S.st.calibration;
  const tb = document.querySelector("#sampleTable tbody");
  tb.innerHTML = "";
  if (!ev.samples.length) {
    tb.innerHTML = '<tr><td colspan="8" class="dim">尚未标注未知样泳道。</td></tr>';
    return;
  }
  for (const s of ev.samples) {
    const tr = document.createElement("tr");
    const click = () => {
      S.selectedLane = s.lane_id; drawPlate(); renderLaneTable();
    };
    const ok = s.status === "ok";
    tr.style.color = ok ? "#5c6" : "var(--warn)";
    tr.innerHTML =
      `<td style="text-align:left;cursor:pointer" data-lane="${s.lane_id}"><b>${esc(s.lane_label)}</b></td>` +
      `<td>${s.spot_no ?? "—"}(峰${s.peak_id ?? "—"})</td>` +
      `<td>${fmt(s.response, 1)}</td>` +
      `<td>${fmt(s.amount_back, 4)}</td>` +
      `<td>${ok ? fmt(s.applied_concentration, 4) : "—"}</td>` +
      `<td><b>${ok ? fmt(s.sample_concentration, 4) + " " + cal.conc_unit : "—"}</b></td>` +
      `<td>${s.in_range == null ? "—" : s.in_range ? "是" : '<span style="color:var(--err)">否</span>'}</td>` +
      `<td style="text-align:left">${statusText(s)}</td>`;
    tr.querySelector("td[data-lane]").onclick = click;
    tb.appendChild(tr);
  }
}

function statusText(s) {
  const map = { ok: "可定量", out_of_range: "范围外,不出结论", no_spot: "未绑定斑点",
                incomplete: "体积/稀释缺失", blocked: "模型被阻断" };
  return esc(map[s.status] || s.status) +
    (s.reasons.length ? `<br><span class="dim">${esc(s.reasons.join(";"))}</span>` : "");
}

// ---------------- 侧栏设置 / 阈值 / 版本 ----------------
function renderSidebar() {
  const cal = S.st.calibration;
  $("fName").value = cal.name;
  $("fTarget").value = cal.target_name;
  $("fRf").value = cal.target_rf ?? "";
  $("fTol").value = cal.rf_tol;
  $("fConcUnit").value = cal.conc_unit;
  $("fVolUnit").value = cal.vol_unit;
  $("metaHint").textContent =
    `板 #${S.st.analysis.id} ${S.st.analysis.name}(几何 v${S.st.analysis.geometry_version})。` +
    `目标 Rf 用于在每条泳道自动建议同位置斑点(容差 ±${cal.rf_tol})。`;
  const t = S.st.defaults;
  $("thresholds").textContent = [
    `成线最少标准点:${t.min_standards} 个(2 点仅警告,建议 ≥3 浓度水平)`,
    `同浓度重复单位点样量响应 CV ≥ ${t.dup_response_cv_pct}% → 响应冲突(阻断)`,
    `响应不单调(允许回落 ${t.monotonic_tol * 100}% 抗噪)→ 阻断`,
    `空白响应 > 最低标准点的 ${t.blank_response_pct}% → 空白异常(阻断)`,
    `R² < ${t.low_r2} → 线性警告(不阻断)`,
    `反算点样量超出标准点范围 → 该样品不出含量结论`,
  ].join("\n");
}

$("btnSaveMeta").onclick = async () => {
  try {
    S.st = await api(`/api/calibrations/${S.cid}/update`, { json: {
      name: $("fName").value, target_name: $("fTarget").value,
      target_rf: $("fRf").value === "" ? null : +$("fRf").value,
      rf_tol: +$("fTol").value, conc_unit: $("fConcUnit").value,
      vol_unit: $("fVolUnit").value,
    }});
    renderAll(); await refreshCalList(S.aid);
  } catch (e) { alert("保存失败: " + e.message); }
};

$("btnDeleteCal").onclick = async () => {
  if (!confirm("删除该校准及其全部模型版本?")) return;
  await api(`/api/calibrations/${S.cid}/delete`, { json: {} });
  S.cid = null;
  $("workspace").hidden = true; $("calExports").hidden = true;
  await refreshCalList(S.aid);
};

$("btnSuggest").onclick = () => {
  let n = 0;
  for (const l of S.st.lanes) {
    if (!l.role || !l.annotation) continue;
    const a = l.annotation;
    if (a.suggestion && a.peak_id !== a.suggestion.peak_id &&
        !(l.role === "standard" && a.excluded)) {
      a.peak_id = a.suggestion.peak_id; n++;
    }
  }
  if (!n) { alert("没有需要自动绑定的标注泳道(已绑定或目标 Rf 附近无候选)"); return; }
  scheduleSave();
};

$("btnFit").onclick = async () => {
  try {
    const r = await api(`/api/calibrations/${S.cid}/fit`, { json: payload() });
    S.st = r.state;
    renderAll();
    alert(`模型 v${r.version} 已保存。`);
  } catch (e) { alert("不能成线: " + e.message); }
};

function renderVersions() {
  const body = $("versionBody");
  const vers = S.st.versions.slice().reverse();
  if (!vers.length) {
    body.innerHTML = '<p class="dim">尚无已保存版本。模型成线后点“保存当前模型为新版本”。</p>';
    return;
  }
  body.innerHTML = "";
  const tbl = document.createElement("table");
  tbl.innerHTML = `<thead><tr><th>版本</th><th>模型</th><th>方程</th><th>R²</th><th>保存时间</th>
    <th>来源状态</th><th>操作</th></tr></thead><tbody>` +
    vers.map(v => {
      const f = v.fit;
      const eq = `${fmt(f.slope, 5)}·x${f.intercept ? ` ${f.intercept >= 0 ? "+" : "−"} ${fmt(Math.abs(f.intercept), 3)}` : ""}`;
      return `<tr${v.is_current ? ' style="background:#2d3a4d"' : ""}>
        <td>v${v.version}${v.is_current ? " (当前)" : ""}</td>
        <td>${S.st.models.find(m => m.kind === v.model)?.label || v.model}</td>
        <td>${eq}</td><td>${fmt(f.r2, 4)}</td><td>${v.created_at.replace("T", " ").replace("+00:00", "Z")}</td>
        <td>${v.stale ? '<span style="color:var(--warn)">已过期(来源变更,仍可查看/导出)</span>'
                      : '<span style="color:#5c6">与当前来源一致</span>'}</td>
        <td style="text-align:left">
          <button data-vid="${v.id}" class="vView">查看</button>
          <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
             href="/api/calibrations/${S.cid}/versions/${v.id}/samples.csv">CSV</a>
          <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
             href="/api/calibrations/${S.cid}/versions/${v.id}/model.json">JSON</a>
          <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
             href="/api/calibrations/${S.cid}/versions/${v.id}/figure.png">校准图</a>
        </td></tr>`;
    }).join("") + "</tbody>";
  body.appendChild(tbl);
  tbl.querySelectorAll(".vView").forEach(b => b.onclick = () => viewVersion(+b.dataset.vid));
}

async function viewVersion(vid) {
  const r = await api(`/api/calibrations/${S.cid}/versions/${vid}`);
  const s = r.snapshot, v = r.version;
  const dlg = document.createElement("dialog");
  dlg.style.maxWidth = "900px"; dlg.style.width = "92%";
  const ptsRows = s.points.map(p => `<tr>
      <td>${esc(p.lane_label)}</td><td>${p.peak_id}</td><td>${fmt(p.rf, 3)}</td>
      <td>${fmt(p.concentration, 4)}</td><td>${fmt(p.volume, 3)}</td>
      <td>${fmt(p.amount, 4)}</td><td>${fmt(p.response, 1)}</td>
      <td>${fmt(p.y0, 1)}~${fmt(p.y1, 1)}</td><td>${fmt(p.residual, 1)}</td></tr>`).join("");
  const exRows = s.excluded_points.map(p => `<tr>
      <td>${esc(p.lane_label)}</td><td>${p.peak_id ?? "—"}</td>
      <td>${fmt(p.concentration, 4)}</td><td>${esc(p.exclude_reason)}</td></tr>`).join("");
  const smpRows = s.samples.map(q => `<tr>
      <td>${esc(q.lane_label)}</td><td>${q.peak_id ?? "—"}</td>
      <td>${fmt(q.response, 1)}</td><td>${fmt(q.volume, 3)}</td><td>${fmt(q.dilution, 3)}</td>
      <td>${fmt(q.amount_back, 4)}</td><td>${fmt(q.sample_concentration, 4)}</td>
      <td>${q.in_range == null ? "—" : q.in_range ? "是" : "否"}</td>
      <td>${esc(q.status)}</td></tr>`).join("");
  dlg.innerHTML = `<h3>模型 v${v.version} ${r.stale
      ? '<span style="color:var(--warn)">— 已过期:来源几何/积分/斑点已变更(以下为成线时留档)</span>'
      : ""}</h3>
    <div class="kv">
模型 ${v.model} · 保存于 ${v.created_at}
板 #${s.analysis_id} ${esc(s.analysis_name)} · 几何 v${s.geometry_version} · 图像 sha1=${s.image_sha1.slice(0, 10)}…
目标 ${esc(s.target.name)} Rf=${s.target.rf ?? "—"}±${s.target.rf_tol} · 单位 ${s.units.conc}/${s.units.vol}
b=${v.fit.slope} a=${v.fit.intercept} R²=${v.fit.r2} · 工作范围 [${s.range?.[0]}, ${s.range?.[1]}]
来源指纹 ${v.source_fp.slice(0, 12)}… ${r.stale ? "(≠ 当前 " + r.current_fp.slice(0, 12) + "…)" : "(= 当前)"}
    </div>
    <h4>纳入标准点(含几何与积分边界、斑点 ID)</h4>
    <table><thead><tr><th style="text-align:left">泳道</th><th>峰ID</th><th>Rf</th>
      <th>浓度(${s.units.conc})</th><th>体积(${s.units.vol})</th><th>点样量</th>
      <th>面积</th><th>积分边界 y0~y1</th><th>残差</th></tr></thead><tbody>${ptsRows}</tbody></table>
    ${exRows ? `<h4>排除点</h4><table><thead><tr><th style="text-align:left">泳道</th>
      <th>峰ID</th><th>浓度</th><th style="text-align:left">理由</th></tr></thead><tbody>${exRows}</tbody></table>` : ""}
    <h4>未知样反算</h4>
    <table><thead><tr><th style="text-align:left">泳道</th><th>峰ID</th><th>面积</th>
      <th>体积</th><th>稀释</th><th>点样量</th><th>原样品浓度(${s.units.conc})</th>
      <th>范围内</th><th style="text-align:left">状态</th></tr></thead><tbody>${smpRows}</tbody></table>
    <form method="dialog" style="margin-top:10px"><button>关闭</button></form>`;
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener("close", () => dlg.remove());
}

// ---------------- 总渲染 ----------------
function renderAll() {
  if (!S.st) return;
  if (S.st.no_geometry) {
    $("hint").textContent = "该板尚未完成几何标定,不能建立校准。";
    return;
  }
  renderSidebar();
  renderLaneTable();
  drawPlate();
  drawCalibration();
  renderModel();
  renderSamples();
  renderVersions();
}

window.addEventListener("resize", () => { drawPlate(); drawCalibration(); });

// 初始化
refreshAnalyses().then(() => refreshCalList(null));
})();
