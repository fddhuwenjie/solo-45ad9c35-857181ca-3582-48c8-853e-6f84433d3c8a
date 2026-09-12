"""显色时间序列校审(纯函数,可复算,不依赖 Web 框架)。

同一块板的多张照片按显色时刻排成序列:每张照片用控制点对齐到参考帧(校正
坐标空间),在冻结的泳道与积分边界上逐帧重算密度曲线与斑点面积,得到每个
斑点的显色动力学曲线,识别上升 / 稳定平台 / 峰值 / 衰减区间,并在有效帧上
确认取值窗口。

时刻重复或倒序、配准不可解 / 残差超限、像素饱和、斑点漂出积分区、有效帧
不足等情形均定位到帧与斑点,并阻断取值窗口确认。

设计约定:
- 帧照片先经控制点单应(至少 4 个点)矫正到与参考分析相同的校正空间,
  再叠加 dx/dy 微调;变换不可解或检查点残差超限 => 该帧不可用。
- 面积/密度曲线口径与 tlc.pipeline 一致(同一背景估计与积分函数)。
"""

import math

from PIL import Image

from . import background, geometry, profile, qc

DEFAULTS = {
    "min_valid_frames": 3,       # 确认取值窗口所需最少有效帧
    "reg_residual_max": 3.0,     # 检查点配准残差 RMS 上限(校正空间 px)
    "plateau_tol": 0.06,         # 平台容差:点落在峰值 ±tol 内
    "plateau_min_frames": 2,     # 平台最少连续帧数
    "drift_centroid_px": 6.0,    # 质心偏离冻结峰中心超过该值 => 漂移
    "drift_area_frac": 0.35,     # 帧面积低于窗口最大面积该比例 => 斑点漂出/塌陷
}

# 阻断取值窗口确认的问题类型(其余为警告)
BLOCKERS = {
    "no_frames", "few_frames", "duplicate_time", "time_reversed",
    "start_missing", "time_invalid", "reg_insufficient", "reg_unsolvable",
    "reg_residual", "saturated", "spot_drift", "window_invalid",
    "window_saturated", "window_drift",
}

CORNERS_REF = [(0.0, 0.0), None, None, None]   # 占位,实际用 derived 尺寸


# ---------- 线性代数:超定 DLT 求 3x3 单应 ----------

