"""定量流水线:几何 -> 矫正 -> 背景 -> 泳道 -> 峰 -> 结果 + 异常。

compute_bundle() 为纯函数:同一张图 + 同一份参数 => 同一份结果,
Flask 后端与 recompute CLI 共用,保证参数文件可复算。
"""

from PIL import Image

from . import background, geometry, profile, qc

DETECTION_DEFAULTS = {"min_snr": 5.0, "min_height": 4.0, "min_distance": 8, "smooth_sigma": 2.0}


def rectify(image, params, derived):
    """按四角参数做透视矫正,返回 L 模式校正图。"""
    gray = image.convert("L")
    coeffs = geometry.rectify_coeffs(params["corners"], derived["width"], derived["height"])
    return gray.transform((derived["width"], derived["height"]), Image.PERSPECTIVE, coeffs, Image.BICUBIC)


def stage_images(image_path, bundle):
    """返回 (校正灰度, 背景, 信号) 三个 L 模式图像。"""
    img = Image.open(image_path)
    corr = rectify(img, bundle["geometry"]["params"], bundle["geometry"]["derived"])
    bgp = dict(background.DEFAULTS)
    bgp.update(bundle.get("background") or {})
    bg = background.estimate_background(corr, **bgp)
    sig = background.signal_image(corr, bg)
    return corr, bg, sig


def _flag(ftype, level, message, lane_id=None, peak_id=None):
    return {
        "key": f"{ftype}:{lane_id or 0}:{peak_id or 0}",
        "type": ftype,
        "level": level,  # error 阻断定量 / warning 结果保留但需注明理由
        "message": message,
        "lane_id": lane_id,
        "peak_id": peak_id,
    }


def compute_bundle(image_path, bundle):
    """核心计算。bundle 结构见 app.build_bundle / recompute。

    返回 {ok, derived, spots, lanes, flags, totals}。
    spots 每元素: lane_id, peak_id, rf, center_y, center_mm, area, height,
                  pct_lane, pct_plate, y0, y1, saturated_px
    """
    g = bundle["geometry"]
    d = g["derived"]
    W, H = d["width"], d["height"]
    base_y, front_y = d["baseline_y"], d["front_y"]
    px_per_mm = d.get("px_per_mm")
    qct = dict(qc.THRESHOLDS)
    qct.update(bundle.get("qc") or {})

    corr, bg, sig = stage_images(image_path, bundle)
    flags = []

    # --- 全局异常:基线与前沿相交/颠倒 ---
    if front_y >= base_y:
        flags.append(_flag(
            "baseline_front_intersect", "error",
            f"溶剂前沿(y={front_y:.1f})不在基线(y={base_y:.1f})上方,几何无效,无法计算 Rf"))
        return {"ok": False, "derived": d, "spots": [], "lanes": [], "flags": flags,
                    "totals": {"plate": 0.0, "lanes": {}}}

    # --- 全局异常:背景拟合不足 ---
    resid = qc.background_residual(corr, bg, sig)
    if resid["ratio"] > qct["bg_resid_ratio"]:
        flags.append(_flag(
            "background_underfit", "warning",
            f"无斑区背景残差 RMS={resid['rms']:.2f},占信号 p99 的 {resid['ratio']*100:.1f}%"
            f"(阈值 {qct['bg_resid_ratio']*100:.0f}%),背景拟合可能不足"))

    lanes_out = []
    spots = []
    lane_totals = {}
    lane_areas = {}

    for lane in bundle.get("lanes", []):
        lid = lane["id"]
        x0, x1 = float(lane["x0"]), float(lane["x1"])
        if qc.lane_out_of_bounds(x0, x1, W):
            flags.append(_flag(
                "lane_out_of_bounds", "error",
                f"泳道 [{x0:.0f},{x1:.0f}] 超出校正图宽度 {W} 或过窄,已跳过定量", lane_id=lid))
            continue
        prof = profile.lane_profile(sig, x0, x1)
        peaks = sorted(lane.get("peaks", []), key=lambda p: p["y0"])
        for pid in qc.find_overlaps([p for p in peaks]):
            flags.append(_flag(
                "peak_overlap", "warning",
                "积分区与相邻峰重叠,面积将被重复计入", lane_id=lid, peak_id=pid))
        lane_spots = []
        for p in peaks:
            r = profile.integrate(prof, p["y0"], p["y1"])
            sat = qc.count_saturated(corr, x0, x1, p["y0"], p["y1"],
                                     lo=qct["sat_lo"], hi=qct["sat_hi"])
            if sat >= qct["sat_min_count"]:
                flags.append(_flag(
                    "saturated_pixels", "warning",
                    f"积分窗口内检出 {sat} 个饱和像素(<= {qct['sat_lo']} 或 >= {qct['sat_hi']}),"
                    "面积可能被低估", lane_id=lid, peak_id=p["id"]))
            cy = r["centroid"]
            rf = (base_y - cy) / (base_y - front_y)
            lane_spots.append({
                "lane_id": lid,
                "peak_id": p["id"],
                "rf": rf,
                "center_y": cy,
                "center_mm": (base_y - cy) / px_per_mm if px_per_mm else None,
                "area": r["area"],
                "height": r["height"],
                "y0": float(p["y0"]),
                "y1": float(p["y1"]),
                "saturated_px": sat,
            })
        lane_totals[lid] = sum(s["area"] for s in lane_spots)
        lane_areas[lid] = lane_spots
        lanes_out.append({"id": lid, "x0": x0, "x1": x1,
                          "label": lane.get("label", ""), "n_peaks": len(lane_spots)})

    plate_total = sum(lane_totals.values())
    for lid, lane_spots in lane_areas.items():
        lt = lane_totals[lid]
        for s in lane_spots:
            s["pct_lane"] = 100.0 * s["area"] / lt if lt > 0 else 0.0
            s["pct_plate"] = 100.0 * s["area"] / plate_total if plate_total > 0 else 0.0
            spots.append(s)

    # 斑点编号:泳道内按 Rf 升序(自基线起)
    for lane in lanes_out:
        ls = sorted((s for s in spots if s["lane_id"] == lane["id"]), key=lambda s: s["rf"])
        for i, s in enumerate(ls, 1):
            s["spot_no"] = i

    return {"ok": True, "derived": d, "spots": spots, "lanes": lanes_out, "flags": flags,
            "totals": {"plate": plate_total, "lanes": lane_totals},
            "background_residual": resid}
