"""端到端冒烟测试:样例板 -> 几何 -> 泳道 -> 峰 -> 结果/异常 -> 导出 -> 复算 -> 几何重设。

运行: .venv/bin/python tests/smoke_test.py
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app  # noqa: E402

META = json.load(open(os.path.join(os.path.dirname(__file__), "..", "sample", "sample_meta.json")))


def main():
    tmp = tempfile.mkdtemp(prefix="tlc_test_")
    app = create_app(data_dir=tmp)
    c = app.test_client()

    # 1. 载入样例
    st = c.post("/api/analyses/sample").get_json()
    aid = st["analysis"]["id"]
    assert st["geometry"] is None
    print(f"1. 样例分析 #{aid} 创建")

    # 2. 几何标定(用样例真值)
    g = c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": META["corners"], "baseline": META["baseline"],
        "front": META["front"], "scale": META["scale"],
    }).get_json()
    geom = g["geometry"]
    assert geom["version"] == 1
    d = geom["derived"]
    assert d["baseline_y"] > d["front_y"], "基线应在前沿下方"
    assert abs(d["px_per_mm"] - 5.0) < 0.8, f"px/mm 应接近 5,实际 {d['px_per_mm']}"
    print(f"2. 几何 v1:校正图 {d['width']}x{d['height']},px/mm={d['px_per_mm']:.2f}")

    # 3. 预览
    for kind in ("corrected", "background", "signal"):
        r = c.get(f"/api/analyses/{aid}/preview/{kind}")
        assert r.status_code == 200 and len(r.data) > 1000, kind
    print("3. 三种预览图正常")

    # 4. 四条泳道(板面 x 映射到校正图)
    W = d["width"]
    lanes = []
    for i, px in enumerate(META["lanes_plate_x"], 1):
        cx = px / META["plate_size"][0] * W
        half = 45 / META["plate_size"][0] * W
        lanes.append({"x0": cx - half, "x1": cx + half, "label": f"L{i}"})
    r = c.post(f"/api/analyses/{aid}/lanes", json={"lanes": lanes}).get_json()
    lanes = r["lanes"]
    assert len(lanes) == 4
    print(f"4. 泳道建立:{[l['id'] for l in lanes]}")

    # 5. 自动检测 + 拆分共洗脱峰(泳道 3)
    lane3 = lanes[2]["id"]
    auto = c.post(f"/api/analyses/{aid}/lanes/{lane3}/autodetect", json={}).get_json()
    assert len(auto["windows"]) >= 2, "泳道 3 至少应检出共洗脱簇 + 高 Rf 峰"
    print(f"5. 泳道 3 自动检测出 {len(auto['windows'])} 个窗口")

    # 全部泳道自动检测并保存;泳道 3 把最宽窗口拆成两个(模拟共洗脱拆分)
    for lane in lanes:
        lid = lane["id"]
        wins = c.post(f"/api/analyses/{aid}/lanes/{lid}/autodetect", json={}).get_json()["windows"]
        if lid == lane3:
            widest = max(wins, key=lambda w: w["y1"] - w["y0"])
            mid = (widest["y0"] + widest["y1"]) / 2
            wins = [w for w in wins if w is not widest] + [
                {"y0": widest["y0"], "y1": mid}, {"y0": mid, "y1": widest["y1"]}]
        peaks = [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]
        r = c.post(f"/api/analyses/{aid}/lanes/{lid}/peaks", json={"peaks": peaks}).get_json()
        lane["peaks"] = r["peaks"]
    n3 = len(lanes[2]["peaks"])
    print(f"   各泳道峰数:{[len(l['peaks']) for l in lanes]}(泳道 3 拆分后 {n3})")

    # 6. 结果与异常
    res = c.get(f"/api/analyses/{aid}/results").get_json()
    assert res["ok"], res["flags"]
    spots = res["spots"]
    assert len(spots) >= 9, f"斑点数 {len(spots)}"
    for s in spots:
        assert -0.1 < s["rf"] < 1.1, s
    types = {f["type"] for f in res["flags"]}
    assert "saturated_pixels" in types, "应检出饱和像素(泳道 4 过浓斑点)"
    sat_flag = next(f for f in res["flags"] if f["type"] == "saturated_pixels")
    print(f"6. {len(spots)} 个斑点,异常:{sorted(types)}")

    # 抽查泳道 1 的 Rf 与真值接近
    l1 = sorted((s for s in spots if s["lane_id"] == lanes[0]["id"]), key=lambda s: s["rf"])
    exp = META["expected_spots"]["lane1_rf"]
    assert len(l1) == 3, f"泳道 1 应有 3 斑,实际 {len(l1)}"
    for s, e in zip(l1, exp):
        assert abs(s["rf"] - e) < 0.06, f"Rf {s['rf']:.3f} vs 真值 {e}"
    print(f"   泳道 1 Rf={[round(s['rf'],3) for s in l1]} ≈ 真值 {exp}")

    # 7. 保留异常须注明理由
    r = c.post(f"/api/analyses/{aid}/flags/{sat_flag['key']}/decision", json={"kept": True, "reason": ""})
    assert r.status_code == 400, "空理由应被拒绝"
    r = c.post(f"/api/analyses/{aid}/flags/{sat_flag['key']}/decision",
               json={"kept": True, "reason": "点样过浓,面积按低估处理,仅用于定性"})
    assert r.status_code == 200
    res2 = c.get(f"/api/analyses/{aid}/results").get_json()
    kept = next(f for f in res2["flags"] if f["key"] == sat_flag["key"])
    assert kept["kept"] and kept["reason"]
    print("7. 异常保留需理由:空理由被拒,填写后留痕")

    # 8. 导出
    png = c.get(f"/api/analyses/{aid}/export/annotated.png")
    csv_r = c.get(f"/api/analyses/{aid}/export/spots.csv")
    params = c.get(f"/api/analyses/{aid}/export/params.json")
    prt = c.get(f"/api/analyses/{aid}/print")
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"
    assert b"rf" in csv_r.data and len(csv_r.data.decode("utf-8-sig").strip().splitlines()) == len(spots) + 1
    assert prt.status_code == 200 and "Rf".encode() in prt.data
    params_path = os.path.join(tmp, "params.json")
    open(params_path, "wb").write(params.data)
    print("8. 标注图/CSV/参数文件/打印记录导出正常")

    # 9. 参数文件复算,面积逐一吻合
    out_csv = os.path.join(tmp, "recomputed.csv")
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), ".."))
    subprocess.run([sys.executable, "-m", "tlc.recompute", params_path, "--out", out_csv],
                   check=True, env=env, capture_output=True)
    recalc = [row for row in open(out_csv).read().strip().splitlines()[1:]]
    assert len(recalc) == len(spots)
    web_areas = sorted(s["area"] for s in spots)
    re_areas = sorted(float(r.split(",")[6]) for r in recalc)
    for a, b in zip(web_areas, re_areas):
        assert abs(a - b) < 1e-6, f"复算面积不一致 {a} vs {b}"
    print(f"9. 复算通过:{len(recalc)} 行,面积与网页结果完全一致")

    # 10. 几何重设 -> 旧结果失效,前后参数留痕
    moved = [[x + 3, y + 2] for x, y in META["corners"]]
    g2 = c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": moved, "baseline": META["baseline"],
        "front": META["front"], "scale": META["scale"],
    }).get_json()
    assert g2["geometry"]["version"] == 2
    assert g2["lanes"] == [], "几何重设后泳道应清空(失效)"
    assert g2["geometry"]["previous_params"]["corners"] == META["corners"]
    versions = g2["versions"]
    assert len(versions) == 2 and versions[0]["is_current"] == 0
    print("10. 几何重设:v1 失效留痕,v2 生效,泳道/峰已清空")

    # 11. 基线与前沿颠倒 -> 错误级异常,定量阻断
    g3 = c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": META["corners"], "baseline": META["front"],
        "front": META["baseline"], "scale": META["scale"],
    }).get_json()
    lanes3 = c.post(f"/api/analyses/{aid}/lanes", json={"lanes": [lanes[0]]}).get_json()
    lid = lanes3["lanes"][0]["id"]
    c.post(f"/api/analyses/{aid}/lanes/{lid}/peaks",
           json={"peaks": [{"y0": 100, "y1": 200, "origin": "manual"}]})
    res3 = c.get(f"/api/analyses/{aid}/results").get_json()
    assert not res3["ok"] and res3["flags"][0]["type"] == "baseline_front_intersect"
    assert res3["flags"][0]["level"] == "error"
    print("11. 基线/前沿相交 -> 错误级异常并阻断定量")

    print("\n全部冒烟测试通过 ✔")


if __name__ == "__main__":
    main()
