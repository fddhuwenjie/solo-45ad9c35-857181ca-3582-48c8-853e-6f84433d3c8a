/* 显色时间序列校审前端:
   编排帧序列 / 登记时刻 / 控制点配准与微调 / 排除坏帧;
   时间滑块联动帧间叠加图、泳道密度曲线与逐斑点响应曲线;
   定位阻断问题(帧+斑点),无阻断时确认取值窗口。计算全部在 Python 端。 */
(() => {
"use strict";

const S = {
  aid: null, sid: null, st: null,
  fi: 0,                       // 当前帧索引(frames_out)
  selLane: null, selPeak: null,
  refImg: null, frameImgs: {}, // fid -> HTMLImage(rectified)
  refProfiles: {},             // lane_id -> 参考板密度曲线(几何不变,缓存)
  origImg: null,              // 配准对话框原图
  reg: { fid: null, cps: [], drag: -1 },
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (x, n = 1) => (x == null || isNaN(x)) ? "—" : (+Number(x).toFixed(n)).toString();
const mmss = (sec) => {
  if (sec == null) return "—";
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.round(sec % 60);
  return (h ? String(h).padStart(2, "0") + ":" : "") +
    String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
};

// 与 tlc.kinetics.BLOCKERS 对应的硬阻断类型
const HARD = new Set(["no_frames", "few_frames", "duplicate_time", "time_reversed",
  "start_missing", "time_invalid", "reg_insufficient", "reg_unsolvable", "reg_residual",
  "saturated", "spot_drift"]);

async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.body = JSON.stringify(opts.json);
    opts.headers = { "Content-Type": "application/json" };
    delete opts.json;
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).description || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ---------------- 板 / 序列列表 ----------------
async function refreshAnalyses() {
  const list = await api("/api/analyses");
  const states = await Promise.all(list.map(a =>
    api(`/api/analyses/${a.id}`).then(st => ({ a, st })).catch(() => null)));
  S.plates = (states || []).filter(x => x && x.st.geometry &&
    x.st.lanes.some(l => l.peaks.length > 0))
    .map(x => ({ id: x.a.id, name: x.a.name }));
  const opts = S.plates.length
    ? '<option value="">选择已标定的板…</option>' +
      S.plates.map(p => `<option value="${p.id}">#${p.id} ${esc(p.name)}</option>`).join("")
    : '<option value="">(没有已标定几何/积分的板)</option>';
  $("newAnalysis").innerHTML = opts;
  $("kinAnalysis").innerHTML = opts;
}

async function refreshSeries(aid, selectSid) {
  const sel = $("kinSelect");
  if (!aid) { sel.innerHTML = '<option value="">打开序列…</option>'; return; }
  const list = await api(`/api/analyses/${aid}/kinetics`);
  sel.innerHTML = '<option value="">打开序列…</option>' +
    list.map(k => `<option value="${k.id}">#${k.id} ${esc(k.name || "未命名")}` +
      `(${k.n_frames}帧/${k.n_version}版)</option>`).join("");
  if (selectSid) sel.value = selectSid;
}

$("newAnalysis").onchange = $("kinAnalysis").onchange = async (e) => {
  S.aid = +e.target.value || null;
  S.sid = null;
  $("workspace").hidden = true; $("kinExports").hidden = true;
  await refreshSeries(S.aid);
};
$("kinSelect").onchange = (e) => { if (e.target.value) openSeries(+e.target.value); };

$("btnNew").onclick = async () => {
  const aid = +$("newAnalysis").value;
  const name = $("newName").value.trim();
  if (!aid) { alert("请先选择一块已标定几何/积分边界的板"); return; }
  try {
    const st = await api(`/api/analyses/${aid}/kinetics`, {
      json: { name: name || "显色序列", start_at: $("newStart").value.trim() } });
    S.aid = aid;
    await refreshSeries(aid, st.series.id);
    await openSeries(st.series.id);
  } catch (e) { alert("创建失败: " + e.message); }
};

// ---------------- 打开 / 重载 ----------------
async function openSeries(sid) {
  S.sid = sid; S.fi = 0; S.selLane = null; S.selPeak = null; S.frameImgs = {};
  await reload();
  $("emptyState").hidden = true;
  $("workspace").hidden = false;
}

async function reload(keepFrame) {
  S.st = await api(`/api/kinetics/${S.sid}`);
  S.aid = S.st.analysis.id;
  S.refProfiles = {};
  if (!keepFrame) S.fi = Math.min(S.fi, Math.max(0, S.st.frames.length - 1));
  await Promise.all([loadRef(), ...S.st.frames.map(f => loadFrameImg(f.id))]);
  renderAll();
  const ver = S.st.series.current_version;
  $("kinExports").hidden = !ver;
  if (ver) {
    for (const [k, tail] of [["expFrames", "frames.csv"], ["expRecompute", "recompute.json"],
                             ["expFig", "figure.png"]])
      $(k).href = `/api/kinetics/versions/${ver}/${tail}`;
  }
}

function loadRef() {
  return new Promise((res) => {
    const gv = S.st.geometry && S.st.geometry.version;
    if (S.refImg && S.refGeom === gv) return res();
    if (!gv) { S.refImg = null; return res(); }
    const im = new Image();
    im.onload = () => { S.refImg = im; S.refGeom = gv; res(); };
    im.onerror = () => { S.refImg = null; res(); };
    im.src = `/api/analyses/${S.aid}/preview/corrected?t=${gv}`;
  });
}

function frameSig(f) {
  return [f.dx, f.dy, JSON.stringify(f.control_points)].join("|");
}

function loadFrameImg(fid) {
  const f = S.st.frames.find(x => x.id === fid);
  if (!f) return Promise.resolve();
  const sig = frameSig(f);
  const cached = S.frameImgs[fid];
  if (cached && cached.sig === sig) return Promise.resolve();
  return new Promise((res) => {
    const im = new Image();
    im.onload = () => { S.frameImgs[fid] = { img: im, sig, ok: true }; res(); };
    im.onerror = () => { S.frameImgs[fid] = { img: null, sig, ok: false }; res(); };
    im.src = `/api/kinetics/frames/${fid}/rectified.png?sig=${encodeURIComponent(sig)}`;
  });
}

const curFrame = () => S.st.frames[S.fi] || null;
const laneById = (lid) => S.st.lanes.find(l => l.id === lid);
const curveByPeak = (pid) => S.st.curves[String(pid)] || S.st.curves[pid];
const peakLane = {}; // pid -> lane_id(从 curves 填)

// ---------------- 帧登记表 ----------------
function renderFrameTable() {
  const tb = document.querySelector("#frameTable tbody");
  tb.innerHTML = "";
  S.st.frames.forEach((f, i) => {
    const tr = document.createElement("tr");
    if (i === S.fi) tr.classList.add("selected");
    const status = frameStatus(f);
    const ta = document.createElement("input");
    ta.type = "text"; ta.value = f.taken_at; ta.style.width = "100px";
    ta.title = "HH:MM:SS(相对显色开始)或显色后秒数";
    ta.onchange = () => postFrame(f.id, { taken_at: ta.value }, "时刻无效").catch(e => {
      alert(e.message); ta.value = f.taken_at;
    });

    const ck = document.createElement("input");
    ck.type = "checkbox"; ck.checked = !!f.excluded;
    ck.title = "排除坏帧(必须填写理由)";
    const rs = document.createElement("input");
    rs.type = "text"; rs.value = f.exclude_reason || "";
    rs.placeholder = "排除理由";
    ck.onchange = async () => {
      if (ck.checked && !rs.value.trim()) {
        rs.focus(); alert("排除坏帧必须填写理由"); ck.checked = false; return;
      }
      try {
        await postFrame(f.id, { excluded: ck.checked, exclude_reason: rs.value });
      } catch (e) { alert(e.message); }
    };
    rs.onchange = () => {
      if (f.excluded) postFrame(f.id, { exclude_reason: rs.value }).catch(e => alert(e.message));
    };
    const exWrap = document.createElement("div");
    exWrap.className = "row wrap"; exWrap.style.margin = "0";
    exWrap.append(ck, rs);

    const btnReg = document.createElement("button");
    btnReg.textContent = "配准";
    btnReg.onclick = () => openRegDialog(f.id);
    const btnDel = document.createElement("button");
    btnDel.textContent = "删除";
    btnDel.onclick = async () => {
      if (!confirm("删除该帧照片?")) return;
      await api(`/api/kinetics/frames/${f.id}/delete`, { json: {} });
      await reload();
    };

    tr.innerHTML =
      `<td style="cursor:pointer">${f.seq + 1}</td>` +
      `<td><a href="/api/kinetics/frames/${f.id}/image" target="_blank">原图</a></td>`;
    tr.append(cell(ta), `<td>${mmss(f.t_sec)}</td>`,
      `<td>${f.error ? "—" : fmt(f.residual, 2)}</td>`,
      cellStatus(status), cell(exWrap), cell(btnReg), cell(btnDel));
    tr.firstElementChild.style.cursor = "pointer";
    tr.firstElementChild.onclick = () => { S.fi = i; renderAll(); };
    tb.appendChild(tr);
  });
}

function frameStatus(f) {
  if (f.excluded) return { kind: "excluded", text: `已排除:${f.exclude_reason}`, color: "var(--dim)" };
  if (f.error) return { kind: "error", text: f.error.message, color: "var(--err)" };
  const sat = (f.spots || []).filter(s => s.saturated_px >= 4);
  const drift = (f.spots || []).filter(s => (s.drift_px ?? 0) > S.st.defaults.drift_centroid_px);
  if (sat.length && drift.length)
    return { kind: "bad", text: `饱和${sat.length}斑 · 漂移${drift.length}斑`, color: "var(--err)", peaks: uniq(sat.concat(drift).map(s => s.peak_id)) };
  if (sat.length)
    return { kind: "saturated", text: `像素饱和(${sat.length}斑)`, color: "var(--warn)", peaks: sat.map(s => s.peak_id) };
  if (drift.length)
    return { kind: "drift", text: `斑点漂移(${drift.length}斑)`, color: "var(--warn)", peaks: drift.map(s => s.peak_id) };
  return { kind: "ok", text: "✔ 有效", color: "#5c6" };
}
const uniq = (a) => [...new Set(a)];
function cell(v) {
  const td = document.createElement("td");
  if (v instanceof Node) td.appendChild(v);
  else td.innerHTML = v;
  return td;
}
function cellStatus(stt) {
  const td = document.createElement("td");
  td.style.color = stt.color;
  td.textContent = stt.text;
  if (stt.peaks) td.title = "峰 " + stt.peaks.join(", ");
  return td;
}

async function postFrame(fid, json, errHint) {
  const st2 = await api(`/api/kinetics/frames/${fid}`, { json });
  S.st = st2;
  await Promise.all(S.st.frames.map(x => loadFrameImg(x.id)));
  renderAll();
}

// ---------------- 滑块 / 时刻刻度 ----------------
function renderSlider() {
  const n = S.st.frames.length;
  const sl = $("timeSlider");
  sl.max = Math.max(0, n - 1); sl.value = S.fi;
  sl.disabled = n === 0;
  const f = curFrame();
  $("frameClock").textContent = f
    ? `帧 ${S.fi + 1}/${n} · t=${mmss(f.t_sec)}${f.taken_at ? " · " + esc(f.taken_at) : ""}` +
      (f.excluded ? " · 已排除" : f.error ? " · 配准失败" : "")
    : "尚无帧";
  const ticks = $("frameTicks");
  ticks.innerHTML = "";
  S.st.frames.forEach((fr, i) => {
    const sp = document.createElement("span");
    sp.textContent = mmss(fr.t_sec);
    if (i === S.fi) sp.classList.add("cur");
    if (fr.excluded) sp.classList.add("excluded");
    else if (fr.error) sp.classList.add("bad");
    sp.onclick = () => { S.fi = i; renderAll(); };
    ticks.appendChild(sp);
  });
}

$("timeSlider").oninput = (e) => { S.fi = +e.target.value; renderAll(); };
$("chkOverlay").onchange = $("chkRaw").onchange = () => renderCharts();

// ---------------- 叠加图 ----------------
function drawOverlay() {
  const cv = $("overlayCv"), ctx = cv.getContext("2d");
  const g = S.st.geometry;
  if (!g || !S.refImg) {
    cv.width = 600; cv.height = 60;
    ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = "#9aa3ad"; ctx.font = "13px sans-serif";
    ctx.fillText("该板尚未完成几何标定,无法叠加。", 12, 34);
    return;
  }
  const d = g.derived;
  const W = d.width, H = d.height;
  cv.width = W; cv.height = H;
  ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, W, H);
  if ($("chkOverlay").checked && S.refImg)
    ctx.globalAlpha = 0.45, ctx.drawImage(S.refImg, 0, 0, W, H), ctx.globalAlpha = 1;
  const f = curFrame();
  const im = f ? S.frameImgs[f.id] : null;
  if (im && im.ok && im.img) ctx.drawImage(im.img, 0, 0, W, H);
  else if (f && f.error) {
    ctx.fillStyle = "rgba(229,83,75,.9)"; ctx.font = "bold 14px sans-serif";
    ctx.fillText("配准失败:" + f.error.message, 12, 30);
  }
  // 冻结泳道 / 积分边界 / 质心
  const curSpots = {};
  if (f && f.spots) for (const s of f.spots) curSpots[s.peak_id] = s;
  for (const l of S.st.lanes) {
    const sel = S.selLane === l.id;
    ctx.strokeStyle = sel ? "#4f9cf9" : "rgba(255,255,255,.55)";
    ctx.lineWidth = sel ? 2 : 1;
    ctx.strokeRect(l.x0, d.front_y, l.x1 - l.x0, d.baseline_y - d.front_y);
    ctx.fillStyle = sel ? "#4f9cf9" : "rgba(255,255,255,.7)";
    ctx.font = "11px sans-serif";
    ctx.fillText(l.label || "L" + l.id, l.x0 + 2, d.front_y + 12);
    for (const p of l.peaks) {
      ctx.strokeStyle = S.selPeak === p.id ? "#2ecc71" : "rgba(255,200,60,.85)";
      ctx.lineWidth = S.selPeak === p.id ? 2 : 1;
      ctx.strokeRect(l.x0, p.y0, l.x1 - l.x0, p.y1 - p.y0);
      const s = curSpots[p.id];
      if (s) {
        const bad = s.saturated_px >= 4 || (s.drift_px ?? 0) > S.st.defaults.drift_centroid_px;
        const cx = (l.x0 + l.x1) / 2, cy = s.center_y;
        ctx.strokeStyle = bad ? "#e5534b" : "#2ecc71"; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(cx, cy, bad ? 8 : 6, 0, 7); ctx.stroke();
        if (S.selPeak === p.id) {
          ctx.fillStyle = "#2ecc71";
          ctx.fillText(`峰${p.id} A=${fmt(s.area, 0)}`, cx + 9, cy - 6);
        }
      }
    }
  }
  // 基线/前沿
  ctx.strokeStyle = "rgba(46,204,113,.9)";
  ctx.beginPath(); ctx.moveTo(0, d.baseline_y); ctx.lineTo(W, d.baseline_y); ctx.stroke();
  ctx.strokeStyle = "rgba(229,83,75,.9)";
  ctx.beginPath(); ctx.moveTo(0, d.front_y); ctx.lineTo(W, d.front_y); ctx.stroke();
}

$("overlayCv").addEventListener("click", (e) => {
  const g = S.st.geometry; if (!g) return;
  const r = e.target.getBoundingClientRect();
  const x = (e.clientX - r.left) * (g.derived.width / r.width);
  const hit = S.st.lanes.find(l => x >= l.x0 && x <= l.x1);
  if (hit) { S.selLane = hit.id; renderAll(); }
});

// ---------------- 泳道选择 / 密度曲线 ----------------
function renderLanePick() {
  const box = $("lanePick");
  box.innerHTML = "";
  S.st.lanes.forEach(l => {
    const b = document.createElement("button");
    b.textContent = l.label || ("L" + l.id);
    if (S.selLane === l.id) b.classList.add("active");
    b.onclick = () => { S.selLane = S.selLane === l.id ? null : l.id; renderAll(); };
    box.appendChild(b);
  });
  if (S.selLane == null && S.st.lanes.length) S.selLane = S.st.lanes[0].id;
}

async function drawDensity() {
  const cv = $("densityCv"), ctx = cv.getContext("2d");
  const W0 = cv.parentElement.clientWidth - 4, Hh = 260;
  cv.width = W0; cv.height = Hh;
  ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, W0, Hh);
  const lane = laneById(S.selLane);
  if (!lane) { ctx.fillStyle = "#9aa3ad"; ctx.fillText("请选择泳道", 12, 20); return; }
  const d = S.st.geometry.derived, H = d.height;
  const M = { l: 46, r: 14, t: 14, b: 22 };
  // 参考曲线(几何/边界不变时缓存)
  let refProf = S.refProfiles[lane.id];
  if (refProf === undefined) {
    try {
      const j = await api(`/api/analyses/${S.aid}/lanes/${lane.id}/profile`);
      refProf = j.profile;
    } catch (e) { refProf = null; }
    S.refProfiles[lane.id] = refProf;
  }
  const f = curFrame();
  const cur = f && f.profiles ? f.profiles[String(lane.id)] : null;
  const allMax = Math.max(
    ...(refProf || [0]), ...(cur || []), 1);
  const X = (y) => M.l + y / H * (W0 - M.l - M.r);
  const Y = (v) => Hh - M.b - v / (allMax * 1.05) * (Hh - M.t - M.b);
  ctx.strokeStyle = "#3a4048"; ctx.fillStyle = "#778"; ctx.font = "10px sans-serif";
  ctx.beginPath(); ctx.moveTo(M.l, M.t); ctx.lineTo(M.l, Hh - M.b);
  ctx.lineTo(W0 - M.r, Hh - M.b); ctx.stroke();
  // 积分边界阴影
  for (const p of lane.peaks) {
    ctx.fillStyle = S.selPeak === p.id ? "rgba(46,204,113,.18)" : "rgba(255,200,60,.10)";
    ctx.fillRect(X(p.y0), M.t, X(p.y1) - X(p.y0), Hh - M.t - M.b);
    ctx.strokeStyle = "rgba(255,200,60,.5)";
    ctx.beginPath(); ctx.moveTo(X(p.y0), M.t); ctx.lineTo(X(p.y0), Hh - M.b);
    ctx.moveTo(X(p.y1), M.t); ctx.lineTo(X(p.y1), Hh - M.b); ctx.stroke();
  }
  ctx.strokeStyle = "rgba(46,204,113,.9)";
  ctx.beginPath(); ctx.moveTo(X(d.baseline_y), M.t); ctx.lineTo(X(d.baseline_y), Hh - M.b); ctx.stroke();
  ctx.strokeStyle = "rgba(229,83,75,.9)";
  ctx.beginPath(); ctx.moveTo(X(d.front_y), M.t); ctx.lineTo(X(d.front_y), Hh - M.b); ctx.stroke();
  const line = (prof, color, dash) => {
    if (!prof) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.setLineDash(dash);
    ctx.beginPath();
    prof.forEach((v, i) => {
      const y = i / prof.length * H;
      const xx = X(y), yy = Y(v);
      i ? ctx.lineTo(xx, yy) : ctx.moveTo(xx, yy);
    });
    ctx.stroke(); ctx.setLineDash([]);
  };
  line(refProf, "rgba(154,163,173,.9)", [5, 4]);
  line(cur, "#4f9cf9", []);
  ctx.fillStyle = "#9aa3ad"; ctx.font = "10px sans-serif";
  ctx.fillText("虚线=参考板  实线=当前帧  绿=基线 红=前沿", M.l, Hh - 6);
}

