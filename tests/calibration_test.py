"""校准曲线测试:calibration 纯函数单元测试 + 端到端 API 流程。

运行: .venv/bin/python tests/calibration_test.py
"""

import io
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw  # noqa: E402

from app import create_app  # noqa: E402
from tlc import calibration as cal  # noqa: E402


# ================= 单元测试 =================

def test_fit_models():
    xs = [10.0, 20.0, 30.0, 40.0]
    ys = [101.0, 198.0, 302.0, 399.0]
    ols = cal.fit_model(xs, ys, "linear")
    assert abs(ols["slope"] - 10) < 0.05 and abs(ols["intercept"]) < 3, ols
    assert ols["r2"] > 0.999
    z = cal.fit_model(xs, ys, "linear_zero")
    assert abs(z["slope"] - 10) < 0.05 and z["intercept"] == 0.0
    assert z["r2"] is not None and 0 < z["r2"] <= 1.0
    # y = 5x 严格过零点:1/x 加权
    w = cal.fit_model([10.0, 20.0, 40.0], [50.0, 100.0, 200.0], "wls_1overx")
    assert abs(w["slope"] - 5) < 1e-9 and abs(w["intercept"]) < 1e-9
    assert abs(w["r2"] - 1.0) < 1e-9
    # 退化输入
    assert cal.fit_model([], [], "linear")["slope"] == 0.0
    assert cal.fit_model([5.0], [9.0], "linear")["intercept"] == 9.0
    print("U1. 三种拟合(普通/过零/1/x 加权)与退化输入 ✔")


def test_evaluate_basic_and_backcalc():
    stds = [
        {"lane_id": i + 1, "lane_label": f"S{i+1}", "peak_id": 100 + i, "spot_no": 1,
         "rf": 0.5, "response": 100.0 * i, "concentration": float(i), "volume": 10.0,
         "excluded": False, "exclude_reason": ""}
        for i in range(1, 5)]
    blanks = [{"lane_id": 9, "lane_label": "B", "peak_id": None, "response": None}]
    unks = [
        {"lane_id": 20, "lane_label": "U1", "peak_id": 201, "spot_no": 1, "rf": 0.5,
         "response": 250.0, "volume": 10.0, "dilution": 2.0},
        {"lane_id": 21, "lane_label": "U2", "peak_id": 202, "spot_no": 1, "rf": 0.5,
         "response": 600.0, "volume": 10.0, "dilution": 1.0},
    ]
    ev = cal.evaluate(stds, blanks, unks, "linear")
    assert not ev["blocked"], [i["message"] for i in ev["issues"]]
    assert abs(ev["fit"]["slope"] - 10.0) < 1e-9
    u1, u2 = ev["samples"]
    assert u1["status"] == "ok" and u1["in_range"]
    assert abs(u1["amount_back"] - 25.0) < 1e-9
    assert abs(u1["applied_concentration"] - 2.5) < 1e-9
    assert abs(u1["sample_concentration"] - 5.0) < 1e-9
    assert u2["status"] == "out_of_range" and u2["sample_concentration"] is None
    assert any(i["type"] == "unknown_out_of_range" for i in ev["issues"])
    # 空白未绑定 => 警告但不阻断
    assert any(i["type"] == "blank_unbound" and i["level"] == "warning" for i in ev["issues"])
    print("U2. 基本评估:反算/稀释/范围外/空白未绑定警告 ✔")


