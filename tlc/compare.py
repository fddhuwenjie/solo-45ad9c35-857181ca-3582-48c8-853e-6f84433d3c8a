"""跨板对照计算:标准品分配、归一化系数、目标候选与汇总(纯函数,可复算)。

模型:同一批样品分在多块板上展开,各板指定一条共同的标准品泳道(含全部目标
成分的标准品)。目标斑点按参考 Rf 定义。程序按 Rf 容差 + 峰形相关 + 标准品次序
为每板每目标提出候选;用户可改绑、拆开或锁定。汇总只纳入通过质控的板:
标准缺失 / 次序颠倒 / Rf 偏移超限 / 系数离群 / 板数据已变更 的板一律排除并说明原因。
"""

import math
import statistics

DEFAULTS = {
    "std_rf_tol": 0.08,        # 标准品名义容许 Rf 偏移;超出即判“Rf 偏移超限”
    "rf_shift_max": 0.15,      # 标准品匹配硬上限;超出判“标准缺失”
    "coef_outlier_ratio": 3.0, # 归一化系数离群倍数(超出 [1/R, R] 判离群)
    "shape_weight": 0.4,       # 候选评分中峰形相关的权重(其余为 Rf 接近度)
    "max_candidates": 5,       # 每目标保留的候选数
    "shape_samples": 16,       # 峰形比较的重采样点数
}

TARGET_COLORS = ["#e6550d", "#3182bd", "#31a354", "#756bb1",
                 "#d6616b", "#e7ba52", "#63c5da", "#ce6dbd"]


# ---------- 标准品分配 ----------

def _monotonic_assign(devs, tol=None):
    """devs[i][j] = 目标 i 与标准点 j 的 |ΔRf|(两边均已按 Rf 升序)。

    单调(不交叉)分配:最大化匹配数,再最小化总偏差。返回 [(i, j)]。
    直线上带容差的二分匹配若有解必有单调解(交换论证),故单调约束不漏解。
    """
    n = len(devs)
    m = len(devs[0]) if n else 0
    dp = [[(0, 0.0)] * (m + 1) for _ in range(n + 1)]
    parent = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(0, m + 1):
            best, bp = dp[i - 1][j], (i - 1, j)          # 跳过目标 i
            if j > 0:
                v = dp[i][j - 1]                          # 跳过标准点 j
                if (v[0], -v[1]) > (best[0], -best[1]):
                    best, bp = v, (i, j - 1)
                d = devs[i - 1][j - 1]                    # 匹配 i-1 ↔ j-1
                if tol is None or d <= tol:
                    prev = dp[i - 1][j - 1]
                    v = (prev[0] + 1, prev[1] + d)
                    if (v[0], -v[1]) > (best[0], -best[1]):
                        best, bp = v, (i - 1, j - 1, True)
            dp[i][j] = best
            parent[i][j] = bp
    pairs = []
    i, j = n, m
    while i > 0:
        bp = parent[i][j]
        if bp is None:
            break
        if len(bp) == 3:
            pairs.append((i - 1, j - 1))
        i, j = bp[0], bp[1]
    pairs.reverse()
    return pairs