def _solve_normal(A, b):
    """高斯消元解 A^T A x = A^T b(A 为 m×n)。"""
    n = len(A[0])
    m = len(A)
    M = [[0.0] * (n + 1) for _ in range(n)]
    for i in range(n):
        for j in range(n):
            M[i][j] = sum(A[k][i] * A[k][j] for k in range(m))
        M[i][n] = sum(A[k][i] * b[k] for k in range(m))
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-14:
            raise ValueError("矩阵奇异")
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col] / M[col][col]
                M[r] = [a - f * bb for a, bb in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_registration(pairs):
    """由 (frame_x, frame_y, ref_x, ref_y) 控制点对求 frame -> ref 的归一化单应。

    返回 9 元素 H(行优先,H[8]=1);点数不足或方程组退化时抛 ValueError。
    """
    if len(pairs) < 4:
        raise ValueError("控制点不足 4 个")
    A, bv = [], []
    for fx, fy, rx, ry in pairs:
        A.append([fx, fy, 1, 0, 0, 0, -rx * fx, -rx * fy])
        bv.append(rx)
        A.append([0, 0, 0, fx, fy, 1, -ry * fx, -ry * fy])
        bv.append(ry)
    h = _solve_normal(A, bv)
    return [h[0], h[1], h[2], h[3], h[4], h[5], h[6], h[7], 1.0]


def _apply_h(H, x, y):
    w = H[6] * x + H[7] * y + 1.0
    return ((H[0] * x + H[1] * y + H[2]) / w,
            (H[3] * x + H[4] * y + H[5]) / w)


def invert_homography(H):
    """求 3x3 齐次单应的逆(返回 9 元素)。"""
    a = [H[0:3], H[3:6], H[6:9]]
    # 伴随矩阵法
    def subdet(r1, r2, c1, c2):
        return a[r1][c1] * a[r2][c2] - a[r1][c2] * a[r2][c1]
    cf = [[0.0] * 3 for _ in range(3)]
    for i in range(3):
        for j in range(3):
            rows = [r for r in range(3) if r != i]
            cols = [c for c in range(3) if c != j]
            d = a[rows[0]][cols[0]] * a[rows[1]][cols[1]] \
                - a[rows[0]][cols[1]] * a[rows[1]][cols[0]]
            cf[i][j] = d * (1.0 if (i + j) % 2 == 0 else -1.0)
    det = sum(a[0][j] * cf[0][j] for j in range(3))
    if abs(det) < 1e-14:
        raise ValueError("单应不可逆(退化)")
    inv = [cf[j][i] / det for i in range(3) for j in range(3)]
    return inv


def warp_coeffs(H_inv, W, H, dx=0.0, dy=0.0):
    """Pillow Image.transform(PERSPECTIVE) 需要 8 系数(a..h),对输出像素 (X,Y)
    反算输入坐标:
        x = (aX + bY + c) / (gX + hY + 1)
        y = (dX + eY + f) / (gX + hY + 1)
    H_inv 即 ref->frame 的单应;微调平移 dx/dy 在参考空间施加(输出先减去位移)。
    """
    a, b, c, d, e, f, g, h, _ = H_inv
    # (X-dx, Y-dy) 经 H_inv 映射 => 平移并入常数项
    return [a, b, c - a * dx - b * dy,
            d, e, f - d * dx - e * dy,
            g, h]


def registration_residual(H, checkpoints):
    """检查点 [(fx, fy, rx, ry), ...] 的重投影 RMS(ref 空间 px)。"""
    if not checkpoints:
        return None
    ss = 0.0
    for fx, fy, rx, ry in checkpoints:
        px, py = _apply_h(H, fx, fy)
        ss += (px - rx) ** 2 + (py - ry) ** 2
    return math.sqrt(ss / len(checkpoints))


def corner_pairs_for(corners, derived):
    """四角控制点对:帧四角(原图坐标) -> 校正矩形四角。"""
    W, H = derived["width"], derived["height"]
    rect = [(0.0, 0.0), (float(W - 1), 0.0), (float(W - 1), float(H - 1)),
            (0.0, float(H - 1))]
    return [(float(corners[i][0]), float(corners[i][1]), rect[i][0], rect[i][1])
            for i in range(4)]


def rectify_frame(image, derived, pairs, checkpoints=(), dx=0.0, dy=0.0,
                  residual_max=None):
    """把一张帧照片矫正到参考校正空间。

    返回 (gray L 图, H, residual)。控制点不足/不可解/残差超限时
    抛 RegistrationError,供上层定位该帧。
    """
    residual_max = DEFAULTS["reg_residual_max"] if residual_max is None else residual_max
    if len(pairs) < 4:
        raise RegistrationError("reg_insufficient",
                                f"控制点仅 {len(pairs)} 个,不足 4 个,无法配准")
    try:
        H = fit_registration(pairs)
        H_inv = invert_homography(H)
    except ValueError as e:
        raise RegistrationError("reg_unsolvable", f"控制点退化,配准方程不可解({e})")
    resid = registration_residual(H, checkpoints)
    if resid is not None and resid > residual_max:
        raise RegistrationError(
            "reg_residual",
            f"配准检查点残差 RMS={resid:.2f}px 超过上限 {residual_max:g}px")
    gray = image.convert("L")
    W, Hh = derived["width"], derived["height"]
    try:
        coeffs = warp_coeffs(H_inv, W, Hh, dx, dy)
    except ValueError as e:
        raise RegistrationError("reg_unsolvable", f"配准变换不可逆({e})")
    out = gray.transform((W, Hh), Image.PERSPECTIVE, coeffs, Image.BICUBIC)
    return out, H, resid


class RegistrationError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------- 时刻 ----------

def parse_time(s):
    """解析登记的拍摄时刻。

    支持 "HH:MM[:SS]"(当日钟点,秒为小数)与秒数(显色开始后)两种口径:
    返回相对显色开始的秒数。无法解析抛 ValueError。
    """
    if s is None:
        raise ValueError("时刻为空")
    s = str(s).strip()
    if not s:
        raise ValueError("时刻为空")
    if ":" in s:
        parts = s.split(":")
        if not 2 <= len(parts) <= 3:
            raise ValueError(f"时刻格式无效: {s}")
        try:
            h = float(parts[0]); m = float(parts[1])
            sec = float(parts[2]) if len(parts) == 3 else 0.0
        except ValueError:
            raise ValueError(f"时刻格式无效: {s}")
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
            raise ValueError(f"时刻超出范围: {s}")
        return h * 3600.0 + m * 60.0 + sec
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"时刻格式无效: {s}(应为 HH:MM:SS 或相对秒数)")