def test_too_few_and_exclude_reason():
    stds = [{"lane_id": 1, "lane_label": "S1", "peak_id": 1, "spot_no": 1, "rf": 0.5,
             "response": 100.0, "concentration": 1.0, "volume": 10.0,
             "excluded": False, "exclude_reason": ""}]
    ev = cal.evaluate(stds, [], [], "linear")
    assert ev["blocked"] and "too_few_standards" in ev["blocker_types"]
    # 排除唯一标准点但不给理由
    stds[0]["excluded"] = True
    ev = cal.evaluate(stds, [], [], "linear")
    assert "exclude_reason_required" in ev["blocker_types"]
    assert any(1 in i["lane_ids"] for i in ev["issues"])
    # 给理由后该点进入 excluded_points
    stds[0]["exclude_reason"] = "点样失败"
    ev = cal.evaluate(stds, [], [], "linear")
    assert len(ev["excluded_points"]) == 1
    assert ev["excluded_points"][0]["exclude_reason"] == "点样失败"
    print("U3. 标准点不足 / 排除必须填理由 / 定位泳道 ✔")


def test_duplicate_conflict():
    stds = [
        {"lane_id": 1, "lane_label": "S1a", "peak_id": 1, "spot_no": 1, "rf": 0.5,
         "response": 100.0, "concentration": 2.0, "volume": 10.0,
         "excluded": False, "exclude_reason": ""},
        {"lane_id": 2, "lane_label": "S1b", "peak_id": 2, "spot_no": 1, "rf": 0.5,
         "response": 200.0, "concentration": 2.0, "volume": 10.0,   # 同浓度差 67%
         "excluded": False, "exclude_reason": ""},
        {"lane_id": 3, "lane_label": "S2", "peak_id": 3, "spot_no": 1, "rf": 0.5,
         "response": 200.0, "concentration": 4.0, "volume": 10.0,
         "excluded": False, "exclude_reason": ""},
    ]
    ev = cal.evaluate(stds, [], [], "linear")
    assert "duplicate_conflict" in ev["blocker_types"]
    issue = next(i for i in i_types(ev) if i["type"] == "duplicate_conflict")
    assert set(issue["lane_ids"]) == {1, 2}
    # 一致的重复不冲突
    stds[1]["response"] = 102.0
    ev = cal.evaluate(stds, [], [], "linear")
    assert "duplicate_conflict" not in ev["blocker_types"]
    print("U4. 同浓度响应冲突:定位冲突泳道,一致重复通过 ✔")


def test_non_monotonic_and_bad_slope():
    stds = [
        std(1, "S1", 100.0, 1.0), std(2, "S2", 320.0, 2.0),
        std(3, "S3", 280.0, 3.0), std(4, "S4", 400.0, 4.0),
    ]
    ev = cal.evaluate(stds, [], [], "linear")
    assert "non_monotonic" in ev["blocker_types"]
    issue = next(i for i in i_types(ev) if i["type"] == "non_monotonic")
    assert set(issue["lane_ids"]) == {2, 3}
    # 响应整体随浓度下降 -> 斜率非正
    stds2 = [std(1, "S1", 400.0, 1.0), std(2, "S2", 300.0, 2.0),
             std(3, "S3", 200.0, 3.0), std(4, "S4", 100.0, 4.0)]
    ev = cal.evaluate(stds2, [], [], "linear")
    assert "bad_slope" in ev["blocker_types"]
    print("U5. 响应不单调 / 斜率非正:阻断并定位泳道 ✔")


def test_blank_anomaly():
    stds = [std(1, "S1", 100.0, 1.0), std(2, "S2", 200.0, 2.0),
            std(3, "S3", 300.0, 3.0)]
    # 空白响应达最低标准点 50% -> 异常
    blanks = [{"lane_id": 9, "lane_label": "B", "peak_id": 90, "response": 50.0}]
    ev = cal.evaluate(stds, blanks, [], "linear")
    assert "blank_anomaly" in ev["blocker_types"]
    assert 9 in next(i for i in i_types(ev) if i["type"] == "blank_anomaly")["lane_ids"]
    # 干净空白(<10%)通过
    blanks[0]["response"] = 5.0
    ev = cal.evaluate(stds, blanks, [], "linear")
    assert not ev["blocked"]
    print("U6. 空白异常阻断并定位空白泳道 ✔")


