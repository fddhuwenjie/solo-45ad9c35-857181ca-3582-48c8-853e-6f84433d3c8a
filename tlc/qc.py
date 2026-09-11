"""质量异常检测:阈值集中在此,全部写入参数文件以便复算。"""

import math

from PIL import ImageChops, ImageStat

THRESHOLDS = {
    "sat_lo": 1,             # 灰度 <= sat_lo 视为暗端饱和(斑点过浓)
    "sat_hi": 254,           # 灰度 >= sat_hi 视为亮端饱和
    "sat_min_count": 4,      # 窗口内饱和像素达到该数才报警
    "bg_resid_ratio": 0.08,  # 无斑区背景残差 RMS / 信号 p99 超过该值判背景拟合不足
    "min_lane_width": 3,     # 泳道最小宽度(px)
}


def count_saturated(gray, x0, x1, y0, y1, lo=None, hi=None):
    """统计窗口内饱和(截断)像素数,暗斑板两端都可能截断。"""
    lo = THRESHOLDS["sat_lo"] if lo is None else lo
    hi = THRESHOLDS["sat_hi"] if hi is None else hi
    xa = max(0, int(math.floor(x0)))
    xb = min(gray.width, int(math.ceil(x1)))
    ya = max(0, int(math.floor(y0)))
    yb = min(gray.height, int(math.ceil(y1)))
    if xb <= xa or yb <= ya:
        return 0
    hist = gray.crop((xa, ya, xb, yb)).histogram()
    return int(sum(hist[: lo + 1]) + sum(hist[hi:]))


def background_residual(gray, bg, sig):
    """无斑区背景残差 RMS 与信号 p99 之比,衡量背景拟合是否充分。"""
    resid = ImageChops.difference(gray, bg)
    hist = sig.histogram()
    total = sum(hist) or 1
    acc, p99 = 0, 0
    for v, c in enumerate(hist):
        acc += c
        if acc >= 0.99 * total:
            p99 = v
            break
    t = max(3, int(0.05 * p99))
    mask = sig.point(lambda v: 255 if v < t else 0)  # 无斑区
    rms = ImageStat.Stat(resid, mask).rms[0]
    return {"rms": float(rms), "p99": float(p99), "ratio": float(rms / max(1.0, p99))}


def lane_out_of_bounds(x0, x1, width):
    return x0 < -0.5 or x1 > width + 0.5 or (x1 - x0) < THRESHOLDS["min_lane_width"]


def find_overlaps(peaks):
    """返回发生积分区重叠的峰 id 集合(peaks 需含 id/y0/y1)。"""
    bad = set()
    ordered = sorted(peaks, key=lambda p: p["y0"])
    for a, b in zip(ordered, ordered[1:]):
        if b["y0"] < a["y1"]:
            bad.add(a["id"])
            bad.add(b["id"])
    return bad