// ---------------- 逐斑点响应曲线 ----------------
function renderResponse() {
  const cv = $("responseCv"), ctx = cv.getContext("2d");
  const W0 = cv.parentElement.clientWidth - 4, Hh = 300;
  cv.width = W0; cv.height = Hh;
  ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, W0, Hh);
  const curves = Object.values(S.st.curves);
  curves.forEach(cv2 => { peakLane[cv2.peak_id] = cv2.lane_id; });
  const M = { l: 50, r: 16, t: 16, b: 28 };
  if (!curves.length || !curves.some(c => c.times.length)) {
    ctx.fillStyle = "#9aa3ad"; ctx.font = "13px sans-serif";
    ctx.fillText("尚无可绘制的有效帧曲线(检查配准/时刻/排除)。", M.l, 40);
    return;
  }
  const raw = $("chkRaw").checked;
  const allT = curves.flatMap(c => c.times);
  let tmax = Math.max(...allT, 1);
  const curT = curFrame() ? curFrame().t_sec : null;
  const win = clientWindow();
  const ymax = () => raw ? Math.max(...curves.flatMap(c => c.areas), 1) * 1.1 : 1.12;
  const X = (t) => M.l + t / tmax * (W0 - M.l - M.r);
  const Y = (v) => Hh - M.b - v / ymax() * (Hh - M.t - M.b);
  // 平台带(浅绿,各自)
  curves.forEach((c, ci) => {
    if (!c.plateau) return;
    const [i0, i1] = c.plateau;
    ctx.fillStyle = "rgba(46,204,113,.10)";
    ctx.fillRect(X(c.times[i0]), M.t, X(c.times[i1]) - X(c.times[i0]), Hh - M.t - M.b);
  });
  // 网格
  ctx.strokeStyle = "#2a2f37"; ctx.fillStyle = "#778"; ctx.font = "10px sans-serif";
  for (let i = 0; i <= 4; i++) {
    const gx = M.l + i * (W0 - M.l - M.r) / 4;
    ctx.beginPath(); ctx.moveTo(gx, M.t); ctx.lineTo(gx, Hh - M.b); ctx.stroke();
    ctx.fillText(mmss(tmax * i / 4), gx - 12, Hh - M.b + 14);
  }
  // 取值窗口蓝带
  if (win) {
    ctx.fillStyle = "rgba(79,156,249,.18)";
    ctx.fillRect(X(win.t0), M.t, X(win.t1) - X(win.t0), Hh - M.t - M.b);
    ctx.strokeStyle = "rgba(79,156,249,.9)";
    ctx.strokeRect(X(win.t0), M.t, X(win.t1) - X(win.t0), Hh - M.t - M.b);
  }
  const palette = ["#3182bd", "#e67e22", "#2ecc71", "#a55eea", "#e84393",
                   "#00cec9", "#fdcb6e", "#e17055", "#74b9ff", "#55efc4"];
  curves.forEach((c, ci) => {
    const col = palette[ci % palette.length];
    const emph = S.selPeak == null || S.selPeak === c.peak_id;
    const vs = raw ? c.areas : c.normalized;
    ctx.strokeStyle = col; ctx.globalAlpha = emph ? 1 : .25; ctx.lineWidth = S.selPeak === c.peak_id ? 2.6 : 1.5;
    ctx.beginPath();
    c.times.forEach((t, i) => i ? ctx.lineTo(X(t), Y(vs[i])) : ctx.moveTo(X(t), Y(vs[i])));
    ctx.stroke();
    if (c.peak) {
      const pv = raw ? c.peak.value : Math.max(...c.normalized);
      ctx.fillStyle = "#e5534b";
      ctx.beginPath(); ctx.arc(X(c.peak.t), Y(pv), 3.5, 0, 7); ctx.fill();
    }
    ctx.globalAlpha = 1;
  });
  if (curT != null) {
    ctx.strokeStyle = "#fff"; ctx.setLineDash([4, 3]);
    ctx.beginPath(); ctx.moveTo(X(curT), M.t); ctx.lineTo(X(curT), Hh - M.b); ctx.stroke();
    ctx.setLineDash([]);
  }
  ctx.fillStyle = "#9aa3ad";
  ctx.fillText(raw ? "原始面积" : "归一化响应(各斑峰值=1) · 绿带=稳定平台 · 蓝带=取值窗口", M.l, Hh - 8);
  // 图例(可点击定位斑点)
  let lx = M.l, ly = M.t + 2;
  ctx.font = "11px sans-serif";
  cv._hits = [];
  curves.forEach((c, ci) => {
    const col = palette[ci % palette.length];
    const label = `${c.lane_label || "L" + c.lane_id}-峰${c.peak_id}`;
    const w = ctx.measureText(label).width + 18;
    if (lx + w > W0 - M.r) { lx = M.l; ly += 16; }
    ctx.fillStyle = col; ctx.fillRect(lx, ly - 8, 10, 10);
    ctx.fillStyle = S.selPeak === c.peak_id ? "#fff" : "#c9ced6";
    ctx.fillText(label, lx + 14, ly + 1);
    cv._hits.push({ x: lx, y: ly - 9, w, peak: c.peak_id });
    lx += w + 10;
  });
}