def test_incomplete_unknown():
    stds = [std(1, "S1", 100.0, 1.0), std(2, "S2", 200.0, 2.0),
            std(3, "S3", 300.0, 3.0)]
    unks = [{"lane_id": 20, "lane_label": "U1", "peak_id": 201, "spot_no": 1,
             "rf": 0.5, "response": 150.0, "volume": None, "dilution": 1.0}]
    ev = cal.evaluate(stds, [], unks, "linear")
    assert not ev["blocked"]
    s = ev["samples"][0]
    assert s["status"] == "incomplete" and s["sample_concentration"] is None
    # 模型被阻断时样品不反算
    ev2 = cal.evaluate(stds[:1], [], unks, "linear")
    assert ev2["samples"][0]["status"] == "blocked"
    print("U7. 未知样体积/稀释缺失不出结论;模型阻断时样品 blocked ✔")


def std(lid, label, area, conc):
    return {"lane_id": lid, "lane_label": label, "peak_id": 100 + lid, "spot_no": 1,
            "rf": 0.5, "response": float(area), "concentration": float(conc),
            "volume": 10.0, "excluded": False, "exclude_reason": ""}


def i_types(ev):
    return ev["issues"]


# ================= 端到端 =================

W, H = 700, 520
BASE_Y, FRONT_Y = 460.0, 60.0
RF_TARGET = 0.5
# 8 条泳道:S1..S5 标准(浓度 1..5),B 空白,U1 范围内未知,U2 范围外未知
LANE_X = [70, 150, 230, 310, 390, 470, 550, 630]
LANE_DEF = [("standard", c) for c in (1.0, 2.0, 3.0, 4.0, 5.0)] + \
           [("blank", None), ("unknown", None), ("unknown", None)]


def spot_y(rf):
    return BASE_Y - rf * (BASE_Y - FRONT_Y)


def make_cal_plate(areas=None, blank_area=0.0, unknown_concs=(3.2, 6.0)):
    """areas: {lane_index: area_response} 覆盖标准;未知由浓度线性给。"""
    img = Image.new("L", (W, H), 220)
    d = ImageDraw.Draw(img)
    d.line([(0, BASE_Y), (W, BASE_Y)], fill=150, width=2)
    d.line([(0, FRONT_Y), (W, FRONT_Y)], fill=150, width=2)
    px = img.load()

    def gauss(xc, amp, sx=13, sy=11):
        y = spot_y(RF_TARGET)
        r = int(sx * 3) + 1
        for j in range(max(0, int(y) - r), min(H, int(y) + r + 1)):
            for i in range(max(0, xc - r), min(W, xc + r + 1)):
                v = amp * math.exp(-0.5 * (((i - xc) / sx) ** 2 + ((j - y) / sy) ** 2))
                px[i, j] = max(0, int(px[i, j] - v))

    for li, (role, c) in enumerate(LANE_DEF):
        if role == "standard":
            amp = areas.get(li, c * 20.0) if areas else c * 20.0
            gauss(LANE_X[li], amp)
        elif role == "blank":
            if blank_area:
                gauss(LANE_X[li], blank_area)
        else:
            ui = li - 6
            if ui < len(unknown_concs):
                gauss(LANE_X[li], unknown_concs[ui] * 20.0)
    return img


def upload_plate(c, **kw):
    buf = io.BytesIO()
    make_cal_plate(**kw).save(buf, "PNG")
    buf.seek(0)
    return c.post("/api/analyses", data={"file": (buf, "cal.png"), "name": "校准板"},
                  content_type="multipart/form-data").get_json()