def assign_standards(targets, std_spots, cfg):
    """把标准品泳道斑点分配给各目标(按 rf_ref / rf 升序单调分配)。

    返回 (assignment {target_id: spot}, offset, problems)。
    offset = 各标准对 (实测Rf - 参考Rf) 的中位数(板级 Rf 偏移)。
    problems: std_missing(标准缺失) / rf_shift(Rf 偏移超限)。
    """
    ts = sorted(targets, key=lambda t: t["rf_ref"])
    ss = sorted(std_spots, key=lambda s: s["rf"])
    if not ss:
        return {}, None, [{"type": "std_missing",
                           "message": "标准品泳道没有任何斑点,无法建立标准"}]
    devs = [[abs(s["rf"] - t["rf_ref"]) for s in ss] for t in ts]
    pairs = _monotonic_assign(devs, tol=cfg["rf_shift_max"])
    assignment = {ts[i]["id"]: ss[j] for i, j in pairs}
    problems = []
    missing = [t for t in ts if t["id"] not in assignment]
    if missing:
        names = "、".join(t["name"] for t in missing)
        problems.append({
            "type": "std_missing",
            "message": f"标准品泳道缺少目标 {names} 的标准斑点"
                       f"(匹配硬上限 ±{cfg['rf_shift_max']:.2f})",
            "target_ids": [t["id"] for t in missing]})
    offset = None
    if assignment:
        offs = [assignment[t["id"]]["rf"] - t["rf_ref"] for t in ts if t["id"] in assignment]
        offset = statistics.median(offs)
        if abs(offset) > cfg["std_rf_tol"]:
            problems.append({
                "type": "rf_shift",
                "message": f"板级 Rf 偏移 {offset:+.3f} 超出容许 ±{cfg['std_rf_tol']:.2f}"})
    return assignment, offset, problems


# ---------- 峰形比较 ----------

def _resample(seg, n):
    if len(seg) == 1:
        return list(seg) * n
    out = []
    for k in range(n):
        pos = k * (len(seg) - 1) / (n - 1)
        i = int(pos)
        f = pos - i
        out.append(seg[i] * (1 - f) + seg[min(i + 1, len(seg) - 1)] * f)
    return out


def shape_corr(prof_a, y0a, y1a, prof_b, y0b, y1b, n=16):
    """两段峰形(各自重采样到 n 点)的 Pearson 相关,越接近 1 形状越像。"""
    def seg(p, y0, y1):
        a = max(0, int(math.floor(y0)))
        b = min(len(p) - 1, int(math.ceil(y1)))
        return p[a:b + 1] if b > a else None
    sa, sb = seg(prof_a, y0a, y1a), seg(prof_b, y0b, y1b)
    if not sa or not sb:
        return 0.0
    xa, xb = _resample(sa, n), _resample(sb, n)
    ma, mb = sum(xa) / n, sum(xb) / n
    va = sum((v - ma) ** 2 for v in xa)
    vb = sum((v - mb) ** 2 for v in xb)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((u - ma) * (v - mb) for u, v in zip(xa, xb))
    return cov / math.sqrt(va * vb)


# ---------- 目标候选 ----------