def frame_times(frames, start_clock=None):
    """计算每帧相对显色开始的秒数并排序检查。

    frames: [{id, taken_at, ...}](taken_at 可为钟点串或相对秒数)。
    start_clock: 显色开始钟点串(HH:MM[:SS]);帧为钟点时用于相减。
    返回 (rows, time_issues):
      rows = [{frame_id, t_sec, sort_index}],按登记顺序附 t_sec;
      time_issues = [ {kind, frame_ids, message} ] 重复/倒序/格式问题。
    """
    issues = []
    start_sec = parse_time(start_clock) if start_clock else None
    rows = []
    for idx, f in enumerate(frames):
        raw = f.get("taken_at", "")
        try:
            t = parse_time(raw)
            if start_sec is not None and ":" in str(raw):
                t = t - start_sec
                if t < 0:
                    t += 24 * 3600.0
        except ValueError as e:
            issues.append({"kind": "time_invalid", "frame_ids": [f["id"]],
                           "message": f"帧 #{f.get('seq', idx + 1)} {e}"})
            t = None
        rows.append({"frame_id": f["id"], "t_sec": t, "sort_index": idx})

    valid = [r for r in rows if r["t_sec"] is not None]
    if start_clock is None and frames and not any(":" in str(f.get("taken_at", "")) for f in frames):
        pass   # 相对秒数口径无需显色开始
    elif frames and any(":" in str(f.get("taken_at", "")) for f in frames) and not start_clock:
        issues.append({"kind": "start_missing", "frame_ids": [],
                       "message": "帧时刻按钟点登记,但未登记显色开始时刻,无法计算显色秒数"})

    seen = {}
    for r in valid:
        if r["t_sec"] in seen:
            issues.append({
                "kind": "duplicate_time",
                "frame_ids": [seen[r["t_sec"]], r["frame_id"]],
                "message": f"两帧拍摄时刻重复(t={format_t(r['t_sec'])}),不能排在同一时刻"})
        seen[r["t_sec"]] = r["frame_id"]
    # 倒序:登记顺序上时刻未单调递增(用户把晚拍的帧排在了前面)
    for prev, cur in zip(valid, valid[1:]):
        if cur["t_sec"] < prev["t_sec"]:
            issues.append({
                "kind": "time_reversed",
                "frame_ids": [prev["frame_id"], cur["frame_id"]],
                "message": "拍摄时刻按登记顺序倒序,请核对帧次序与拍摄时刻"})
    neg = [r["frame_id"] for r in valid if r["t_sec"] < 0]
    if neg:
        issues.append({"kind": "time_invalid", "frame_ids": neg,
                       "message": "存在早于显色开始的拍摄时刻"})
    return rows, issues