def setup_cal_plate(c):
    """上传 + 几何 + 8 泳道 + 自动检峰,返回 {aid, lanes:[{id,label,peaks}]}。"""
    st = upload_plate(c)
    aid = st["analysis"]["id"]
    c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": [[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]],
        "baseline": [W / 2, BASE_Y], "front": [W / 2, FRONT_Y],
        "scale": {"p1": [5, BASE_Y], "p2": [5, FRONT_Y], "mm": 80.0}})
    half = 26
    lanes_in = [{"x0": x - half, "x1": x + half, "label": f"L{i+1}"}
                for i, x in enumerate(LANE_X)]
    lanes = c.post(f"/api/analyses/{aid}/lanes", json={"lanes": lanes_in}).get_json()["lanes"]
    for lane in lanes:
        wins = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/autodetect",
                      json={}).get_json()["windows"]
        # 只保留最接近目标 Rf 的窗(避免铅笔线/前沿误检)
        wins = sorted(wins, key=lambda w:
                      abs(((w["y0"] + w["y1"]) / 2 - spot_y(RF_TARGET))))[:1]
        peaks = [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]
        r = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/peaks",
                   json={"peaks": peaks}).get_json()["peaks"]
        lane["peaks"] = r
    return aid, lanes


def cal_payload(lanes, model="linear", overrides=None):
    """按 LANE_DEF 顺序标注全部泳道并绑定各泳道第一个峰。"""
    overrides = overrides or {}
    out = []
    for li, (role, cval) in enumerate(LANE_DEF):
        lane = lanes[li]
        o = overrides.get(li, {})
        peak_id = o.get("peak_id", lane["peaks"][0]["id"] if lane["peaks"] else None)
        row = {"lane_id": lane["id"], "role": o.get("role", role),
               "peak_id": peak_id}
        if role == "standard":
            row.update({"concentration": o.get("concentration", cval),
                        "volume": o.get("volume", 10.0),
                        "excluded": o.get("excluded", False),
                        "exclude_reason": o.get("exclude_reason", "")})
        elif role == "unknown":
            row.update({"volume": o.get("volume", 10.0),
                        "dilution": o.get("dilution", 1.0)})
        out.append(row)
    return {"model": model, "lanes": out}


