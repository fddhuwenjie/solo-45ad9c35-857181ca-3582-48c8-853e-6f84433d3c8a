"""泳道密度曲线、峰检测与积分(纯 Python)。"""

import math

from PIL import Image


def lane_profile(signal_img, x0, x1):
    """泳道 [x0,x1) 列平均得到 1D 密度曲线(BOX 重采样即算术平均)。"""
    w, h = signal_img.size
    xa = max(0, min(w - 1, int(round(x0))))
    xb = max(xa + 1, min(w, int(round(x1))))
    col = signal_img.crop((xa, 0, xb, h)).resize((1, h), Image.BOX)
    return [float(v) for v in col.getdata()]


def smooth(profile, sigma=2.0):
    """高斯平滑(边缘重复延拓)。"""
    if sigma <= 0:
        return list(profile)
    r = max(1, int(3 * sigma))
    kern = [math.exp(-(i * i) / (2 * sigma * sigma)) for i in range(-r, r + 1)]
    s = sum(kern)
    kern = [k / s for k in kern]
    n = len(profile)
    out = [0.0] * n
    for i in range(n):
        acc = 0.0
        for j, k in enumerate(kern):
            idx = i + j - r
            idx = 0 if idx < 0 else (n - 1 if idx >= n else idx)
            acc += profile[idx] * k
        out[i] = acc
    return out


def noise_level(profile):
    """一阶差分 MAD 估计噪声标准差。"""
    diffs = sorted(abs(profile[i + 1] - profile[i]) for i in range(len(profile) - 1))
    if not diffs:
        return 0.0
    med = diffs[len(diffs) // 2]
    return med * 1.4826 / math.sqrt(2.0)


def detect_peaks(profile, min_snr=5.0, min_height=4.0, min_distance=8, smooth_sigma=2.0,
                 y_min=0.0, y_max=None):
    """自动峰检测,返回 [(y0, y1), ...] 积分窗口(互不重叠)。

    峰心 = 平滑曲线局部极大且高于阈值;窗口边界 = 相邻峰间最小值,
    两端边界 = 信号回落至 floor 处。
    y_min/y_max 限定搜索区间(通常取溶剂前沿与基线,避免铅笔线等被误检)。
    """
    n = len(profile)
    if n < 3:
        return []
    lo = max(0, int(math.floor(y_min)))
    hi = min(n - 1, int(math.ceil(y_max))) if y_max is not None else n - 1
    if hi - lo < 3:
        return []
    sm = smooth(profile, smooth_sigma)
    noise = max(noise_level(profile), 1e-6)
    thresh = max(float(min_height), float(min_snr) * noise)
    maxima = []
    i = lo + 1
    while i < hi:
        if sm[i] >= thresh and sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1]:
            j = i
            while j + 1 < hi and sm[j + 1] == sm[i]:  # 平台取中心
                j += 1
            maxima.append((i + j) // 2)
            i = j + 1
        else:
            i += 1
    # 距离过近的峰只保留更高者
    kept = []
    for m in sorted(maxima, key=lambda m: -sm[m]):
        if all(abs(m - k) >= min_distance for k in kept):
            kept.append(m)
    kept.sort()
    if not kept:
        return []
    floor = max(2.0 * noise, 0.02 * max(sm))
    windows = []
    for k, m in enumerate(kept):
        if k == 0:
            l = m
            while l > lo and sm[l] > floor:
                l -= 1
        else:
            l = min(range(kept[k - 1], m + 1), key=lambda t: sm[t])
        if k == len(kept) - 1:
            r = m
            while r < hi and sm[r] > floor:
                r += 1
        else:
            r = min(range(m, kept[k + 1] + 1), key=lambda t: sm[t])
        if r - l >= 2:
            windows.append((float(l), float(r)))
    return windows


def integrate(profile, y0, y1):
    """窗口 [y0,y1] 积分:面积、强度加权质心、峰高。"""
    n = len(profile)
    a = max(0, min(n, int(math.floor(y0))))
    b = max(0, min(n, int(math.ceil(y1))))
    if b <= a:
        return {"area": 0.0, "centroid": (y0 + y1) / 2.0, "height": 0.0}
    seg = profile[a:b]
    area = float(sum(seg))
    height = float(max(seg))
    if area <= 0:
        return {"area": 0.0, "centroid": (a + b) / 2.0, "height": height}
    centroid = sum((a + i) * v for i, v in enumerate(seg)) / area
    return {"area": area, "centroid": float(centroid), "height": height}