def propose_targets(targets, spots, std_lane_id, offset, std_assignment, profiles, cfg):
    """为每个目标在非标准品泳道中提出候选并评分。

    评分 = (1-w)·Rf接近度 + w·峰形相关(与该目标的标准斑点比)。
    同一泳道内按目标 rf_ref 升序做单调分配,保证匹配次序与标准品次序一致,
    且一个斑点不会被两个目标共用。返回 (best, candidates):
      best[target_id]       = 最优候选 dict
      candidates[target_id] = 按得分降序的候选列表(<= max_candidates)
    """
    ts = sorted(targets, key=lambda t: t["rf_ref"])
    by_lane = {}
    for s in spots:
        if s["lane_id"] == std_lane_id:
            continue
        by_lane.setdefault(s["lane_id"], []).append(s)
    best = {}
    candidates = {t["id"]: [] for t in ts}
    std_prof = profiles.get(std_lane_id)
    for lid, lspots in by_lane.items():
        lspots = sorted(lspots, key=lambda s: s["rf"])
        prof = profiles.get(lid)
        score = {}   # (目标序号, 斑点序号) -> (score, cand)
        for ti, t in enumerate(ts):
            exp = t["rf_ref"] + (offset or 0.0)
            for si, s in enumerate(lspots):
                dev = abs(s["rf"] - exp)
                if dev > t["rf_tol"]:
                    continue
                std = std_assignment.get(t["id"])
                if std and prof is not None and std_prof is not None:
                    shape = max(0.0, shape_corr(std_prof, std["y0"], std["y1"],
                                                prof, s["y0"], s["y1"],
                                                cfg["shape_samples"]))
                else:
                    shape = 0.5   # 无标准可比对时取中性
                sc = (1 - cfg["shape_weight"]) * (1 - dev / t["rf_tol"]) \
                    + cfg["shape_weight"] * shape
                score[(ti, si)] = (sc, {
                    "peak_id": s["peak_id"], "lane_id": lid,
                    "lane_label": s.get("lane_label", ""),
                    "spot_no": s.get("spot_no"), "rf": s["rf"], "area": s["area"],
                    "dev": dev, "shape": shape, "score": sc})
        # 泳道内单调分配(与标准品次序一致)
        n, m = len(ts), len(lspots)
        dp = [[0.0] * (m + 1) for _ in range(n + 1)]
        parent = [[None] * (m + 1) for _ in range(n + 1)]
        for i in range(1, n + 1):
            for j in range(0, m + 1):
                bv, bp = dp[i - 1][j], (i - 1, j)
                if j > 0:
                    if dp[i][j - 1] > bv:
                        bv, bp = dp[i][j - 1], (i, j - 1)
                    if (i - 1, j - 1) in score:
                        v = dp[i - 1][j - 1] + score[(i - 1, j - 1)][0]
                        if v > bv:
                            bv, bp = v, (i - 1, j - 1, True)
                dp[i][j] = bv
                parent[i][j] = bp
        i, j = n, m
        while i > 0:
            bp = parent[i][j]
            if bp is None:
                break
            if len(bp) == 3:
                cand = score[(i - 1, j - 1)][1]
                tid = ts[i - 1]["id"]
                if tid not in best or cand["score"] > best[tid]["score"]:
                    best[tid] = cand
            i, j = bp[0], bp[1]
        for (ti, _), (sc, cand) in score.items():
            candidates[ts[ti]["id"]].append(cand)
    for tid in candidates:
        candidates[tid].sort(key=lambda c: -c["score"])
        del candidates[tid][cfg["max_candidates"]:]
    return best, candidates


# ---------- 匹配次序检查(对最终确认关系) ----------

def order_violations(targets, member_matches, spots_by_peak):
    """同一泳道内,目标 rf_ref 次序与所绑斑点 Rf 次序必须一致(标准品次序)。

    自动提议已保证次序,此检查主要拦截用户手动改绑出的颠倒。
    返回冲突目标对 [(target_id_a, target_id_b)](rf_ref 低的反而绑了更高 Rf 的斑点)。
    """
    tby = {t["id"]: t for t in targets}
    by_lane = {}
    for mt in member_matches:
        if mt.get("peak_id") is None:
            continue
        spot = spots_by_peak.get(mt["peak_id"])
        if not spot or mt["target_id"] not in tby:
            continue
        by_lane.setdefault(mt["lane_id"], []).append(
            (tby[mt["target_id"]]["rf_ref"], spot["rf"], mt["target_id"]))
    bad = []
    for arr in by_lane.values():
        arr.sort(key=lambda v: v[0])
        for a, b in zip(arr, arr[1:]):
            if b[1] < a[1] - 1e-9:
                bad.append((a[2], b[2]))
    return bad


# ---------- 归一化系数与汇总 ----------

def normalization(responses, ratio):
    """由标准品响应(标准斑点面积和)求各板归一化系数。

    coef = 参考响应 / 本板响应,参考取各板响应中位数(抗离群)。
    返回 (coefs {member_id: coef}, outliers {member_id})。
    """
    ok = {m: r for m, r in responses.items() if r and r > 0}
    if not ok:
        return {}, set()
    ref = statistics.median(ok.values())
    coefs = {m: ref / r for m, r in ok.items()}
    outliers = {m for m, cf in coefs.items() if cf > ratio or cf < 1.0 / ratio}
    return coefs, outliers


def summarize(values):
    """板间变异统计:n / 均值 / 样本标准差 / CV% / 极差。"""
    n = len(values)
    if not n:
        return {"n": 0, "mean": None, "sd": None, "cv_pct": None, "min": None, "max": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "mean": mean, "sd": sd,
            "cv_pct": (100.0 * sd / mean) if mean else 0.0,
            "min": min(values), "max": max(values)}