def format_t(sec):
    """秒 -> mm:ss 字符串(曲线轴用)。"""
    if sec is None:
        return "—"
    sec = max(0.0, sec)
    m, s = divmod(int(round(sec)), 60)
    return f"{m:d}:{s:02d}"


# ---------- 逐帧指标 ----------

def _issue(kind, level, message, frame_ids=None, lane_id=None, peak_id=None):
    return {"type": kind, "level": level, "message": message,
            "frame_ids": sorted(set(frame_ids or [])),
            "lane_id": lane_id, "peak_id": peak_id}


def frame_metrics(image_path, derived, lanes, pairs, checkpoints=(), dx=0.0, dy=0.0,
                  bg_params=None, qc_params=None, residual_max=None,
                  reference_centers=None):
    """单帧配准 + 逐泳道密度曲线 + 逐斑点面积/质心/饱和。

    lanes: [{id, x0, x1, peaks:[{id, y0, y1}]}](冻结边界,校正空间)。
    reference_centers: {peak_id: center_y}(冻结峰中心,用于漂移判定)。
    返回 dict:profiles {lane_id: [float]},spots [{lane_id, peak_id, area,
    height, center_y, saturated_px, drift}], residual, size。
    配准失败抛 RegistrationError。
    """
    bgp = dict(background.DEFAULTS); bgp.update(bg_params or {})
    qct = dict(qc.THRESHOLDS); qct.update(qc_params or {})
    with Image.open(image_path) as im:
        gray, H, resid = rectify_frame(im, derived, pairs, checkpoints, dx, dy,
                                       residual_max)
    bg = background.estimate_background(gray, **bgp)
    sig = background.signal_image(gray, bg)
    W, Hh = sig.size
    profiles, spots = {}, []
    for lane in lanes:
        lid, x0, x1 = lane["id"], float(lane["x0"]), float(lane["x1"])
        if qc.lane_out_of_bounds(x0, x1, W):
            continue
        prof = profile.lane_profile(sig, x0, x1)
        profiles[lid] = prof
        for p in sorted(lane.get("peaks", []), key=lambda p: p["y0"]):
            r = profile.integrate(prof, p["y0"], p["y1"])
            sat = qc.count_saturated(gray, x0, x1, p["y0"], p["y1"],
                                     lo=qct["sat_lo"], hi=qct["sat_hi"])
            drift_v = None
            ref_cy = (reference_centers or {}).get(p["id"])
            if ref_cy is not None:
                drift_v = abs(r["centroid"] - ref_cy)
            spots.append({
                "lane_id": lid, "peak_id": p["id"],
                "area": r["area"], "height": r["height"],
                "center_y": r["centroid"], "saturated_px": sat,
                "drift_px": drift_v,
                "y0": float(p["y0"]), "y1": float(p["y1"]),
            })
    return {"profiles": profiles, "spots": spots, "residual": resid,
            "width": W, "height": Hh}


def downsample_profile(prof, n=240):
    """密度曲线降采样供前端绘制(等窗均值)。"""
    if len(prof) <= n:
        return [float(v) for v in prof]
    step = len(prof) / n
    out = []
    for i in range(n):
        a = int(round(i * step)); b = max(a + 1, int(round((i + 1) * step)))
        seg = prof[a:min(b, len(prof))]
        out.append(sum(seg) / len(seg) if seg else 0.0)
    return out


# ---------- 动力学:平台 / 峰 / 衰减 ----------

