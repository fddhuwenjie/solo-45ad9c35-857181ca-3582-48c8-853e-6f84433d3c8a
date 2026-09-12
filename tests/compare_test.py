"""跨板对照测试:compare 纯函数单元测试 + 端到端 API 流程。

运行: .venv/bin/python tests/compare_test.py
"""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image  # noqa: E402

from app import create_app  # noqa: E402
from tlc import compare  # noqa: E402

META = json.load(open(os.path.join(os.path.dirname(__file__), "..", "sample", "sample_meta.json")))
SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample", "sample_plate.png")
CFG = dict(compare.DEFAULTS)


# ================= 单元测试 =================

def test_assign_standards():
    T = [{"id": 1, "name": "A", "rf_ref": 0.2}, {"id": 2, "name": "B", "rf_ref": 0.5},
         {"id": 3, "name": "C", "rf_ref": 0.8}]
    # 正常:全部匹配,偏移小
    spots = [{"peak_id": 1, "rf": 0.21, "area": 10}, {"peak_id": 2, "rf": 0.52, "area": 20},
             {"peak_id": 3, "rf": 0.79, "area": 30}]
    a, off, probs = compare.assign_standards(T, spots, CFG)
    assert set(a) == {1, 2, 3} and not probs and abs(off - 0.0067) < 0.01, (a, off, probs)
    # 标准缺失:斑点数不足
    a, off, probs = compare.assign_standards(T, spots[:2], CFG)
    assert any(p["type"] == "std_missing" for p in probs) and 3 not in a
    # 标准缺失:空泳道
    _, _, probs = compare.assign_standards(T, [], CFG)
    assert probs[0]["type"] == "std_missing"
    # 标准缺失:偏差超硬上限
    far = [{"peak_id": 1, "rf": 0.45, "area": 1}, {"peak_id": 2, "rf": 0.75, "area": 1}]
    a, _, probs = compare.assign_standards(T[:2], far, CFG)
    assert any(p["type"] == "std_missing" for p in probs)
    # Rf 偏移超限:在硬上限内但超名义容差
    shifted = [{"peak_id": 1, "rf": 0.31, "area": 1}, {"peak_id": 2, "rf": 0.61, "area": 1}]
    a, off, probs = compare.assign_standards(T[:2], shifted, CFG)
    assert set(a) == {1, 2} and any(p["type"] == "rf_shift" for p in probs), (a, off, probs)
    print("U1. 标准品分配:正常/缺失/偏移超限 ✔")


def test_normalization():
    coefs, outliers = compare.normalization({1: 100.0, 2: 100.0, 3: 25.0}, 3.0)
    assert abs(coefs[1] - 1.0) < 1e-9 and abs(coefs[3] - 4.0) < 1e-9
    assert outliers == {3}
    coefs, outliers = compare.normalization({1: 100.0, 2: 110.0}, 3.0)
    assert not outliers
    print("U2. 归一化系数与离群判定 ✔")


def test_shape_corr():
    import math
    g = [math.exp(-0.5 * ((i - 20) / 5) ** 2) for i in range(40)]
    assert abs(compare.shape_corr(g, 10, 30, g, 10, 30) - 1.0) < 1e-9
    flat = [1.0] * 40
    assert compare.shape_corr(g, 10, 30, flat, 10, 30) == 0.0
    print("U3. 峰形相关 ✔")


def test_propose_order():
    targets = [{"id": 1, "name": "A", "rf_ref": 0.50, "rf_tol": 0.05},
               {"id": 2, "name": "B", "rf_ref": 0.55, "rf_tol": 0.05}]
    spots = [{"peak_id": 10, "lane_id": 2, "rf": 0.48, "area": 5, "y0": 0, "y1": 5},
             {"peak_id": 11, "lane_id": 2, "rf": 0.52, "area": 5, "y0": 6, "y1": 10}]
    best, cands = compare.propose_targets(targets, spots, std_lane_id=1, offset=0.0,
                                          std_assignment={}, profiles={}, cfg=CFG)
    # 单调约束:两个目标不得共用同一斑点,且次序与 rf_ref 一致
    assert best[1]["peak_id"] == 10 and best[2]["peak_id"] == 11, best
    assert len(cands[1]) == 2 and len(cands[2]) == 1
    print("U4. 候选提议:标准品次序单调约束 ✔")


