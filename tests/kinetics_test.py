"""显色时间序列校审测试:kinetics 纯函数单元测试 + 端到端 API 流程。

运行: .venv/bin/python tests/kinetics_test.py
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
from tlc import kinetics as k  # noqa: E402


# ================= 单元测试 =================

def test_registration():
    # 4 对点:在 (10,20) 平移坐标系中 => frame = ref + (10,20)
    W, H = 100, 80
    corners_ref = [(0.0, 0.0), (W - 1.0, 0.0), (W - 1.0, H - 1.0), (0.0, H - 1.0)]
    dx, dy = 10.0, 20.0
    pairs = [(rx + dx, ry + dy, rx, ry) for rx, ry in corners_ref]
    Hm = k.fit_registration(pairs)
    px, py = k._apply_h(Hm, 50 + dx, 40 + dy)
    assert abs(px - 50) < 1e-6 and abs(py - 40) < 1e-6, (px, py)
    inv = k.invert_homography(Hm)
    qx, qy = k._apply_h(inv, 50, 40)
    assert abs(qx - 60) < 1e-6 and abs(qy - 60) < 1e-6
    # 检查点残差:准确配准应近 0
    resid = k.registration_residual(Hm, pairs)
    assert resid < 1e-6, resid
    # 退化点共线 => 不可解
    try:
        k.fit_registration([(0, 0, 0, 0), (1, 0, 1, 0), (2, 0, 2, 0), (3, 0, 3, 0)])
        assert False, "退化控制点应抛错"
    except ValueError:
        pass
    # 检查点错位 => 超定拟合下残差超限(4 个角点 + 2 个额外检查点)
    extra = [(50.0 + dx, 20.0 + dy, 50.0, 20.0), (70.0 + dx, 60.0 + dy, 70.0, 60.0)]
    good = k.fit_registration(pairs + extra)
    assert k.registration_residual(good, extra) < 1e-6
    bad_extra = extra[:1] + [(70.0 + dx + 8, 60.0 + dy, 70.0, 60.0)]
    Hb = k.fit_registration(pairs + bad_extra)
    assert k.registration_residual(Hb, bad_extra) > 3.0
    print("U1. 控制点单应拟合/求逆/残差/退化拒绝 ✔")


def test_time_parse_and_order():
    assert abs(k.parse_time("01:00") - 3600) < 1e-9
    assert abs(k.parse_time("00:30") - 1800) < 1e-9
    assert abs(k.parse_time("12.5") - 12.5) < 1e-9
    frames = [{"id": 1, "taken_at": "10:00:30"}, {"id": 2, "taken_at": "10:01:00"},
              {"id": 3, "taken_at": "10:02:00"}]
    rows, issues = k.frame_times(frames, "10:00:00")
    assert [r["t_sec"] for r in rows] == [30, 60, 120]
    assert not issues
    # 重复
    frames2 = [{"id": 1, "taken_at": "30"}, {"id": 2, "taken_at": "30"}]
    _, issues = k.frame_times([{"id": i + 1, "taken_at": f["taken_at"]}
                               for i, f in enumerate(frames2)])
    assert any(i["kind"] == "duplicate_time" for i in issues)
    # 倒序
    frames3 = [{"id": 1, "taken_at": "60"}, {"id": 2, "taken_at": "30"}]
    _, issues = k.frame_times(frames3)
    assert any(i["kind"] == "time_reversed" for i in issues)
    # 钟点缺显色开始
    frames4 = [{"id": 1, "taken_at": "10:01"}, {"id": 2, "taken_at": "10:02"}]
    _, issues = k.frame_times(frames4, None)
    assert any(i["kind"] == "start_missing" for i in issues)
    # 非法
    _, issues = k.frame_times([{"id": 1, "taken_at": "abc"}])
    assert any(i["kind"] == "time_invalid" for i in issues)
    print("U2. 时刻解析/重复/倒序/缺开始/非法 ✔")


def test_plateau_and_window():
    # 上升 -> 平台 -> 衰减
    ts = [10, 20, 30, 40, 50, 60]
    vs = [10, 48, 50, 51, 30, 12]
    an = k.analyze_series(ts, vs, tol=0.08, plateau_min=2)
    assert an["peak"]["t"] == 40, an["peak"]
    assert an["plateau"] == [1, 3], an["plateau"]
    kinds = [s["kind"] for s in an["segments"]]
    assert kinds == ["rise", "plateau", "decay"], kinds
    # 多斑点平台交集
    sc1 = {"peak_id": 1, "times": ts, **an}
    vs2 = [5, 47, 50, 49, 28, 10]
    an2 = k.analyze_series(ts, vs2, tol=0.08, plateau_min=2)
    sc2 = {"peak_id": 2, "times": ts, **an2}
    sug = k.suggest_window([sc1, sc2], min_frames=2)
    assert sug and sug["t0"] >= 20 and sug["t1"] <= 40, sug
    # 窗口均值
    wm = k.window_means(ts, vs, 20, 40)
    assert wm["n"] == 3 and abs(wm["mean"] - (48 + 50 + 51) / 3) < 1e-9
    # 平台不足:单调上升无平台
    an3 = k.analyze_series(ts, [1, 2, 3, 4, 5, 6], tol=0.06, plateau_min=2)
    assert an3["plateau"] is None
    print("U3. 平台/峰/衰减识别 + 公共窗口 + 窗口统计 ✔")


def test_window_validation():
    ts = [10, 20, 30, 40, 50, 60]
    vs = [10, 48, 50, 51, 30, 12]
    an = k.analyze_series(ts, vs, tol=0.08, plateau_min=2)
    curves = {1: {"peak_id": 1, "times": an["times"], "areas": an["areas"],
                  "plateau": an["plateau"]}}
    recs = [{"id": i + 1, "t_sec": t, "usable": True, "excluded": False,
             "per_spot": {1: {"area": v, "saturated_px": 0, "drift_px": 1.0}}}
            for i, (t, v) in enumerate(zip(ts, vs))]
    cfg = dict(k.DEFAULTS); cfg["min_valid_frames"] = 2
    vw = k.validate_window({"t0": 20, "t1": 40}, recs, curves, cfg)
    assert vw["ok"], [i["message"] for i in vw["issues"]]
    assert abs(vw["window_stats"][1]["mean"] - 49.6666667) < 1e-4
    # 窗口跨过衰减帧 => 阻断
    vw2 = k.validate_window({"t0": 20, "t1": 60}, recs, curves, cfg)
    assert not vw2["ok"]
    assert any(i["type"] == "window_invalid" for i in vw2["issues"])
    # 饱和帧在窗口内
    recs[2]["per_spot"][1]["saturated_px"] = 99
    vw3 = k.validate_window({"t0": 20, "t1": 40}, recs, curves, cfg)
    assert any(i["type"] == "window_saturated" for i in vw3["issues"])
    # 帧数不足
    vw4 = k.validate_window({"t0": 20, "t1": 30}, recs, curves, k.DEFAULTS)
    assert any(i["type"] == "window_invalid" for i in vw4["issues"])
    print("U4. 窗口校验:平台一致/跨区阻断/饱和/帧数不足 ✔")


# ================= 端到端 =================

W, H = 700, 520
BASE_Y, FRONT_Y = 460.0, 60.0
LANE_X = [70, 150, 230, 310]
SPOT_RF = [0.22, 0.66]
# 板面四角在帧画布内留边(模拟真实照片板外有背景),帧间平移后四角仍在画面内可精确配准
INSET = 22
REF_CORNERS = [[INSET, INSET], [W - 1 - INSET, INSET],
               [W - 1 - INSET, H - 1 - INSET], [INSET, H - 1 - INSET]]


def spot_y(rf):
    return BASE_Y - rf * (BASE_Y - FRONT_Y)


def make_frame(amp_fn, shift=(0, 0), saturated=False):
    """amp_fn(t_index)(lane_i, rf_j) -> 幅度;shift 为帧内板面相对参考的平移。

    四角与斑点整体平移并保持在帧画布内(平移量小,不超出边界)。
    """
    img = Image.new("L", (W, H), 220)
    ox, oy = shift
    px = img.load()

    def gauss(xc, yc, amp, sx=11, sy=8):
        val = 0 if saturated else None
        r = int(sx * 3) + 1
        for j in range(max(0, int(yc) - r), min(H, int(yc) + r + 1)):
            for i in range(max(0, int(xc) - r), min(W, int(xc) + r + 1)):
                v = amp * math.exp(-0.5 * (((i - xc) / sx) ** 2 + ((j - yc) / sy) ** 2))
                if val is not None:
                    if v > 30:
                        px[i, j] = val
                    else:
                        px[i, j] = max(0, int(px[i, j] - v))
                else:
                    px[i, j] = max(0, int(px[i, j] - v))

    for li, xc in enumerate(LANE_X):
        for rf in SPOT_RF:
            gauss(xc + ox, spot_y(rf) + oy, amp_fn(li, rf))
    return img


def frame_corners(shift):
    """帧板面四角 = 参考板四角平移(帧像素) -> 校正空间矩形四角(控制点对)。"""
    ox, oy = shift
    dW, dH = W - 1 - 2 * INSET, H - 1 - 2 * INSET
    ref = [[0, 0], [dW, 0], [dW, dH], [0, dH]]
    return [{"fx": cx + ox, "fy": cy + oy, "rx": float(rx), "ry": float(ry),
             "kind": "corner"}
            for (cx, cy), (rx, ry) in zip(REF_CORNERS, ref)]


def amp_profile(t):
    """显色动力学:10s 弱、30/40/50s 平台(均在峰值 ±6% 内)、60s 后衰减。"""
    table = {0: 0.25, 1: 0.98, 2: 1.0, 3: 0.99, 4: 0.6, 5: 0.3}
    return table[t]


def upload_plate_image(c, img, name):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return c.post("/api/analyses", data={"file": (buf, "p.png"), "name": name},
                  content_type="multipart/form-data").get_json()


def setup_base_plate(c):
    """参考板 = 平台期(t=2,幅度 1.0),完成几何/泳道/峰。板面四角内缩留边。"""
    st = upload_plate_image(c, make_frame(lambda li, rf: 20.0 + 5 * li),
                            "动力学板")
    aid = st["analysis"]["id"]
    c.post(f"/api/analyses/{aid}/geometry", json={
        "corners": REF_CORNERS,
        "baseline": [W / 2, BASE_Y], "front": [W / 2, FRONT_Y],
        "scale": {"p1": [5, BASE_Y], "p2": [5, FRONT_Y], "mm": 80.0}})
    half = 24
    lanes_in = [{"x0": x - half, "x1": x + half, "label": f"L{i+1}"}
                for i, x in enumerate(LANE_X)]
    lanes = c.post(f"/api/analyses/{aid}/lanes",
                   json={"lanes": lanes_in}).get_json()["lanes"]
    for lane in lanes:
        wins = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/autodetect",
                      json={}).get_json()["windows"]
        peaks = [{"y0": w["y0"], "y1": w["y1"], "origin": "auto"} for w in wins]
        r = c.post(f"/api/analyses/{aid}/lanes/{lane['id']}/peaks",
                   json={"peaks": peaks}).get_json()["peaks"]
        lane["peaks"] = r
    return aid, lanes


def upload_frame(c, sid, t, img, taken_at):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return c.post(f"/api/kinetics/{sid}/frames",
                  data={"file": (buf, f"f{t}.png"), "taken_at": taken_at},
                  content_type="multipart/form-data").get_json()


def e2e():
    tmp = tempfile.mkdtemp(prefix="tlc_kin_")
    app = create_app(data_dir=tmp)
    c = app.test_client()

    # 1. 参考板
    aid, lanes = setup_base_plate(c)
    n_peaks = sum(len(l["peaks"]) for l in lanes)
    print(f"1. 参考板 #{aid}:{len(lanes)} 泳道,共 {n_peaks} 个峰")
    assert n_peaks >= len(lanes)

    # 2. 建序列 + 6 帧(平移 6px 验证配准)
    sid = c.post(f"/api/analyses/{aid}/kinetics", json={
        "name": "批K显色序列", "start_at": "10:00:00"}).get_json()["series"]["id"]
    times = ["10:00:10", "10:00:30", "10:00:40", "10:00:50", "10:01:00", "10:01:10"]
    SHIFT = (12, 9)
    st = None
    for i, ta in enumerate(times):
        f = amp_profile(i)
        img = make_frame(lambda li, rf, f=f: (20.0 + 5 * li) * f *
                         (1.0 + 0.15 * rf), shift=SHIFT)
        st = upload_frame(c, sid, i, img, ta)
    assert len(st["frames_out"]) == 6
    print(f"2. 序列 #{sid} 建立,上传 {len(st['frames_out'])} 帧(板面平移 {SHIFT})")

    # 默认四角假设板面铺满帧,平移未补偿 => 质心漂移;再把四角移到正确位置 => 漂移清除
    def set_corners(fr, ox, oy):
        cps = frame_corners((ox, oy))
        # 保留检查点
        cps += [cp for cp in fr["control_points"] if cp.get("kind") == "check"]
        return c.post(f"/api/kinetics/frames/{fr['id']}",
                      json={"control_points": cps}).get_json()

    st = c.get(f"/api/kinetics/{sid}").get_json()
    drift_issues = [i for i in st["issues"] if i["kind"] == "spot_drift"]
    assert drift_issues, "未配准的平移帧应报漂移"
    for fr in st["frames_out"]:
        st = set_corners(fr, *SHIFT)
    assert not [i for i in st["issues"] if i["kind"] in ("reg_residual", "spot_drift")], \
        [i["message"] for i in st["issues"]]
    print("3. 四角配准:平移帧未配准报漂移,校正后残差/漂移清除 ✔")

    # 4. 曲线与平台建议
    curves = st["curves"]
    assert curves, "应有逐斑点曲线"
    one = next(iter(curves.values()))
    areas = one["areas"]
    # 平台(30~50s)面积应高于首帧与末帧
    assert max(areas[1:4]) > areas[0] * 2 and max(areas[1:4]) > areas[-1] * 1.5, areas
    assert one["plateau"] is not None, one
    sug = st["suggest"]
    assert sug and abs(sug["t0"] - 30) < 1 and abs(sug["t1"] - 50) < 1, sug
    print(f"4. 逐斑点动力学曲线:峰 t={one['peak']['t']:.0f}s,平台 "
          f"[{k.format_t(one['plateau_range'][0])},{k.format_t(one['plateau_range'][1])}],"
          f"建议窗口 {sug} ✔")

    # 5. 时刻重复 / 倒序阻断
    bad = c.post(f"/api/kinetics/frames/{st['frames_out'][0]['id']}",
                 json={"taken_at": "10:00:30"}).get_json()
    assert any(i["kind"] == "duplicate_time" for i in bad["issues"])
    r = c.post(f"/api/kinetics/{sid}/confirm",
               json={"t0": sug["t0"], "t1": sug["t1"]})
    assert r.status_code == 400 and "重复" in r.get_json()["description"]
    c.post(f"/api/kinetics/frames/{st['frames_out'][0]['id']}",
           json={"taken_at": times[0]})
    print("5. 时刻重复定位到帧并阻断确认 ✔")

    # 6. 排除坏帧必须填理由;有效帧不足阻断
    fid = st["frames_out"][0]["id"]
    r = c.post(f"/api/kinetics/frames/{fid}",
               json={"excluded": True, "exclude_reason": ""})
    assert r.status_code == 400 and "理由" in r.get_json()["description"]
    st2 = c.post(f"/api/kinetics/frames/{fid}",
                 json={"excluded": True,
                       "exclude_reason": "喷板瞬间晃动,画面模糊"}).get_json()
    assert st2["frames_out"][0]["excluded"]
    # 再排除到有效帧 < 3
    for fr in st2["frames_out"][1:4]:
        st2 = c.post(f"/api/kinetics/frames/{fr['id']}",
                     json={"excluded": True, "exclude_reason": "坏帧测试排除"}).get_json()
    r = c.post(f"/api/kinetics/{sid}/confirm",
               json={"t0": sug["t0"], "t1": sug["t1"]})
    assert r.status_code == 400 and "有效帧" in r.get_json()["description"]
    print("6. 排除需理由;有效帧不足阻断确认 ✔")
    # 恢复
    for fr in st2["frames_out"][:4]:
        c.post(f"/api/kinetics/frames/{fr['id']}",
               json={"excluded": False, "exclude_reason": ""})

    # 7. 窗口跨衰减区阻断
    st = c.get(f"/api/kinetics/{sid}").get_json()
    r = c.post(f"/api/kinetics/{sid}/confirm", json={"t0": 30, "t1": 70})
    assert r.status_code == 400 and "平台" in r.get_json()["description"]
    print("7. 取值窗口跨过衰减帧阻断 ✔")

    # 8. 正常确认成版
    r = c.post(f"/api/kinetics/{sid}/confirm",
               json={"t0": sug["t0"], "t1": sug["t1"]})
    assert r.status_code == 200, r.get_json() if r.status_code != 200 else ""
    vid, ver = r.get_json()["version_id"], r.get_json()["version"]
    st = c.get(f"/api/kinetics/{sid}").get_json()
    assert st["versions"][-1]["is_current"] == 1
    assert not st["versions"][-1]["stale"]
    # 逐斑点窗口统计
    wm = next(iter(st["curves"].values()))["window_stat"]
    assert wm and wm["n"] >= 2 and wm["cv_pct"] < 15, wm
    print(f"8. 确认取值窗口 v{ver}:窗口帧数 {wm['n']},CV={wm['cv_pct']:.2f}% ✔")

    # 9. 导出:逐帧 CSV / 复算 JSON / 动力学图
    csv_r = c.get(f"/api/kinetics/versions/{vid}/frames.csv")
    js_r = c.get(f"/api/kinetics/versions/{vid}/recompute.json")
    fig = c.get(f"/api/kinetics/versions/{vid}/figure.png")
    assert csv_r.status_code == 200 and b"window_mean" in csv_r.data
    payload = json.loads(js_r.data.decode())
    assert payload["snapshot"]["window"]["t0"] == sug["t0"]
    assert len(payload["snapshot"]["frames"]) == 6
    assert fig.status_code == 200 and fig.data[:4] == b"\x89PNG"
    print("9. 逐帧 CSV / 复算 JSON(帧/配准/窗口)/ 动力学图导出 ✔")

    # 10. 下游校准只引用有效版本
    cal_id = c.post(f"/api/analyses/{aid}/calibrations", json={
        "name": "K校准", "target_name": "目标", "target_rf": SPOT_RF[0]}).get_json()[
        "calibration"]["id"]
    # 标注前两条泳道为标准(同 Rf 斑点),浓度 1、2
    lanes_st = c.get(f"/api/analyses/{aid}").get_json()["lanes"]
    anno = []
    concs = [1.0, 2.0, None, None]
    for lane, conc in zip(lanes_st, concs):
        spot = next((p for p in lane["peaks"]), None)
        # 绑定 Rf 接近目标的峰
        spot = min(lane["peaks"], key=lambda p: abs(
            ((p["y0"] + p["y1"]) / 2 - spot_y(SPOT_RF[0]))))
        row = {"lane_id": lane["id"], "peak_id": spot["id"]}
        if conc:
            row.update({"role": "standard", "concentration": conc, "volume": 10.0})
        anno.append(row)
    ev = c.post(f"/api/calibrations/{cal_id}/evaluate",
                json={"model": "linear", "lanes": anno}).get_json()["evaluation"]
    kin_msgs = [i for i in ev["issues"] if i["type"] == "kinetic_window"]
    assert kin_msgs and "锁定窗口" in kin_msgs[0]["message"]
    # 标准点响应应为窗口均值而非单帧
    resp = sorted(p["response"] for p in ev["points"])
    assert resp[1] > resp[0]
    print(f"10. 校准引用有效时间序列窗口:响应={['%.0f' % v for v in resp]} ✔")

    # 11. 来源变化 -> 旧版过期;校准回退单帧并警告
    # 改积分边界 => 板指纹变化 => 时间序列版本 stale
    lanes_state = c.get(f"/api/analyses/{aid}").get_json()["lanes"]
    target_peak = lanes_state[0]["peaks"][0]
    new_peaks = [{"id": target_peak["id"], "y0": target_peak["y0"] - 4,
                  "y1": target_peak["y1"] + 4, "origin": "manual"}]
    new_peaks += [{"id": p["id"], "y0": p["y0"], "y1": p["y1"]}
                  for p in lanes_state[0]["peaks"][1:]]
    c.post(f"/api/analyses/{aid}/lanes/{lanes_state[0]['id']}/peaks",
           json={"peaks": new_peaks})
    st = c.get(f"/api/kinetics/{sid}").get_json()
    assert all(v["stale"] for v in st["versions"]), "积分边界变化后旧版应过期"
    vr = c.get(f"/api/kinetics/versions/{vid}").get_json()
    assert vr["stale"] and vr["snapshot"]["frames"]
    fig_old = c.get(f"/api/kinetics/versions/{vid}/figure.png")
    assert b"EXPIRED" in fig_old.headers.get("Content-Disposition", "").encode()
    ev2 = c.post(f"/api/calibrations/{cal_id}/evaluate",
                 json={"model": "linear", "lanes": anno}).get_json()["evaluation"]
    expired = [i for i in ev2["issues"] if i["type"] == "kinetic_expired"]
    assert expired and "过期" in expired[0]["message"]
    print("11. 积分边界变化 -> 时间序列旧版过期(图带 EXPIRED),"
          "校准回退单帧并警告 ✔")

    # 12. 配准残差超限阻断:把某帧四角严重错位
    st = c.get(f"/api/kinetics/{sid}").get_json()
    fr = st["frames_out"][1]
    cps = [{"fx": cp["fx"] + 40, "fy": cp["fy"], "rx": cp["rx"], "ry": cp["ry"],
            "kind": "corner"} for cp in fr["control_points"] if cp["kind"] == "corner"]
    # 四角错位可能仍能配准但帧指标整体错位;用检查点验证残差更直接
    # 加一对检查点,故意给错 ref => 残差超限
    cps = [{"fx": cp["fx"], "fy": cp["fy"], "rx": cp["rx"], "ry": cp["ry"],
            "kind": cp.get("kind", "corner")} for cp in fr["control_points"]]
    cps.append({"fx": 200, "fy": 200, "rx": 260, "ry": 200, "kind": "check"})
    st2 = c.post(f"/api/kinetics/frames/{fr['id']}",
                 json={"control_points": cps}).get_json()
    e = next(x for x in st2["frames_out"] if x["id"] == fr["id"])["error"]
    assert e and e["kind"] == "reg_residual", e
    # rectified.png 返回 409
    rr = c.get(f"/api/kinetics/frames/{fr['id']}/rectified.png")
    assert rr.status_code == 409 and rr.get_json()["kind"] == "reg_residual"
    print("12. 检查点残差超限:帧定位为不可用、校正图 409 ✔")

    # 13. 饱和帧阻断窗口确认(恢复错点后构造饱和)
    c.post(f"/api/kinetics/frames/{fr['id']}",
           json={"control_points": [cp for cp in cps if cp["kind"] == "corner"]})
    sat_img = make_frame(lambda li, rf: 999, saturated=True, shift=SHIFT)
    buf = io.BytesIO(); sat_img.save(buf, "PNG"); buf.seek(0)
    st_sat = c.post(f"/api/kinetics/{sid}/frames",
                    data={"file": (buf, "sat.png"), "taken_at": "10:01:15"},
                    content_type="multipart/form-data").get_json()
    sat_id = st_sat["frames_out"][-1]["id"]
    st_sat = c.post(f"/api/kinetics/frames/{sat_id}",
                    json={"control_points": frame_corners(SHIFT)}).get_json()
    assert any(i["kind"] == "saturated" for i in st_sat["issues"])
    # 饱和帧在 75s,超出 [30,50] 窗口本不影响;把窗口扩到 75s 以触发窗口内饱和阻断
    r = c.post(f"/api/kinetics/{sid}/confirm", json={"t0": 30, "t1": 75})
    assert r.status_code == 400 and "饱和" in r.get_json()["description"]
    # 排除饱和帧(带理由)后,原平台窗口可重新确认(得到新版本)
    c.post(f"/api/kinetics/frames/{sat_id}",
           json={"excluded": True, "exclude_reason": "像素饱和,面积截断"})
    st = c.get(f"/api/kinetics/{sid}").get_json()
    r = c.post(f"/api/kinetics/{sid}/confirm", json={"t0": 30, "t1": 50})
    assert r.status_code == 200, r.get_json().get("description", "")
    print("13. 窗口内像素饱和阻断;排除坏帧(留理由)后可重新确认 ✔")

    # 14. 删除序列
    r = c.post(f"/api/kinetics/{sid}/delete", json={}).get_json()
    assert r["ok"] and c.get(f"/api/kinetics/{sid}").status_code == 404
    print("14. 删除时间序列 ✔")

    print("\n显色时间序列端到端测试通过 ✔")


def main():
    test_registration()
    test_time_parse_and_order()
    test_plateau_and_window()
    test_window_validation()
    e2e()
    print("\n全部显色时间序列测试通过 ✔")


if __name__ == "__main__":
    main()
