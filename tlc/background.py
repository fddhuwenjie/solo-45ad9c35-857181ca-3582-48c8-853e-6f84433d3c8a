"""背景估计与扣除:降采样 + 大核中值 + 高斯,逼近缓慢变化的照明梯度。"""

from PIL import Image, ImageChops, ImageFilter

# 默认参数会写入参数文件,保证可复算
DEFAULTS = {"downscale": 6, "median": 9, "blur": 4}


def estimate_background(gray, downscale=6, median=9, blur=4):
    """估计 2D 背景面。

    gray: L 模式图像。先降采样使大核中值可行,再升采样回原尺寸。
    对斑点(小尺度暗结构)不敏感,跟随照明梯度等大尺度变化。
    """
    w, h = gray.size
    sw, sh = max(1, w // int(downscale)), max(1, h // int(downscale))
    small = gray.resize((sw, sh), Image.BILINEAR)
    k = max(3, int(median) | 1)  # MedianFilter 要求奇数核
    small = small.filter(ImageFilter.MedianFilter(k))
    if blur > 0:
        small = small.filter(ImageFilter.GaussianBlur(float(blur)))
    return small.resize((w, h), Image.BICUBIC)


def signal_image(gray, bg):
    """暗斑为正信号:背景 - 灰度(Pillow subtract 自动截断到 0)。"""
    return ImageChops.subtract(bg, gray)
