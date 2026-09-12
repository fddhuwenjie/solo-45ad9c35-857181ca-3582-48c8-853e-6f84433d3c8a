# TLC 薄层色谱图像定量工作台

上传薄层板照片 → 几何标定(四角/基线/前沿/标尺)→ 透视矫正与背景扣除 →
泳道与峰积分 → 逐斑点 Rf/面积/相对含量与异常处置 → 导出标注图/CSV/参数文件/打印记录。
同批样品分板展开时,还可用**跨板对照**:指定共同标准品泳道与目标斑点,程序按
Rf 容差 + 峰形 + 标准品次序给出候选,用标准品响应求各板归一化系数,输出跨板
CSV / 匹配关系 JSON / 对照图。
同板展开的标准系列可做**校准曲线与含量反算**:泳道标注标准/空白/未知样并绑定
斑点,普通线性 / 过零线性 / 1/x 加权三模型成线,联动校准图、散点、拟合线与残差,
反算逐样品含量;标准点不足、同浓度响应冲突、不单调、空白异常或样品超范围时只
定位泳道、不出含量结论;每版模型留痕所用几何、积分边界与斑点 ID,来源变化后
旧结果标为过期仍可查看,并导出逐样品 CSV / 模型 JSON / 带来源标记的校准图。

仅依赖 Flask 与 Pillow,核心计算(tlc 包)不依赖 Web 框架,可用参数文件离线复算。

## 安装

需要 Python 3.10+。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 运行

```bash
# 可选:重新生成样例板图(仓库已附带 sample/sample_plate.png)
.venv/bin/python sample/make_sample.py

.venv/bin/python app.py
# 打开 http://127.0.0.1:5000      单板定量
# 打开 http://127.0.0.1:5000/compare  跨板对照
# 打开 http://127.0.0.1:5000/calibration  校准曲线与含量反算
```

数据(上传图、SQLite 库、导出文件)默认落在 `data/`,可用环境变量
`TLC_DATA_DIR` 改到其他目录。

## 测试

```bash
.venv/bin/python tests/smoke_test.py        # 单板定量端到端
.venv/bin/python tests/compare_test.py      # 跨板对照:单元 + 端到端
.venv/bin/python tests/calibration_test.py  # 校准曲线:拟合/质控/反算 + 端到端
```

## 参数文件复算

网页端导出的 params.json 可在原图上重跑同一条流水线,验证结果可复算:

```bash
.venv/bin/python -m tlc.recompute params.json --image 原图.png --out spots.csv
```

## 目录

- `app.py` — Flask 后端(单板定量 + 跨板对照 + 校准曲线 API)
- `db.py` — SQLite 持久化(几何版本留痕、泳道/峰、异常处置、对照组、校准与模型版本)
- `tlc/` — 核心计算:几何/背景/密度曲线/流水线/异常/标注/跨板对照/校准拟合与成图
- `templates/`, `static/` — 单板定量、跨板对照与校准曲线页面
- `sample/` — 样例板生成器与几何真值
- `tests/` — 冒烟测试、跨板对照与校准曲线测试