def e2e():
    tmp = tempfile.mkdtemp(prefix="tlc_cal_")
    app = create_app(data_dir=tmp)
    c = app.test_client()

    # 1. 建板 + 校准
    aid, lanes = setup_cal_plate(c)
    print(f"1. 校准板 #{aid} 建立:{len(lanes)} 泳道,各泳道峰数 "
          f"{[len(l['peaks']) for l in lanes]}")
    cal_id = c.post(f"/api/analyses/{aid}/calibrations", json={
        "name": "批A-校准", "target_name": "目标X", "target_rf": RF_TARGET,
        "rf_tol": 0.08, "conc_unit": "ng/uL", "vol_unit": "uL"}).get_json()["calibration"]["id"]
    st = c.get(f"/api/calibrations/{cal_id}").get_json()
    assert st["calibration"]["target_name"] == "目标X" and st["evaluation"]
    print(f"2. 校准 #{cal_id} 创建,初始状态标准点数 {len(st['evaluation']['points'])}")

    # 3. 提交标注 -> 线性成线,反算两个未知
    st = c.post(f"/api/calibrations/{cal_id}/evaluate",
                json=cal_payload(lanes)).get_json()
    ev = st["evaluation"]
    assert not ev["blocked"], [i["message"] for i in ev["issues"]]
    assert len(ev["points"]) == 5, len(ev["points"])
    assert ev["fit"]["r2"] > 0.99, ev["fit"]
    b = ev["fit"]["slope"]
    assert b > 20, ev["fit"]
    u1 = next(s for s in ev["samples"] if s["lane_label"] == "L7")
    u2 = next(s for s in ev["samples"] if s["lane_label"] == "L8")
    assert u1["status"] == "ok", u1
    # 未知按 3.2 与 6.0 浓度点样;3.2 在 1~5 范围内,6.0 超出
    assert abs(u1["sample_concentration"] - 3.2) < 0.4, u1
    assert u2["status"] == "out_of_range" and u2["sample_concentration"] is None
    oor = next(i for i in ev["issues"] if i["type"] == "unknown_out_of_range")
    assert oor["lane_ids"] == [lanes[7]["id"]]
    print(f"3. 线性成线:b={b:.2f} R²={ev['fit']['r2']:.4f},"
          f"U1={u1['sample_concentration']:.3f} ng/uL(≈3.2),U2 范围外并定位泳道 ✔")

    # 4. 排除标准点必须填理由:无理由拒绝保存版本
    payload_no_reason = cal_payload(lanes, overrides={4: {"excluded": True}})
    r = c.post(f"/api/calibrations/{cal_id}/fit", json=payload_no_reason)
    assert r.status_code == 400 and "理由" in r.get_json()["description"]
    # 填理由后可保存(仍剩 4 点);该点进入快照 excluded_points
    payload_reason = cal_payload(lanes, overrides={
        4: {"excluded": True, "exclude_reason": "第5点点样划破,响应异常偏低"}})
    st_eval = c.post(f"/api/calibrations/{cal_id}/evaluate",
                     json=payload_reason).get_json()
    assert len(st_eval["evaluation"]["points"]) == 4
    assert st_eval["evaluation"]["excluded_points"][0]["exclude_reason"]
    r = c.post(f"/api/calibrations/{cal_id}/fit", json=payload_reason).get_json()
    vid, ver = r["version_id"], r["version"]
    assert ver == 1
    snap = c.get(f"/api/calibrations/{cal_id}/versions/{vid}").get_json()["snapshot"]
    assert snap["points"] and snap["excluded_points"][0]["y1"] > snap["excluded_points"][0]["y0"]
    assert {p["peak_id"] for p in snap["points"]}
    print(f"4. 排除无理由被拒;填理由后保存 v{ver}:快照含积分边界/斑点ID/排除理由 ✔")

    # 5. 切换过零 / 1/x 加权模型并各存一版
    for m in ("linear_zero", "wls_1overx"):
        st2 = c.post(f"/api/calibrations/{cal_id}/evaluate",
                     json=cal_payload(lanes, model=m)).get_json()
        assert not st2["evaluation"]["blocked"]
        rr = c.post(f"/api/calibrations/{cal_id}/fit",
                    json=cal_payload(lanes, model=m)).get_json()
        assert rr["version"] == (2 if m == "linear_zero" else 3)
    versions = c.get(f"/api/calibrations/{cal_id}").get_json()["versions"]
    assert [v["version"] for v in versions] == [1, 2, 3]
    assert versions[-1]["is_current"] == 1 and versions[0]["is_current"] == 0
    print("5. 过零线性 / 1/x 加权模型切换并留版(v2/v3),当前版本指向最新 ✔")

    # 6. 导出:逐样品 CSV / 模型 JSON / 校准图(v1 含排除点,验证排除留痕)
    csv_r = c.get(f"/api/calibrations/{cal_id}/versions/{vid}/samples.csv")
    js_r = c.get(f"/api/calibrations/{cal_id}/versions/{vid}/model.json")
    fig = c.get(f"/api/calibrations/{cal_id}/versions/{vid}/figure.png")
    assert csv_r.status_code == 200 and b"sample_concentration" in csv_r.data
    txt = csv_r.data.decode("utf-8-sig")
    assert "out_of_range" in txt and "L7" in txt and "L8" in txt
    payload = json.loads(js_r.data.decode())
    assert payload["fit"]["slope"] and payload["snapshot"]["geometry"]["derived"]
    assert payload["snapshot"]["excluded_points"]
    assert fig.status_code == 200 and fig.data[:4] == b"\x89PNG"
    print("6. 逐样品 CSV / 模型 JSON(几何/边界/斑点ID)/ 校准图导出 ✔")

    # 7. 阻断情形:同浓度响应冲突 / 不单调 / 标准点不足 / 空白异常
    # 7a. 把 S2 浓度改成与 S1 相同(1.0),面积 40 vs 20 => 单位响应冲突
    conflict = cal_payload(lanes, overrides={1: {"concentration": 1.0}})
    r = c.post(f"/api/calibrations/{cal_id}/fit", json=conflict)
    assert r.status_code == 400 and "冲突" in r.get_json()["description"]
    st_e = c.post(f"/api/calibrations/{cal_id}/evaluate", json=conflict).get_json()
    assert "duplicate_conflict" in st_e["evaluation"]["blocker_types"]
    assert st_e["evaluation"]["samples"][0]["status"] == "blocked"
    print("7a. 同浓度响应冲突:定位泳道、阻断成线与反算 ✔")

    # 7b. 不单调:重排浓度顺序使响应随点样量回落(S3 标成 4.0,S4 标成 3.0)
    nonmono = cal_payload(lanes, overrides={
        2: {"concentration": 4.0}, 3: {"concentration": 3.0}})
    st_e = c.post(f"/api/calibrations/{cal_id}/evaluate", json=nonmono).get_json()
    assert "non_monotonic" in st_e["evaluation"]["blocker_types"]
    bad = next(i for i in st_e["evaluation"]["issues"] if i["type"] == "non_monotonic")
    assert set(bad["lane_ids"]) == {lanes[2]["id"], lanes[3]["id"]}
    r = c.post(f"/api/calibrations/{cal_id}/fit", json=nonmono)
    assert r.status_code == 400 and "单调" in r.get_json()["description"]
    print("7b. 响应不单调:定位回落泳道并阻断成线 ✔")

    # 7c. 排除 4 个标准点(仅剩 1 个且有理由)=> 不足
    few = cal_payload(lanes, overrides={
        li: {"excluded": True, "exclude_reason": f"复测排除 {li}"} for li in range(1, 5)})
    r = c.post(f"/api/calibrations/{cal_id}/fit", json=few)
    assert r.status_code == 400 and "不足" in r.get_json()["description"]
    st_e = c.post(f"/api/calibrations/{cal_id}/evaluate", json=few).get_json()
    assert "too_few_standards" in st_e["evaluation"]["blocker_types"]
    print("7c. 标准点不足阻断成线 ✔")

    # 7d. 空白异常:空白泳道绑一个峰(正常板空白无峰)—— 用响应高的板重建
    aid2, lanes2 = setup_blank_anomaly_plate(c)
    cal2 = c.post(f"/api/analyses/{aid2}/calibrations", json={
        "name": "空白异常校准", "target_name": "目标X", "target_rf": RF_TARGET}).get_json()
    cid2 = cal2["calibration"]["id"]
    pl = cal_payload(lanes2)
    st_e = c.post(f"/api/calibrations/{cid2}/evaluate", json=pl).get_json()
    assert "blank_anomaly" in st_e["evaluation"]["blocker_types"]
    bid = next(l["id"] for l in lanes2 if l["label"] == "L6")
    assert bid in next(i for i in st_e["evaluation"]["issues"]
                       if i["type"] == "blank_anomaly")["lane_ids"]
    r = c.post(f"/api/calibrations/{cid2}/fit", json=pl)
    assert r.status_code == 400
    print("7d. 空白异常:定位空白泳道并阻断 ✔")

    # 8. 来源变更 -> 旧版本过期,仍可查看/导出(带 EXPIRED 标记)
    before = c.get(f"/api/calibrations/{cal_id}").get_json()["versions"]
    assert all(not v["stale"] for v in before)
    # 移动一条泳道边界(泳道 id 保留但面积/指纹变化)
    lanes_state = c.get(f"/api/analyses/{aid}").get_json()["lanes"]
    lanes_state[0]["x0"] += 3.0
    c.post(f"/api/analyses/{aid}/lanes", json={"lanes": [
        {"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"]}
        for l in lanes_state]})
    after = c.get(f"/api/calibrations/{cal_id}").get_json()["versions"]
    assert all(v["stale"] for v in after), "来源几何/边界变更后旧模型应过期"
    old_vid = after[0]["id"]
    vr = c.get(f"/api/calibrations/{cal_id}/versions/{old_vid}").get_json()
    assert vr["stale"] and vr["snapshot"]["fit"]["slope"], "过期版本仍可查看留档"
    csv_old = c.get(f"/api/calibrations/{cal_id}/versions/{old_vid}/samples.csv")
    fig_old = c.get(f"/api/calibrations/{cal_id}/versions/{old_vid}/figure.png")
    assert csv_old.status_code == 200 and b"EXPIRED" in csv_old.headers.get(
        "Content-Disposition", "").encode()
    assert fig_old.status_code == 200
    print("8. 来源变更 -> 全部旧版本标为过期,仍可查看/导出(文件名带 EXPIRED) ✔")

    # 9. 几何重设 -> 泳道全部消失:旧标注成为 orphan,不参与评估但仍可查看/清理
    moved_corners = [[5, 5], [W - 6, 5], [W - 6, H - 6], [5, H - 6]]
    c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": moved_corners,
        "baseline": [W / 2, BASE_Y], "front": [W / 2, FRONT_Y],
        "scale": {"p1": [5, BASE_Y], "p2": [5, FRONT_Y], "mm": 80.0}})
    st_o = c.get(f"/api/calibrations/{cal_id}").get_json()
    orphans = [l for l in st_o["lanes"] if l.get("orphan")]
    assert len(orphans) == len(lanes), "几何重建后旧泳道标注应列为 orphan"
    # 评估不被 orphan 污染(没有标准点 -> 仅 too_few 阻断)
    assert "too_few_standards" in st_o["evaluation"]["blocker_types"]
    assert sorted(st_o["missing_lanes"]) == sorted(l["id"] for l in lanes)
    # 显式清除所有 orphan 标注后,不再列出
    clear = c.post(f"/api/calibrations/{cal_id}/evaluate", json={
        "model": "linear", "lanes": [],
        "removed_lane_ids": [l["id"] for l in lanes]}).get_json()
    assert not [l for l in clear["lanes"] if l.get("orphan")]
    print("9. 几何重建 -> 旧标注 orphan 化(不参与评估、可追溯、可清除) ✔")

    # 10. 删除校准
    r = c.post(f"/api/calibrations/{cid2}/delete", json={}).get_json()
    assert r["ok"] and c.get(f"/api/calibrations/{cid2}").status_code == 404
    print("10. 删除校准 ✔")

    print("\n校准曲线端到端测试通过 ✔")