# ---------- 对照图 ----------

def _hex(color):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _dashed_line(d, p, q, color, dash=6, gap=4, width=2):
    x1, y1 = p
    x2, y2 = q
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= 0:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    t = 0.0
    while t < length:
        t2 = min(length, t + dash)
        d.line([(x1 + ux * t, y1 + uy * t), (x1 + ux * t2, y1 + uy * t2)],
               fill=color, width=width)
        t = t2 + gap


def _font(size):
    from PIL import ImageFont
    for name in ("DejaVuSans.ttf",):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_comparison(panels, links, legend, out_h=420):
    """拼接对照图:各板校正图并排,标准品泳道底色,目标斑点标记,相邻板同目标连线。

    panels: [{title, subtitle, valid, image, std_lane:(x0,x1)|None,
              marks:[{x, y, color, label, locked}]}]   (x/y 为各自图像坐标)
    links:  [{p1, m1, p2, m2, color, dashed}]          (m1/m2 为 marks 下标)
    legend: [(color, text)]
    返回 RGB 图。注:运行环境无中文字体,图内文字用 ASCII 标记(T1/T2…)。
    """
    from PIL import Image, ImageDraw
    PAD, GAP, HEAD = 14, 56, 46
    font, small = _font(13), _font(11)
    scaled = []
    for p in panels:
        img = p["image"].convert("RGB")
        k = out_h / img.height
        w = max(1, round(img.width * k))
        scaled.append((img.resize((w, out_h), Image.BICUBIC), k))
    leg_rows = max(1, (len(legend) + 2) // 3)
    W = PAD * 2 + sum(im.width for im, _ in scaled) + GAP * max(0, len(scaled) - 1)
    H = PAD + HEAD + out_h + 10 + leg_rows * 18 + PAD
    out = Image.new("RGB", (W, H), (24, 26, 30))
    d = ImageDraw.Draw(out, "RGBA")
    origins = []
    x = PAD
    for (im, k), p in zip(scaled, panels):
        y0 = PAD + HEAD
        out.paste(im, (x, y0))
        ok = p["valid"]
        d.rectangle([x, y0, x + im.width, y0 + out_h],
                    outline=(58, 64, 72) if ok else (229, 83, 75), width=2)
        d.text((x, PAD + 2), p["title"], font=font,
               fill=(230, 232, 235) if ok else (229, 83, 75))
        d.text((x, PAD + 20), p["subtitle"], font=small,
               fill=(154, 163, 173) if ok else (229, 83, 75))
        if p.get("std_lane"):
            x0, x1 = p["std_lane"]
            d.rectangle([x + x0 * k, y0, x + x1 * k, y0 + out_h], fill=(79, 156, 249, 40))
        for mk in p["marks"]:
            cx, cy = x + mk["x"] * k, y0 + mk["y"] * k
            rgb = _hex(mk["color"])
            d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=rgb, width=2)
            d.text((cx + 8, cy - 8), mk["label"], font=small, fill=rgb)
        origins.append((x, y0, k))
        x += im.width + GAP
    for lk in links:
        x1, y1, k1 = origins[lk["p1"]]
        x2, y2, k2 = origins[lk["p2"]]
        m1 = panels[lk["p1"]]["marks"][lk["m1"]]
        m2 = panels[lk["p2"]]["marks"][lk["m2"]]
        a = (x1 + m1["x"] * k1, y1 + m1["y"] * k1)
        b = (x2 + m2["x"] * k2, y2 + m2["y"] * k2)
        rgb = _hex(lk["color"])
        if lk.get("dashed"):
            _dashed_line(d, a, b, rgb)
        else:
            d.line([a, b], fill=rgb, width=2)
    ly = PAD + HEAD + out_h + 12
    for i, (color, text) in enumerate(legend):
        col, row = i % 3, i // 3
        d.text((PAD + col * max(1, W // 3), ly + row * 18), text, font=small, fill=_hex(color))
    return out