$("responseCv").addEventListener("click", (e) => {
  const cv = e.target, r = cv.getBoundingClientRect();
  const x = (e.clientX - r.left) * (cv.width / r.width);
  const y = (e.clientY - r.top) * (cv.height / r.height);
  for (const h of (cv._hits || []))
    if (x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + 14) {
      S.selPeak = S.selPeak === h.peak ? null : h.peak;
      const lid = peakLane[h.peak];
      if (lid) S.selLane = lid;
      renderAll();
      return;
    }
});

// ---------------- 窗口 ----------------
function parseWindow(str) {
  str = (str || "").trim();
  if (!str) return null;
  if (str.includes(":")) {
    const p = str.split(":").map(Number);
    if (p.some(isNaN)) return NaN;
    return p.reduce((a, b) => a * 60 + b, 0);  // mm:ss 或 hh:mm:ss(相对秒)
  }
  const v = Number(str);
  return isNaN(v) ? NaN : v;
}

function clientWindow() {
  // 优先用输入框(用户正在拖),其次服务端草稿
  const t0 = parseWindow($("winT0").value);
  const t1 = parseWindow($("winT1").value);
  if (t0 != null && t1 != null && !isNaN(t0) && !isNaN(t1)) return { t0, t1 };
  const w = S.st.window;
  return (w.t0 != null && w.t1 != null) ? { t0: w.t0, t1: w.t1 } : null;
}