def classify_series(values, tol=None, plateau_min=None):
    """把一条(有效帧)面积序列分段为 rise / plateau / decay,并定峰。

    values: [(t_sec, value), ...](已按时间排序)。
    返回 {segments:[{kind, i0, i1, ...}], peak_index, plateau:[i0,i1]|None}。
    判定(基于峰值归一化,容许 tol 抗噪):
      自峰位向左/右扩展,值 >= (1-tol)*peak 的最长连续段为平台候选;
      峰左侧低于阈值 => rise;右侧低于阈值 => decay;
      平台帧数不足时返回 None,调用方按“无稳定平台”处理。
    """
    tol = DEFAULTS["plateau_tol"] if tol is None else tol
    plateau_min = DEFAULTS["plateau_min_frames"] if plateau_min is None else plateau_min
    n = len(values)
    if n == 0:
        return {"segments": [], "peak_index": None, "plateau": None}
    peak_i = max(range(n), key=lambda i: values[i][1])
    peak_v = values[peak_i][1]
    threshold = peak_v * (1.0 - tol) if peak_v > 0 else 0.0
    l = peak_i
    while l - 1 >= 0 and values[l - 1][1] >= threshold:
        l -= 1
    r = peak_i
    while r + 1 < n and values[r + 1][1] >= threshold:
        r += 1
    segs = []
    if l > 0:
        segs.append({"kind": "rise", "i0": 0, "i1": l - 1})
    segs.append({"kind": "plateau", "i0": l, "i1": r})
    if r < n - 1:
        segs.append({"kind": "decay", "i0": r + 1, "i1": n - 1})
    plateau = [l, r] if (r - l + 1) >= plateau_min else None
    return {"segments": segs, "peak_index": peak_i, "plateau": plateau}


def analyze_series(times, areas, tol=None, plateau_min=None):
    """单斑点曲线分析。times/areas 等长(含 None 帧与排除帧由调用方剔除)。

    返回 {times, areas, normalized, peak, plateau:[i0,i1]|None,
    plateau_range:[t0,t1]|None, segments}。
    """
    pairs = [(t, a) for t, a in zip(times, areas)
             if t is not None and a is not None]
    pairs.sort(key=lambda z: z[0])
    res = classify_series(pairs, tol, plateau_min)
    ts = [p[0] for p in pairs]
    vs = [p[1] for p in pairs]
    peak = None
    if res["peak_index"] is not None:
        pi = res["peak_index"]
        peak = {"index": pi, "t": ts[pi], "value": vs[pi]}
    prange = None
    if res["plateau"]:
        i0, i1 = res["plateau"]
        prange = [ts[i0], ts[i1]]
    mx = max(vs) if vs else 0.0
    normalized = [v / mx for v in vs] if mx > 0 else [0.0] * len(vs)
    return {"times": ts, "areas": vs, "normalized": normalized,
            "peak": peak, "plateau": res["plateau"],
            "plateau_range": prange, "segments": res["segments"]}


def suggest_window(spot_curves, min_frames=None, tol=None):
    """在所有斑点平台帧区间上求公共取值窗口。

    选择策略:各斑点平台(帧数满足)求交集,取覆盖斑点数最多且帧数足够的
    连续区间;无法满足时返回 None(界面不得确认取值窗口)。
    spot_curves: [{peak_id, times, plateau:[i0,i1]|None, ...}]
    返回 {t0, t1, frame_indices?} 或 None。
    """
    min_frames = DEFAULTS["min_valid_frames"] if min_frames is None else min_frames
    tol = DEFAULTS["plateau_tol"] if tol is None else tol
    intervals = []
    for sc in spot_curves:
        if not sc.get("plateau"):
            continue
        i0, i1 = sc["plateau"]
        intervals.append((sc["times"][i0], sc["times"][i1], sc["peak_id"]))
    if not intervals:
        return None
    # 公共交集
    t0 = max(a for a, _, _ in intervals)
    t1 = min(b for _, b, _ in intervals)
    if t1 >= t0:
        covered = [pid for a, b, pid in intervals if a <= t0 and b >= t1]
        if len(covered) >= 1:
            return {"t0": t0, "t1": t1, "covered_peaks": sorted(covered),
                    "common": True}
    # 无公共平台:按帧统计处于平台的斑点数,选支持最多的连续区间
    # (统计在各曲线并集时间点上的覆盖数)
    all_t = sorted({t for sc in spot_curves if sc.get("plateau")
                    for t in sc["times"][sc["plateau"][0]:sc["plateau"][1] + 1]})
    best = None
    for i, ta in enumerate(all_t):
        for tb in all_t[i:]:
            cov = [sc["peak_id"] for sc in spot_curves if sc.get("plateau")
                   and sc["times"][sc["plateau"][0]] <= ta
                   and sc["times"][sc["plateau"][1]] >= tb]
            if len(cov) >= 1 and (best is None or len(cov) > len(best[2])):
                best = (ta, tb, cov)
    if best and len(all_t) >= min_frames:
        return {"t0": best[0], "t1": best[1], "covered_peaks": sorted(best[2]),
                "common": False}
    return None