def setup_blank_anomaly_plate(c):
    """空白处有强峰(响应 > 最低标准点 10%)的板。"""
    st = upload_plate(c, blank_area=25.0)
    aid = st["analysis"]["id"]
    c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": [[0, 0], [W - 1, 0], [W - 1, H - 1], [0, H - 1]],
        "baseline": [W / 2, BASE_Y], "front": [W / 2, FRONT_Y],
        "scale": {"p1": [5, BASE_Y], "p2": [5, FRONT_Y], "mm": 80.0}})
    half = 26
    lanes_in = [{"x0": x - half, "x1": x + half, "label": f"L{i+1}"}
                for i, x in enumerate(LANE_X)]
    lanes = c.post(f"/api/analyses/{aid}/lanes", json={"lanes": lanes_in}).get_json()["lanes"]
    for lane in lanes:
        wins = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/autodetect",
                      json={}).get_json()["windows"]
        wins = sorted(wins, key=lambda w:
                      abs(((w["y0"] + w["y1"]) / 2 - spot_y(RF_TARGET))))[:1]
        peaks = [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]
        r = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/peaks",
                   json={"peaks": peaks}).get_json()["peaks"]
        lane["peaks"] = r
    return aid, lanes


def main():
    test_fit_models()
    test_evaluate_basic_and_backcalc()
    test_too_few_and_exclude_reason()
    test_duplicate_conflict()
    test_non_monotonic_and_bad_slope()
    test_blank_anomaly()
    test_incomplete_unknown()
    e2e()
    print("\n全部校准曲线测试通过 ✔")


if __name__ == "__main__":
    main()