def test_order_violations():
    targets = [{"id": 1, "name": "A", "rf_ref": 0.5}, {"id": 2, "name": "B", "rf_ref": 0.8}]
    spots_by_peak = {10: {"rf": 0.7}, 11: {"rf": 0.3}}
    matches = [{"target_id": 1, "peak_id": 10, "lane_id": 2},
               {"target_id": 2, "peak_id": 11, "lane_id": 2}]
    bad = compare.order_violations(targets, matches, spots_by_peak)
    assert bad == [(1, 2)], bad
    matches[0]["lane_id"] = 3  # 不同泳道不构成颠倒
    assert not compare.order_violations(targets, matches, spots_by_peak)
    print("U5. 次序颠倒检查 ✔")


def test_summarize():
    s = compare.summarize([10.0, 20.0, 30.0])
    assert s["n"] == 3 and abs(s["mean"] - 20) < 1e-9 and abs(s["cv_pct"] - 50) < 1e-9
    assert compare.summarize([])["n"] == 0
    print("U6. 板间变异统计 ✔")


# ================= 端到端 =================

def setup_plate(c, aid):
    """几何 + 4 泳道 + 自动检峰(L4 手动补一个弱斑点),返回泳道列表。"""
    g = c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": META["corners"], "baseline": META["baseline"],
        "front": META["front"], "scale": META["scale"]}).get_json()
    d = g["geometry"]["derived"]
    W = d["width"]
    lanes = []
    for i, px in enumerate(META["lanes_plate_x"], 1):
        cx = px / META["plate_size"][0] * W
        half = 45 / META["plate_size"][0] * W
        lanes.append({"x0": cx - half, "x1": cx + half, "label": f"L{i}"})
    lanes = c.post(f"/api/analyses/{aid}/lanes", json={"lanes": lanes}).get_json()["lanes"]
    for lane in lanes:
        wins = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/autodetect",
                      json={}).get_json()["windows"]
        peaks = [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]
        if lane["label"] == "L4":
            # 弱斑点(Rf≈0.25)自动检不出,手动补积分窗
            y = d["baseline_y"] - 0.25 * (d["baseline_y"] - d["front_y"])
            peaks.append({"y0": y - 12, "y1": y + 12, "origin": "manual"})
        c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/peaks",
               json={"peaks": peaks})
    return lanes


def make_analysis(c, factor=1.0, name="板", std_size_scale=1.0):
    """factor<1 做显色更弱的板;std_size_scale<1 做标准品点样量更小的板。"""
    if factor == 1.0 and std_size_scale == 1.0:
        st = c.post("/api/analyses/sample").get_json()
    else:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sample"))
        import make_sample
        plate = make_sample.build_plate(amp_scale=factor, std_size_scale=std_size_scale)
        photo = make_sample.build_photo(plate)
        buf = io.BytesIO()
        photo.save(buf, "PNG")
        buf.seek(0)
        st = c.post("/api/analyses",
                    data={"file": (buf, "weak.png"), "name": name},
                    content_type="multipart/form-data").get_json()
    return st["analysis"]["id"]


def get_state(c, cid):
    return c.get(f"/api/comparisons/{cid}").get_json()


def member_of(st, mid=None, aid=None):
    for m in st["members"]:
        if (mid is not None and m["id"] == mid) or (aid is not None and m["analysis_id"] == aid):
            return m
    raise AssertionError("成员不存在")


def match_of(st, tid, mid):
    return next(mt for mt in st["matches"] if mt["target_id"] == tid and mt["member_id"] == mid)


