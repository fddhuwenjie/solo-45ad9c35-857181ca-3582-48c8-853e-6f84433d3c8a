"""生成样例板照片:sample_plate.png + sample_meta.json(几何真值,供测试/演示)。

样例包含:
- 4 条泳道;正常斑点、拖尾斑点(向基线方向)、共洗脱双峰、过浓饱和斑点、弱斑点
- 明显照明梯度与暗角、噪声、轻微透视与旋转
- 板左缘毫米刻度(0..120 mm),供长度标尺点选
"""

import json
import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tlc import geometry  # noqa: E402

PLATE_W, PLATE_H = 900, 680          # 板面(未矫正)像素
PHOTO_W, PHOTO_H = 1100, 850
BASELINE_Y, FRONT_Y = 600.0, 80.0    # 板面坐标
PX_PER_MM = 5.0                      # 板面 5 px/mm(180 mm x 136 mm)
QUAD = [(150, 80), (950, 55), (1010, 790), (120, 770)]  # 照片中的板四角 TL,TR,BR,BL

LANES_X = [150, 350, 550, 750]


def rf_to_y(rf):
    return BASELINE_Y - rf * (BASELINE_Y - FRONT_Y)


def add_spot(img, x, y, amp, sx, sy_up, sy_down=None):
    """高斯斑点(可为上下不对称 => 拖尾)。amp 从背景中扣除(暗斑)。"""
    sy_down = sy_down if sy_down is not None else sy_up
    r = int(max(sx, sy_up, sy_down) * 3.5) + 1
    px = img.load()
    for j in range(max(0, int(y) - r), min(img.height, int(y) + r + 1)):
        dy = j - y
        sy = sy_up if dy < 0 else sy_down
        for i in range(max(0, int(x) - r), min(img.width, int(x) + r + 1)):
            dx = i - x
            v = amp * math.exp(-0.5 * ((dx / sx) ** 2 + (dy / sy) ** 2))
            nv = px[i, j] - v
            px[i, j] = 0 if nv < 0 else int(nv)


def build_plate(amp_scale=1.0, std_size_scale=1.0):
    """构建板面图像。

    amp_scale:      按比例缩放全部斑点显色强度(模拟板间显色差异)。
    std_size_scale: 按比例缩放泳道 1(标准品泳道)斑点尺寸,面积随之变化
                    (模拟标准品点样量差异,峰高基本不变,仍可稳定检出)。
    """
    # 照明梯度:左上亮右下暗
    gx = Image.linear_gradient("L").rotate(90, expand=True).resize((PLATE_W, PLATE_H))
    gy = Image.linear_gradient("L").resize((PLATE_W, PLATE_H))
    illum = Image.new("L", (PLATE_W, PLATE_H), 236)
    illum = ImageChops.subtract(illum, gx.point(lambda v: int(v * 0.14)))
    illum = ImageChops.subtract(illum, gy.point(lambda v: int(v * 0.10)))

    d = ImageDraw.Draw(illum)
    # 基线与溶剂前沿铅笔线
    d.line([(0, BASELINE_Y), (PLATE_W, BASELINE_Y)], fill=150, width=2)
    d.line([(0, FRONT_Y), (PLATE_W, FRONT_Y)], fill=150, width=2)
    # 左缘毫米刻度:0 mm 在 y=650,每 10 mm 一短刻,50 mm 长刻
    for k in range(13):
        y = 650 - k * 10 * PX_PER_MM
        ln = 16 if k % 5 == 0 else 9
        d.line([(0, y), (ln, y)], fill=90, width=2)

    ss = std_size_scale
    # 泳道 1:三个正常斑点(标准品泳道,尺寸可独立缩放)
    add_spot(illum, LANES_X[0], rf_to_y(0.20), 70 * amp_scale, 11 * ss, 9 * ss)
    add_spot(illum, LANES_X[0], rf_to_y(0.50), 100 * amp_scale, 12 * ss, 10 * ss)
    add_spot(illum, LANES_X[0], rf_to_y(0.80), 55 * amp_scale, 10 * ss, 8 * ss)
    # 泳道 2:拖尾斑点(向基线方向拉长)+ 一个正常斑点
    add_spot(illum, LANES_X[1], rf_to_y(0.35), 95 * amp_scale, 11, 7, sy_down=30)
    add_spot(illum, LANES_X[1], rf_to_y(0.70), 70 * amp_scale, 11, 9)
    # 泳道 3:共洗脱双峰 + 一个正常斑点
    add_spot(illum, LANES_X[2], rf_to_y(0.45), 80 * amp_scale, 12, 11)
    add_spot(illum, LANES_X[2], rf_to_y(0.50), 75 * amp_scale, 12, 11)
    add_spot(illum, LANES_X[2], rf_to_y(0.75), 60 * amp_scale, 10, 8)
    # 泳道 4:过浓饱和斑点(中心截断到 0)+ 弱斑点
    add_spot(illum, LANES_X[3], rf_to_y(0.50), 400 * amp_scale, 13, 12)
    add_spot(illum, LANES_X[3], rf_to_y(0.25), 20 * amp_scale, 9, 8)

    noise = Image.effect_noise((PLATE_W, PLATE_H), 5)
    return Image.blend(illum, noise, 0.15)


