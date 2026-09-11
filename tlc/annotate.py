"""导出用标注图:在校正图上绘制基线/前沿、泳道、积分窗、编号斑点与异常标记。"""

from PIL import Image, ImageDraw, ImageFont

COLOR_BASELINE = (30, 160, 60, 255)
COLOR_FRONT = (220, 40, 40, 255)
COLOR_LANE = (40, 90, 220, 90)
COLOR_LANE_EDGE = (40, 90, 220, 220)
COLOR_WINDOW = (255, 150, 0, 60)
COLOR_WINDOW_EDGE = (255, 150, 0, 220)
COLOR_SPOT = (200, 0, 200, 255)
COLOR_FLAG = (255, 200, 0, 255)
COLOR_ERROR = (255, 0, 0, 255)


def _font():
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 14)
    except OSError:
        return ImageFont.load_default()


def draw_annotated(corr, derived, lanes, spots, flags):
    """corr: L 或 RGB 校正图。返回标注后的 RGB 图。"""
    img = corr.convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    font = _font()

    base_y, front_y = derived["baseline_y"], derived["front_y"]
    d.line([(0, base_y), (W, base_y)], fill=COLOR_BASELINE, width=2)
    d.text((4, base_y + 2), "baseline", font=font, fill=COLOR_BASELINE)
    d.line([(0, front_y), (W, front_y)], fill=COLOR_FRONT, width=2)
    d.text((4, front_y - 16), "solvent front", font=font, fill=COLOR_FRONT)

    lane_by_id = {l["id"]: l for l in lanes}
    for lane in lanes:
        x0, x1 = lane["x0"], lane["x1"]
        d.rectangle([x0, 0, x1, H], outline=COLOR_LANE_EDGE, width=1)
        d.rectangle([x0, 0, x1, H], fill=COLOR_LANE)
        d.text((x0 + 2, 4), lane.get("label") or f"L{lane['id']}", font=font, fill=COLOR_LANE_EDGE)

    # 异常标记位置:按峰聚合
    flag_by_peak = {}
    global_flags = []
    for f in flags:
        if f.get("peak_id"):
            flag_by_peak.setdefault(f["peak_id"], []).append(f)
        else:
            global_flags.append(f)

    for s in spots:
        lane = lane_by_id.get(s["lane_id"])
        if not lane:
            continue
        x0, x1 = lane["x0"], lane["x1"]
        d.rectangle([x0, s["y0"], x1, s["y1"]], fill=COLOR_WINDOW,
                    outline=COLOR_WINDOW_EDGE, width=1)
        cy = s["center_y"]
        cx = (x0 + x1) / 2
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=COLOR_SPOT, width=2)
        label = f"{lane.get('label') or 'L'}-{s['spot_no']}"
        d.text((x1 + 3, cy - 7), label, font=font, fill=COLOR_SPOT)
        if s["peak_id"] in flag_by_peak:  # 警告三角
            tx, ty = x0 + 3, s["y0"] + 3
            d.polygon([(tx, ty + 12), (tx + 6, ty), (tx + 12, ty + 12)],
                      fill=COLOR_FLAG, outline=(0, 0, 0, 255))
            d.text((tx + 4, ty + 2), "!", font=font, fill=(0, 0, 0, 255))

    # 全局异常写在图顶
    y = 20
    for f in global_flags:
        color = COLOR_ERROR if f["level"] == "error" else COLOR_FLAG
        d.rectangle([2, y - 2, 2 + d.textlength(f["message"], font=font) + 8, y + 16],
                    fill=(0, 0, 0, 160))
        d.text((6, y), f["message"], font=font, fill=color)
        y += 20
    return img