def window_means(times, areas, t0, t1):
    """窗口 [t0,t1] 内有效点的均值/SD/CV/帧数;窗口无点返回 None。"""
    sel = [a for t, a in zip(times, areas)
           if t is not None and a is not None and t0 - 1e-9 <= t <= t1 + 1e-9]
    if not sel:
        return None
    n = len(sel)
    mean = sum(sel) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in sel) / (n - 1)) if n > 1 else 0.0
    cv = 100.0 * sd / mean if mean > 0 else None
    return {"mean": mean, "sd": sd, "cv_pct": cv, "n": n}


# ---------- 序列级校审与窗口校验 ----------

def validate_frames(ordered):
    """ordered: [{frame_id, t_sec, usable(bool), issue:{kind,message}|None}]。

    汇总帧数/时刻问题(重复倒序由 frame_times 给出)。返回问题列表与
    可用帧 id。few_frames / no_frames 为阻断错误。
    """
    issues = []
    usable = [f for f in ordered if f["usable"] and f["t_sec"] is not None]
    if not ordered:
        issues.append(_issue("no_frames", "error", "序列中还没有任何帧"))
    elif len(usable) < DEFAULTS["min_valid_frames"]:
        issues.append(_issue(
            "few_frames", "error",
            f"有效帧仅 {len(usable)} 个,不足 {DEFAULTS['min_valid_frames']} 个,"
            "不能确认取值窗口", [f["frame_id"] for f in usable]))
    return issues, usable