def build_photo(plate):
    photo = Image.new("L", (PHOTO_W, PHOTO_H), 62)
    noise = Image.effect_noise((PHOTO_W, PHOTO_H), 8)
    photo = Image.blend(photo, noise, 0.25)
    # 板面透视贴入
    rgba = plate.convert("RGBA")
    coeffs = geometry.homography(QUAD, [(0, 0), (PLATE_W, 0), (PLATE_W, PLATE_H), (0, PLATE_H)])
    warped = rgba.transform((PHOTO_W, PHOTO_H), Image.PERSPECTIVE, coeffs,
                            Image.BICUBIC, fillcolor=(0, 0, 0, 0))
    photo = photo.convert("RGB")
    photo.paste(warped.convert("RGB"), (0, 0), warped.split()[3])
    # 暗角 + 轻微失焦
    vig = Image.radial_gradient("L").resize((PHOTO_W, PHOTO_H))
    photo = ImageChops.subtract(photo, Image.merge("RGB", [vig.point(lambda v: int(v * 0.22))] * 3))
    return photo.filter(ImageFilter.GaussianBlur(0.7))


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))
    plate = build_plate()
    photo = build_photo(plate)
    png_path = os.path.join(out_dir, "sample_plate.png")
    photo.save(png_path)

    # 几何真值(照片坐标),供前端演示与冒烟测试
    to_photo = geometry.forward_map(QUAD, PLATE_W, PLATE_H)  # 校正图->照片 的反函数需另求
    H = geometry.homography([(0, 0), (PLATE_W, 0), (PLATE_W, PLATE_H), (0, PLATE_H)], QUAD)
    to_photo = lambda x, y: geometry.apply(H, x, y)  # noqa: E731
    meta = {
        "image": "sample_plate.png",
        "corners": [list(map(float, p)) for p in QUAD],
        "baseline": list(to_photo(PLATE_W / 2, BASELINE_Y)),
        "front": list(to_photo(PLATE_W / 2, FRONT_Y)),
        "scale": {"p1": list(to_photo(5, 650)), "p2": list(to_photo(5, 150)), "mm": 100.0},
        "lanes_plate_x": LANES_X,
        "plate_size": [PLATE_W, PLATE_H],
        "expected_spots": {
            "lane1_rf": [0.20, 0.50, 0.80],
            "lane2_rf": [0.35, 0.70],
            "lane3_rf": [0.45, 0.50, 0.75],
            "lane4_rf": [0.25, 0.50],
        },
    }
    with open(os.path.join(out_dir, "sample_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"已生成 {png_path} 与 sample_meta.json")


if __name__ == "__main__":
    main()