// 镜像后端 validate_window,用于在界面定位帧+斑点
function clientWindowIssues(win) {
  const cfg = S.st.defaults, out = [];
  if (!win) return out;
  const valid = S.st.frames.filter(f => !f.excluded && !f.error && f.t_sec != null);
  if (win.t1 <= win.t0)
    return [{ type: "window_invalid", message: "取值窗口结束时刻须晚于开始时刻" }];
  const inWin = valid.filter(f => f.t_sec >= win.t0 - 1e-9 && f.t_sec <= win.t1 + 1e-9);
  if (inWin.length < cfg.min_valid_frames)
    out.push({ type: "window_invalid", frame_ids: inWin.map(f => f.id),
               message: `取值窗口内有效帧仅 ${inWin.length} 个,不足 ${cfg.min_valid_frames} 个` });
  Object.values(S.st.curves).forEach(c => {
    const pts = c.times.map((t, i) => ({ t, v: c.areas[i] }))
      .filter(p => p.t >= win.t0 - 1e-9 && p.t <= win.t1 + 1e-9);
    if (!pts.length) {
      out.push({ type: "window_invalid", peak_id: c.peak_id,
                 message: `峰 ${c.peak_id} 在窗口内没有有效面积数据` });
      return;
    }
    if (!c.plateau) {
      out.push({ type: "window_invalid", peak_id: c.peak_id,
                 message: `峰 ${c.peak_id} 未识别出满足容差的稳定平台` });
      return;
    }
    const mean = pts.reduce((a, p) => a + p.v, 0) / pts.length;
    const off = pts.find(p => mean > 0 && Math.abs(p.v - mean) / mean > cfg.plateau_tol);
    if (off) {
      const fr = valid.find(f => f.t_sec === off.t);
      out.push({ type: "window_invalid", peak_id: c.peak_id,
                 frame_ids: fr ? [fr.id] : [],
                 message: `峰 ${c.peak_id} 窗口跨过非平台帧(t=${mmss(off.t)})` });
    }
    inWin.forEach(f => {
      const s = (f.spots || []).find(x => x.peak_id === c.peak_id);
      if (!s) return;
      if (s.saturated_px >= 4)
        out.push({ type: "window_saturated", peak_id: c.peak_id, frame_ids: [f.id],
                   message: `帧 ${f.seq + 1} 峰 ${c.peak_id} 像素饱和` });
      if ((s.drift_px ?? 0) > cfg.drift_centroid_px)
        out.push({ type: "window_drift", peak_id: c.peak_id, frame_ids: [f.id],
                   message: `帧 ${f.seq + 1} 峰 ${c.peak_id} 质心漂移 ${fmt(s.drift_px)}px` });
    });
  });
  return out;
}

