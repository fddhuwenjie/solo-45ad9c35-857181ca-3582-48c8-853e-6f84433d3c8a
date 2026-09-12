"""校准曲线与含量反算(纯函数,可复算,不依赖 Web 框架)。

同板展开的标准系列与未知样:用户把泳道标为 standard / blank / unknown,
填写标准浓度、进样体积与稀释倍数并绑定目标斑点。本模块以斑点积分面积为响应,
点样量 x = 浓度 c × 进样体积 v,支持三种模型:

- linear:       普通线性      y = a + b·x(OLS,带截距)
- linear_zero:  过零线性      y = b·x
- wls_1overx:   1/x 加权线性  y = a + b·x(权重 1/x)

反算:x̂ = (y - a) / b;点样液浓度 x̂/v;原样品浓度 x̂/v × 稀释倍数。

标准点不足 / 同浓度重复响应冲突 / 响应不单调 / 空白异常 / 斜率非正 /
样品落在工作范围外等情形均定位到泳道,并阻断相应含量结论。
"""

import math

MODELS = ("linear", "linear_zero", "wls_1overx")
MODEL_LABELS = {
    "linear": "普通线性 y=a+bx",
    "linear_zero": "过零线性 y=bx",
    "wls_1overx": "1/x 加权线性 y=a+bx",
}

DEFAULTS = {
    "rf_hint_tol": 0.08,        # 自动建议斑点:与目标 Rf 的最大偏差
    "min_standards": 2,         # 成线所需最少标准点
    "dup_response_cv_pct": 15.0,  # 同浓度重复:单位点样量响应 CV% 超过即冲突
    "blank_response_pct": 10.0,   # 空白响应占最低标准响应的百分比上限
    "monotonic_tol": 0.05,        # 单调性允许的相对回落(抗噪)
    "low_r2": 0.99,               # R² 低于此值给警告(不阻断)
    "max_candidates": 8,          # 每泳道自动建议候选斑点数
}


# ---------- 拟合 ----------

def fit_model(xs, ys, model):
    """三种最小二乘拟合。返回 {slope, intercept, r2, kind}。

    R² 口径与拟合一致:带截距模型用中心化 R²,过零模型用未中心化 R²,
    加权模型用加权中心化 R²。退化输入(零方差等)返回斜率 0。
    """
    n = len(xs)
    if n == 0:
        return {"slope": 0.0, "intercept": 0.0, "r2": None}
    if model == "linear_zero":
        sxx = sum(x * x for x in xs)
        if sxx <= 0:
            return {"slope": 0.0, "intercept": 0.0, "r2": None}
        b = sum(x * y for x, y in zip(xs, ys)) / sxx
        a = 0.0
        ss_res = sum((y - b * x) ** 2 for x, y in zip(xs, ys))
        ss_tot = sum(y * y for y in ys)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
        return {"slope": b, "intercept": a, "r2": r2}

    if model == "wls_1overx":
        w = [1.0 / x if x > 0 else 0.0 for x in xs]
        W = sum(w)
        if W <= 0:
            return {"slope": 0.0, "intercept": 0.0, "r2": None}
        swx = sum(wi * x for wi, x in zip(w, xs))
        swy = sum(wi * y for wi, y in zip(w, ys))
        swxx = sum(wi * x * x for wi, x in zip(w, xs))
        swxy = sum(wi * x * y for wi, x, y in zip(w, xs, ys))
        den = W * swxx - swx * swx
        if abs(den) <= 1e-18:
            return {"slope": 0.0, "intercept": swy / W, "r2": None}
        b = (W * swxy - swx * swy) / den
        a = (swy - b * swx) / W
        ybar = swy / W
        ss_res = sum(wi * (y - (a + b * x)) ** 2 for wi, x, y in zip(w, xs, ys))
        ss_tot = sum(wi * (y - ybar) ** 2 for wi, y in zip(w, ys))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
        return {"slope": b, "intercept": a, "r2": r2}

    # 默认普通线性 OLS
    if n < 2:
        return {"slope": 0.0, "intercept": ys[0] if n else 0.0, "r2": None}
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    den = sum((x - xbar) ** 2 for x in xs)
    if den <= 0:
        return {"slope": 0.0, "intercept": ybar, "r2": None}
    b = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / den
    a = ybar - b * xbar
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None
    return {"slope": b, "intercept": a, "r2": r2}