def e2e():
    tmp = tempfile.mkdtemp(prefix="tlc_cmp_")
    app = create_app(data_dir=tmp)
    c = app.test_client()

    # 1. 准备 5 块板:3 正常 + 显色 0.5x(系数≈2,有效)+ 标准品点样量 0.5x
    #    (标准响应≈0.25,系数≈4,离群)
    aids, lane_ids = [], []
    cfgs = [({}, "板1"), ({}, "板2"), ({}, "板3"),
            ({"factor": 0.5}, "板4-显色弱"), ({"std_size_scale": 0.5}, "板5-标准点样少")]
    for kw, name in cfgs:
        aid = make_analysis(c, name=name, **kw)
        lanes = setup_plate(c, aid)
        aids.append(aid)
        lane_ids.append({l["label"]: l["id"] for l in lanes})
    print(f"1. 5 块板定量完成:{aids}")

    # 2. 未定量板不能加入对照
    naked = c.post("/api/analyses/sample").get_json()["analysis"]["id"]
    cid = c.post("/api/comparisons", json={"name": "批间对照"}).get_json()["comparison"]["id"]
    r = c.post(f"/api/comparisons/{cid}/members",
               json={"analysis_id": naked, "std_lane_id": 1})
    assert r.status_code == 400, "未完成定量的板应被拒绝"
    print("2. 未定量板加入被拒 ✔")

    # 3. 加入 5 个成员(标准品泳道均为 L1)
    mids = []
    for i, aid in enumerate(aids):
        st = c.post(f"/api/comparisons/{cid}/members",
                    json={"analysis_id": aid, "std_lane_id": lane_ids[i]["L1"]}).get_json()
        mids.append(st["members"][-1]["id"])
    assert len(st["members"]) == 5
    print(f"3. 成员加入:{mids}")

    # 4. 定义 3 个目标(对应标准泳道 L1 的 0.5/0.8/0.2 标准品)
    tids = {}
    for name, rf in [("组分A", 0.5), ("组分B", 0.8), ("组分C", 0.2)]:
        st = c.post(f"/api/comparisons/{cid}/targets",
                    json={"name": name, "rf_ref": rf, "rf_tol": 0.06}).get_json()
        tids[name] = st["targets"][-1]["id"]
    tA, tB, tC = tids["组分A"], tids["组分B"], tids["组分C"]
    st = get_state(c, cid)
    # 自动匹配:正常板三个目标都应有绑定
    for mid in mids[:3]:
        for tid in tids.values():
            mt = match_of(st, tid, mid)
            assert mt["peak_id"], f"成员 {mid} 目标 {tid} 应有自动匹配"
    # 系数:正常板 ≈1,显色 0.5x 板 ≈2,标准点样 0.5x 板 ≈4 且离群
    coefs = {m["analysis_id"]: m["coef"] for m in st["members"]}
    assert abs(coefs[aids[0]] - 1.0) < 0.2, coefs
    assert abs(coefs[aids[3]] - 2.0) < 0.5, coefs
    m_dark = member_of(st, aid=aids[4])
    assert not m_dark["valid"] and any(p["type"] == "coef_outlier" for p in m_dark["problems"]), \
        m_dark["problems"]
    # 系数离群板被排除出汇总并说明原因
    ex = next(e for e in st["summary"]["excluded"] if e["analysis_id"] == aids[4])
    assert any("离群" in r for r in ex["reasons"])
    # 其余 4 板进入汇总;目标B 候选唯一(L3 的 0.754),归一化后板间变异应很小
    rowB = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tB)
    assert rowB["stats"]["n"] == 4, rowB["stats"]
    assert rowB["stats"]["cv_pct"] < 15, rowB["stats"]
    print(f"4. 自动匹配 + 归一化:系数 {[round(coefs[a],2) for a in aids]},"
          f"系数离群板被排除,目标B CV={rowB['stats']['cv_pct']:.1f}%")

    # 5. 改绑 + 锁定 + 重配保留
    mt = match_of(st, tA, mids[0])
    other = next(cd for cd in mt["candidates"] if cd["peak_id"] != mt["peak_id"])
    st = c.post(f"/api/comparisons/{cid}/matches/bind",
                json={"target_id": tA, "member_id": mids[0], "peak_id": other["peak_id"]}).get_json()
    mt = match_of(st, tA, mids[0])
    assert mt["peak_id"] == other["peak_id"] and mt["status"] == "manual"
    st = c.post(f"/api/comparisons/{cid}/matches/lock",
                json={"target_id": tA, "member_id": mids[0], "locked": True}).get_json()
    assert match_of(st, tA, mids[0])["locked"]
    # 锁定后不能改绑
    r = c.post(f"/api/comparisons/{cid}/matches/bind",
               json={"target_id": tA, "member_id": mids[0], "peak_id": None})
    assert r.status_code == 400
    # 重配不动手动/锁定
    st = c.post(f"/api/comparisons/{cid}/members/{mids[0]}/rematch", json={}).get_json()
    assert match_of(st, tA, mids[0])["peak_id"] == other["peak_id"]
    print("5. 改绑/锁定/重配保留 ✔")

    # 6. 拆开误配:汇总 n 减少
    n_before = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tB)["stats"]["n"]
    st = c.post(f"/api/comparisons/{cid}/matches/bind",
                json={"target_id": tB, "member_id": mids[1], "peak_id": None}).get_json()
    rowB = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tB)
    assert rowB["stats"]["n"] == n_before - 1, (n_before, rowB["stats"])
    assert not match_of(st, tB, mids[1])["peak_id"]
    print(f"6. 拆开误配:目标B 汇总 n {n_before} -> {rowB['stats']['n']}")

    # 7. 次序颠倒:把目标B(参考0.8)绑到比目标A(参考0.5)更低 Rf 的同泳道斑点
    res1 = c.get(f"/api/analyses/{aids[0]}/results").get_json()
    l4 = lane_ids[0]["L4"]
    p50 = next(s for s in res1["spots"] if s["lane_id"] == l4 and abs(s["rf"] - 0.5) < 0.06)
    p25 = next(s for s in res1["spots"] if s["lane_id"] == l4 and abs(s["rf"] - 0.25) < 0.06)
    # tA 在第 5 步已锁定,先解锁再改绑到 L4 的 0.50 斑点
    c.post(f"/api/comparisons/{cid}/matches/lock",
           json={"target_id": tA, "member_id": mids[0], "locked": False})
    c.post(f"/api/comparisons/{cid}/matches/bind",
           json={"target_id": tA, "member_id": mids[0], "peak_id": p50["peak_id"]})
    st = c.post(f"/api/comparisons/{cid}/matches/bind",
                json={"target_id": tB, "member_id": mids[0], "peak_id": p25["peak_id"]}).get_json()
    m0 = member_of(st, mid=mids[0])
    assert not m0["valid"] and any(p["type"] == "order_reversed" for p in m0["problems"]), \
        m0["problems"]
    assert any(e["analysis_id"] == aids[0] for e in st["summary"]["excluded"])
    # 拆开后恢复
    st = c.post(f"/api/comparisons/{cid}/matches/bind",
                json={"target_id": tB, "member_id": mids[0], "peak_id": None}).get_json()
    assert member_of(st, mid=mids[0])["valid"]
    print("7. 次序颠倒:同泳道反序绑定被排除,拆开后恢复 ✔")

    # 8. 泳道改动 -> 该成员匹配逐条失效;单独改绑 A 只恢复 A,其余继续失效;重配全恢复
    st0 = get_state(c, cid)
    m0_tA_area = match_of(st0, tA, mids[0])["spot"]["area"]
    lanes2 = c.get(f"/api/analyses/{aids[1]}").get_json()["lanes"]
    lanes2[0]["x0"] += 2.0
    c.post(f"/api/analyses/{aids[1]}/lanes", json={"lanes": [
        {"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"]} for l in lanes2]})
    st = get_state(c, cid)
    m1 = member_of(st, mid=mids[1])
    assert m1["stale"], "板数据变更后成员应处于 stale"
    assert m1["stale_matches"] == 2, m1  # tA/tC 的绑定失效(tB 第 6 步已拆开)
    assert not match_of(st, tA, mids[1])["valid"]
    assert not match_of(st, tC, mids[1])["valid"]
    for row in st["summary"]["rows"]:
        assert all(e["member_id"] != mids[1] for e in row["entries"]), \
            f"{row['target']['name']} 的失效匹配不得进入汇总"
    # 其他成员不受影响:匹配仍有效,原始面积不变
    assert match_of(st, tA, mids[0])["valid"]
    assert match_of(st, tA, mids[0])["spot"]["area"] == m0_tA_area
    rowA0 = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tA)
    assert any(e["member_id"] == mids[0] for e in rowA0["entries"])
    print("8. 泳道边界改动 -> 该成员匹配逐条失效并排除,其他成员不受影响 ✔")

    # 8b. 回归:单独改绑组分A -> 只有 A 恢复;未重新匹配的 C 继续失效
    cand = match_of(st, tA, mids[1])["candidates"][0]
    st = c.post(f"/api/comparisons/{cid}/matches/bind",
                json={"target_id": tA, "member_id": mids[1],
                      "peak_id": cand["peak_id"]}).get_json()
    m1 = member_of(st, mid=mids[1])
    assert m1["stale"], "改绑不刷新成员级指纹,成员仍 stale"
    assert m1["stale_matches"] == 1, m1
    assert match_of(st, tA, mids[1])["valid"], "改绑的 A 应恢复有效"
    mtC = match_of(st, tC, mids[1])
    assert mtC["stale"] and not mtC["valid"], "未重新匹配的 C 必须继续失效"
    rowA = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tA)
    rowC = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tC)
    assert any(e["member_id"] == mids[1] for e in rowA["entries"]), "A 应回到汇总"
    assert all(e["member_id"] != mids[1] for e in rowC["entries"]), \
        "C 不得以旧 auto 关系重新进入汇总"
    assert match_of(st, tA, mids[0])["valid"] and \
        match_of(st, tA, mids[0])["spot"]["area"] == m0_tA_area, "其他成员不受影响"
    print("   单独改绑 A:仅 A 恢复,C 保持失效并排除在汇总外 ✔")

    # 8c. 重新匹配 -> 全部恢复
    st = c.post(f"/api/comparisons/{cid}/members/{mids[1]}/rematch", json={}).get_json()
    m1 = member_of(st, mid=mids[1])
    assert not m1["stale"] and m1["stale_matches"] == 0
    assert match_of(st, tC, mids[1])["valid"]
    rowC = next(r for r in st["summary"]["rows"] if r["target"]["id"] == tC)
    assert any(e["member_id"] == mids[1] for e in rowC["entries"])
    print("   重新匹配后成员整体恢复 ✔")

    # 9. 几何改动 -> 失效
    moved = [[x + 3, y + 2] for x, y in META["corners"]]
    c.post(f"/api/analyses/{aids[2]}/geometry", json={
        "corners": moved, "baseline": META["baseline"],
        "front": META["front"], "scale": META["scale"]})
    st = get_state(c, cid)
    assert member_of(st, mid=mids[2])["stale"]
    print("9. 几何改动 -> 引用它的匹配失效 ✔")

    # 10. 标准缺失:标准品泳道内无斑点
    aid6 = make_analysis(c)
    g6 = c.post(f"/api/analyses/{aid6}/geometry", json={
        "corners": META["corners"], "baseline": META["baseline"],
        "front": META["front"], "scale": META["scale"]}).get_json()
    W = g6["geometry"]["derived"]["width"]
    lanes6 = c.post(f"/api/analyses/{aid6}/lanes", json={"lanes": [
        {"x0": 0.90 * W, "x1": 0.98 * W, "label": "空泳道"},
        {"x0": 0.38 * W, "x1": 0.44 * W, "label": "样品"}]}).get_json()["lanes"]
    sample_lane = next(l for l in lanes6 if l["label"] == "样品")
    wins = c.post(f"/api/analyses/{aid6}/lanes/{sample_lane['id']}/autodetect",
                  json={}).get_json()["windows"]
    c.post(f"/api/analyses/{aid6}/lanes/{sample_lane['id']}/peaks",
           json={"peaks": [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]})
    empty_lane = next(l for l in lanes6 if l["label"] == "空泳道")
    st = c.post(f"/api/comparisons/{cid}/members",
                json={"analysis_id": aid6, "std_lane_id": empty_lane["id"]}).get_json()
    m6 = member_of(st, aid=aid6)
    assert not m6["valid"] and any(p["type"] == "std_missing" for p in m6["problems"])
    assert any("标准" in r for e in st["summary"]["excluded"] if e["analysis_id"] == aid6
               for r in e["reasons"])
    print("10. 标准缺失被排除并说明原因 ✔")

    # 11. 导出
    csv_r = c.get(f"/api/comparisons/{cid}/export/compare.csv")
    js_r = c.get(f"/api/comparisons/{cid}/export/matches.json")
    fig_r = c.get(f"/api/comparisons/{cid}/export/figure.png")
    assert csv_r.status_code == 200 and b"norm_area" in csv_r.data
    txt = csv_r.data.decode("utf-8-sig")
    assert "coef" in txt and "target_cv_pct" in txt
    payload = json.loads(js_r.data.decode("utf-8"))
    assert payload["matches"] and payload["summary"]["rows"] and payload["members"]
    assert fig_r.status_code == 200 and fig_r.data[:4] == b"\x89PNG"
    print("11. 跨板 CSV / 匹配 JSON / 对照图导出 ✔")

    # 12. 泳道删除重建(标准品泳道引用失效)-> 重指标准品泳道后恢复
    g1 = c.get(f"/api/analyses/{aids[0]}").get_json()
    W1 = g1["geometry"]["derived"]["width"]
    lanes_new = []
    for i, px in enumerate(META["lanes_plate_x"], 1):
        cx = px / META["plate_size"][0] * W1
        half = 45 / META["plate_size"][0] * W1
        lanes_new.append({"x0": cx - half, "x1": cx + half, "label": f"L{i}"})
    lanes_new = c.post(f"/api/analyses/{aids[0]}/lanes",
                       json={"lanes": lanes_new}).get_json()["lanes"]
    for lane in lanes_new:
        wins = c.post(f"/api/analyses/{aids[0]}/lanes/{lane['id']}/autodetect",
                      json={}).get_json()["windows"]
        c.post(f"/api/analyses/{aids[0]}/lanes/{lane['id']}/peaks",
               json={"peaks": [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]})
    st = get_state(c, cid)
    m0 = member_of(st, mid=mids[0])
    assert m0["stale"] and any(p["type"] == "std_missing" for p in m0["problems"])
    new_l1 = next(l for l in lanes_new if l["label"] == "L1")
    st = c.post(f"/api/comparisons/{cid}/members/{mids[0]}/std_lane",
                json={"std_lane_id": new_l1["id"]}).get_json()
    assert member_of(st, mid=mids[0])["valid"]
    print("12. 泳道重建 -> 标准品泳道失效,重指后恢复 ✔")

    print("\n跨板对照端到端测试通过 ✔")


def main():
    test_assign_standards()
    test_normalization()
    test_shape_corr()
    test_propose_order()
    test_order_violations()
    test_summarize()
    e2e()
    print("\n全部跨板对照测试通过 ✔")


if __name__ == "__main__":
    main()