def validate_window(window, frame_records, spot_curve_by_peak, cfg=None):
    """校验用户确认的取值窗口 [t0,t1]。

    frame_records: [{id, t_sec, usable, excluded, per_spot:{peak_id:{
                    area, saturated_px, drift_px, center_y}}, sat_any}]
    spot_curve_by_peak: {peak_id: analyze_series 结果}(含 segments/plateau)。
    返回 {ok, issues:[...], window_stats:{peak_id: window_means}}。
    """
    c = dict(DEFAULTS); c.update(cfg or {})
    issues = []
    t0, t1 = window["t0"], window["t1"]
    if t1 <= t0:
        issues.append(_issue("window_invalid", "error",
                             "取值窗口结束时刻须晚于开始时刻"))
        return {"ok": False, "issues": issues, "window_stats": {}}
    in_win = [f for f in frame_records if not f["excluded"] and f["usable"]
              and f["t_sec"] is not None and t0 - 1e-9 <= f["t_sec"] <= t1 + 1e-9]
    if len(in_win) < c["min_valid_frames"]:
        issues.append(_issue(
            "window_invalid", "error",
            f"取值窗口内有效帧仅 {len(in_win)} 个,不足 {c['min_valid_frames']} 个",
            [f["id"] for f in in_win]))
    stats = {}
    sat_frames, drift_frames = set(), set()
    for peak_id, sc in spot_curve_by_peak.items():
        wm = window_means(sc["times"], sc["areas"], t0, t1)
        stats[peak_id] = wm
        if wm is None:
            issues.append(_issue(
                "window_invalid", "error",
                f"斑点(峰 {peak_id})在窗口内没有任何有效面积数据",
                peak_id=peak_id))
            continue
        # 平台一致性:窗口不应跨越平台之外(超出平台容差的帧 => 取到上升/衰减)
        if sc.get("plateau"):
            i0, i1 = sc["plateau"]
            pa, pb = sc["times"][i0], sc["times"][i1]
            mean = wm["mean"]
            for t, v in zip(sc["times"], sc["areas"]):
                if t0 - 1e-9 <= t <= t1 + 1e-9 and mean > 0 and \
                        abs(v - mean) / mean > c["plateau_tol"]:
                    issues.append(_issue(
                        "window_invalid", "error",
                        f"峰 {peak_id} 的取值窗口跨过非平台帧(t={format_t(t)},"
                        f" A={v:.0f} 偏离窗口均值 {mean:.0f} 超过 "
                        f"{c['plateau_tol'] * 100:.0f}%),请缩到稳定平台内",
                        [next((f["id"] for f in frame_records if f["t_sec"] == t), None)],
                        peak_id=peak_id))
                    break
        else:
            issues.append(_issue(
                "window_invalid", "error",
                f"斑点(峰 {peak_id})未识别出满足容差的稳定平台,不能确认取值窗口",
                peak_id=peak_id))
        # 窗口内逐帧饱和 / 漂移
        for f in in_win:
            ps = f.get("per_spot", {}).get(peak_id)
            if not ps:
                continue
            if ps["saturated_px"] >= qc.THRESHOLDS["sat_min_count"]:
                sat_frames.add(f["id"])
            if ps.get("drift_px") is not None and ps["drift_px"] > c["drift_centroid_px"]:
                drift_frames.add(f["id"])
    if sat_frames:
        issues.append(_issue(
            "window_saturated", "error",
            f"取值窗口内 {len(sat_frames)} 帧存在像素饱和,面积被截断,"
            "请排除坏帧(填理由)或缩小窗口", sorted(sat_frames)))
    if drift_frames:
        issues.append(_issue(
            "window_drift", "error",
            f"取值窗口内 {len(drift_frames)} 帧斑点质心漂移超过 "
            f"{c['drift_centroid_px']:g}px,请排除或重配准", sorted(drift_frames)))
    return {"ok": not issues, "issues": issues, "window_stats": stats}


# ---------- 成图(PIL,ASCII 标注) ----------