# ---------- 异常构造 ----------

def _issue(itype, level, message, lane_ids=None, peak_ids=None, scope="model"):
    return {"type": itype, "level": level, "scope": scope,
            "message": message, "lane_ids": sorted(set(lane_ids or [])),
            "peak_ids": sorted(set(peak_ids or []))}


# 阻断含量结论的模型级错误类型
BLOCKERS = {"exclude_reason_required", "invalid_amount", "nonpositive_response",
            "too_few_standards", "duplicate_conflict", "non_monotonic",
            "blank_anomaly", "bad_slope"}


# ---------- 主评估 ----------

def evaluate(standards, blanks, unknowns, model, cfg=None, target_name=""):
    """由已标注泳道数据评估校准并反算样品。

    standards: [{lane_id, lane_label, peak_id, spot_no, rf, response(area),
                 concentration, volume, excluded, exclude_reason}]
    blanks:    [{lane_id, lane_label, peak_id, response}]   (peak_id 可为 None)
    unknowns:  [{lane_id, lane_label, peak_id, spot_no, rf, response,
                 volume, dilution}]
    返回 {model, fit, points, samples, issues, blocked, blocker_types, range}。
    """
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if model not in MODELS:
        raise ValueError(f"未知模型: {model}")
    issues = []

    # --- 标准点整理与校验 ---
    points, excluded_points = [], []
    for s in standards:
        lane = s.get("lane_id")
        label = s.get("lane_label") or f"#{lane}"
        if s.get("excluded"):
            if not (s.get("exclude_reason") or "").strip():
                issues.append(_issue(
                    "exclude_reason_required", "error",
                    f"标准泳道 {label} 被排除但未填写理由,不能成线", [lane]))
            excluded_points.append(_point_dict(
                s, s["concentration"] * s["volume"]
                if s.get("concentration") and s.get("volume") else None))
            continue
        if not s.get("peak_id"):
            issues.append(_issue(
                "std_no_spot", "warning",
                f"标准泳道 {label} 尚未绑定目标斑点,不参与拟合", [lane]))
            continue
        conc, vol = s.get("concentration"), s.get("volume")
        if conc is None or vol is None or conc <= 0 or vol <= 0:
            issues.append(_issue(
                "invalid_amount", "error",
                f"标准泳道 {label} 的浓度或进样体积缺失/非正,无法计算点样量", [lane]))
            continue
        y = s.get("response")
        if y is None or y <= 0:
            issues.append(_issue(
                "nonpositive_response", "error",
                f"标准泳道 {label} 的响应面积为 {y},不能用于校准",
                [lane], [s.get("peak_id")]))
            continue
        points.append(_point_dict(s, conc * vol))

    # 同浓度重复响应冲突:单位点样量响应 area/(c·v) 的 CV
    by_conc = {}
    for p in points:
        by_conc.setdefault(p["concentration"], []).append(p)
    for conc, grp in sorted(by_conc.items()):
        if len(grp) < 2:
            continue
        per = [p["response"] / p["amount"] for p in grp]
        mean = sum(per) / len(per)
        sd = math.sqrt(sum((v - mean) ** 2 for v in per) / (len(per) - 1))
        cv = 100.0 * sd / mean if mean else 0.0
        if cv >= c["dup_response_cv_pct"]:
            names = "、".join(p["lane_label"] for p in grp)
            issues.append(_issue(
                "duplicate_conflict", "error",
                f"浓度 {conc:g} 的标准点({names})单位点样量响应 CV={cv:.1f}%"
                f"≥ {c['dup_response_cv_pct']:g}%,响应冲突,请核对浓度/绑定",
                [p["lane_id"] for p in grp], [p["peak_id"] for p in grp]))

    points.sort(key=lambda p: p["amount"])

    # 点数不足
    if len(points) < c["min_standards"]:
        issues.append(_issue(
            "too_few_standards", "error",
            f"有效标准点仅 {len(points)} 个,不足 {c['min_standards']} 个,不能建立校准曲线",
            [p["lane_id"] for p in points]))
    elif len(points) == 2:
        issues.append(_issue(
            "two_points", "warning",
            "仅有 2 个标准点,直线可定但无法评估线性,建议至少 3 个浓度水平",
            [p["lane_id"] for p in points]))

    # 单调性:响应应随点样量非递减(允许 monotonic_tol 抗噪回落)
    for lo, hi in zip(points, points[1:]):
        if hi["response"] < lo["response"] * (1.0 - c["monotonic_tol"]):
            issues.append(_issue(
                "non_monotonic", "error",
                f"响应不单调:{lo['lane_label']}(x={lo['amount']:g}, "
                f"A={lo['response']:.0f}) 的响应高于 {hi['lane_label']}"
                f"(x={hi['amount']:g}, A={hi['response']:.0f}),请核对标准次序与绑定",
                [lo["lane_id"], hi["lane_id"]], [lo["peak_id"], hi["peak_id"]]))

    # 空白
    bound_blanks = [b for b in blanks if b.get("peak_id") and (b.get("response") or 0) > 0]
    if not blanks:
        issues.append(_issue("no_blank", "warning", "未设置空白泳道(建议设置以监控背景干扰)"))
    elif not bound_blanks:
        issues.append(_issue(
            "blank_unbound", "warning",
            "空白泳道未绑定可积分斑点(背景干净可忽略,否则请绑定空白处峰)",
            [b.get("lane_id") for b in blanks]))
    if points and bound_blanks:
        y_min = min(p["response"] for p in points)
        for b in bound_blanks:
            pct = 100.0 * b["response"] / y_min
            if pct > c["blank_response_pct"]:
                issues.append(_issue(
                    "blank_anomaly", "error",
                    f"空白泳道 {b.get('lane_label')} 响应 A={b['response']:.0f} "
                    f"达最低标准点的 {pct:.1f}%(上限 {c['blank_response_pct']:g}%),"
                    "空白异常,含量结论不可靠",
                    [b.get("lane_id")], [b.get("peak_id")]))

    # --- 拟合 ---
    fit = None
    x_range = None
    if len(points) >= c["min_standards"]:
        xs = [p["amount"] for p in points]
        ys = [p["response"] for p in points]
        fit = fit_model(xs, ys, model)
        x_range = [min(xs), max(xs)]
        for p in points:
            p["fitted"] = fit["intercept"] + fit["slope"] * p["amount"]
            p["residual"] = p["response"] - p["fitted"]
            p["resid_pct"] = (100.0 * p["residual"] / p["fitted"]) if p["fitted"] else None
        if fit["slope"] <= 0:
            issues.append(_issue(
                "bad_slope", "error",
                f"拟合斜率 b={fit['slope']:.4g} 非正,响应未随浓度上升,校准无效"))
        elif fit["r2"] is not None and fit["r2"] < c["low_r2"]:
            issues.append(_issue(
                "low_r2", "warning",
                f"R²={fit['r2']:.4f} 低于 {c['low_r2']:g},线性不佳,请检查标准点或改用加权模型"))

    blocker_types = sorted({i["type"] for i in issues
                            if i["level"] == "error" and i["type"] in BLOCKERS})
    blocked = bool(blocker_types)

    # --- 样品反算 ---
    samples = []
    for u in unknowns:
        row = {
            "lane_id": u.get("lane_id"), "lane_label": u.get("lane_label") or "",
            "peak_id": u.get("peak_id"), "spot_no": u.get("spot_no"), "rf": u.get("rf"),
            "response": u.get("response"), "volume": u.get("volume"),
            "dilution": u.get("dilution"),
            "amount_back": None, "applied_concentration": None,
            "sample_concentration": None, "in_range": None,
            "status": "blocked", "reasons": [],
        }
        label = row["lane_label"] or f"#{row['lane_id']}"
        if blocked:
            row["reasons"].append("校准模型存在阻断性问题,暂不反算含量")
        elif not row["peak_id"] or not row["response"]:
            row["status"] = "no_spot"
            row["reasons"].append(f"未知样泳道 {label} 未绑定目标斑点")
            issues.append(_issue("unknown_no_spot", "warning", row["reasons"][-1],
                                 [row["lane_id"]], scope="sample"))
        elif not row["volume"] or row["volume"] <= 0 or \
                row["dilution"] is None or row["dilution"] <= 0:
            row["status"] = "incomplete"
            row["reasons"].append(f"未知样泳道 {label} 的进样体积/稀释倍数缺失或非正")
            issues.append(_issue("unknown_incomplete", "warning", row["reasons"][-1],
                                 [row["lane_id"]], scope="sample"))
        else:
            x = (row["response"] - fit["intercept"]) / fit["slope"]
            row["amount_back"] = x
            row["in_range"] = x_range[0] <= x <= x_range[1]
            if not row["in_range"]:
                row["status"] = "out_of_range"
                row["reasons"].append(
                    f"反算点样量 {x:g} 落在工作范围 [{x_range[0]:g}, {x_range[1]:g}] 外")
                issues.append(_issue(
                    "unknown_out_of_range", "warning",
                    f"未知样泳道 {label} 落在工作范围外,暂不形成含量结论",
                    [row["lane_id"]], [row["peak_id"]], scope="sample"))
            else:
                row["status"] = "ok"
                row["applied_concentration"] = x / row["volume"]
                row["sample_concentration"] = x / row["volume"] * row["dilution"]
        samples.append(row)

    return {
        "model": model,
        "model_label": MODEL_LABELS[model],
        "fit": fit,
        "points": points,
        "excluded_points": excluded_points,
        "samples": samples,
        "issues": issues,
        "blocked": blocked,
        "blocker_types": blocker_types,
        "range": x_range,
    }