function renderWindowPanel() {
  const w = S.st.window;
  if (document.activeElement !== $("winT0") && document.activeElement !== $("winT1")) {
    $("winT0").placeholder = w.t0 != null ? mmss(w.t0) : "mm:ss";
    $("winT1").placeholder = w.t1 != null ? mmss(w.t1) : "mm:ss";
  }
  const win = clientWindow();
  const issues = win ? clientWindowIssues(win) : [];
  const sug = S.st.suggest;
  $("btnSuggest").disabled = !sug;
  $("btnSuggest").title = sug ? `建议 [${mmss(sug.t0)}, ${mmss(sug.t1)}]` : "没有可求交的稳定平台";
  // 统计
  let html = "";
  if (win) {
    const rows = Object.values(S.st.curves).map(c => {
      const sel = c.times.map((t, i) => ({ t, v: c.areas[i] }))
        .filter(p => p.t >= win.t0 - 1e-9 && p.t <= win.t1 + 1e-9);
      if (!sel.length) return "";
      const n = sel.length, mean = sel.reduce((a, p) => a + p.v, 0) / n;
      const sd = n > 1 ? Math.sqrt(sel.reduce((a, p) => a + (p.v - mean) ** 2, 0) / (n - 1)) : 0;
      const cv = mean > 0 ? 100 * sd / mean : null;
      return `<tr><td style="text-align:left">${esc(c.lane_label)} 峰${c.peak_id}</td>` +
        `<td>${n}</td><td>${fmt(mean, 1)}</td><td>${fmt(sd, 1)}</td><td>${fmt(cv, 2)}%</td></tr>`;
    }).join("");
    html = `<table><thead><tr><th style="text-align:left">斑点</th><th>帧数</th>` +
      `<th>窗口均值</th><th>SD</th><th>CV</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  $("windowStats").innerHTML = html;
  $("windowHint").innerHTML = issues.length
    ? issues.map(i => `✕ ${esc(i.message)}` +
        (i.frame_ids && i.frame_ids.length
          ? ` <a data-fid="${i.frame_ids[0]}" data-pid="${i.peak_id || ""}" class="loc">[定位]</a>` : "")).join("<br>")
    : (win ? '<span style="color:#5c6">窗口内斑点均处于稳定平台,可确认。</span>' : "拖滑块查看各帧,采用建议平台窗口或手填起止时刻。");
  $("windowHint").querySelectorAll("a.loc").forEach(a => a.onclick = () =>
    locate(+a.dataset.fid, a.dataset.pid ? +a.dataset.pid : null));
  return issues;
}

$("winT0").oninput = $("winT1").oninput = () => { renderBlockers(); renderResponse(); renderWindowPanel(); };
$("btnSuggest").onclick = () => {
  const s = S.st.suggest;
  if (!s) return;
  $("winT0").value = mmss(s.t0); $("winT1").value = mmss(s.t1);
  renderAll();
};
$("btnConfirm").onclick = async () => {
  const win = clientWindow();
  if (!win) { alert("请先填写取值窗口起止时刻(或采用建议平台窗口)"); return; }
  try {
    const r = await api(`/api/kinetics/${S.sid}/confirm`, { json: win });
    await reload(true);
    S.fi = Math.min(S.fi, S.st.frames.length - 1);
    renderAll();
    alert(`取值窗口 v${r.version} 已确认:锁定所用帧、配准参数与窗口。`);
  } catch (e) {
    alert("不能确认取值窗口:\n" + e.message);
    await reload(true);   // 服务端会保留草稿窗口
    // 用客户端定位第一条窗口级问题
    const issues = clientWindowIssues(clientWindow());
    if (issues.length) locate(issues[0].frame_ids && issues[0].frame_ids[0],
                              issues[0].peak_id);
    renderAll();
  }
};

// ---------------- 阻断项 / 定位 ----------------
function renderBlockers() {
  const body = $("blockerBody");
  const seqHard = [], seqWarn = [];
  for (const i of S.st.issues)
    (HARD.has(i.kind) ? seqHard : seqWarn).push(i);
  let html = "";
  if (!seqHard.length)
    html += '<div class="seq-ok">✔ 序列级校审通过(时刻、配准、饱和、漂移、有效帧数)。</div>';
  html += seqHard.map(i => {
    const fid = i.frame_ids && i.frame_ids.length ? i.frame_ids[0] : null;
    return `<div class="flag error"><span class="msg">✕ ${esc(i.message)}</span>` +
      (fid ? ` <a class="loc" data-fid="${fid}">[定位帧]</a>` : "") + `</div>`;
  }).join("");
  const win = clientWindow();
  const wIssues = win ? clientWindowIssues(win) : [];
  html += wIssues.map(i => {
    const fid = i.frame_ids && i.frame_ids.length ? i.frame_ids[0] : null;
    return `<div class="flag error"><span class="msg">✕ 窗口:${esc(i.message)}</span>` +
      (fid ? ` <a class="loc" data-fid="${fid}" data-pid="${i.peak_id || ""}">[定位]</a>` : "") +
      `</div>`;
  }).join("");
  html += seqWarn.map(i => {
    const fid = i.frame_ids && i.frame_ids.length ? i.frame_ids[0] : null;
    return `<div class="flag"><span class="msg">⚠ ${esc(i.message)}</span>` +
      (fid ? ` <a class="loc" data-fid="${fid}">[定位帧]</a>` : "") + `</div>`;
  }).join("");
  body.innerHTML = html;
  body.querySelectorAll("a.loc").forEach(a => a.onclick = () =>
    locate(+a.dataset.fid, a.dataset.pid ? +a.dataset.pid : null));
  const blocked = seqHard.length + wIssues.length > 0;
  $("btnConfirm").disabled = blocked;
  $("hint").textContent =
    `帧 ${S.st.frames.length} · 有效 ${S.st.n_usable} · ` +
    (blocked ? `存在 ${seqHard.length + wIssues.length} 项阻断,不能确认取值窗口`
             : (win ? "无阻断,可确认取值窗口" : "请设定取值窗口"));
}

function locate(fid, pid) {
  if (fid != null) {
    const i = S.st.frames.findIndex(f => f.id === fid);
    if (i >= 0) S.fi = i;
  }
  if (pid != null) {
    S.selPeak = pid;
    const lid = peakLane[pid] || (curveByPeak(pid) || {}).lane_id;
    if (lid) S.selLane = lid;
  }
  renderAll();
  $("overlayWrap").scrollIntoView({ behavior: "smooth", block: "center" });
}

// ---------------- 侧栏 / 阈值 / 版本 ----------------
function renderSidebar() {
  const s = S.st.series, g = S.st.geometry, d = S.st.defaults;
  $("fName").value = s.name || "";
  $("fStart").value = s.start_at || "";
  $("kinBadge").textContent =
    `#${s.id} ${s.name || "显色序列"} · 几何 v${g ? g.version : "—"} · ${S.st.n_usable}/${S.st.frames.length} 有效帧`;
  $("metaHint").textContent =
    `板 #${S.st.analysis.id} ${S.st.analysis.name}\n冻结泳道/积分边界来自该板当前几何;板几何或边界变化后,已确认版本标为过期。`;
  $("thresholds").textContent = [
    `确认取值窗口最少有效帧:${d.min_valid_frames}`,
    `配准检查点残差上限:${d.reg_residual_max} px`,
    `稳定平台容差:峰值 ±${d.plateau_tol * 100}%(至少 ${d.plateau_min_frames} 帧)`,
    `质心漂移上限:${d.drift_centroid_px} px(超出即斑点漂出/坏帧)`,
    `饱和像素:单积分窗 ≥ 4 个暗端/亮端截断像素`,
  ].join("\n");
}

$("btnSaveMeta").onclick = async () => {
  try {
    S.st = await api(`/api/kinetics/${S.sid}/meta`, { json: {
      name: $("fName").value, start_at: $("fStart").value } });
    await Promise.all(S.st.frames.map(loadFrameImg));
    renderAll(); await refreshSeries(S.aid, S.sid);
  } catch (e) { alert("保存失败: " + e.message); }
};
$("btnDelete").onclick = async () => {
  if (!confirm("删除整个显色序列及其帧照片与版本?")) return;
  await api(`/api/kinetics/${S.sid}/delete`, { json: {} });
  S.sid = null;
  $("workspace").hidden = true; $("kinExports").hidden = true;
  await refreshSeries(S.aid);
};

function renderVersions() {
  const body = $("versionBody");
  const vers = S.st.versions.slice().reverse();
  if (!vers.length) {
    body.innerHTML = '<p class="dim">尚无确认版本。校审通过后点“确认取值窗口”锁定所用帧、配准参数与窗口。</p>';
    return;
  }
  body.innerHTML = "";
  const tbl = document.createElement("table");
  tbl.innerHTML = `<thead><tr><th>版本</th><th>窗口</th><th>保存时间</th><th>来源状态</th><th>操作</th></tr></thead><tbody>` +
    vers.map(v => `<tr${v.is_current ? ' style="background:#2d3a4d"' : ""}>
      <td>v${v.version}${v.is_current ? " (当前)" : ""}</td>
      <td>${mmss(v.window_t0)} ~ ${mmss(v.window_t1)}</td>
      <td>${v.created_at.replace("T", " ").replace("+00:00", "Z")}</td>
      <td>${v.stale ? '<span style="color:var(--warn)">已过期(照片/几何/边界变更),下游不可引用</span>'
                    : '<span style="color:#5c6">有效,下游校准可引用</span>'}</td>
      <td style="text-align:left">
        <button data-vid="${v.id}" class="vView">查看</button>
        <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
           href="/api/kinetics/versions/${v.id}/frames.csv">CSV</a>
        <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
           href="/api/kinetics/versions/${v.id}/recompute.json">JSON</a>
        <a class="btn" style="padding:1px 8px;font-size:12px" target="_blank"
           href="/api/kinetics/versions/${v.id}/figure.png">动力学图</a>
      </td></tr>`).join("") + "</tbody>";
  body.appendChild(tbl);
  tbl.querySelectorAll(".vView").forEach(b => b.onclick = () => viewVersion(+b.dataset.vid));
}

async function viewVersion(vid) {
  const r = await api(`/api/kinetics/versions/${vid}`);
  const s = r.snapshot, v = r.version;
  const rows = s.curves.map(c => {
    const st = c.window_stat || {};
    return `<tr><td style="text-align:left">${esc(c.lane_label)} 峰${c.peak_id}</td>
      <td>${c.times.length}</td><td>${fmt(st.mean, 1)}</td><td>${fmt(st.cv_pct, 2)}%</td>
      <td>${c.plateau ? mmss(c.times[c.plateau[0]]) + "~" + mmss(c.times[c.plateau[1]]) : "—"}</td></tr>`;
  }).join("");
  const dlg = document.createElement("dialog");
  dlg.style.maxWidth = "860px"; dlg.style.width = "92%";
  dlg.innerHTML = `<h3>确认版 v${v.version} ${r.stale
      ? '<span style="color:var(--warn)">— 已过期:照片/板面几何/积分边界已变更(留档不可引用)</span>'
      : '<span style="color:#5c6">— 有效,下游校准引用此窗口</span>'}</h3>
    <div class="kv">保存于 ${v.created_at} · 板 #${s.analysis_id} ${esc(s.analysis_name)}
几何 v${s.geometry_version} · 显色开始 ${esc(s.start_at || "(相对秒)")}
窗口 [${mmss(v.window_t0)}, ${mmss(v.window_t1)}] · 帧 ${s.frames.length}(排除 ${s.frames.filter(f => f.excluded).length})
来源指纹 ${v.source_fp.slice(0, 12)}… ${r.stale ? "(≠ 当前 " + r.current_fp.slice(0, 12) + "…)" : "(= 当前)"}</div>
    <h4>逐斑点窗口统计</h4>
    <table><thead><tr><th style="text-align:left">斑点</th><th>窗口帧数</th><th>均值</th><th>CV</th><th>平台区间</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <h4>锁定帧</h4>
    <table><thead><tr><th>#</th><th>时刻</th><th>t(s)</th><th>残差px</th><th>排除</th><th>理由</th></tr></thead><tbody>
    ${s.frames.map(f => `<tr><td>${f.seq + 1}</td><td>${esc(f.taken_at)}</td>
      <td>${fmt(f.t_sec, 1)}</td><td>${fmt(f.residual, 2)}</td>
      <td>${f.excluded ? "是" : ""}</td><td style="text-align:left">${esc(f.exclude_reason)}</td></tr>`).join("")}
    </tbody></table>
    <form method="dialog" style="margin-top:10px"><button>关闭</button></form>`;
  document.body.appendChild(dlg);
  dlg.showModal();
  dlg.addEventListener("close", () => dlg.remove());
}

// ---------------- 添加帧 ----------------
$("btnAddFrame").onclick = () => $("frameFile").click();
$("frameFile").onchange = async () => {
  const file = $("frameFile").files[0];
  if (!file) return;
  const taken = $("frameTaken").value.trim();
  const fd = new FormData();
  fd.append("file", file);
  if (taken) fd.append("taken_at", taken);
  try {
    S.st = await api(`/api/kinetics/${S.sid}/frames`, { method: "POST", body: fd });
    S.fi = S.st.frames.length - 1;
    await Promise.all(S.st.frames.map(loadFrameImg));
    $("frameTaken").value = "";
    renderAll();
  } catch (e) { alert("添加帧失败: " + e.message); }
  $("frameFile").value = "";
};

// ---------------- 配准对话框(控制点 + 微调) ----------------
async function openRegDialog(fid) {
  const f = S.st.frames.find(x => x.id === fid);
  S.reg = { fid, cps: JSON.parse(JSON.stringify(f.control_points)), drag: -1 };
  $("regTitle").textContent = `#${f.seq + 1} (${f.taken_at || "未登记时刻"})`;
  $("regDx").value = f.dx; $("regDy").value = f.dy;
  await new Promise((res) => {
    const im = new Image();
    im.onload = () => { S.origImg = im; res(); };
    im.onerror = () => { S.origImg = null; res(); };
    im.src = `/api/kinetics/frames/${fid}/image`;
  });
  drawReg();
  $("regDialog").showModal();
}

function drawReg() {
  const cv = $("regCv"), ctx = cv.getContext("2d");
  const im = S.origImg;
  if (!im) {
    cv.width = 400; cv.height = 60;
    ctx.fillStyle = "#14161a"; ctx.fillRect(0, 0, 400, 60);
    ctx.fillStyle = "#e5534b"; ctx.fillText("帧照片缺失", 12, 34);
    return;
  }
  const maxW = Math.min(860, im.naturalWidth);
  const k = maxW / im.naturalWidth;
  cv.width = im.naturalWidth * k; cv.height = im.naturalHeight * k;
  S.reg.k = k;
  ctx.drawImage(im, 0, 0, cv.width, cv.height);
  // 校正矩形目标轮廓(rx,ry)提示
  S.reg.cps.forEach((cp, i) => {
    const x = cp.fx * k, y = cp.fy * k;
    const col = cp.kind === "check" ? "#e6b93d" : "#e5534b";
    ctx.strokeStyle = col; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.stroke();
    ctx.fillStyle = col;
    ctx.fillText((cp.kind === "check" ? "检" : "角") + (i + 1), x + 9, y - 8);
  });
  // 残差预览需保存后才有
  const f = S.st.frames.find(x => x.id === S.reg.fid);
  $("regResid").textContent = f.error ? ("当前:" + f.error.message)
    : (f.residual != null ? `当前检查点残差 RMS=${fmt(f.residual, 2)}px` : "当前无检查点残差");
}

function regHit(mx, my) {
  const k = S.reg.k || 1;
  for (let i = S.reg.cps.length - 1; i >= 0; i--) {
    const cp = S.reg.cps[i];
    if ((mx - cp.fx * k) ** 2 + (my - cp.fy * k) ** 2 <= 90) return i;
  }
  return -1;
}
$("regCv").addEventListener("mousedown", (e) => {
  const r = e.target.getBoundingClientRect();
  S.reg.drag = regHit(e.clientX - r.left, e.clientY - r.top);
});
window.addEventListener("mousemove", (e) => {
  if (S.reg.drag < 0 || !$("regDialog").open) return;
  const cv = $("regCv"), r = cv.getBoundingClientRect();
  const k = S.reg.k || 1;
  const cp = S.reg.cps[S.reg.drag];
  cp.fx = Math.max(0, Math.min(S.origImg.naturalWidth, (e.clientX - r.left) / k));
  cp.fy = Math.max(0, Math.min(S.origImg.naturalHeight, (e.clientY - r.top) / k));
  drawReg();
});
window.addEventListener("mouseup", () => { S.reg.drag = -1; });

$("btnAddCheck").onclick = () => {
  const im = S.origImg;
  if (!im) return;
  // 在板面中心放一对初始重合的检查点(帧点=目标点),再拖动帧点
  const cx = im.naturalWidth / 2, cy = im.naturalHeight / 2;
  S.reg.cps.push({ fx: cx, fy: cy, rx: cx, ry: cy, kind: "check" });
  drawReg();
};

$("regSave").onclick = async () => {
  try {
    await postFrame(S.reg.fid, {
      control_points: S.reg.cps,
      dx: +$("regDx").value || 0, dy: +$("regDy").value || 0 });
    $("regDialog").close();
  } catch (e) { alert("配准保存失败: " + e.message); }
};
$("regCancel").onclick = () => $("regDialog").close();

// ---------------- 总渲染 ----------------
async function renderCharts() {
  renderSlider();
  drawOverlay();
  renderResponse();
  renderWindowPanel();
  renderBlockers();
  await drawDensity();
}
function renderAll() {
  if (!S.st) return;
  renderSidebar();
  renderLanePick();
  renderFrameTable();
  renderVersions();
  renderCharts();
}
window.addEventListener("resize", () => { drawOverlay(); renderResponse(); });

// 初始化
refreshAnalyses().then(() => refreshSeries(null));
})();