def draw_kinetics(analysis, curves, window, frames, stale=False, plateau_tol=None):
    """绘动力学曲线图:上=各斑点面积-时间(归一化),下=原始面积。

    curves: [{lane_label, spot_no, peak_id, times, areas, normalized,
              plateau, peak, window_mean}]
    frames: [{seq, t_sec, excluded, usable}]
    返回 RGB 图(图内文字一律 ASCII)。
    """
    from PIL import Image, ImageDraw
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("DejaVuSans.ttf", 11)
        small = ImageFont.truetype("DejaVuSans.ttf", 10)
        bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 13)
    except OSError:
        font = small = bold = ImageFont.load_default()

    W, H = 920, 640
    ML, MR, MT, MB = 64, 250, 64, 40
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img, "RGBA")
    palette = [(31, 110, 200), (214, 90, 30), (40, 150, 80), (160, 60, 170),
               (200, 170, 30), (30, 160, 170), (190, 60, 90), (90, 120, 60)]

    all_t = [t for c in curves for t in c["times"]]
    tmax = max(all_t) if all_t else 1.0
    panels = [(MT, H // 2 - 20, "normalized response", 1.08),
              (H // 2 + 10, H - MB, "spot area", None)]
    ascii_name = (analysis.get("name", "") or "").encode("ascii", "replace").decode()
    d.text((ML, 10), f"analysis #{analysis.get('id')} {ascii_name}  |  "
           f"color-development kinetics  |  {len(curves)} spots, "
           f"{len(frames)} frames", font=bold, fill=(20, 24, 30))
    if window:
        d.text((ML, 30), f"locked window t=[{format_t(window['t0'])}, "
               f"{format_t(window['t1'])}]  plateau tol="
               f"{(plateau_tol or DEFAULTS['plateau_tol']) * 100:.0f}%",
               font=small, fill=(40, 120, 70))

    for pi, (top, bot, ylabel, yfix) in enumerate(panels):
        d.rectangle([ML, top, W - MR, bot], outline=(120, 128, 140))
        ymax = yfix
        if ymax is None:
            ymax = max([v for c in curves for v in c["areas"]] + [1.0]) * 1.1
        # 窗口底色
        if window:
            x0 = ML + window["t0"] / tmax * (W - MR - ML)
            x1 = ML + window["t1"] / tmax * (W - MR - ML)
            d.rectangle([x0, top, x1, bot], fill=(40, 150, 80, 22))
        # 网格
        for i in range(5):
            gx = ML + i * (W - MR - ML) / 4
            gy = top + i * (bot - top) / 4
            d.line([gx, top, gx, bot], fill=(232, 234, 238))
            d.line([ML, gy, W - MR, gy], fill=(232, 234, 238))
            d.text((gx - 12, bot + 4), format_t(tmax * i / 4), font=small,
                   fill=(90, 98, 110))
            d.text((ML - 46, gy - 6), f"{ymax * (1 - i / 4):.2g}", font=small,
                   fill=(90, 98, 110))
        d.text((ML, top - 14), ylabel, font=small, fill=(60, 66, 76))

        def X(t):
            return ML + t / tmax * (W - MR - ML)

        def Y(v):
            return bot - v / ymax * (bot - top)

        for ci, c in enumerate(curves):
            col = palette[ci % len(palette)]
            vs = c["normalized"] if pi == 0 else c["areas"]
            pts = [(X(t), Y(v)) for t, v in zip(c["times"], vs)]
            for p, q in zip(pts, pts[1:]):
                d.line([p, q], fill=col, width=2)
            for (x, y), fr in zip(pts, frames):
                if fr["excluded"]:
                    d.ellipse([x - 3, y - 3, x + 3, y + 3], outline=(150, 150, 150))
            if c.get("peak"):
                pk = c["peak"]
                pv = pk["value"] if pi == 1 else max(c["normalized"] or [0])
                px_, py_ = X(pk["t"]), Y(pv)
                d.ellipse([px_ - 3, py_ - 3, px_ + 3, py_ + 3],
                          fill=(220, 60, 60), outline=(120, 20, 20))
        d.text((ML, bot + 16), "time after spraying (mm:ss)", font=small,
               fill=(60, 66, 76))

    # 图例
    lx, ly = W - MR + 14, MT
    d.text((lx, ly - 14), "spots:", font=small, fill=(60, 66, 76))
    for ci, c in enumerate(curves):
        col = palette[ci % len(palette)]
        d.line([lx, ly + 3, lx + 14, ly + 3], fill=col, width=2)
        tag = f"{c.get('lane_label', '?')}-{c.get('spot_no', '?')} (p{c['peak_id']})"
        if c.get("window_mean") is not None:
            tag += f" A={c['window_mean']:.0f}"
        d.text((lx + 18, ly - 3), tag[:30], font=small, fill=(50, 56, 66))
        ly += 16
    d.ellipse([lx, ly, lx + 6, ly + 6], fill=(220, 60, 60))
    d.text((lx + 12, ly - 2), "peak", font=small, fill=(60, 66, 76))
    d.rectangle([lx, ly + 14, lx + 10, ly + 24], fill=(40, 150, 80, 40))
    d.text((lx + 14, ly + 13), "locked window", font=small, fill=(60, 66, 76))

    if stale:
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        do = ImageDraw.Draw(ov)
        do.text((W / 2 - 170, H / 2), "EXPIRED - SOURCE CHANGED",
                font=bold, fill=(200, 60, 60, 70))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img
