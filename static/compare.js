/* 跨板对照前端:并排校正图 + 密度曲线 + 匹配连线,改绑/拆开/锁定。 */
(() => {
"use strict";

// ---------------- 状态 ----------------
const S = {
  cid: null,
  st: null,            // 对照组完整状态
  plates: [],          // 已完成定量的板(供挑选) [{id,name,lanes}]
  images: {},          // analysis_id -> {img, version}
  profiles: {},        // `${aid}:${laneId}:v${ver}` -> profile data
  memberGeom: [],      // strip 布局 [{member_id, x, w, k}]
  markers: [],         // strip 上的斑点标记(命中检测)
  focusLane: {},       // member_id -> lane_id(密度曲线展示哪条泳道)
  editingCell: null,   // "tid:mid" 正在改绑的单元格
  editTargetId: null,  // 正在编辑的目标
};

const $ = (id) => document.getElementById(id);
const cv = $("stripCv"), ctx = cv.getContext("2d");
const STRIP_H = 300, GAP = 64, PAD = 8, HEAD = 40;
const COLORS = ["#e6550d", "#3182bd", "#31a354", "#756bb1",
                "#d6616b", "#e7ba52", "#63c5da", "#ce6dbd"];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function tColor(tid) {
  const i = S.st.targets.findIndex(t => t.id === tid);
  return COLORS[(i < 0 ? 0 : i) % COLORS.length];
}
function tIndex(tid) { return S.st.targets.findIndex(t => t.id === tid); }

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

// ---------------- 载入 ----------------
async function refreshCmpList() {
  const list = await api("/api/comparisons");
  const sel = $("cmpSelect");
  sel.innerHTML = '<option value="">打开对照组…</option>' +
    list.map(c => `<option value="${c.id}">#${c.id} ${esc(c.name)} (${c.n_members}板/${c.n_targets}目标)</option>`).join("");
  if (S.cid) sel.value = S.cid;
}

async function refreshPlates() {
  // 只列出"已完成定量"的板:有几何且至少一个积分峰
  const list = await api("/api/analyses");
  const states = await Promise.all(list.map(a =>
    api(`/api/analyses/${a.id}`).then(st => ({ a, st })).catch(() => null)));
  S.plates = states.filter(x => x && x.st.geometry &&
    x.st.lanes.some(l => l.peaks.length > 0))
    .map(x => ({
      id: x.a.id, name: x.a.name,
      lanes: x.st.lanes.map(l => ({ id: l.id, label: l.label, n_peaks: l.peaks.length })),
    }));
  const sel = $("addAnalysis");
  sel.innerHTML = S.plates.length
    ? S.plates.map(p => `<option value="${p.id}">#${p.id} ${esc(p.name)}</option>`).join("")
    : '<option value="">(没有已定量的板)</option>';
  fillStdLanes();
  $("addHint").textContent = S.plates.length
    ? "选择成员板及其标准品泳道(含全部目标成分标准品的泳道)。"
    : "请先在“单板定量”页完成几何标定与峰积分。";
}

function fillStdLanes() {
  const aid = +$("addAnalysis").value;
  const p = S.plates.find(p => p.id === aid);
  $("addStdLane").innerHTML = p
    ? p.lanes.map(l => `<option value="${l.id}">${esc(l.label || "L" + l.id)} (${l.n_peaks}峰)</option>`).join("")
    : "";
}

async function openCmp(cid) {
  S.cid = cid;
  await reload();
  $("emptyState").hidden = true;
  $("workspace").hidden = false;
  $("cmpExports").hidden = false;
  $("expCsv").href = `/api/comparisons/${cid}/export/compare.csv`;
  $("expJson").href = `/api/comparisons/${cid}/export/matches.json`;
  $("expFig").href = `/api/comparisons/${cid}/export/figure.png`;
  refreshCmpList();
}

async function reload() {
  S.st = await api(`/api/comparisons/${S.cid}`);
  $("cmpBadge").textContent =
    `${S.st.comparison.name} · ${S.st.members.length} 板 · ${S.st.targets.length} 目标`;
  await Promise.all(S.st.members.map(loadImage));
  renderAll();
}

function loadImage(m) {
  return new Promise((res) => {
    const rec = S.images[m.analysis_id];
    if (rec && rec.version === m.geometry_version) return res();
    const im = new Image();
    im.onload = () => { S.images[m.analysis_id] = { img: im, version: m.geometry_version }; res(); };
    im.onerror = () => res();
    im.src = `/api/analyses/${m.analysis_id}/preview/corrected?t=${m.geometry_version}`;
  });
}

// ---------------- 并排校正图 + 匹配连线 ----------------
function drawStrip() {
  const st = S.st;
  if (!st) return;
  let x = PAD;
  S.memberGeom = [];
  S.markers = [];
  for (const m of st.members) {
    const rec = S.images[m.analysis_id];
    const iw = rec ? rec.img.naturalWidth : 200;
    const ih = rec ? rec.img.naturalHeight : 300;
    const k = STRIP_H / ih, w = iw * k;
    S.memberGeom.push({ member_id: m.id, x, w, k });
    x += w + GAP;
  }
  cv.width = Math.max(400, x - GAP + PAD);
  cv.height = HEAD + STRIP_H + 8;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.font = "12px sans-serif";
  const markBy = {};   // `${target_id}:${member_id}` -> {x, y}
  st.members.forEach((m, mi) => {
    const g = S.memberGeom[mi];
    ctx.fillStyle = m.valid ? "#e6e8eb" : "#e5534b";
    ctx.fillText(`#${m.analysis_id} ${m.analysis_name}` +
      (m.coef != null ? `  coef=${m.coef.toFixed(3)}` : ""), g.x, 14);
    ctx.fillStyle = m.valid ? "#9aa3ad" : "#e5534b";
    ctx.fillText(m.valid
      ? `几何v${m.geometry_version} · 标准泳道 ${m.std_lane_label || "?"}`
      : "✕ " + (m.problems[0] ? m.problems[0].message.slice(0, 30) : "无效"), g.x, 30);
    const rec = S.images[m.analysis_id];
    if (rec) ctx.drawImage(rec.img, g.x, HEAD, g.w, STRIP_H);
    ctx.strokeStyle = m.valid ? "#3a4048" : "#e5534b";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(g.x, HEAD, g.w, STRIP_H);
    const stdLane = (m.lanes || []).find(l => l.id === m.std_lane_id);
    if (stdLane) {
      ctx.fillStyle = "rgba(79,156,249,.16)";
      ctx.fillRect(g.x + stdLane.x0 * g.k, HEAD, (stdLane.x1 - stdLane.x0) * g.k, STRIP_H);
    }
    for (const mt of st.matches) {
      if (mt.member_id !== m.id || !mt.spot) continue;
      const lane = (m.lanes || []).find(l => l.id === mt.spot.lane_id);
      if (!lane) continue;
      const cx = g.x + (lane.x0 + lane.x1) / 2 * g.k;
      const cy = HEAD + mt.spot.center_y * g.k;
      const ti = tIndex(mt.target_id);
      if (mt.stale) {
        // 失效匹配:灰色空心标记,不参与连线
        ctx.strokeStyle = "#778"; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(cx, cy, 5, 0, 7); ctx.stroke();
        ctx.fillStyle = "#778";
        ctx.fillText(`T${ti + 1}✕`, cx + 7, cy - 4);
        continue;
      }
      const color = tColor(mt.target_id);
      markBy[`${mt.target_id}:${m.id}`] = { x: cx, y: cy };
      S.markers.push({ x: cx, y: cy, target_id: mt.target_id, member_id: m.id });
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(cx, cy, 6, 0, 7); ctx.stroke();
      ctx.fillStyle = color;
      ctx.fillText(`T${ti + 1}${mt.locked ? "🔒" : ""}`, cx + 8, cy - 4);
    }
  });
  // 同目标相邻成员连线:实线=双方已锁定,虚线=待确认
  for (const t of st.targets) {
    const color = tColor(t.id);
    for (let i = 0; i < st.members.length - 1; i++) {
      const a = markBy[`${t.id}:${st.members[i].id}`];
      const b = markBy[`${t.id}:${st.members[i + 1].id}`];
      if (!a || !b) continue;
      const ma = st.matches.find(mt => mt.target_id === t.id && mt.member_id === st.members[i].id);
      const mb = st.matches.find(mt => mt.target_id === t.id && mt.member_id === st.members[i + 1].id);
      ctx.strokeStyle = color; ctx.lineWidth = 1.5;
      ctx.setLineDash(ma && mb && ma.locked && mb.locked ? [] : [5, 4]);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
  }
  ctx.setLineDash([]);
}

cv.addEventListener("click", (e) => {
  if (!S.st) return;
  const r = cv.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  for (const mk of S.markers) {
    if (Math.hypot(mk.x - mx, mk.y - my) < 10) {
      const mt = S.st.matches.find(x => x.target_id === mk.target_id && x.member_id === mk.member_id);
      if (mt && mt.spot) S.focusLane[mk.member_id] = mt.spot.lane_id;
      renderProfiles();
      const td = document.querySelector(`td.cell[data-tid="${mk.target_id}"][data-mid="${mk.member_id}"]`);
      if (td) {
        document.querySelectorAll("td.cell.hit").forEach(x => x.classList.remove("hit"));
        td.classList.add("hit");
        td.scrollIntoView({ block: "nearest", inline: "nearest" });
      }
      return;
    }
  }
  // 点击某成员图的泳道 → 切换该板密度曲线
  const g = S.memberGeom.find(g => mx >= g.x && mx <= g.x + g.w);
  if (g && my >= HEAD) {
    const m = S.st.members.find(m => m.id === g.member_id);
    const ix = (mx - g.x) / g.k;
    const lane = (m.lanes || []).find(l => ix >= l.x0 && ix <= l.x1);
    if (lane) { S.focusLane[m.id] = lane.id; renderProfiles(); }
  }
});

// ---------------- 密度曲线 ----------------
async function renderProfiles() {
  const row = $("profileRow");
  row.innerHTML = "";
  if (!S.st) return;
  for (const m of S.st.members) {
    const g = S.memberGeom.find(g => g.member_id === m.id);
    const wrap = document.createElement("div");
    wrap.className = "profCell";
    wrap.style.width = Math.max(140, g ? g.w : 200) + "px";
    const title = document.createElement("div");
    title.className = "profTitle";
    const cvs = document.createElement("canvas");
    cvs.height = 150;
    wrap.append(title, cvs);
    row.appendChild(wrap);
    const laneId = S.focusLane[m.id] || m.std_lane_id;
    const lane = (m.lanes || []).find(l => l.id === laneId);
    title.textContent = lane
      ? `#${m.analysis_id} 泳道 ${lane.label || lane.id}${lane.id === m.std_lane_id ? "(标准)" : ""}`
      : `#${m.analysis_id}`;
    if (!lane) continue;
    const key = `${m.analysis_id}:${laneId}:v${m.geometry_version}`;
    if (!S.profiles[key]) {
      try {
        S.profiles[key] = await api(`/api/analyses/${m.analysis_id}/lanes/${laneId}/profile`);
      } catch (e) { continue; }
    }
    drawProfile(cvs, S.profiles[key], m, laneId);
  }
}

function drawProfile(cvs, data, m, laneId) {
  const w = cvs.clientWidth || 200;
  cvs.width = w;
  const c2 = cvs.getContext("2d");
  const H = cvs.height, prof = data.profile;
  const vmax = Math.max(...prof, 1) * 1.15;
  const X = (yPx) => yPx / data.height * w;
  const Y = (v) => H - 16 - (v / vmax) * (H - 26);
  c2.clearRect(0, 0, w, H);
  c2.strokeStyle = "#2a2f37"; c2.fillStyle = "#778"; c2.font = "9px sans-serif";
  for (let rf = 0; rf <= 1.001; rf += 0.2) {
    const yPx = data.baseline_y - rf * (data.baseline_y - data.front_y);
    c2.beginPath(); c2.moveTo(X(yPx), 0); c2.lineTo(X(yPx), H - 16); c2.stroke();
    c2.fillText(rf.toFixed(1), X(yPx) - 5, H - 4);
  }
  // 各目标期望位置(参考 Rf + 板级偏移)
  const off = m.offset || 0;
  c2.setLineDash([3, 3]);
  S.st.targets.forEach((t, ti) => {
    const rf = t.rf_ref + off;
    if (rf < -0.05 || rf > 1.05) return;
    const yPx = data.baseline_y - rf * (data.baseline_y - data.front_y);
    c2.strokeStyle = COLORS[ti % COLORS.length] + "99";
    c2.beginPath(); c2.moveTo(X(yPx), 0); c2.lineTo(X(yPx), H - 16); c2.stroke();
  });
  c2.setLineDash([]);
  c2.strokeStyle = "#4f9cf9"; c2.lineWidth = 1.2; c2.beginPath();
  prof.forEach((v, i) => { const x = X(i), y = Y(v); i ? c2.lineTo(x, y) : c2.moveTo(x, y); });
  c2.stroke();
  // 已绑斑点(本泳道)
  for (const mt of S.st.matches) {
    if (mt.member_id !== m.id || !mt.spot || mt.spot.lane_id !== laneId) continue;
    const x = X(mt.spot.center_y);
    c2.fillStyle = tColor(mt.target_id);
    c2.fillRect(x - 2, 0, 4, H - 16);
  }
}

// ---------------- 匹配关系表 ----------------
function renderMatchTable() {
  const st = S.st;
  const tbl = $("matchTable");
  let html = "<thead><tr><th>目标 \\ 板</th>" +
    st.members.map(m =>
      `<th class="${m.valid ? "" : "invalid"}" title="${m.valid ? "" : esc(m.problems.map(p => p.message).join(";"))}">` +
      `#${m.analysis_id} ${esc(m.analysis_name)}${m.valid ? "" : " ✕"}</th>`).join("") +
    "</tr></thead><tbody>";
  st.targets.forEach((t, ti) => {
    html += `<tr><td><span style="color:${COLORS[ti % COLORS.length]}">●</span> ` +
      `T${ti + 1} ${esc(t.name)}<br><span class="dim">Rf ${t.rf_ref.toFixed(2)}±${t.rf_tol.toFixed(2)}</span></td>`;
    for (const m of st.members) {
      const mt = st.matches.find(x => x.target_id === t.id && x.member_id === m.id);
      html += `<td class="cell" data-tid="${t.id}" data-mid="${m.id}">${cellHtml(t, m, mt)}</td>`;
    }
    html += "</tr>";
  });
  tbl.innerHTML = html + "</tbody>";
  tbl.querySelectorAll("td.cell").forEach(td => {
    const tid = +td.dataset.tid, mid = +td.dataset.mid;
    td.querySelectorAll("button").forEach(b =>
      b.onclick = () => cellAction(tid, mid, b.dataset.act, td));
  });
}

function cellHtml(t, m, mt) {
  const key = `${t.id}:${m.id}`;
  if (S.editingCell === key) {
    const cands = (mt && mt.candidates) || [];
    return `<select data-role="cand">
        <option value="">— 拆开(不匹配)—</option>
        ${cands.map(c => `<option value="${c.peak_id}" ${mt && mt.peak_id === c.peak_id ? "selected" : ""}>` +
          `${esc(c.lane_label || "L")}-${c.spot_no} Rf${c.rf.toFixed(3)} A${c.area.toFixed(0)} ` +
          `得分${c.score.toFixed(2)}</option>`).join("")}
      </select>
      <button data-act="confirmBind">✓</button><button data-act="cancelBind">✗</button>`;
  }
  if (!mt || !mt.peak_id) {
    return `<span class="dim">—</span><br><button data-act="edit">改绑</button>`;
  }
  if (mt.stale) {
    // 板数据变更后该关系未重新确认,不进入汇总
    const old = mt.spot ? ` ${esc(mt.spot.lane_label || "L")}-${mt.spot.spot_no}` : "";
    return `<span class="warn">已失效${old}(数据已变更)</span><br>` +
      `<button data-act="edit">改绑</button><button data-act="unbind">拆开</button>`;
  }
  if (!mt.spot) {
    // 绑定关系引用的斑点已不存在
    return `<span class="warn">斑点已失效</span><br><button data-act="edit">改绑</button>` +
      `<button data-act="unbind">拆开</button>`;
  }
  const s = mt.spot;
  const badge = `${mt.locked ? "🔒" : ""}${mt.status === "manual" ? "✎" : ""}`;
  return `<div>${esc(s.lane_label || "L")}-${s.spot_no} Rf${s.rf.toFixed(3)}<br>` +
    `A=${s.area.toFixed(0)} ${badge}</div>` +
    `<button data-act="edit">改绑</button>` +
    `<button data-act="unbind">拆开</button>` +
    `<button data-act="lock">${mt.locked ? "解锁" : "锁定"}</button>`;
}

async function cellAction(tid, mid, act, td) {
  const mt = S.st.matches.find(x => x.target_id === tid && x.member_id === mid);
  if (act === "edit") {
    S.editingCell = `${tid}:${mid}`;
    renderMatchTable();
  } else if (act === "cancelBind") {
    S.editingCell = null;
    renderMatchTable();
  } else if (act === "confirmBind") {
    const sel = td.querySelector('select[data-role="cand"]');
    const v = sel ? sel.value : "";
    S.editingCell = null;
    await api(`/api/comparisons/${S.cid}/matches/bind`,
      { json: { target_id: tid, member_id: mid, peak_id: v === "" ? null : +v } });
    await reload();
  } else if (act === "unbind") {
    await api(`/api/comparisons/${S.cid}/matches/bind`,
      { json: { target_id: tid, member_id: mid, peak_id: null } });
    await reload();
  } else if (act === "lock") {
    await api(`/api/comparisons/${S.cid}/matches/lock`,
      { json: { target_id: tid, member_id: mid, locked: !(mt && mt.locked) } });
    await reload();
  }
}

// ---------------- 汇总 ----------------
function renderSummary() {
  const st = S.st;
  let html = "";
  for (const row of st.summary.rows) {
    const t = row.target, s = row.stats;
    const ti = tIndex(t.id);
    html += `<h4 style="color:${COLORS[ti % COLORS.length]}">T${ti + 1} ${esc(t.name)} · 参考 Rf ${t.rf_ref.toFixed(2)}</h4>`;
    html += `<table><thead><tr><th>板</th><th>来源斑点</th><th>Rf</th><th>原始面积</th>` +
      `<th>系数</th><th>归一化面积</th><th>状态</th></tr></thead><tbody>`;
    for (const e of row.entries) {
      html += `<tr><td>#${e.analysis_id} ${esc(e.analysis_name)}</td>` +
        `<td>${esc(e.lane_label || "L")}-${e.spot_no} (峰${e.peak_id})</td>` +
        `<td>${e.rf.toFixed(3)}</td><td>${e.raw_area.toFixed(0)}</td>` +
        `<td>${e.coef == null ? "—" : e.coef.toFixed(3)}</td>` +
        `<td>${e.norm_area == null ? "—" : e.norm_area.toFixed(0)}</td>` +
        `<td>${e.locked ? "🔒已锁定" : e.status === "manual" ? "手动" : "自动"}</td></tr>`;
    }
    html += `</tbody></table><div class="kv">n=${s.n}` +
      (s.n ? ` · 均值 ${s.mean.toFixed(1)} · SD ${s.sd.toFixed(1)} · ` +
        `板间变异 CV ${s.cv_pct.toFixed(1)}% · 范围 ${s.min.toFixed(0)}–${s.max.toFixed(0)}`
        : " · 无有效数据") + `</div>`;
  }
  $("summaryBody").innerHTML = html || '<p class="dim">暂无目标斑点,请先在右侧添加。</p>';
  const ex = st.summary.excluded;
  $("excludedBody").innerHTML = ex.length
    ? `<h4 class="warn">未进入汇总的板(原因)</h4><ul>` +
      ex.map(e => `<li>#${e.analysis_id} ${esc(e.analysis_name)}:${e.reasons.map(esc).join(";")}</li>`).join("") +
      `</ul>` : "";
}

// ---------------- 侧栏:成员 / 目标 / 阈值 ----------------
function renderMembers() {
  const ul = $("memberList");
  ul.innerHTML = "";
  for (const m of S.st.members) {
    const li = document.createElement("li");
    li.className = m.valid ? (m.stale ? "stale" : "") : "invalid";
    const probs = m.valid ? "" :
      `<br><span class="warn">${m.problems.map(p => esc(p.message)).join("<br>")}</span>`;
    const staleNote = (m.valid && m.stale)
      ? `<br><span class="warn">板数据已变更:${m.stale_matches} 条匹配待重新确认` +
        `(逐条改绑或整体重新匹配)</span>`
      : "";
    li.innerHTML = `<span class="grow">#${m.analysis_id} ${esc(m.analysis_name)}<br>` +
      `<span class="dim">几何v${m.geometry_version}` +
      (m.coef != null ? ` · 系数 ${m.coef.toFixed(3)}` : "") +
      (m.offset != null ? ` · Rf偏移 ${m.offset >= 0 ? "+" : ""}${m.offset.toFixed(3)}` : "") +
      `</span>${probs}${staleNote}</span>`;
    // 标准品泳道选择(泳道被重建后需重新指定)
    const sel = document.createElement("select");
    sel.title = "标准品泳道";
    sel.innerHTML = (m.lanes || []).map(l =>
      `<option value="${l.id}" ${l.id === m.std_lane_id ? "selected" : ""}>` +
      `${esc(l.label || "L" + l.id)}</option>`).join("");
    if (m.std_lane_id && !(m.lanes || []).some(l => l.id === m.std_lane_id)) {
      sel.innerHTML = `<option value="${m.std_lane_id}" selected>(已失效)</option>` + sel.innerHTML;
    }
    sel.onchange = async () => {
      await api(`/api/comparisons/${S.cid}/members/${m.id}/std_lane`,
        { json: { std_lane_id: +sel.value } });
      await reload();
    };
    const head = document.createElement("div");
    head.className = "row";
    head.style.margin = "2px 0";
    head.appendChild(sel);
    li.insertBefore(head, li.firstChild);
    const mkBtn = (txt, fn) => {
      const b = document.createElement("button");
      b.textContent = txt;
      b.onclick = fn;
      return b;
    };
    li.append(mkBtn("重新匹配", async () => {
      await api(`/api/comparisons/${S.cid}/members/${m.id}/rematch`, { json: {} });
      await reload();
    }), mkBtn("移除", async () => {
      if (!confirm(`移除成员板 #${m.analysis_id} 及其全部匹配?`)) return;
      await api(`/api/comparisons/${S.cid}/members/${m.id}/delete`, { json: {} });
      await reload();
    }));
    ul.appendChild(li);
  }
}

function renderTargets() {
  const ul = $("targetList");
  ul.innerHTML = "";
  S.st.targets.forEach((t, ti) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="grow" style="color:${COLORS[ti % COLORS.length]}">● ` +
      `<span style="color:var(--fg)">${esc(t.name)} · Rf ${t.rf_ref.toFixed(2)}±${t.rf_tol.toFixed(2)}</span></span>`;
    const ed = document.createElement("button");
    ed.textContent = "编辑";
    ed.onclick = () => {
      S.editTargetId = t.id;
      $("tName").value = t.name;
      $("tRf").value = t.rf_ref;
      $("tTol").value = t.rf_tol;
      $("btnAddTarget").textContent = "更新";
      $("btnCancelEdit").hidden = false;
    };
    const del = document.createElement("button");
    del.textContent = "删除";
    del.onclick = async () => {
      if (!confirm(`删除目标 ${t.name} 及其全部匹配?`)) return;
      await api(`/api/comparisons/${S.cid}/targets/${t.id}/delete`, { json: {} });
      await reload();
    };
    li.append(ed, del);
    ul.appendChild(li);
  });
}

function renderThresholds() {
  const th = S.st.thresholds;
  $("thresholds").textContent = [
    `标准品名义容许 Rf 偏移 ±${th.std_rf_tol}(超出 → Rf 偏移超限)`,
    `标准品匹配硬上限 ±${th.rf_shift_max}(超出 → 标准缺失)`,
    `归一化系数离群倍数 ${th.coef_outlier_ratio}(超出 → 系数离群)`,
    `候选评分 = ${(1 - th.shape_weight).toFixed(1)}·Rf接近 + ${th.shape_weight.toFixed(1)}·峰形相关`,
    "同一泳道内匹配次序必须与标准品次序一致(颠倒 → 排除)",
  ].join("\n");
}

function renderAll() {
  drawStrip();
  renderProfiles();
  renderMatchTable();
  renderSummary();
  renderMembers();
  renderTargets();
  renderThresholds();
  $("hint").textContent =
    "并排图为各板校正图(蓝底=标准品泳道);点击斑点标记定位匹配,点击泳道切换下方密度曲线。";
}

// ---------------- 控件绑定 ----------------
$("btnNewCmp").onclick = async () => {
  const name = $("cmpName").value.trim();
  if (!name) { alert("请输入对照组名称"); return; }
  const st = await api("/api/comparisons", { json: { name } });
  $("cmpName").value = "";
  await openCmp(st.comparison.id);
};
$("cmpSelect").onchange = (e) => { if (e.target.value) openCmp(+e.target.value); };
$("addAnalysis").onchange = fillStdLanes;
$("btnAddMember").onclick = async () => {
  const aid = +$("addAnalysis").value;
  const lid = +$("addStdLane").value;
  if (!aid || !lid) { alert("请选择成员板与标准品泳道"); return; }
  await api(`/api/comparisons/${S.cid}/members`,
    { json: { analysis_id: aid, std_lane_id: lid } });
  await reload();
};
$("btnAddTarget").onclick = async () => {
  const name = $("tName").value.trim();
  const rf = parseFloat($("tRf").value);
  const tol = parseFloat($("tTol").value);
  if (!name || !(rf >= 0 && rf <= 1) || !(tol > 0)) {
    alert("请填写名称、参考 Rf(0~1)与容差(>0)");
    return;
  }
  if (S.editTargetId) {
    await api(`/api/comparisons/${S.cid}/targets/${S.editTargetId}/update`,
      { json: { name, rf_ref: rf, rf_tol: tol } });
  } else {
    await api(`/api/comparisons/${S.cid}/targets`,
      { json: { name, rf_ref: rf, rf_tol: tol } });
  }
  resetTargetForm();
  await reload();
};
$("btnCancelEdit").onclick = resetTargetForm;
function resetTargetForm() {
  S.editTargetId = null;
  $("tName").value = ""; $("tRf").value = ""; $("tTol").value = 0.05;
  $("btnAddTarget").textContent = "添加";
  $("btnCancelEdit").hidden = true;
}

window.addEventListener("resize", () => { drawStrip(); renderProfiles(); });

refreshCmpList();
refreshPlates();
})();