def _point_dict(s, amount):
    return {
        "lane_id": s.get("lane_id"), "lane_label": s.get("lane_label") or "",
        "peak_id": s.get("peak_id"), "spot_no": s.get("spot_no"), "rf": s.get("rf"),
        "response": s.get("response"),
        "concentration": s.get("concentration"), "volume": s.get("volume"),
        "amount": amount, "excluded": bool(s.get("excluded")),
        "exclude_reason": s.get("exclude_reason") or "",
        "fitted": None, "residual": None, "resid_pct": None,
    }


def suggest_spot(spots, target_rf, tol):
    """在某泳道斑点中选 |Rf-target| 最近且在容差内者;无则 None。"""
    cands = [s for s in spots if abs(s["rf"] - target_rf) <= tol]
    if not cands:
        return None
    return min(cands, key=lambda s: abs(s["rf"] - target_rf))


# ---------- 校准图(PIL,ASCII 标注) ----------

def draw_calibration(ev, meta, stale=False):
    """由 evaluate() 结果绘校准图(上图:散点+拟合线+样品投影;下图:残差)。

    meta: {calibration_name, analysis_id, analysis_name, version, target_name,
           conc_unit, vol_unit, created_at}
    点标记带来源(泳道-斑点号/峰ID);stale=True 加过期水印。
    返回 RGB 图。
    """
    from PIL import Image, ImageDraw

    W, H = 880, 700
    M_L, M_R, M_T, M_B = 76, 200, 100, 30
    RP_H = 150
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img, "RGBA")
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("DejaVuSans.ttf", 12)
        small = ImageFont.truetype("DejaVuSans.ttf", 10)
        bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 13)
    except OSError:
        font = small = bold = ImageFont.load_default()

    points = ev["points"]
    fit = ev["fit"]
    xrange = ev["range"]

    # 坐标范围:含标准点、排除点、反算样品投影
    xs = [p["amount"] for p in points if p.get("amount") is not None]
    ys = [p["response"] for p in points if p.get("response") is not None]
    for s in ev["samples"]:
        if s.get("amount_back") is not None:
            xs.append(s["amount_back"])
        if s.get("response"):
            ys.append(s["response"])
    if not xs:
        xs, ys = [0.0, 1.0], [0.0, 1.0]
    x0v, x1v = min(xs + [0.0]), max(xs)
    y0v, y1v = 0.0, max(ys) * 1.12
    if x1v <= x0v:
        x1v = x0v + 1.0

    px0, px1 = M_L, W - M_R
    top0, top1 = M_T + 20, H - M_B - RP_H - 64
    rp0 = top1 + 40

    def X(v):
        return px0 + (v - x0v) / (x1v - x0v) * (px1 - px0)

    def Y(v):
        return top1 - (v - y0v) / (y1v - y0v) * (top1 - top0)

    # 标题块(图内文字一律 ASCII:运行环境无中文字体)
    ascii_name = (meta.get("calibration_name", "") or "").encode("ascii", "replace").decode()
    ascii_analysis = (meta.get("analysis_name", "") or "").encode("ascii", "replace").decode()
    ascii_target = (meta.get("target_name", "") or "").encode("ascii", "replace").decode()
    d.text((M_L, 8), f"{ascii_name}  |  analysis #{meta.get('analysis_id')} "
           f"{ascii_analysis}  |  v{meta.get('version')}", font=bold, fill=(20, 24, 30))
    d.text((M_L, 28), f"target={ascii_target or '-'}   model={ev['model']}",
           font=small, fill=(90, 98, 110))
    if fit:
        eq = (f"y = {fit['slope']:.5g}*x "
              f"{'+' if fit['intercept'] >= 0 else '-'} {abs(fit['intercept']):.5g}")
        r2 = "-" if fit["r2"] is None else f"{fit['r2']:.5f}"
        d.text((M_L, 46),
               f"{eq}    R2={r2}    n={len(points)}    "
               f"range x=[{xrange[0]:.5g}, {xrange[1]:.5g}] "
               f"{meta.get('conc_unit', '')}*{meta.get('vol_unit', '')}",
               font=small, fill=(20, 90, 160))
        d.text((M_L, 62), "x = concentration x injection volume ; "
               "sample conc = x/volume x dilution",
               font=small, fill=(120, 128, 140))
    else:
        d.text((M_L, 46), "no fit (standards insufficient)", font=small, fill=(200, 60, 60))
    if ev["blocked"]:
        d.text((M_L, 80), "BLOCKED: " + "; ".join(ev["blocker_types"]),
               font=small, fill=(200, 60, 60))

    # 网格与轴
    d.rectangle([px0, top0, px1, top1], outline=(120, 128, 140))
    for i in range(5):
        gx = px0 + i * (px1 - px0) / 4
        gy = top0 + i * (top1 - top0) / 4
        d.line([gx, top0, gx, top1], fill=(232, 234, 238))
        d.line([px0, gy, px1, gy], fill=(232, 234, 238))
        xv = x0v + i * (x1v - x0v) / 4
        yv = y1v - i * (y1v - y0v) / 4
        d.text((gx - 14, top1 + 4), f"{xv:.3g}", font=small, fill=(90, 98, 110))
        d.text((px0 - 52, gy - 6), f"{yv:.3g}", font=small, fill=(90, 98, 110))
    d.text((px0, top1 + 20), f"amount  ({meta.get('conc_unit', '')}*{meta.get('vol_unit', '')})",
           font=small, fill=(60, 66, 76))
    d.text((8, top0 + 40), "response", font=small, fill=(60, 66, 76))
    d.text((8, top0 + 54), "(spot area)", font=small, fill=(60, 66, 76))

    # 工作范围底色
    if xrange:
        d.rectangle([X(xrange[0]), top0, X(xrange[1]), top1], fill=(49, 130, 189, 16))

    # 拟合线(范围内实线,外推虚线)
    if fit and xrange:
        xl = max(x0v, xrange[0])
        xr = min(x1v, xrange[1])
        d.line([(X(xl), Y(fit["intercept"] + fit["slope"] * xl)),
                (X(xr), Y(fit["intercept"] + fit["slope"] * xr))],
               fill=(200, 60, 60), width=2)
        if x0v < xl:
            _dashed(d, (X(x0v), Y(fit["intercept"] + fit["slope"] * x0v)),
                    (X(xl), Y(fit["intercept"] + fit["slope"] * xl)), (200, 60, 60, 140))
        if x1v > xr:
            _dashed(d, (X(xr), Y(fit["intercept"] + fit["slope"] * xr)),
                    (X(x1v), Y(fit["intercept"] + fit["slope"] * x1v)), (200, 60, 60, 140))

    # 标准散点(含来源标记)
    for p in points:
        cx, cy = X(p["amount"]), Y(p["response"])
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(31, 110, 200), outline=(20, 60, 120))
        tag = f"{p['lane_label']}-{p.get('spot_no') or '?'}(p{p['peak_id']})"
        d.text((cx + 6, cy - 14), tag, font=small, fill=(31, 90, 150))
    for p in ev.get("excluded_points", []):
        if p.get("amount") is None or not p.get("response"):
            continue
        cx = X(p["amount"])
        yy = Y(p["response"])
        d.line([cx - 5, yy - 5, cx + 5, yy + 5], fill=(150, 150, 150), width=2)
        d.line([cx - 5, yy + 5, cx + 5, yy - 5], fill=(150, 150, 150), width=2)
        reason = (p.get("exclude_reason") or "")[:18]
        d.text((cx + 7, yy + 4),
               f"{p['lane_label']} EXCLUDED: {reason}",
               font=small, fill=(140, 140, 140))

    # 未知样投影
    for s in ev["samples"]:
        if s.get("amount_back") is None or not s.get("response"):
            continue
        cx, cy = X(s["amount_back"]), Y(s["response"])
        col = (230, 120, 20) if s["status"] == "ok" else (190, 150, 60)
        _dashed(d, (px0, cy), (cx, cy), (*col, 160))
        _dashed(d, (cx, top1), (cx, cy), (*col, 160))
        d.polygon([(cx, cy - 6), (cx - 5, cy + 4), (cx + 5, cy + 4)], fill=col)
        tag = f"{s['lane_label']} {s['status']}"
        d.text((cx - 46, cy + 8), tag, font=small, fill=col)

    # 残差面板
    rp1 = rp0 + RP_H
    d.rectangle([px0, rp0, px1, rp1], outline=(120, 128, 140))
    d.text((px0, rp0 - 16), "residual (response - fitted)", font=small, fill=(60, 66, 76))
    if points and all(p["residual"] is not None for p in points):
        res = [p["residual"] for p in points]
        m = max(abs(min(res)), abs(max(res)), 1e-9)
        zy = rp0 + (rp1 - rp0) / 2
        d.line([px0, zy, px1, zy], fill=(120, 128, 140))
        for p in points:
            cx = X(p["amount"])
            ry = zy - p["residual"] / m * (RP_H / 2 - 14)
            d.line([cx, zy, cx, ry], fill=(31, 110, 200), width=4)
            d.text((cx - 14, rp1 + 3), f"{p['amount']:.3g}", font=small, fill=(90, 98, 110))
        d.text((px1 - 150, rp0 - 16), f"max resid% = "
               f"{max(abs(p['resid_pct']) for p in points if p['resid_pct'] is not None):.2f}%",
               font=small, fill=(90, 98, 110))

    # 图例 / 来源脚注
    lx = px1 + 14
    d.ellipse([lx, top0 + 4, lx + 8, top0 + 12], fill=(31, 110, 200))
    d.text((lx + 12, top0 + 3), "standard (included)", font=small, fill=(60, 66, 76))
    d.line([lx, top0 + 26, lx + 8, top0 + 18], fill=(150, 150, 150), width=2)
    d.line([lx, top0 + 18, lx + 8, top0 + 26], fill=(150, 150, 150), width=2)
    d.text((lx + 12, top0 + 17), "excluded (+reason)", font=small, fill=(60, 66, 76))
    d.polygon([(lx + 4, top0 + 34), (lx - 1, top0 + 44), (lx + 9, top0 + 44)],
              fill=(230, 120, 20))
    d.text((lx + 12, top0 + 33), "unknown (back-calc)", font=small, fill=(60, 66, 76))
    d.line([lx, top0 + 54, lx + 10, top0 + 54], fill=(200, 60, 60), width=2)
    d.text((lx + 14, top0 + 49), "fitted line (in range)", font=small, fill=(60, 66, 76))
    d.text((lx, top0 + 70), "tags = lane-spot(peak_id)",
           font=small, fill=(120, 128, 140))

    # 过期水印:来源已变更
    if stale:
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        do = ImageDraw.Draw(ov)
        do.text((W / 2 - 150, H / 2), "EXPIRED - SOURCE CHANGED",
                font=bold, fill=(200, 60, 60, 70))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img


def _dashed(d, p, q, color, dash=6, gap=4, width=1):
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
