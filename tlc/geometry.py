"""透视几何:四角单应矫正与坐标映射(纯 Python,不依赖 numpy)。

约定:
- 四角点顺序固定为 左上 TL、右上 TR、右下 BR、左下 BL(原图像素坐标)。
- 校正图为矩形,其四角 (0,0)-(W,0)-(W,H)-(0,H) 与 TL-TR-BR-BL 对应。
"""

MAX_SIDE = 2000  # 校正图单边上限,防止超大输出


def _solve(A, b):
    """高斯消元(部分主元)解 n 元线性方程组。"""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("四角点退化(共线或重合),无法求解单应矩阵")
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col] / M[col][col]
                M[r] = [a - f * bb for a, bb in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] for i in range(n)]


def homography(src, dst):
    """求单应 H 使 dst_i = H·src_i。src/dst 各为 4 个 (x, y),返回 8 系数。"""
    A, b = [], []
    for (x, y), (X, Y) in zip(src, dst):
        A.append([x, y, 1.0, 0.0, 0.0, 0.0, -X * x, -X * y])
        b.append(X)
        A.append([0.0, 0.0, 0.0, x, y, 1.0, -Y * x, -Y * y])
        b.append(Y)
    return _solve(A, b)


def apply(H, x, y):
    """单应 H 作用于点 (x, y)。"""
    a, b, c, d, e, f, g, h = H
    w = g * x + h * y + 1.0
    return (a * x + b * y + c) / w, (d * x + e * y + f) / w


def _dist(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def corrected_size(corners):
    """由四角边长估计校正图尺寸(取对边较大者,并限制最大边)。"""
    tl, tr, br, bl = corners
    w = max(_dist(tl, tr), _dist(bl, br))
    h = max(_dist(tl, bl), _dist(tr, br))
    s = min(1.0, MAX_SIDE / max(w, h, 1.0))
    return max(8, round(w * s)), max(8, round(h * s))


def rectify_coeffs(corners, out_w, out_h):
    """Pillow Image.transform(PERSPECTIVE) 系数:输出矩形 -> 原图四边形。"""
    rect = [(0.0, 0.0), (float(out_w), 0.0), (float(out_w), float(out_h)), (0.0, float(out_h))]
    return homography(rect, corners)


def forward_map(corners, out_w, out_h):
    """返回 原图 -> 校正图 的点映射函数。"""
    rect = [(0.0, 0.0), (float(out_w), 0.0), (float(out_w), float(out_h)), (0.0, float(out_h))]
    H = homography(corners, rect)
    return lambda x, y: apply(H, x, y)


def derive(params):
    """由用户输入参数计算派生几何量。

    params = {
      "corners": [[x,y]x4],
      "baseline": [x,y],          # 基线上一点(原图坐标)
      "front":    [x,y],          # 溶剂前沿上一点(原图坐标)
      "scale": {"p1":[x,y], "p2":[x,y], "mm": float}   # 长度标尺
    }
    """
    corners = [tuple(map(float, p)) for p in params["corners"]]
    W, H = corrected_size(corners)
    fmap = forward_map(corners, W, H)
    _, baseline_y = fmap(*params["baseline"])
    _, front_y = fmap(*params["front"])
    s1 = fmap(*params["scale"]["p1"])
    s2 = fmap(*params["scale"]["p2"])
    scale_px = _dist(s1, s2)
    mm = float(params["scale"]["mm"])
    px_per_mm = scale_px / mm if mm > 0 else None
    return {
        "width": W,
        "height": H,
        "baseline_y": baseline_y,
        "front_y": front_y,
        "scale_px": scale_px,
        "px_per_mm": px_per_mm,
    }
