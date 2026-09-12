"""TLC 薄层色谱图像定量 — Flask 后端。

工作流:上传板面照片 -> 点选四角/基线/前沿/标尺 -> 透视矫正与背景扣除预览
-> 增删泳道、拖动积分边界、拆分共洗脱峰 -> 逐斑点 Rf/面积/相对含量
-> 异常标记与处置留痕 -> 导出标注图/CSV/参数文件/打印记录。
"""

import csv
import hashlib
import io
import json
import os
import shutil

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image

import db
from tlc import TOOL, __version__, annotate, background, calibration, compare, geometry, kinetics, pipeline, profile, qc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_IMAGE = os.path.join(BASE_DIR, "sample", "sample_plate.png")


def create_app(data_dir=None):
    data_dir = data_dir or os.environ.get("TLC_DATA_DIR", os.path.join(BASE_DIR, "data"))
    uploads = os.path.join(data_dir, "uploads")
    exports = os.path.join(data_dir, "exports")
    os.makedirs(uploads, exist_ok=True)
    os.makedirs(exports, exist_ok=True)
    db_path = os.path.join(data_dir, "tlc.db")
    db.init_db(db_path)

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False

    @app.errorhandler(400)
    @app.errorhandler(404)
    def json_error(e):
        # /api 路径统一返回 JSON 错误描述,供前端 alert;页面路由交给默认处理器
        if request.path.startswith("/api/"):
            return jsonify({"error": e.code, "description": e.description}), e.code
        raise e

    def con():
        return db.connect(db_path)

    # ---------- 辅助 ----------

    def get_analysis_or_404(c, aid):
        a = db.get_analysis(c, aid)
        if not a:
            abort(404, "分析不存在")
        return a

    def build_bundle(c, aid):
        """由数据库当前状态组装可复算参数包。"""
        a = get_analysis_or_404(c, aid)
        g = db.current_geometry(c, aid)
        if not g:
            return None, None
        lanes = db.list_lanes(c, aid, g["version"])
        bundle = {
            "tool": TOOL,
            "tool_version": __version__,
            "format_version": 1,
            "analysis_id": aid,
            "analysis_name": a["name"],
            "image_path": a["image_path"],
            "image_sha1": a["image_sha1"],
            "geometry_version": g["version"],
            "geometry": {"params": g["params"], "derived": g["derived"]},
            "background": dict(background.DEFAULTS),
            "detection": dict(pipeline.DETECTION_DEFAULTS),
            "qc": dict(qc.THRESHOLDS),
            "lanes": [
                {"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"],
                 "peaks": [{"id": p["id"], "y0": p["y0"], "y1": p["y1"],
                            "origin": p["origin"], "note": p["note"]} for p in l["peaks"]]}
                for l in lanes
            ],
        }
        return a, bundle

    def compute_with_decisions(c, aid):
        a, bundle = build_bundle(c, aid)
        if not bundle:
            return None, None, None
        result = pipeline.compute_bundle(a["image_path"], bundle)
        decisions = db.get_decisions(c, aid, bundle["geometry_version"])
        for f in result["flags"]:
            dec = decisions.get(f["key"])
            f["kept"] = bool(dec and dec["kept"])
            f["reason"] = dec["reason"] if dec else ""
        return a, bundle, result

    def state(c, aid):
        a = get_analysis_or_404(c, aid)
        g = db.current_geometry(c, aid)
        out = {"analysis": a, "geometry": None, "lanes": [], "versions": []}
        if g:
            out["geometry"] = {"version": g["version"], "params": g["params"],
                               "derived": g["derived"], "previous_params": g["previous_params"],
                               "previous_derived": g["previous_derived"]}
            out["lanes"] = db.list_lanes(c, aid, g["version"])
        out["versions"] = [
            {"version": v["version"], "is_current": v["is_current"],
             "created_at": v["created_at"], "params": v["params"], "derived": v["derived"],
             "previous_params": v["previous_params"]}
            for v in db.list_versions(c, aid)]
        return out

    # ---------- 页面 ----------

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/calibration")
    def calibration_page():
        return render_template("calibration.html")

    @app.get("/kinetics")
    def kinetics_page():
        return render_template("kinetics.html")

    # ---------- 分析管理 ----------

    @app.get("/api/analyses")
    def list_analyses():
        with con() as c:
            rows = c.execute("SELECT * FROM analyses ORDER BY id DESC").fetchall()
            return jsonify([db.row_to_dict(r) for r in rows])

    def create_analysis(c, image_path, name):
        with Image.open(image_path) as im:
            w, h = im.size
        with open(image_path, "rb") as f:
            sha1 = hashlib.sha1(f.read()).hexdigest()
        cur = c.execute(
            "INSERT INTO analyses (name, image_path, image_width, image_height, image_sha1, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, image_path, w, h, sha1, db.now()))
        return cur.lastrowid

    @app.post("/api/analyses")
    def upload():
        f = request.files.get("file")
        if not f:
            abort(400, "缺少文件")
        name = request.form.get("name") or os.path.splitext(f.filename or "plate")[0]
        with con() as c:
            # 先取 id 再落盘,避免文件名冲突
            cur = c.execute(
                "INSERT INTO analyses (name, image_path, image_width, image_height, image_sha1, created_at)"
                " VALUES ('', '', 0, 0, '', ?)", (db.now(),))
            aid = cur.lastrowid
            ext = os.path.splitext(f.filename or "plate.png")[1] or ".png"
            path = os.path.join(uploads, f"a{aid}{ext}")
            f.save(path)
            with Image.open(path) as im:
                w, h = im.size
                im.convert("RGB")  # 校验可解码
            with open(path, "rb") as fh:
                sha1 = hashlib.sha1(fh.read()).hexdigest()
            c.execute(
                "UPDATE analyses SET name=?, image_path=?, image_width=?, image_height=?, image_sha1=?"
                " WHERE id=?", (name, path, w, h, sha1, aid))
            return jsonify(state(c, aid))

    @app.post("/api/analyses/sample")
    def load_sample():
        if not os.path.exists(SAMPLE_IMAGE):
            abort(404, "样例图缺失,请先运行 python sample/make_sample.py")
        with con() as c:
            cur = c.execute(
                "INSERT INTO analyses (name, image_path, image_width, image_height, image_sha1, created_at)"
                " VALUES ('', '', 0, 0, '', ?)", (db.now(),))
            aid = cur.lastrowid
            path = os.path.join(uploads, f"a{aid}_sample.png")
            shutil.copyfile(SAMPLE_IMAGE, path)
            with Image.open(path) as im:
                w, h = im.size
            with open(path, "rb") as fh:
                sha1 = hashlib.sha1(fh.read()).hexdigest()
            c.execute(
                "UPDATE analyses SET name=?, image_path=?, image_width=?, image_height=?, image_sha1=?"
                " WHERE id=?", ("样例板(拖尾/共洗脱/照明不均)", path, w, h, sha1, aid))
            return jsonify(state(c, aid))

    @app.get("/api/analyses/<int:aid>")
    def get_state(aid):
        with con() as c:
            return jsonify(state(c, aid))

    @app.get("/api/analyses/<int:aid>/image")
    def original_image(aid):
        with con() as c:
            a = get_analysis_or_404(c, aid)
            return send_file(a["image_path"])

    # ---------- 几何 ----------

    @app.post("/api/analyses/<int:aid>/geometry")
    def set_geometry(aid):
        payload = request.get_json(force=True)
        try:
            params = {
                "corners": [[float(x), float(y)] for x, y in payload["corners"]],
                "baseline": [float(payload["baseline"][0]), float(payload["baseline"][1])],
                "front": [float(payload["front"][0]), float(payload["front"][1])],
                "scale": {
                    "p1": [float(payload["scale"]["p1"][0]), float(payload["scale"]["p1"][1])],
                    "p2": [float(payload["scale"]["p2"][0]), float(payload["scale"]["p2"][1])],
                    "mm": float(payload["scale"]["mm"]),
                },
            }
            if len(params["corners"]) != 4:
                raise ValueError
            derived = geometry.derive(params)
        except (KeyError, TypeError, ValueError) as e:
            abort(400, f"几何参数无效: {e}")
        with con() as c:
            get_analysis_or_404(c, aid)
            version, _ = db.set_geometry(c, aid, params, derived)
            return jsonify(state(c, aid))

    # ---------- 预览 ----------

    @app.get("/api/analyses/<int:aid>/preview/<kind>")
    def preview(aid, kind):
        with con() as c:
            a, bundle = build_bundle(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            corr, bg, sig = pipeline.stage_images(a["image_path"], bundle)
            img = {"corrected": corr, "background": bg, "signal": sig}.get(kind)
            if img is None:
                abort(404, "未知预览类型")
            buf = io.BytesIO()
            img.save(buf, "PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png")

    # ---------- 泳道与峰 ----------

    @app.post("/api/analyses/<int:aid>/lanes")
    def set_lanes(aid):
        payload = request.get_json(force=True)
        with con() as c:
            g = db.current_geometry(c, aid)
            if not g:
                abort(400, "尚未设置几何")
            lanes = db.replace_lanes(c, aid, g["version"], payload.get("lanes", []))
            return jsonify({"lanes": lanes})

    @app.post("/api/analyses/<int:aid>/lanes/<int:lid>/peaks")
    def set_peaks(aid, lid):
        payload = request.get_json(force=True)
        with con() as c:
            g = db.current_geometry(c, aid)
            if not g:
                abort(400, "尚未设置几何")
            lane = c.execute("SELECT * FROM lanes WHERE id=? AND analysis_id=? AND geometry_version=?",
                             (lid, aid, g["version"])).fetchone()
            if not lane:
                abort(404, "泳道不存在")
            peaks = db.replace_peaks(c, lid, payload.get("peaks", []))
            return jsonify({"peaks": peaks})

    @app.post("/api/analyses/<int:aid>/lanes/<int:lid>/autodetect")
    def autodetect(aid, lid):
        with con() as c:
            a, bundle = build_bundle(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            lane = next((l for l in bundle["lanes"] if l["id"] == lid), None)
            if not lane:
                abort(404, "泳道不存在")
            _, _, sig = pipeline.stage_images(a["image_path"], bundle)
            prof = profile.lane_profile(sig, lane["x0"], lane["x1"])
            det = dict(pipeline.DETECTION_DEFAULTS)
            det.update(request.get_json(silent=True) or {})
            d = bundle["geometry"]["derived"]
            # 峰搜索限定在 前沿~基线 之间,并向内收缩少许,避免铅笔线等被误检
            margin = 3.0
            det["y_min"] = min(d["front_y"], d["baseline_y"]) + margin
            det["y_max"] = max(d["front_y"], d["baseline_y"]) - margin
            windows = profile.detect_peaks(prof, **det)
            return jsonify({"windows": [{"y0": y0, "y1": y1} for y0, y1 in windows]})

    @app.get("/api/analyses/<int:aid>/lanes/<int:lid>/profile")
    def lane_profile(aid, lid):
        with con() as c:
            a, bundle = build_bundle(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            lane = next((l for l in bundle["lanes"] if l["id"] == lid), None)
            if not lane:
                abort(404, "泳道不存在")
            _, _, sig = pipeline.stage_images(a["image_path"], bundle)
            prof = profile.lane_profile(sig, lane["x0"], lane["x1"])
            d = bundle["geometry"]["derived"]
            return jsonify({"profile": prof, "height": d["height"],
                            "baseline_y": d["baseline_y"], "front_y": d["front_y"],
                            "noise": profile.noise_level(prof)})

    # ---------- 结果与异常 ----------

    @app.get("/api/analyses/<int:aid>/results")
    def results(aid):
        with con() as c:
            a, bundle, result = compute_with_decisions(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            lane_label = {l["id"]: l["label"] for l in bundle["lanes"]}
            for s in result["spots"]:
                s["lane_label"] = lane_label.get(s["lane_id"], "")
            result["geometry_version"] = bundle["geometry_version"]
            return jsonify(result)

    @app.post("/api/analyses/<int:aid>/flags/<path:key>/decision")
    def flag_decision(aid, key):
        payload = request.get_json(force=True)
        kept = bool(payload.get("kept"))
        reason = (payload.get("reason") or "").strip()
        if kept and not reason:
            abort(400, "保留异常结果必须注明处理理由")
        with con() as c:
            g = db.current_geometry(c, aid)
            if not g:
                abort(400, "尚未设置几何")
            db.set_decision(c, aid, g["version"], key, kept, reason)
            return jsonify({"ok": True})

    # ---------- 导出 ----------

    @app.get("/api/analyses/<int:aid>/export/annotated.png")
    def export_annotated(aid):
        with con() as c:
            a, bundle, result = compute_with_decisions(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            corr, _, _ = pipeline.stage_images(a["image_path"], bundle)
            img = annotate.draw_annotated(corr, bundle["geometry"]["derived"],
                                          bundle["lanes"], result["spots"], result["flags"])
            path = os.path.join(exports, f"a{aid}_v{bundle['geometry_version']}_annotated.png")
            img.save(path)
            return send_file(path, mimetype="image/png",
                             as_attachment=request.args.get("dl") == "1",
                             download_name=os.path.basename(path))

    @app.get("/api/analyses/<int:aid>/export/spots.csv")
    def export_csv(aid):
        with con() as c:
            a, bundle, result = compute_with_decisions(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            lane_label = {l["id"]: l["label"] for l in bundle["lanes"]}
            flag_by_peak = {}
            for f in result["flags"]:
                if f.get("peak_id"):
                    flag_by_peak.setdefault(f["peak_id"], []).append(f)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["analysis_id", "analysis_name", "geometry_version",
                        "lane_id", "lane_label", "spot_no", "rf", "center_y_px",
                        "center_mm_from_baseline", "area", "height",
                        "pct_lane", "pct_plate", "y0", "y1", "saturated_px",
                        "flags", "kept_reasons"])
            for s in sorted(result["spots"], key=lambda s: (s["lane_id"], s.get("spot_no", 0))):
                fs = flag_by_peak.get(s["peak_id"], [])
                w.writerow([
                    aid, a["name"], bundle["geometry_version"],
                    s["lane_id"], lane_label.get(s["lane_id"], ""), s.get("spot_no", ""),
                    f"{s['rf']:.4f}", f"{s['center_y']:.2f}",
                    "" if s["center_mm"] is None else f"{s['center_mm']:.2f}",
                    f"{s['area']:.1f}", f"{s['height']:.1f}",
                    f"{s['pct_lane']:.2f}", f"{s['pct_plate']:.2f}",
                    f"{s['y0']:.1f}", f"{s['y1']:.1f}", s["saturated_px"],
                    ";".join(f["type"] for f in fs),
                    ";".join(f["reason"] for f in fs if f.get("kept") and f.get("reason")),
                ])
            mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
            mem.seek(0)
            return send_file(mem, mimetype="text/csv", as_attachment=True,
                             download_name=f"a{aid}_v{bundle['geometry_version']}_spots.csv")

    @app.get("/api/analyses/<int:aid>/export/params.json")
    def export_params(aid):
        with con() as c:
            a, bundle = build_bundle(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            mem = io.BytesIO(json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"))
            mem.seek(0)
            return send_file(mem, mimetype="application/json", as_attachment=True,
                             download_name=f"a{aid}_v{bundle['geometry_version']}_params.json")

    @app.get("/api/analyses/<int:aid>/print")
    def print_record(aid):
        with con() as c:
            a, bundle, result = compute_with_decisions(c, aid)
            if not bundle:
                abort(400, "尚未设置几何")
            lane_label = {l["id"]: l["label"] for l in bundle["lanes"]}
            versions = db.list_versions(c, aid)
            return render_template(
                "print_record.html", analysis=a, bundle=bundle, result=result,
                lane_label=lane_label, versions=versions, now=db.now())

    # ---------- 跨板对照 ----------

    # (analysis_id, 指纹) -> 计算缓存;指纹随几何/泳道/峰边界改变,缓存自动失效
    cmp_cache = {}

    def member_fingerprint(bundle):
        payload = {
            "v": bundle["geometry_version"],
            "lanes": [{"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"],
                       "peaks": [{"id": p["id"], "y0": p["y0"], "y1": p["y1"]}
                                 for p in l["peaks"]]}
                      for l in bundle["lanes"]],
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def member_compute(c, aid):
        """单板定量数据(结果/斑点/各泳道密度曲线/指纹),按指纹缓存。"""
        a, bundle = bundle_for(c, aid)
        if not bundle:
            return None
        fp = member_fingerprint(bundle)
        key = (aid, fp)
        if key in cmp_cache:
            return cmp_cache[key]
        result = pipeline.compute_bundle(a["image_path"], bundle)
        lane_label = {l["id"]: l["label"] for l in bundle["lanes"]}
        spots = []
        for s in result["spots"]:
            s2 = dict(s)
            s2["lane_label"] = lane_label.get(s["lane_id"], "")
            spots.append(s2)
        _, _, sig = pipeline.stage_images(a["image_path"], bundle)
        profiles = {l["id"]: profile.lane_profile(sig, l["x0"], l["x1"])
                    for l in bundle["lanes"]}
        data = {"analysis": a, "bundle": bundle, "spots": spots,
                "profiles": profiles, "fingerprint": fp}
        cmp_cache[key] = data
        return data

    def bundle_for(c, aid):
        """build_bundle 的不抛 404 版本:分析不存在或未标定几何返回 (a|None, None)。"""
        a = db.get_analysis(c, aid)
        if not a:
            return None, None
        g = db.current_geometry(c, aid)
        if not g:
            return a, None
        lanes = db.list_lanes(c, aid, g["version"])
        return a, {
            "analysis_id": aid,
            "image_path": a["image_path"],
            "geometry_version": g["version"],
            "geometry": {"params": g["params"], "derived": g["derived"]},
            "background": dict(background.DEFAULTS),
            "detection": dict(pipeline.DETECTION_DEFAULTS),
            "qc": dict(qc.THRESHOLDS),
            "lanes": [
                {"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"],
                 "peaks": [{"id": p["id"], "y0": p["y0"], "y1": p["y1"],
                            "origin": p["origin"], "note": p["note"]} for p in l["peaks"]]}
                for l in lanes
            ],
        }

    def auto_match_member(c, cid, mid):
        """重新匹配成员:重算 auto 匹配,并把所有关系的指纹刷新为当前数据。

        manual/locked 关系不被改写;若其引用的峰在当前数据中仍存在,
        视为关系仍然成立,一并刷新指纹(重新确认);已消失的峰保持旧指纹,
        在状态中显示为失效。成员级指纹同步刷新,成员脱离 stale。
        """
        m = db.get_member(c, mid)
        if not m:
            return
        d = member_compute(c, m["analysis_id"])
        if not d:
            return
        cfg = dict(compare.DEFAULTS)
        targets = db.list_targets(c, cid)
        std_spots = [s for s in d["spots"] if s["lane_id"] == m["std_lane_id"]]
        assignment, offset, _ = compare.assign_standards(targets, std_spots, cfg)
        best, _ = compare.propose_targets(targets, d["spots"], m["std_lane_id"],
                                          offset or 0.0, assignment, d["profiles"], cfg)
        fp = d["fingerprint"]
        existing = {mt["target_id"]: mt for mt in db.list_matches(c, cid)
                    if mt["member_id"] == mid}
        live_peaks = {s["peak_id"] for s in d["spots"]}
        for t in targets:
            mt = existing.get(t["id"])
            if mt and (mt["locked"] or mt["status"] == "manual"):
                if mt["peak_id"] in live_peaks and mt["data_fp"] != fp:
                    # 手动/锁定关系引用的峰仍在:重新确认其数据版本
                    db.upsert_match(c, cid, t["id"], mid, mt["peak_id"], mt["lane_id"],
                                    mt["status"], mt["locked"], data_fp=fp)
                continue
            cand = best.get(t["id"])
            db.upsert_match(c, cid, t["id"], mid,
                            cand["peak_id"] if cand else None,
                            cand["lane_id"] if cand else None, "auto", 0, data_fp=fp)
        db.update_member(c, mid, fingerprint=fp)

    def comparison_state(c, cid):
        """组装对照组完整状态:成员质控、候选、匹配、汇总(被排除者附原因)。"""
        comp = db.get_comparison(c, cid)
        if not comp:
            abort(404, "对照组不存在")
        cfg = dict(compare.DEFAULTS)
        targets = db.list_targets(c, cid)
        members = db.list_members(c, cid)
        matches = db.list_matches(c, cid)
        match_by = {(m["target_id"], m["member_id"]): m for m in matches}

        infos = []
        responses = {}
        for m in members:
            info = {"member": m, "problems": [], "data": None, "stale": False,
                    "assignment": {}, "offset": None, "coef": None, "response": None,
                    "candidates": {}}
            d = member_compute(c, m["analysis_id"])
            if not d:
                info["problems"].append({"type": "no_geometry",
                                         "message": "该板尚未完成几何标定或已被删除"})
                infos.append(info)
                continue
            info["data"] = d
            # 板数据变更不再整板阻断:成员级 stale 仅作提示,
            # 具体哪条匹配失效按各匹配自带的 data_fp 逐条判定(见下)。
            if d["fingerprint"] != m["fingerprint"]:
                info["stale"] = True
            lane_ids = {l["id"] for l in d["bundle"]["lanes"]}
            if m["std_lane_id"] not in lane_ids:
                info["problems"].append({"type": "std_missing",
                                         "message": "指定的标准品泳道在当前几何版本中不存在"})
                infos.append(info)
                continue
            if targets:
                std_spots = [s for s in d["spots"] if s["lane_id"] == m["std_lane_id"]]
                assignment, offset, probs = compare.assign_standards(targets, std_spots, cfg)
                info["assignment"] = assignment
                info["offset"] = offset
                info["problems"].extend(probs)
                if assignment and not any(p["type"] == "std_missing" for p in probs):
                    resp = sum(assignment[t["id"]]["area"] for t in targets
                               if t["id"] in assignment)
                    info["response"] = resp
                    responses[m["id"]] = resp
                # 候选(始终按当前数据计算,供改绑选择)
                _, cands = compare.propose_targets(
                    targets, d["spots"], m["std_lane_id"], offset or 0.0,
                    assignment, d["profiles"], cfg)
                info["candidates"] = cands
            infos.append(info)

        # 归一化系数:仅标准品质控通过的板参与参考
        coefs, outliers = compare.normalization(responses, cfg["coef_outlier_ratio"])
        for info in infos:
            mid = info["member"]["id"]
            info["coef"] = coefs.get(mid)
            if mid in outliers:
                r = cfg["coef_outlier_ratio"]
                info["problems"].append({
                    "type": "coef_outlier",
                    "message": f"归一化系数 {coefs[mid]:.2f} 离群"
                               f"(超出 [{1 / r:.2f}, {r:.2f}])"})

        # 逐条匹配判定有效性:峰已绑定 + 关系确认时的数据版本与当前一致 + 斑点仍存在。
        # 板数据变更后,只有被重新匹配/改绑确认过的关系才有效,
        # 其余保持失效并排除在汇总外(旧 auto 关系不会随成员状态自动复活)。
        info_by_mid = {i["member"]["id"]: i for i in infos}
        match_state = {}   # (target_id, member_id) -> {"valid": bool, "stale": bool}
        for mt in matches:
            info = info_by_mid.get(mt["member_id"])
            d = info["data"] if info else None
            if mt["peak_id"] is None:
                match_state[(mt["target_id"], mt["member_id"])] = \
                    {"valid": False, "stale": False}
                continue
            cur_fp = d["fingerprint"] if d else None
            spot = (next((s for s in d["spots"] if s["peak_id"] == mt["peak_id"]), None)
                    if d else None)
            synced = cur_fp is not None and mt["data_fp"] == cur_fp
            match_state[(mt["target_id"], mt["member_id"])] = {
                "valid": bool(synced and spot), "stale": not synced}

        # 次序颠倒检查(只对当前有效的确认关系)
        for info in infos:
            d = info["data"]
            if not d:
                continue
            mid = info["member"]["id"]
            mmatches = [mt for mt in matches
                        if mt["member_id"] == mid and mt["peak_id"]
                        and match_state[(mt["target_id"], mid)]["valid"]]
            spots_by_peak = {s["peak_id"]: s for s in d["spots"]}
            bad = compare.order_violations(targets, mmatches, spots_by_peak)
            if bad:
                tname = {t["id"]: t["name"] for t in targets}
                pairs = "、".join(f"{tname.get(a, a)}↔{tname.get(b, b)}" for a, b in bad)
                info["problems"].append({
                    "type": "order_reversed",
                    "message": f"匹配次序与标准品次序颠倒:{pairs},请改绑或拆开"})

        # 输出成员
        out_members = []
        for info in infos:
            m = info["member"]
            d = info["data"]
            a = d["analysis"] if d else db.get_analysis(c, m["analysis_id"])
            lanes = d["bundle"]["lanes"] if d else []
            std_lane = next((l for l in lanes if l["id"] == m["std_lane_id"]), None)
            tname = {t["id"]: t["name"] for t in targets}
            n_stale = sum(1 for (tid, mid), ms in match_state.items()
                          if mid == m["id"] and ms["stale"])
            out_members.append({
                "id": m["id"], "analysis_id": m["analysis_id"],
                "analysis_name": a["name"] if a else f"#{m['analysis_id']}",
                "geometry_version": d["bundle"]["geometry_version"] if d else None,
                "std_lane_id": m["std_lane_id"],
                "std_lane_label": std_lane["label"] if std_lane else "",
                "valid": not info["problems"], "stale": info["stale"],
                "stale_matches": n_stale,
                "problems": info["problems"],
                "coef": info["coef"], "offset": info["offset"],
                "response": info["response"],
                "lanes": [{"id": l["id"], "x0": l["x0"], "x1": l["x1"],
                           "label": l["label"]} for l in lanes],
                "derived": d["bundle"]["geometry"]["derived"] if d else None,
                "standards": [{"target_id": tid, "target_name": tname.get(tid, ""),
                               "peak_id": s["peak_id"], "rf": s["rf"], "area": s["area"]}
                              for tid, s in info["assignment"].items()],
            })

        # 输出匹配(附斑点详情、候选与逐条有效性)
        out_matches = []
        for mt in matches:
            info = info_by_mid.get(mt["member_id"])
            d = info["data"] if info else None
            spot = None
            if d and mt["peak_id"] is not None:
                spot = next((s for s in d["spots"] if s["peak_id"] == mt["peak_id"]), None)
            ms = match_state[(mt["target_id"], mt["member_id"])]
            out_matches.append({
                "target_id": mt["target_id"], "member_id": mt["member_id"],
                "peak_id": mt["peak_id"], "lane_id": mt["lane_id"],
                "status": mt["status"], "locked": bool(mt["locked"]),
                "valid": ms["valid"], "stale": ms["stale"],
                "spot": ({k: spot[k] for k in
                          ("peak_id", "lane_id", "lane_label", "spot_no", "rf",
                           "area", "center_y", "y0", "y1")} if spot else None),
                "candidates": (info["candidates"].get(mt["target_id"], [])
                               if info else []),
            })

        # 汇总:成员须无阻断性问题,且该目标匹配逐条判定有效
        summary_rows = []
        for t in sorted(targets, key=lambda t: t["rf_ref"]):
            entries = []
            for info in infos:
                if info["problems"]:
                    continue
                mid = info["member"]["id"]
                mt = match_by.get((t["id"], mid))
                if not mt or not mt["peak_id"]:
                    continue
                if not match_state[(t["id"], mid)]["valid"]:
                    continue
                spot = next((s for s in info["data"]["spots"]
                             if s["peak_id"] == mt["peak_id"]), None)
                if not spot:
                    continue
                coef = info["coef"]
                entries.append({
                    "member_id": mid, "analysis_id": info["member"]["analysis_id"],
                    "analysis_name": next(m2["analysis_name"] for m2 in out_members
                                          if m2["id"] == mid),
                    "peak_id": spot["peak_id"], "lane_label": spot["lane_label"],
                    "spot_no": spot["spot_no"], "rf": spot["rf"],
                    "raw_area": spot["area"], "coef": coef,
                    "norm_area": (spot["area"] * coef) if coef else None,
                    "status": mt["status"], "locked": bool(mt["locked"]),
                })
            stats = compare.summarize([e["norm_area"] for e in entries
                                       if e["norm_area"] is not None])
            summary_rows.append({"target": t, "entries": entries, "stats": stats})
        excluded = [{"member_id": i["member"]["id"],
                     "analysis_id": i["member"]["analysis_id"],
                     "analysis_name": next(m2["analysis_name"] for m2 in out_members
                                           if m2["id"] == i["member"]["id"]),
                     "reasons": [p["message"] for p in i["problems"]]}
                    for i in infos if i["problems"]]

        return {
            "comparison": comp, "thresholds": cfg, "targets": targets,
            "members": out_members, "matches": out_matches,
            "summary": {"rows": summary_rows, "excluded": excluded},
        }

    @app.get("/compare")
    def compare_page():
        return render_template("compare.html")

    @app.get("/api/comparisons")
    def list_comparisons():
        with con() as c:
            return jsonify(db.list_comparisons(c))

    @app.post("/api/comparisons")
    def create_comparison():
        payload = request.get_json(force=True) or {}
        name = (payload.get("name") or "").strip()
        if not name:
            abort(400, "缺少对照组名称")
        with con() as c:
            cid = db.create_comparison(c, name)
            return jsonify(comparison_state(c, cid))

    @app.get("/api/comparisons/<int:cid>")
    def get_comparison_state(cid):
        with con() as c:
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/members")
    def add_cmp_member(cid):
        payload = request.get_json(force=True)
        aid = int(payload["analysis_id"])
        std_lane_id = int(payload["std_lane_id"])
        with con() as c:
            if not db.get_comparison(c, cid):
                abort(404, "对照组不存在")
            a, bundle = bundle_for(c, aid)
            if not a:
                abort(404, "分析不存在")
            if not bundle:
                abort(400, "该板尚未完成几何标定,不能加入对照")
            if not any(l["peaks"] for l in bundle["lanes"]):
                abort(400, "该板尚未定量(没有任何积分峰),不能加入对照")
            if not any(l["id"] == std_lane_id for l in bundle["lanes"]):
                abort(400, "标准品泳道不存在于该板当前几何版本")
            d = member_compute(c, aid)
            mid = db.add_member(c, cid, aid, std_lane_id, d["fingerprint"])
            auto_match_member(c, cid, mid)
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/members/<int:mid>/std_lane")
    def set_std_lane(cid, mid):
        payload = request.get_json(force=True)
        std_lane_id = int(payload["std_lane_id"])
        with con() as c:
            m = db.get_member(c, mid)
            if not m or m["comparison_id"] != cid:
                abort(404, "成员不存在")
            d = member_compute(c, m["analysis_id"])
            if not d or not any(l["id"] == std_lane_id for l in d["bundle"]["lanes"]):
                abort(400, "标准品泳道不存在于该板当前几何版本")
            db.update_member(c, mid, std_lane_id=std_lane_id)
            auto_match_member(c, cid, mid)
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/members/<int:mid>/rematch")
    def rematch_member(cid, mid):
        with con() as c:
            m = db.get_member(c, mid)
            if not m or m["comparison_id"] != cid:
                abort(404, "成员不存在")
            auto_match_member(c, cid, mid)
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/members/<int:mid>/delete")
    def delete_cmp_member(cid, mid):
        with con() as c:
            m = db.get_member(c, mid)
            if not m or m["comparison_id"] != cid:
                abort(404, "成员不存在")
            db.delete_member(c, mid)
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/targets")
    def add_cmp_target(cid):
        payload = request.get_json(force=True)
        try:
            name = (payload.get("name") or "").strip()
            rf_ref = float(payload["rf_ref"])
            rf_tol = float(payload.get("rf_tol") or 0.05)
            if not name or not (0 <= rf_ref <= 1) or not (0.001 <= rf_tol <= 0.5):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            abort(400, "目标参数无效:需名称、参考 Rf∈[0,1]、容差∈[0.001,0.5]")
        with con() as c:
            if not db.get_comparison(c, cid):
                abort(404, "对照组不存在")
            db.add_target(c, cid, name, rf_ref, rf_tol)
            for m in db.list_members(c, cid):
                auto_match_member(c, cid, m["id"])
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/targets/<int:tid>/update")
    def update_cmp_target(cid, tid):
        payload = request.get_json(force=True)
        try:
            name = (payload.get("name") or "").strip()
            rf_ref = float(payload["rf_ref"])
            rf_tol = float(payload.get("rf_tol") or 0.05)
            if not name or not (0 <= rf_ref <= 1) or not (0.001 <= rf_tol <= 0.5):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            abort(400, "目标参数无效:需名称、参考 Rf∈[0,1]、容差∈[0.001,0.5]")
        with con() as c:
            if not any(t["id"] == tid for t in db.list_targets(c, cid)):
                abort(404, "目标不存在")
            db.update_target(c, tid, name, rf_ref, rf_tol)
            for m in db.list_members(c, cid):
                auto_match_member(c, cid, m["id"])
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/targets/<int:tid>/delete")
    def delete_cmp_target(cid, tid):
        with con() as c:
            db.delete_target(c, tid)
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/matches/bind")
    def bind_match(cid):
        """改绑(peak_id=候选斑点)或拆开(peak_id=null);改绑视为确认当前数据,刷新指纹。"""
        payload = request.get_json(force=True)
        tid = int(payload["target_id"])
        mid = int(payload["member_id"])
        peak_id = payload.get("peak_id")
        with con() as c:
            m = db.get_member(c, mid)
            if not m or m["comparison_id"] != cid:
                abort(404, "成员不存在")
            if not any(t["id"] == tid for t in db.list_targets(c, cid)):
                abort(404, "目标不存在")
            existing = {mt["target_id"]: mt for mt in db.list_matches(c, cid)
                        if mt["member_id"] == mid}
            cur = existing.get(tid)
            if cur and cur["locked"]:
                abort(400, "该匹配已锁定,请先解锁再改绑/拆开")
            d = member_compute(c, m["analysis_id"])
            if not d:
                abort(400, "该板尚未完成几何标定")
            lane_id = None
            if peak_id is not None:
                spot = next((s for s in d["spots"] if s["peak_id"] == int(peak_id)), None)
                if not spot:
                    abort(400, "斑点不存在(板数据可能已变更,请重新匹配)")
                if spot["lane_id"] == m["std_lane_id"]:
                    abort(400, "不能绑定标准品泳道上的斑点")
                lane_id = spot["lane_id"]
                peak_id = int(peak_id)
            # 只把这一条关系确认到当前数据版本;其余匹配不受影响,
            # 未重新确认的匹配在板数据变更后继续保持失效。
            db.upsert_match(c, cid, tid, mid, peak_id, lane_id, "manual",
                            bool(cur and cur["locked"]), data_fp=d["fingerprint"])
            return jsonify(comparison_state(c, cid))

    @app.post("/api/comparisons/<int:cid>/matches/lock")
    def lock_match(cid):
        payload = request.get_json(force=True)
        tid = int(payload["target_id"])
        mid = int(payload["member_id"])
        locked = bool(payload.get("locked"))
        with con() as c:
            m = db.get_member(c, mid)
            if not m or m["comparison_id"] != cid:
                abort(404, "成员不存在")
            db.set_match_lock(c, tid, mid, locked)
            return jsonify(comparison_state(c, cid))

    @app.get("/api/comparisons/<int:cid>/export/compare.csv")
    def export_compare_csv(cid):
        with con() as c:
            st = comparison_state(c, cid)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["comparison_id", "comparison_name", "target", "rf_ref", "rf_tol",
                        "analysis_id", "analysis_name", "geometry_version", "std_lane",
                        "lane_label", "spot_no", "peak_id", "rf", "raw_area", "coef",
                        "norm_area", "match_status", "locked", "member_valid",
                        "exclude_reasons", "target_n", "target_mean_norm",
                        "target_sd_norm", "target_cv_pct", "target_min_norm",
                        "target_max_norm"])
            comp = st["comparison"]
            mname = {m["id"]: m for m in st["members"]}
            for row in st["summary"]["rows"]:
                t = row["target"]
                s = row["stats"]
                stat_cols = [s["n"],
                             "" if s["mean"] is None else f"{s['mean']:.1f}",
                             "" if s["sd"] is None else f"{s['sd']:.1f}",
                             "" if s["cv_pct"] is None else f"{s['cv_pct']:.2f}",
                             "" if s["min"] is None else f"{s['min']:.1f}",
                             "" if s["max"] is None else f"{s['max']:.1f}"]
                for e in row["entries"]:
                    m = mname[e["member_id"]]
                    w.writerow([cid, comp["name"], t["name"], f"{t['rf_ref']:.4f}",
                                f"{t['rf_tol']:.4f}", e["analysis_id"], e["analysis_name"],
                                m["geometry_version"], m["std_lane_label"],
                                e["lane_label"], e["spot_no"], e["peak_id"],
                                f"{e['rf']:.4f}", f"{e['raw_area']:.1f}",
                                "" if e["coef"] is None else f"{e['coef']:.4f}",
                                "" if e["norm_area"] is None else f"{e['norm_area']:.1f}",
                                e["status"], 1 if e["locked"] else 0, 1, ""] + stat_cols)
            for ex in st["summary"]["excluded"]:
                m = mname[ex["member_id"]]
                w.writerow([cid, comp["name"], "", "", "", ex["analysis_id"],
                            ex["analysis_name"], m["geometry_version"],
                            m["std_lane_label"], "", "", "", "", "", "", "", "", 0, 0,
                            ";".join(ex["reasons"]), "", "", "", "", "", ""])
            # 成员未整体排除但单条匹配失效(板数据变更后未重新确认)也逐条留痕
            tname = {t["id"]: t["name"] for t in st["targets"]}
            for mt in st["matches"]:
                if not mt["stale"]:
                    continue
                m = mname[mt["member_id"]]
                if not m["valid"]:
                    continue   # 成员整体排除,上面已列原因
                w.writerow([cid, comp["name"], tname.get(mt["target_id"], ""), "", "",
                            m["analysis_id"], m["analysis_name"], m["geometry_version"],
                            m["std_lane_label"], "", "", mt["peak_id"] or "", "", "",
                            "", "", mt["status"], 1 if mt["locked"] else 0, 0,
                            "板数据已变更,该匹配未重新确认(改绑或重新匹配)",
                            "", "", "", "", "", ""])
            mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
            mem.seek(0)
            return send_file(mem, mimetype="text/csv", as_attachment=True,
                             download_name=f"c{cid}_cross_plate.csv")

    @app.get("/api/comparisons/<int:cid>/export/matches.json")
    def export_compare_json(cid):
        with con() as c:
            st = comparison_state(c, cid)
            payload = {
                "tool": TOOL, "tool_version": __version__,
                "generated_at": db.now(),
                "comparison": st["comparison"], "thresholds": st["thresholds"],
                "targets": st["targets"],
                "members": [{k: m[k] for k in
                             ("id", "analysis_id", "analysis_name", "geometry_version",
                              "std_lane_id", "std_lane_label", "valid", "stale",
                              "stale_matches", "problems", "coef", "offset",
                              "response", "standards")}
                            for m in st["members"]],
                "matches": [{k: mt[k] for k in
                             ("target_id", "member_id", "peak_id", "lane_id",
                              "status", "locked", "valid", "stale", "spot")}
                            for mt in st["matches"]],
                "summary": st["summary"],
            }
            mem = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            mem.seek(0)
            return send_file(mem, mimetype="application/json", as_attachment=True,
                             download_name=f"c{cid}_matches.json")

    @app.get("/api/comparisons/<int:cid>/export/figure.png")
    def export_compare_figure(cid):
        with con() as c:
            st = comparison_state(c, cid)
            tidx = {t["id"]: i for i, t in enumerate(st["targets"])}
            tcolor = {t["id"]: compare.TARGET_COLORS[i % len(compare.TARGET_COLORS)]
                      for i, t in enumerate(st["targets"])}
            panels = []
            mark_index = {}   # (target_id, member_id) -> (panel_i, mark_i)
            for m in st["members"]:
                d = member_compute(c, m["analysis_id"])
                if not d:
                    continue
                pi = len(panels)
                corr, _, _ = pipeline.stage_images(d["analysis"]["image_path"], d["bundle"])
                lane_by_id = {l["id"]: l for l in d["bundle"]["lanes"]}
                marks = []
                for mt in st["matches"]:
                    if mt["member_id"] != m["id"] or not mt["spot"]:
                        continue
                    lane = lane_by_id.get(mt["spot"]["lane_id"])
                    if not lane:
                        continue
                    ti = tidx.get(mt["target_id"], 0)
                    if mt["stale"]:
                        # 失效匹配:灰色标记,不参与连线
                        marks.append({
                            "x": (lane["x0"] + lane["x1"]) / 2, "y": mt["spot"]["center_y"],
                            "color": "#778899", "label": f"T{ti + 1}x",
                            "locked": False})
                        continue
                    mark_index[(mt["target_id"], m["id"])] = (pi, len(marks))
                    marks.append({
                        "x": (lane["x0"] + lane["x1"]) / 2, "y": mt["spot"]["center_y"],
                        "color": tcolor[mt["target_id"]], "label": f"T{ti + 1}",
                        "locked": mt["locked"]})
                std_lane = lane_by_id.get(m["std_lane_id"])
                subtitle = (f"coef={m['coef']:.3f}" if m["coef"] is not None else
                            (m["problems"][0]["message"][:40] if m["problems"] else ""))
                panels.append({
                    "title": f"#{m['analysis_id']} plate (geom v{m['geometry_version']})",
                    "subtitle": subtitle, "valid": m["valid"], "image": corr,
                    "std_lane": (std_lane["x0"], std_lane["x1"]) if std_lane else None,
                    "marks": marks})
            links = []
            # 按成员顺序连接相邻两块板上同一目标的标记(实线=双方已锁定)
            for t in st["targets"]:
                prev = None
                for m in st["members"]:
                    key = (t["id"], m["id"])
                    if key not in mark_index:
                        continue
                    cur = mark_index[key]
                    if prev is not None:
                        both_locked = all(
                            next((mt for mt in st["matches"]
                                  if mt["target_id"] == t["id"] and mt["member_id"] == mid),
                                 {}).get("locked")
                            for mid in (prev[2], m["id"]))
                        links.append({"p1": prev[0], "m1": prev[1],
                                      "p2": cur[0], "m2": cur[1],
                                      "color": tcolor[t["id"]], "dashed": not both_locked})
                    prev = (cur[0], cur[1], m["id"])
            legend = [(tcolor[t["id"]],
                       f"T{tidx[t['id']] + 1} {t['name']} Rf={t['rf_ref']:.2f}+-{t['rf_tol']:.2f}")
                      for t in st["targets"]]
            legend.append(("#9aa3ad", "solid=locked  dashed=unconfirmed"))
            img = compare.draw_comparison(panels, links, legend)
            path = os.path.join(exports, f"c{cid}_compare.png")
            img.save(path)
            return send_file(path, mimetype="image/png",
                             as_attachment=request.args.get("dl") == "1",
                             download_name=os.path.basename(path))

    # ---------- 显色时间序列校审 ----------

    def kin_default_controls(derived, fw=None, fh=None):
        """新帧默认控制点:帧四角(帧图像素,fx/fy) -> 校正矩形四角(rx/ry)。

        帧尺寸缺省时假设帧与校正图同尺寸;实际由上传帧的宽高决定。
        """
        W, H = derived["width"], derived["height"]
        fw = W if fw is None else fw
        fh = H if fh is None else fh
        frame_rect = [(0.0, 0.0), (float(fw - 1), 0.0),
                      (float(fw - 1), float(fh - 1)), (0.0, float(fh - 1))]
        ref_rect = [(0.0, 0.0), (float(W - 1), 0.0),
                    (float(W - 1), float(H - 1)), (0.0, float(H - 1))]
        return [{"fx": fx, "fy": fy, "rx": rx, "ry": ry, "kind": "corner"}
                for (fx, fy), (rx, ry) in zip(frame_rect, ref_rect)]

    def kin_source_fingerprint(frames, series_row, plate):
        """时间序列来源指纹:照片内容/尺寸 + 时刻 + 控制点 + 微调 + 排除 +
        板面几何版本 + 泳道/积分边界指纹。任一变化 => 已成版快照过期。"""
        payload = {
            "image_sha1": plate["analysis"]["image_sha1"] if plate else "",
            "plate_fp": plate["fingerprint"] if plate else "",
            "geometry_version": plate["bundle"]["geometry_version"] if plate else None,
            "start_at": series_row["start_at"],
            "frames": [{
                "id": f["id"], "seq": f["seq"], "sha1": f["image_sha1"],
                "w": f["image_width"], "h": f["image_height"],
                "taken_at": f["taken_at"], "cp": f["control_points"],
                "dx": f["dx"], "dy": f["dy"],
                "excluded": bool(f["excluded"]),
                "exclude_reason": f["exclude_reason"],
            } for f in sorted(frames, key=lambda f: (f["seq"], f["id"]))],
        }
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def kin_compute(c, sid):
        """组装序列并逐帧重算指标。返回 {series, frames:[{row, metrics, error}],
        plate, times, issues, curves:{peak_id}, suggest, usable_ids, current_v?}。"""
        srow = db.get_kinetic_series(c, sid)
        if not srow:
            abort(404, "时间序列不存在")
        frames = db.list_kinetic_frames(c, sid)
        # 单板当前几何/泳道/斑点(与校准、对照共用指纹缓存)
        a, bundle = bundle_for(c, srow["analysis_id"])
        if not a:
            abort(404, "分析不存在")
        plate = cal_plate_data(c, srow["analysis_id"]) if bundle else None
        # 冻结泳道/峰(当前几何版本)
        lanes_def = (bundle or {}).get("lanes", [])
        derived = (bundle or {}).get("geometry", {}).get("derived")
        ref_centers = {}
        if plate:
            ref_centers = {s["peak_id"]: s["center_y"] for s in plate["spots"]}

        # 时刻
        rows_for_time = [{"id": f["id"], "seq": f["seq"], "taken_at": f["taken_at"]}
                         for f in frames]
        trows, time_issues = kinetics.frame_times(rows_for_time, srow["start_at"] or None)
        t_by_id = {r["frame_id"]: r["t_sec"] for r in trows}
        out_frames = []
        issues = list(time_issues)
        lane_ids = [{"id": l["id"], "x0": l["x0"], "x1": l["x1"],
                     "peaks": l["peaks"]} for l in lanes_def]
        for f in frames:
            entry = {"row": f, "t_sec": t_by_id.get(f["id"]),
                     "metrics": None, "error": None}
            if not bundle:
                entry["error"] = {"kind": "no_geometry",
                                  "message": "该板尚未完成几何标定"}
                out_frames.append(entry)
                continue
            if f["excluded"]:
                # 排除帧仍尝试配准/重算(供图上灰线),失败不报错
                pass
            cps = f["control_points"]
            pairs_all = [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in cps]
            corners = [p for p in cps if p.get("kind") == "corner"]
            checks = [p for p in cps if p.get("kind") == "check"]
            # 优先用四角 + 检查点;控制点不足 4 时用全部点
            try:
                if len(corners) == 4:
                    pairs = [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in corners]
                else:
                    pairs = pairs_all
                check_pairs = [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in checks]
                m = kinetics.frame_metrics(
                    f["image_path"], derived, lane_ids, pairs, check_pairs,
                    f["dx"], f["dy"], residual_max=kinetics.DEFAULTS["reg_residual_max"],
                    reference_centers=ref_centers)
                entry["metrics"] = m
                if len(pairs_all) < 4:
                    entry["error"] = {"kind": "reg_insufficient",
                                      "message": f"控制点仅 {len(pairs_all)} 个,不足 4 个"}
            except kinetics.RegistrationError as e:
                entry["error"] = {"kind": e.kind, "message": e.message}
            except FileNotFoundError:
                entry["error"] = {"kind": "image_missing", "message": "帧照片文件缺失"}
            out_frames.append(entry)

        usable = [e for e in out_frames
                  if e["metrics"] is not None and e["error"] is None
                  and not e["row"]["excluded"] and e["t_sec"] is not None]
        # 逐斑点曲线
        curves = {}
        lane_label = {l["id"]: l["label"] for l in lanes_def}
        if bundle:
            for l in lanes_def:
                for p in l["peaks"]:
                    pid = p["id"]
                    ts, vs = [], []
                    per_spot_frames = []
                    for e in usable:
                        ps = next((s for s in e["metrics"]["spots"]
                                   if s["peak_id"] == pid), None)
                        if not ps:
                            ts.append(None); vs.append(None); continue
                        ts.append(e["t_sec"]); vs.append(ps["area"])
                    an = kinetics.analyze_series(ts, vs)
                    curves[pid] = {
                        "peak_id": pid, "lane_id": l["id"],
                        "lane_label": l.get("label", ""),
                        "y0": p["y0"], "y1": p["y1"],
                        "times": an["times"], "areas": an["areas"],
                        "normalized": an["normalized"], "segments": an["segments"],
                        "plateau": an["plateau"], "plateau_range": an["plateau_range"],
                        "peak": an["peak"], "window_stat": None,
                        "frame_ids": [e["row"]["id"] for e in usable
                                      for ps in [next((s for s in e["metrics"]["spots"]
                                                       if s["peak_id"] == pid), None)]
                                      if ps],
                    }
            sug = kinetics.suggest_window(list(curves.values()))
        else:
            sug = None
        # 窗口草稿/已确认窗口统计
        win = {"t0": srow["window_t0"], "t1": srow["window_t1"]}
        if win["t0"] is not None and win["t1"] is not None:
            frame_records = _kin_frame_records(out_frames)
            vw = kinetics.validate_window(win, frame_records, curves,
                                          cfg=kinetics.DEFAULTS)
            for pid, st in vw["window_stats"].items():
                if pid in curves:
                    curves[pid]["window_stat"] = st
        # 帧级问题上抛(饱和/漂移为警告,窗口内才阻断)
        sat_frames, drift_frames, reg_bad = [], [], []
        for e in out_frames:
            fid = e["row"]["id"]
            if e["error"] and e["error"]["kind"] in kinetics.BLOCKERS \
                    and not e["row"]["excluded"]:
                reg_bad.append((fid, e["error"]))
                continue
            if not e["metrics"] or e["row"]["excluded"]:
                continue
            if any(s["saturated_px"] >= qc.THRESHOLDS["sat_min_count"]
                   for s in e["metrics"]["spots"]):
                sat_frames.append(fid)
            if any(s.get("drift_px") is not None
                   and s["drift_px"] > kinetics.DEFAULTS["drift_centroid_px"]
                   for s in e["metrics"]["spots"]):
                drift_frames.append(fid)
        for fid, err in reg_bad:
            issues.append({"kind": err["kind"], "frame_ids": [fid],
                           "message": f"帧 #{fid}: {err['message']}"})
        if sat_frames:
            issues.append({"kind": "saturated", "frame_ids": sat_frames,
                           "message": f"帧 {sat_frames} 积分区检出像素饱和"})
        if drift_frames:
            issues.append({"kind": "spot_drift", "frame_ids": drift_frames,
                           "message": f"帧 {drift_frames} 存在斑点质心漂移超限"})
        # 帧数
        if not frames:
            issues.append({"kind": "no_frames", "frame_ids": [],
                           "message": "序列中还没有任何帧"})
        elif len(usable) < kinetics.DEFAULTS["min_valid_frames"]:
            issues.append({
                "kind": "few_frames", "frame_ids": [e["row"]["id"] for e in usable],
                "message": f"有效帧仅 {len(usable)} 个,不足 "
                           f"{kinetics.DEFAULTS['min_valid_frames']} 个"})
        # 排除无理由
        for f in frames:
            if f["excluded"] and not f["exclude_reason"].strip():
                issues.append({"kind": "exclude_reason_required", "frame_ids": [f["id"]],
                               "message": f"帧 #{f['seq'] + 1} 已排除但未填写理由"})

        # 版本(过期判定)
        plate_fp = plate["fingerprint"] if plate else ""
        source_fp = kin_source_fingerprint(frames, srow,
                                           {"analysis": a, "bundle": bundle,
                                            "fingerprint": plate_fp})
        versions = []
        for v in db.list_kinetic_versions(c, sid):
            v["stale"] = v["source_fp"] != source_fp
            versions.append(v)
        return {
            "series": srow, "analysis": {"id": a["id"], "name": a["name"],
                                         "image_sha1": a["image_sha1"]},
            "geometry": ({"version": bundle["geometry_version"],
                          "derived": derived} if bundle else None),
            "lanes": lanes_def, "lane_label": lane_label,
            "frames": out_frames, "curves": curves, "suggest": sug,
            "window": win, "issues": issues, "source_fingerprint": source_fp,
            "versions": versions,
            "n_usable": len(usable), "defaults": dict(kinetics.DEFAULTS),
        }

    def _kin_frame_records(out_frames):
        """validate_window 需要的帧记录结构。"""
        recs = []
        for e in out_frames:
            f = e["row"]
            per_spot = {}
            sat_any = False
            if e["metrics"]:
                for s in e["metrics"]["spots"]:
                    per_spot[s["peak_id"]] = s
                    if s["saturated_px"] >= qc.THRESHOLDS["sat_min_count"]:
                        sat_any = True
            recs.append({"id": f["id"], "seq": f["seq"], "t_sec": e["t_sec"],
                         "usable": e["metrics"] is not None and e["error"] is None,
                         "excluded": bool(f["excluded"]),
                         "reason": f["exclude_reason"], "sat_any": sat_any,
                         "per_spot": per_spot})
        return recs

    def _kin_compact_frame(entry, profile_bins=240):
        """逐帧结果压缩为前端 JSON:密度曲线降采样,逐斑点标量。"""
        f, m = entry["row"], entry["metrics"]
        out = {"id": f["id"], "seq": f["seq"], "taken_at": f["taken_at"],
               "t_sec": entry["t_sec"], "width": f["image_width"],
               "height": f["image_height"], "excluded": bool(f["excluded"]),
               "exclude_reason": f["exclude_reason"], "dx": f["dx"], "dy": f["dy"],
               "control_points": f["control_points"], "error": entry["error"],
               "residual": m["residual"] if m else None, "spots": [], "profiles": {}}
        if m:
            out["spots"] = m["spots"]
            out["profiles"] = {str(lid): kinetics.downsample_profile(p, profile_bins)
                               for lid, p in m["profiles"].items()}
        return out

    def kinetic_state(c, sid):
        st = kin_compute(c, sid)
        # 内部 frames 含全长逐行密度曲线,仅供服务端计算;JSON 只下发降采样压缩帧
        st["frames"] = [_kin_compact_frame(e) for e in st["frames"]]
        st["frames_out"] = st["frames"]
        return st

    @app.get("/api/analyses/<int:aid>/kinetics")
    def list_kinetics(aid):
        with con() as c:
            get_analysis_or_404(c, aid)
            return jsonify(db.list_kinetic_series(c, aid))

    @app.post("/api/analyses/<int:aid>/kinetics")
    def create_kinetics(aid):
        p = request.get_json(force=True) or {}
        with con() as c:
            get_analysis_or_404(c, aid)
            name = (p.get("name") or "").strip()[:80]
            start_at = (p.get("start_at") or "").strip()
            if start_at:
                try:
                    kinetics.parse_time(start_at)
                except ValueError as e:
                    abort(400, f"显色开始时刻无效: {e}")
            sid = db.create_kinetic_series(c, aid, name, start_at)
            return jsonify(kinetic_state(c, sid))

    @app.get("/api/kinetics/<int:sid>")
    def get_kinetics(sid):
        with con() as c:
            return jsonify(kinetic_state(c, sid))

    @app.post("/api/kinetics/<int:sid>/meta")
    def update_kinetics_meta(sid):
        p = request.get_json(force=True) or {}
        with con() as c:
            srow = db.get_kinetic_series(c, sid)
            if not srow:
                abort(404, "时间序列不存在")
            fields = {}
            if "name" in p:
                fields["name"] = (str(p["name"] or "")).strip()[:80]
            if "start_at" in p:
                start_at = (str(p["start_at"] or "")).strip()
                if start_at:
                    try:
                        kinetics.parse_time(start_at)
                    except ValueError as e:
                        abort(400, f"显色开始时刻无效: {e}")
                fields["start_at"] = start_at
            if "window_t0" in p:
                fields["window_t0"] = None if p["window_t0"] in (None, "") \
                    else float(p["window_t0"])
            if "window_t1" in p:
                fields["window_t1"] = None if p["window_t1"] in (None, "") \
                    else float(p["window_t1"])
            db.update_kinetic_series(c, sid, fields)
            return jsonify(kinetic_state(c, sid))

    @app.post("/api/kinetics/<int:sid>/delete")
    def delete_kinetics(sid):
        with con() as c:
            srow = db.get_kinetic_series(c, sid)
            if not srow:
                abort(404, "时间序列不存在")
            aid = srow["analysis_id"]
            db.delete_kinetic_series(c, sid)
            return jsonify({"ok": True, "analysis_id": aid,
                            "series": db.list_kinetic_series(c, aid)})

    @app.post("/api/kinetics/<int:sid>/frames")
    def upload_kinetic_frame(sid):
        f = request.files.get("file")
        if not f:
            abort(400, "缺少帧照片文件")
        taken_at = (request.form.get("taken_at") or "").strip()
        with con() as c:
            srow = db.get_kinetic_series(c, sid)
            if not srow:
                abort(404, "时间序列不存在")
            a, bundle = bundle_for(c, srow["analysis_id"])
            if not bundle:
                abort(400, "该板尚未完成几何标定,请先在单板定量页标定几何与积分边界")
            if taken_at:
                try:
                    kinetics.parse_time(taken_at)
                except ValueError as e:
                    abort(400, f"拍摄时刻无效: {e}")
            seq = c.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM kinetic_frames WHERE series_id=?",
                (sid,)).fetchone()[0]
            fid = db.add_kinetic_frame(c, sid, seq, "", 0, 0, "", taken_at, [])
            ext = os.path.splitext(f.filename or "frame.png")[1] or ".png"
            path = os.path.join(uploads, f"k{sid}_f{fid}{ext}")
            f.save(path)
            with Image.open(path) as im:
                w, h = im.size
                im.convert("RGB")
            with open(path, "rb") as fh:
                sha1 = hashlib.sha1(fh.read()).hexdigest()
            cps = kin_default_controls(bundle["geometry"]["derived"], w, h)
            db.update_kinetic_frame(c, fid, {"control_points": cps})
            # add 时路径为空,直接补列
            c.execute("UPDATE kinetic_frames SET image_path=?, image_width=?,"
                      " image_height=?, image_sha1=? WHERE id=?",
                      (path, w, h, sha1, fid))
            return jsonify(kinetic_state(c, sid))

    @app.get("/api/kinetics/frames/<int:fid>/image")
    def kinetic_frame_image(fid):
        with con() as c:
            fr = db.get_kinetic_frame(c, fid)
            if not fr:
                abort(404, "帧不存在")
            if not os.path.exists(fr["image_path"]):
                abort(404, "帧照片文件缺失")
            return send_file(fr["image_path"])

    @app.get("/api/kinetics/frames/<int:fid>/rectified.png")
    def kinetic_frame_rectified(fid):
        """配准后的校正图(供叠加图与配准检查)。配准失败返回 409 + 原因。"""
        with con() as c:
            fr = db.get_kinetic_frame(c, fid)
            if not fr:
                abort(404, "帧不存在")
            srow = db.get_kinetic_series(c, fr["series_id"])
            a, bundle = bundle_for(c, srow["analysis_id"])
            if not bundle:
                abort(400, "该板尚未完成几何标定")
            cps = fr["control_points"]
            corners = [p for p in cps if p.get("kind") == "corner"]
            checks = [p for p in cps if p.get("kind") == "check"]
            pairs = [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in corners] \
                if len(corners) == 4 else \
                [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in cps]
            try:
                with Image.open(fr["image_path"]) as im:
                    gray, _, resid = kinetics.rectify_frame(
                        im, bundle["geometry"]["derived"], pairs,
                        [(p["fx"], p["fy"], p["rx"], p["ry"]) for p in checks],
                        fr["dx"], fr["dy"], kinetics.DEFAULTS["reg_residual_max"])
            except kinetics.RegistrationError as e:
                return jsonify({"error": 409, "description": e.message,
                                "kind": e.kind}), 409
            buf = io.BytesIO()
            gray.save(buf, "PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png")

    @app.post("/api/kinetics/frames/<int:fid>")
    def update_kinetic_frame_api(fid):
        p = request.get_json(force=True) or {}
        with con() as c:
            fr = db.get_kinetic_frame(c, fid)
            if not fr:
                abort(404, "帧不存在")
            fields = {}
            if "taken_at" in p:
                ta = (str(p["taken_at"] or "")).strip()
                if ta:
                    try:
                        kinetics.parse_time(ta)
                    except ValueError as e:
                        abort(400, f"拍摄时刻无效: {e}")
                fields["taken_at"] = ta
            if "dx" in p:
                fields["dx"] = float(p["dx"] or 0)
            if "dy" in p:
                fields["dy"] = float(p["dy"] or 0)
            if "excluded" in p:
                excluded = bool(p["excluded"])
                reason = (str(p.get("exclude_reason") or "")).strip()
                if excluded and not reason:
                    abort(400, "排除坏帧必须填写理由")
                fields["excluded"] = 1 if excluded else 0
                fields["exclude_reason"] = reason
            elif "exclude_reason" in p:
                fields["exclude_reason"] = (str(p["exclude_reason"] or "")).strip()
            if "control_points" in p:
                cps = p["control_points"]
                clean = []
                for cp in cps:
                    clean.append({"fx": float(cp["fx"]), "fy": float(cp["fy"]),
                                  "rx": float(cp["rx"]), "ry": float(cp["ry"]),
                                  "kind": "check" if cp.get("kind") == "check"
                                  else "corner"})
                fields["control_points"] = clean
            db.update_kinetic_frame(c, fid, fields)
            return jsonify(kinetic_state(c, fr["series_id"]))

    @app.post("/api/kinetics/frames/<int:fid>/delete")
    def delete_kinetic_frame_api(fid):
        with con() as c:
            fr = db.get_kinetic_frame(c, fid)
            if not fr:
                abort(404, "帧不存在")
            sid = fr["series_id"]
            try:
                if fr["image_path"] and os.path.exists(fr["image_path"]):
                    os.remove(fr["image_path"])
            except OSError:
                pass
            db.delete_kinetic_frame(c, fid)
            return jsonify(kinetic_state(c, sid))

    def _kin_build_snapshot(c, st, win):
        """成版快照:锁定所用帧、配准参数与窗口,逐帧面积/曲线/窗口统计全部留痕。"""
        curves, frames = st["curves"], st["frames"]
        frame_records = _kin_frame_records(frames)
        vw = kinetics.validate_window(win, frame_records, curves,
                                      cfg=kinetics.DEFAULTS)
        spot_by_peak = {}
        if st.get("geometry"):
            data = cal_plate_data(c, st["analysis"]["id"])
            spot_by_peak = {s["peak_id"]: s for s in data["spots"]}
        snap_frames = []
        for e in frames:
            f = e["row"]
            snap_frames.append({
                "frame_id": f["id"], "seq": f["seq"], "taken_at": f["taken_at"],
                "t_sec": e["t_sec"], "image_sha1": f["image_sha1"],
                "image_width": f["image_width"], "image_height": f["image_height"],
                "control_points": f["control_points"], "dx": f["dx"], "dy": f["dy"],
                "excluded": bool(f["excluded"]), "exclude_reason": f["exclude_reason"],
                "residual": e["metrics"]["residual"] if e["metrics"] else None,
                "error": e["error"],
                "spots": ([{"peak_id": s["peak_id"], "lane_id": s["lane_id"],
                            "area": s["area"], "height": s["height"],
                            "center_y": s["center_y"], "drift_px": s["drift_px"],
                            "saturated_px": s["saturated_px"]}
                           for s in e["metrics"]["spots"]] if e["metrics"] else []),
            })
        snap_curves = []
        for pid, cv in curves.items():
            sp = spot_by_peak.get(pid, {})
            snap_curves.append({
                "peak_id": pid, "lane_id": cv["lane_id"],
                "lane_label": cv["lane_label"], "spot_no": sp.get("spot_no"),
                "rf": sp.get("rf"), "y0": cv["y0"], "y1": cv["y1"],
                "times": cv["times"], "areas": cv["areas"],
                "normalized": cv["normalized"], "segments": cv["segments"],
                "plateau": cv["plateau"], "peak": cv["peak"],
                "window_stat": vw["window_stats"].get(pid),
            })
        g = st["geometry"]
        return {
            "tool": TOOL, "tool_version": __version__, "saved_at": db.now(),
            "analysis_id": st["analysis"]["id"], "analysis_name": st["analysis"]["name"],
            "image_sha1": st["analysis"]["image_sha1"],
            "geometry_version": g["version"] if g else None,
            "derived": g["derived"] if g else None,
            "lanes": [{"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"],
                       "peaks": [{"id": p["id"], "y0": p["y0"], "y1": p["y1"]}
                                 for p in l["peaks"]]} for l in st["lanes"]],
            "start_at": st["series"]["start_at"], "window": win,
            "thresholds": dict(kinetics.DEFAULTS),
            "frames": snap_frames, "curves": snap_curves,
            "window_issues": vw["issues"], "window_ok": vw["ok"],
        }

    @app.post("/api/kinetics/<int:sid>/confirm")
    def confirm_kinetic_window(sid):
        """确认取值窗口:阻断问题存在时拒绝,不形成版本。"""
        p = request.get_json(force=True) or {}
        with con() as c:
            srow = db.get_kinetic_series(c, sid)
            if not srow:
                abort(404, "时间序列不存在")
            try:
                t0 = float(p.get("t0")); t1 = float(p.get("t1"))
            except (TypeError, ValueError):
                abort(400, "取值窗口时刻无效")
            if t1 <= t0:
                abort(400, "取值窗口结束时刻须晚于开始时刻")
            st = kin_compute(c, sid)
            # 先把草稿窗口落库(被拒绝也保留用户拖动结果)
            db.update_kinetic_series(c, sid, {"window_t0": t0, "window_t1": t1})
            frame_records = _kin_frame_records(st["frames"])
            vw = kinetics.validate_window({"t0": t0, "t1": t1}, frame_records,
                                          st["curves"], cfg=kinetics.DEFAULTS)
            hard = [i for i in vw["issues"] if i["type"] in kinetics.BLOCKERS]
            # 序列级阻断(时刻/配准/有效帧)
            seq_hard = [i for i in st["issues"] if i["kind"] in kinetics.BLOCKERS]
            if hard or seq_hard:
                msgs = [i["message"] for i in hard] + \
                       [i["message"] for i in seq_hard]
                abort(400, "不能确认取值窗口:" + ";".join(msgs))
            snap = _kin_build_snapshot(c, st, {"t0": t0, "t1": t1})
            vid, ver = db.add_kinetic_version(c, sid, t0, t1, snap,
                                              st["source_fingerprint"])
            return jsonify({"version_id": vid, "version": ver,
                            "state": kinetic_state(c, sid)})

    @app.get("/api/kinetics/versions/<int:vid>")
    def get_kinetic_version(vid):
        with con() as c:
            v = db.get_kinetic_version(c, vid)
            if not v:
                abort(404, "版本不存在")
            srow = db.get_kinetic_series(c, v["series_id"])
            frames = db.list_kinetic_frames(c, v["series_id"])
            a, bundle = bundle_for(c, srow["analysis_id"])
            cur_fp = kin_source_fingerprint(
                frames, srow,
                {"analysis": a, "bundle": bundle,
                 "fingerprint": cal_plate_data(c, srow["analysis_id"])["fingerprint"]
                 if bundle else ""})
            return jsonify({"version": {k: v[k] for k in
                                        ("id", "version", "window_t0", "window_t1",
                                         "source_fp", "is_current", "created_at")},
                            "snapshot": v["snapshot"], "stale": v["source_fp"] != cur_fp,
                            "current_fp": cur_fp})

    def _kin_version_or_400(c, vid):
        v = db.get_kinetic_version(c, vid)
        if not v:
            abort(404, "版本不存在")
        srow = db.get_kinetic_series(c, v["series_id"])
        frames = db.list_kinetic_frames(c, v["series_id"])
        a, bundle = bundle_for(c, srow["analysis_id"])
        cur_fp = kin_source_fingerprint(
            frames, srow,
            {"analysis": a, "bundle": bundle,
             "fingerprint": cal_plate_data(c, srow["analysis_id"])["fingerprint"]
             if bundle else ""})
        stale = v["source_fp"] != cur_fp
        return v, v["snapshot"], stale, cur_fp

    @app.get("/api/kinetics/versions/<int:vid>/frames.csv")
    def export_kinetic_csv(vid):
        with con() as c:
            v, snap, stale, _ = _kin_version_or_400(c, vid)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["series_id", "version", "stale", "frame_id", "frame_seq",
                        "taken_at", "t_sec", "excluded", "exclude_reason",
                        "residual_px", "lane_id", "lane_label", "peak_id", "spot_no",
                        "rf", "area", "height", "center_y", "drift_px",
                        "saturated_px", "in_window", "window_mean", "window_cv_pct",
                        "window_n"])
            win = snap["window"]
            stat_by_peak = {cv["peak_id"]: cv.get("window_stat") for cv in snap["curves"]}
            spot_no = {cv["peak_id"]: cv.get("spot_no") for cv in snap["curves"]}
            rf_by = {cv["peak_id"]: cv.get("rf") for cv in snap["curves"]}
            lane_label = {l["id"]: l["label"] for l in snap["lanes"]}
            for fr in snap["frames"]:
                in_win = win["t0"] <= (fr["t_sec"] or -1) <= win["t1"] \
                    and not fr["excluded"]
                for s in fr["spots"]:
                    stt = stat_by_peak.get(s["peak_id"]) or {}
                    w.writerow([
                        v["series_id"], v["version"], 1 if stale else 0,
                        fr["frame_id"], fr["seq"], fr["taken_at"],
                        "" if fr["t_sec"] is None else f"{fr['t_sec']:.3f}",
                        1 if fr["excluded"] else 0, fr["exclude_reason"],
                        "" if fr["residual"] is None else f"{fr['residual']:.3f}",
                        s["lane_id"], lane_label.get(s["lane_id"], ""),
                        s["peak_id"], spot_no.get(s["peak_id"], ""),
                        "" if rf_by.get(s["peak_id"]) is None
                        else f"{rf_by[s['peak_id']]:.4f}",
                        f"{s['area']:.1f}", f"{s['height']:.1f}",
                        f"{s['center_y']:.2f}",
                        "" if s["drift_px"] is None else f"{s['drift_px']:.2f}",
                        s["saturated_px"], 1 if in_win else 0,
                        f"{stt['mean']:.1f}" if stt else "",
                        f"{stt['cv_pct']:.2f}" if stt and stt.get("cv_pct") is not None else "",
                        stt.get("n", "") if stt else ""])
            mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
            mem.seek(0)
            tag = "_EXPIRED" if stale else ""
            return send_file(mem, mimetype="text/csv", as_attachment=True,
                             download_name=f"k{v['series_id']}_v{v['version']}_frames{tag}.csv")

    @app.get("/api/kinetics/versions/<int:vid>/recompute.json")
    def export_kinetic_json(vid):
        with con() as c:
            v, snap, stale, cur_fp = _kin_version_or_400(c, vid)
            payload = {"tool": TOOL, "tool_version": __version__,
                       "exported_at": db.now(), "series_id": v["series_id"],
                       "version": v["version"], "version_id": v["id"],
                       "window": {"t0": v["window_t0"], "t1": v["window_t1"]},
                       "source_fingerprint": v["source_fp"],
                       "current_fingerprint": cur_fp, "stale": stale,
                       "snapshot": snap}
            mem = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode())
            mem.seek(0)
            tag = "_EXPIRED" if stale else ""
            return send_file(mem, mimetype="application/json", as_attachment=True,
                             download_name=f"k{v['series_id']}_v{v['version']}_recompute{tag}.json")

    @app.get("/api/kinetics/versions/<int:vid>/figure.png")
    def export_kinetic_figure(vid):
        with con() as c:
            v, snap, stale, _ = _kin_version_or_400(c, vid)
            curves = []
            for cv in snap["curves"]:
                wm = (cv.get("window_stat") or {}).get("mean")
                curves.append({"lane_label": cv["lane_label"],
                               "spot_no": cv.get("spot_no"), "peak_id": cv["peak_id"],
                               "times": cv["times"], "areas": cv["areas"],
                               "normalized": cv["normalized"], "peak": cv["peak"],
                               "window_mean": wm})
            frames = [{"seq": f["seq"], "t_sec": f["t_sec"],
                       "excluded": f["excluded"], "usable": not f["error"]}
                      for f in snap["frames"]]
            img = kinetics.draw_kinetics(
                {"id": snap["analysis_id"], "name": snap["analysis_name"]},
                curves, snap["window"], frames, stale=stale)
            path = os.path.join(exports, f"k{v['series_id']}_v{v['version']}_kinetics.png")
            img.save(path)
            tag = "_EXPIRED" if stale else ""
            return send_file(path, mimetype="image/png",
                             as_attachment=request.args.get("dl") == "1",
                             download_name=os.path.basename(path).replace(
                                 ".png", f"{tag}.png"))

    # ---------- 校准曲线与含量反算 ----------

    cal_cache = {}   # analysis_id -> {fp, data}

    def cal_plate_data(c, aid):
        """单板当前数据(分析/几何/泳道斑点/指纹),按指纹缓存(同 member_compute)。"""
        rec = cal_cache.get(aid)
        a, bundle = bundle_for(c, aid)
        if not a:
            return None
        if not bundle:
            return {"analysis": a, "bundle": None, "spots": [],
                    "fingerprint": "", "geometry_version": None}
        fp = member_fingerprint(bundle)
        if rec and rec["fp"] == fp:
            return rec["data"]
        result = pipeline.compute_bundle(a["image_path"], bundle)
        lane_label = {l["id"]: l["label"] for l in bundle["lanes"]}
        spots = []
        for s in result["spots"]:
            s2 = dict(s)
            s2["lane_label"] = lane_label.get(s["lane_id"], "")
            spots.append(s2)
        data = {"analysis": a, "bundle": bundle, "spots": spots,
                "fingerprint": fp, "geometry_version": bundle["geometry_version"]}
        cal_cache[aid] = {"fp": fp, "data": data}
        return data

    def cal_kinetic_override(c, aid):
        """下游校准引用显色时间序列:取该板当前有效(未过期)的已确认版本,
        用其锁定窗口内面积均值替换单帧面积。只允许引用有效版本:
        - 无已确认版本 / 版本过期(照片/几何/积分边界变更)=> 不替换,回退单帧;
        - 过期已确认版本给警告(不阻断),提示回时间序列页重新确认。
        返回 (kin_info|None, warnings:[str])。kin_info 含快照窗口统计。
        """
        series = db.list_kinetic_series(c, aid)
        chosen = None
        for srow in series:
            vid = srow.get("current_version")
            if not vid:
                continue
            v = db.get_kinetic_version(c, vid)
            if not v or not v["is_current"]:
                continue
            cur_fp = kin_compute(c, srow["id"])["source_fingerprint"]
            if v["source_fp"] == cur_fp:
                chosen = (srow, v, False)
                break
            chosen = chosen or (srow, v, True)   # 记下过期版本做警告
        if not chosen:
            return None, []
        srow, v, stale = chosen
        if stale:
            return ({"series_id": srow["id"], "series_name": srow["name"],
                     "version_id": v["id"], "version": v["version"],
                     "window": {"t0": v["window_t0"], "t1": v["window_t1"]},
                     "stale": True, "stats": {}},
                    [f"显色时间序列 #{srow['id']} 的已确认取值窗口 v{v['version']}"
                     "已因照片/板面几何/积分边界变更而过期,本次校准回退为单帧面积,"
                     "请回时间序列页重新确认后再成线"])
        stats = {}
        for cv in v["snapshot"]["curves"]:
            st = cv.get("window_stat")
            if st:
                stats[cv["peak_id"]] = st
        return ({"series_id": srow["id"], "series_name": srow["name"],
                 "version_id": v["id"], "version": v["version"],
                 "window": {"t0": v["window_t0"], "t1": v["window_t1"]},
                 "stale": False, "stats": stats,
                 "source_fp": v["source_fp"]}, [])

    def cal_data_with_kinetics(c, aid):
        """cal_plate_data + 时间序列窗口面积替换(浅拷贝,不污染指纹缓存)。"""
        base = cal_plate_data(c, aid)
        if not base or not base["bundle"]:
            return base, None, []
        kin, warns = cal_kinetic_override(c, aid)
        if not kin or kin.get("stale"):
            return base, kin, warns
        stats = kin["stats"]
        spots = []
        for s in base["spots"]:
            s2 = dict(s)
            st = stats.get(s["peak_id"])
            if st:
                s2["area"] = st["mean"]
                s2["kinetic_window_n"] = st["n"]
                s2["kinetic_cv_pct"] = st["cv_pct"]
            spots.append(s2)
        data = dict(base)
        data["spots"] = spots
        return data, kin, warns

    def cal_source_fingerprint(data, cal, rows, kin=None):
        """校准来源指纹:板指纹(几何/泳道/峰边界)+ 影响拟合、反算与标签的全部输入。

        含校准元数据(名称/目标/参考 Rf/容差/单位)与每条泳道标注(角色/绑定斑点/
        浓度/进样体积/稀释/排除及理由)。任一变化 => 已成线版本过期。
        时间序列版本切换/失效(窗口面积变)也纳入指纹。
        模型种类不参与:每版模型在快照中各自固定 model,切换模型不使旧模型失效。
        """
        payload = {
            "plate_fp": data["fingerprint"] if data else "",
            "image_sha1": (data["analysis"]["image_sha1"] if data else ""),
            "kinetic": (None if not kin else
                        {"series_id": kin["series_id"], "version_id": kin["version_id"],
                         "stale": kin.get("stale", False),
                         "source_fp": kin.get("source_fp", "")}),
            "cfg": {"name": cal["name"], "target_name": cal["target_name"],
                    "target_rf": cal["target_rf"], "rf_tol": cal["rf_tol"],
                    "conc_unit": cal["conc_unit"], "vol_unit": cal["vol_unit"]},
            "lanes": [
                {"lane_id": r["lane_id"], "role": r["role"], "peak_id": r["peak_id"],
                 "concentration": r["concentration"], "volume": r["volume"],
                 "dilution": r["dilution"], "excluded": r["excluded"],
                 "exclude_reason": r["exclude_reason"]}
                for r in sorted(rows, key=lambda r: r["lane_id"])],
        }
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def _spot_brief(s):
        return {"peak_id": s["peak_id"], "spot_no": s.get("spot_no"), "rf": s["rf"],
                "area": s["area"], "center_y": s["center_y"], "y0": s["y0"], "y1": s["y1"],
                "height": s["height"]}

    def _roles_from_rows(rows, lane_by_id, spots_by_lane, target_rf, tol):
        """把持久化的标注行映射为 evaluate() 输入与前端 lane 条目。"""
        spot_by_peak = {}
        for lspots in spots_by_lane.values():
            for s in lspots:
                spot_by_peak[s["peak_id"]] = s
        standards, blanks, unknowns = [], [], []
        lane_entries = {}
        for r in rows:
            lid = r["lane_id"]
            role = r["role"]
            lane = lane_by_id.get(lid)
            if lane is None:
                # 几何/泳道重建后的失效标注:不参与本次评估,仅在泳道表中列出待清理
                lane_entries[lid] = {
                    "lane_id": lid, "role": role, "peak_id": r["peak_id"],
                    "concentration": r["concentration"], "volume": r["volume"],
                    "dilution": r["dilution"], "excluded": bool(r["excluded"]),
                    "exclude_reason": r["exclude_reason"],
                    "bound_spot": None, "binding_valid": False, "suggestion": None}
                continue
            lane_label = lane["label"] if lane else ""
            spot = spot_by_peak.get(r["peak_id"]) if r["peak_id"] else None
            suggestion = calibration.suggest_spot(
                spots_by_lane.get(lid, []), target_rf, tol) if target_rf is not None else None
            entry = {
                "lane_id": lid, "role": role,
                "peak_id": r["peak_id"], "concentration": r["concentration"],
                "volume": r["volume"], "dilution": r["dilution"],
                "excluded": bool(r["excluded"]), "exclude_reason": r["exclude_reason"],
                "bound_spot": _spot_brief(spot) if spot else None,
                "binding_valid": spot is not None if r["peak_id"] else True,
                "suggestion": _spot_brief(suggestion) if suggestion else None,
            }
            lane_entries[lid] = entry
            base = {"lane_id": lid, "lane_label": lane_label}
            if role == "standard":
                standards.append({
                    **base, "peak_id": r["peak_id"],
                    "spot_no": spot.get("spot_no") if spot else None,
                    "rf": spot["rf"] if spot else None,
                    "response": spot["area"] if spot else None,
                    "y0": spot["y0"] if spot else None, "y1": spot["y1"] if spot else None,
                    "concentration": r["concentration"], "volume": r["volume"],
                    "excluded": bool(r["excluded"]),
                    "exclude_reason": r["exclude_reason"]})
            elif role == "blank":
                blanks.append({
                    **base, "peak_id": r["peak_id"],
                    "response": spot["area"] if spot else None})
            elif role == "unknown":
                unknowns.append({
                    **base, "peak_id": r["peak_id"],
                    "spot_no": spot.get("spot_no") if spot else None,
                    "rf": spot["rf"] if spot else None,
                    "response": spot["area"] if spot else None,
                    "y0": spot["y0"] if spot else None, "y1": spot["y1"] if spot else None,
                    "volume": r["volume"], "dilution": r["dilution"]})
        return standards, blanks, unknowns, lane_entries

    def calibration_state(c, cal_id):
        """组装校准页完整状态:板面泳道/候选斑点、标注、实时评估、历史版本与过期标记。"""
        cal = db.get_calibration(c, cal_id)
        if not cal:
            abort(404, "校准不存在")
        data, kin, kin_warns = cal_data_with_kinetics(c, cal["analysis_id"])
        analysis = data["analysis"]
        bundle = data["bundle"]
        rows = db.list_cal_lanes(c, cal_id)

        out = {"calibration": cal, "analysis": {
                   "id": analysis["id"], "name": analysis["name"],
                   "geometry_version": data["geometry_version"]},
               "kinetic": kin, "kinetic_warnings": kin_warns,
               "lanes": [], "evaluation": None, "versions": [],
               "defaults": dict(calibration.DEFAULTS),
               "models": [{"kind": m, "label": calibration.MODEL_LABELS[m]}
                          for m in calibration.MODELS]}

        if not bundle:
            out["no_geometry"] = True
            out["source_fingerprint"] = cal_source_fingerprint(data, cal, rows, kin)
            return out
        spots_by_lane = {}
        lane_by_id = {}
        for l in bundle["lanes"]:
            lane_by_id[l["id"]] = l
            spots_by_lane[l["id"]] = [s for s in data["spots"] if s["lane_id"] == l["id"]]
        row_lane_ids = {r["lane_id"] for r in rows}
        missing = sorted(row_lane_ids - set(spots_by_lane))
        standards, blanks, unknowns, entries = _roles_from_rows(
            rows, lane_by_id, spots_by_lane, cal["target_rf"], cal["rf_tol"])
        ev = calibration.evaluate(
            standards, blanks, unknowns, cal["model"],
            target_name=cal["target_name"])
        # 时间序列窗口引用:有效替换给提示;过期已确认版本给警告(不阻断成线)
        if kin and not kin.get("stale"):
            ev["issues"].append({
                "type": "kinetic_window", "level": "info", "scope": "model",
                "message": f"响应面积取自显色时间序列 #{kin['series_id']} "
                           f"v{kin['version']} 锁定窗口 "
                           f"[{kinetics.format_t(kin['window']['t0'])},"
                           f" {kinetics.format_t(kin['window']['t1'])}] 内均值",
                "lane_ids": [], "peak_ids": []})
        for msg in kin_warns:
            ev["issues"].append({"type": "kinetic_expired", "level": "warning",
                                 "scope": "model", "message": msg,
                                 "lane_ids": [], "peak_ids": []})
        out["evaluation"] = ev
        out["derived"] = bundle["geometry"]["derived"]

        # 泳道视图(全部泳道 + 每条泳道可选斑点与程序建议)
        for l in bundle["lanes"]:
            lspots = sorted(spots_by_lane[l["id"]], key=lambda s: s["rf"])
            e = entries.get(l["id"])
            out["lanes"].append({
                "id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"],
                "spots": [_spot_brief(s) for s in lspots],
                "role": e["role"] if e else None,
                "annotation": e if e else None,
            })
        # 几何重建后旧泳道消失:标注行仍保留(供追溯),单独列出以便用户删除/重建
        lane_by_id_now = {l["id"]: l for l in bundle["lanes"]}
        for lid in missing:
            r = next(rr for rr in rows if rr["lane_id"] == lid)
            out["lanes"].append({
                "id": lid, "x0": None, "x1": None,
                "label": f"(旧泳道 #{lid})", "spots": [], "role": r["role"],
                "annotation": entries[lid], "orphan": True})
        out["missing_lanes"] = missing

        # 当前来源指纹(板几何/边界 + 校准元数据 + 泳道标注 + 时间序列窗口版本)
        source_fp = cal_source_fingerprint(data, cal, rows, kin)
        out["source_fingerprint"] = source_fp

        # 历史版本(含过期判定):来源指纹任一变化即过期
        for v in db.list_cal_versions(c, cal_id):
            v["stale"] = v["source_fp"] != source_fp
            snap = None
            if v["id"] == cal["current_version"]:
                snap_row = db.get_cal_version(c, v["id"])
                snap = snap_row["snapshot"]
            v["snapshot"] = snap
            out["versions"].append(v)
        return out

    def _check_lane_payload(c, aid, cal_id, payload):
        data = cal_plate_data(c, aid)
        live = set()
        bundle = data["bundle"] if data else None
        if bundle is not None:
            live = {l["id"] for l in bundle["lanes"]}
        removed = [int(x) for x in (payload.get("removed_lane_ids") or [])]
        for lid in removed:
            if lid in live:
                abort(400, f"泳道 {lid} 仍存在,不能按失效泳道删除")
        lanes_in = payload.get("lanes", [])
        if bundle is None and lanes_in:
            abort(400, "该板尚未完成几何标定,不能标注泳道(可先清除失效标注)")
        peak_lanes = {}
        if bundle is not None:
            for s in data["spots"]:
                peak_lanes[s["peak_id"]] = s["lane_id"]
        clean = []
        for l in payload.get("lanes", []):
            lid = int(l["lane_id"])
            if lid not in live:
                abort(400, f"泳道 {lid} 不属于该板当前几何版本")
            role = l.get("role", "unknown")
            if role not in ("standard", "blank", "unknown"):
                abort(400, f"泳道 {lid} 角色无效")
            peak_id = l.get("peak_id")
            if peak_id is not None:
                peak_id = int(peak_id)
                if peak_lanes.get(peak_id) != lid:
                    abort(400, f"绑定的斑点 {peak_id} 不属于泳道 {lid}")

            def num(k):
                v = l.get(k)
                if v in (None, "", []):
                    return None
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    abort(400, f"泳道 {lid} 数值无效")
                return v

            excluded = bool(l.get("excluded"))
            reason = (l.get("exclude_reason") or "").strip()
            if excluded and role == "standard" and not reason:
                abort(400, f"排除标准点(泳道 {lid})必须填写理由")
            # 角色无关字段不落库,避免标准/未知样互改后残留旧数值
            conc = num("concentration") if role == "standard" else None
            vol = num("volume") if role in ("standard", "unknown") else None
            dil = num("dilution") if role == "unknown" else None
            if role != "standard":
                excluded, reason = False, ""
            clean.append({
                "lane_id": lid, "role": role, "peak_id": peak_id,
                "concentration": conc, "volume": vol, "dilution": dil,
                "excluded": excluded, "exclude_reason": reason})
        return data, clean, peak_lanes, removed

    @app.get("/api/calibrations/by-analysis/<int:aid>")
    def list_calibrations(aid):
        with con() as c:
            get_analysis_or_404(c, aid)
            return jsonify(db.list_calibrations(c, aid))

    @app.post("/api/analyses/<int:aid>/calibrations")
    def create_calibration(aid):
        p = request.get_json(force=True) or {}
        name = (p.get("name") or "").strip()
        target = (p.get("target_name") or "").strip()
        if not name or not target:
            abort(400, "需填写校准名称与目标成分")
        try:
            rf = p.get("target_rf")
            target_rf = None if rf in (None, "") else float(rf)
            rf_tol = float(p.get("rf_tol") or calibration.DEFAULTS["rf_hint_tol"])
            if target_rf is not None and not (0 <= target_rf <= 1):
                raise ValueError
            if not (0.001 <= rf_tol <= 0.5):
                raise ValueError
        except (TypeError, ValueError):
            abort(400, "目标 Rf 需在 [0,1],容差在 [0.001,0.5]")
        conc_unit = (p.get("conc_unit") or "ng/uL").strip()[:16]
        vol_unit = (p.get("vol_unit") or "uL").strip()[:16]
        with con() as c:
            get_analysis_or_404(c, aid)
            cal_id = db.create_calibration(c, aid, name, target, target_rf, rf_tol,
                                           conc_unit, vol_unit)
            return jsonify(calibration_state(c, cal_id))

    @app.get("/api/calibrations/<int:cal_id>")
    def get_calibration(cal_id):
        with con() as c:
            return jsonify(calibration_state(c, cal_id))

    @app.post("/api/calibrations/<int:cal_id>/update")
    def update_calibration_meta(cal_id):
        p = request.get_json(force=True) or {}
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            fields = {}
            for k in ("name", "target_name", "conc_unit", "vol_unit"):
                if k in p:
                    v = (str(p[k]) if p[k] is not None else "").strip()
                    if k in ("name", "target_name") and not v:
                        abort(400, "名称与目标成分不能为空")
                    fields[k] = v[:16] if k.endswith("unit") else v
            if "target_rf" in p:
                rf = p["target_rf"]
                fields["target_rf"] = None if rf in (None, "") else float(rf)
                if fields["target_rf"] is not None and not (0 <= fields["target_rf"] <= 1):
                    abort(400, "目标 Rf 需在 [0,1]")
            if "rf_tol" in p:
                fields["rf_tol"] = float(p["rf_tol"])
                if not (0.001 <= fields["rf_tol"] <= 0.5):
                    abort(400, "容差需在 [0.001,0.5]")
            db.update_calibration(c, cal_id, fields)
            return jsonify(calibration_state(c, cal_id))

    @app.post("/api/calibrations/<int:cal_id>/lanes")
    def set_cal_lanes(cal_id):
        payload = request.get_json(force=True) or {}
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            data2, clean, _, removed = _check_lane_payload(c, cal["analysis_id"], cal_id, payload)
            live = {l["id"] for l in data2["bundle"]["lanes"]} if data2["bundle"] else set()
            db.replace_cal_lanes(c, cal_id, clean, live_lane_ids=live,
                                 remove_lane_ids=removed)
            return jsonify(calibration_state(c, cal_id))

    @app.post("/api/calibrations/<int:cal_id>/evaluate")
    def preview_calibration(cal_id):
        """实时预览:先把提交的标注落库,再按给定模型评估(不生成版本)。"""
        payload = request.get_json(force=True) or {}
        model = payload.get("model", "linear")
        if model not in calibration.MODELS:
            abort(400, "未知模型")
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            data2, clean, _, removed = _check_lane_payload(c, cal["analysis_id"], cal_id, payload)
            live = {l["id"] for l in data2["bundle"]["lanes"]} if data2["bundle"] else set()
            db.replace_cal_lanes(c, cal_id, clean, live_lane_ids=live,
                                 remove_lane_ids=removed)
            db.update_calibration(c, cal_id, {"model": model})
            return jsonify(calibration_state(c, cal_id))

    def _build_snapshot(cal, data, ev, kin=None):
        """成线快照:几何、背景/检测参数、每条来源的积分边界与斑点 ID、评估结果。

        响应取自时间序列锁定窗口时,kin 记录来源序列/版本/窗口,供下游审计。
        """
        bundle = data["bundle"]
        spot_by_peak = {s["peak_id"]: s for s in data["spots"]}

        def enrich(p):
            s = spot_by_peak.get(p["peak_id"])
            return {**p, "y0": s["y0"] if s else p.get("y0"),
                    "y1": s["y1"] if s else p.get("y1"),
                    "center_y": s["center_y"] if s else None,
                    "saturated_px": s.get("saturated_px") if s else None}

        snap = {
            "tool": TOOL, "tool_version": __version__,
            "saved_at": db.now(),
            "analysis_id": cal["analysis_id"],
            "calibration_name": cal["name"],
            "analysis_name": data["analysis"]["name"],
            "image_sha1": data["analysis"]["image_sha1"],
            "geometry_version": bundle["geometry_version"],
            "geometry": bundle["geometry"],
            "background": bundle.get("background"),
            "detection": bundle.get("detection"),
            "qc": bundle.get("qc"),
            "target": {"name": cal["target_name"], "rf": cal["target_rf"],
                       "rf_tol": cal["rf_tol"]},
            "units": {"conc": cal["conc_unit"], "vol": cal["vol_unit"]},
            "model": ev["model"],
            "kinetic_source": (None if not kin else {
                "series_id": kin["series_id"], "series_name": kin.get("series_name"),
                "version_id": kin["version_id"], "version": kin["version"],
                "window": kin["window"], "stale": kin.get("stale", False)}),
            "fit": ev["fit"],
            "range": ev["range"],
            "issues": ev["issues"],
            "blocked": ev["blocked"],
            "blocker_types": ev["blocker_types"],
            "points": [enrich(p) for p in ev["points"]],
            "excluded_points": [enrich(p) for p in ev["excluded_points"]],
            "samples": [enrich(s) for s in ev["samples"]],
            "lanes": [
                {"id": l["id"], "x0": l["x0"], "x1": l["x1"], "label": l["label"]}
                for l in bundle["lanes"]],
        }
        return snap

    @app.post("/api/calibrations/<int:cal_id>/fit")
    def fit_calibration(cal_id):
        """保存当前模型为新版本。阻断性问题或未填排除理由时拒绝(不形成含量结论)。"""
        p = request.get_json(force=True) or {}
        model = p.get("model", "linear")
        if model not in calibration.MODELS:
            abort(400, "未知模型")
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            data2, clean, _, removed = _check_lane_payload(c, cal["analysis_id"], cal_id, p)
            if data2["bundle"] is None:
                abort(400, "该板尚未完成几何标定,不能成线")
            live_ids = {l["id"] for l in data2["bundle"]["lanes"]}
            # 排除理由与阻断问题在实时状态上复核
            db.replace_cal_lanes(c, cal_id, clean, live_lane_ids=live_ids,
                                 remove_lane_ids=removed)
            db.update_calibration(c, cal_id, {"model": model})
            st = calibration_state(c, cal_id)
            ev = st["evaluation"]
            missing_reason = [i for i in ev["issues"]
                              if i["type"] == "exclude_reason_required"]
            if missing_reason:
                abort(400, "排除标准点必须填写理由")
            if ev["blocked"]:
                abort(400, "校准存在阻断性问题,不能成线:" +
                      ";".join(i["message"] for i in ev["issues"]
                               if i["level"] == "error" and i["type"] in calibration.BLOCKERS))
            data, kin, _ = cal_data_with_kinetics(c, cal["analysis_id"])
            cal = db.get_calibration(c, cal_id)   # 取含 model 更新后的元数据
            rows = db.list_cal_lanes(c, cal_id)
            source_fp = cal_source_fingerprint(data, cal, rows, kin)
            snap = _build_snapshot(cal, data, ev, kin)
            vid, ver = db.add_cal_version(c, cal_id, model, ev["fit"], snap,
                                          source_fp)
            return jsonify({"version_id": vid, "version": ver,
                            "state": calibration_state(c, cal_id)})

    @app.get("/api/calibrations/<int:cal_id>/versions/<int:vid>")
    def get_cal_version(cal_id, vid):
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            v = db.get_cal_version(c, vid)
            if not v or v["calibration_id"] != cal_id:
                abort(404, "版本不存在")
            data, kin, _ = cal_data_with_kinetics(c, cal["analysis_id"])
            cur_fp = cal_source_fingerprint(
                data, cal, db.list_cal_lanes(c, cal_id), kin)
            return jsonify({"version": {k: v[k] for k in
                                        ("id", "version", "model", "fit", "source_fp",
                                         "is_current", "created_at")},
                            "snapshot": v["snapshot"],
                            "stale": v["source_fp"] != cur_fp,
                            "current_fp": cur_fp})

    @app.post("/api/calibrations/<int:cal_id>/delete")
    def delete_calibration(cal_id):
        with con() as c:
            cal = db.get_calibration(c, cal_id)
            if not cal:
                abort(404, "校准不存在")
            aid = cal["analysis_id"]
            db.delete_calibration(c, cal_id)
            return jsonify({"ok": True, "analysis_id": aid,
                            "calibrations": db.list_calibrations(c, aid)})

    def _version_or_400(c, cal_id, vid):
        """取版本与成线时快照;stale 按“板+校准元数据+标注”联合来源指纹判定。

        返回 (cal_row, version_row, snap, stale)。所有导出标签一律取 snap,
        保证过期版本导出与其保存时输入一致,不被当前元数据污染。
        """
        cal = db.get_calibration(c, cal_id)
        if not cal:
            abort(404, "校准不存在")
        v = db.get_cal_version(c, vid)
        if not v or v["calibration_id"] != cal_id:
            abort(404, "版本不存在")
        data, kin, _ = cal_data_with_kinetics(c, cal["analysis_id"])
        cur_fp = cal_source_fingerprint(
            data, cal, db.list_cal_lanes(c, cal_id), kin)
        stale = v["source_fp"] != cur_fp
        return cal, v, v["snapshot"], stale, cur_fp

    @app.get("/api/calibrations/<int:cal_id>/versions/<int:vid>/samples.csv")
    def export_cal_samples_csv(cal_id, vid):
        with con() as c:
            cal, v, snap, stale, _ = _version_or_400(c, cal_id, vid)
            # 标签/单位/目标全部读保存时快照;旧快照缺 calibration_name 时回退行名
            cal_name = snap.get("calibration_name") or cal["name"]
            target = snap.get("target", {})
            units = snap.get("units", {})
            conc_unit = units.get("conc", "")
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["calibration_id", "calibration_name", "version", "model",
                        "stale", "analysis_id", "analysis_name", "geometry_version",
                        "target", "lane_id", "lane_label", "spot_no", "peak_id", "rf",
                        "response_area", "injection_volume", "dilution",
                        "back_amount", "applied_concentration", "sample_concentration",
                        "conc_unit", "in_range", "status", "reasons"])
            for s in snap["samples"]:
                w.writerow([
                    cal_id, cal_name, v["version"], v["model"],
                    1 if stale else 0, cal["analysis_id"], snap["analysis_name"],
                    snap["geometry_version"], target.get("name", ""),
                    s["lane_id"], s["lane_label"], s.get("spot_no", ""),
                    s.get("peak_id", ""),
                    "" if s.get("rf") is None else f"{s['rf']:.4f}",
                    f"{s['response']:.2f}" if s.get("response") is not None else "",
                    s.get("volume", ""), s.get("dilution", ""),
                    "" if s.get("amount_back") is None else f"{s['amount_back']:.6g}",
                    "" if s.get("applied_concentration") is None
                    else f"{s['applied_concentration']:.6g}",
                    "" if s.get("sample_concentration") is None
                    else f"{s['sample_concentration']:.6g}",
                    conc_unit,
                    "" if s.get("in_range") is None else (1 if s["in_range"] else 0),
                    s["status"], ";".join(s.get("reasons", []))])
            mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
            mem.seek(0)
            tag = "_EXPIRED" if stale else ""
            return send_file(mem, mimetype="text/csv", as_attachment=True,
                             download_name=f"cal{cal_id}_v{v['version']}_samples{tag}.csv")

    @app.get("/api/calibrations/<int:cal_id>/versions/<int:vid>/model.json")
    def export_cal_model_json(cal_id, vid):
        with con() as c:
            cal, v, snap, stale, cur_fp = _version_or_400(c, cal_id, vid)
            cal_name = snap.get("calibration_name") or cal["name"]
            payload = {
                "tool": TOOL, "tool_version": __version__,
                "exported_at": db.now(),
                "calibration_id": cal_id, "name": cal_name,
                "analysis_id": cal["analysis_id"],
                "version": v["version"], "version_id": v["id"],
                "model": v["model"], "fit": v["fit"],
                "source_fingerprint": v["source_fp"],
                "current_fingerprint": cur_fp, "stale": stale,
                "snapshot": snap}
            mem = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            mem.seek(0)
            tag = "_EXPIRED" if stale else ""
            return send_file(mem, mimetype="application/json", as_attachment=True,
                             download_name=f"cal{cal_id}_v{v['version']}_model{tag}.json")

    @app.get("/api/calibrations/<int:cal_id>/versions/<int:vid>/figure.png")
    def export_cal_figure(cal_id, vid):
        with con() as c:
            cal, v, snap, stale, _ = _version_or_400(c, cal_id, vid)
            ev = {
                "model": v["model"], "fit": v["fit"], "range": snap["range"],
                "points": snap["points"], "excluded_points": snap["excluded_points"],
                "samples": snap["samples"], "issues": snap["issues"],
                "blocked": snap["blocked"], "blocker_types": snap["blocker_types"]}
            units = snap.get("units", {})
            target = snap.get("target", {})
            meta = {"calibration_name": snap.get("calibration_name") or cal["name"],
                    "analysis_id": cal["analysis_id"],
                    "analysis_name": snap["analysis_name"], "version": v["version"],
                    "target_name": target.get("name", ""),
                    "conc_unit": units.get("conc", ""),
                    "vol_unit": units.get("vol", ""),
                    "created_at": v["created_at"]}
            img = calibration.draw_calibration(ev, meta, stale=stale)
            # 来源元数据写入 PNG 文本块,便于核对图像来自哪一版快照
            from PIL import PngImagePlugin
            info = PngImagePlugin.PngInfo()
            for k, val in (("calibration_id", str(cal_id)),
                           ("version", str(v["version"])),
                           ("model", v["model"]),
                           ("target", target.get("name", "")),
                           ("source_fingerprint", v["source_fp"]),
                           ("stale", "1" if stale else "0"),
                           ("calibration_name", snap.get("calibration_name") or cal["name"]),
                           ("conc_unit", units.get("conc", "")),
                           ("vol_unit", units.get("vol", ""))):
                info.add_text(k, val.encode("ascii", "replace").decode())
            path = os.path.join(exports, f"cal{cal_id}_v{v['version']}_calibration.png")
            img.save(path, pnginfo=info)
            tag = "_EXPIRED" if stale else ""
            return send_file(path, mimetype="image/png",
                             as_attachment=request.args.get("dl") == "1",
                             download_name=os.path.basename(path).replace(
                                 ".png", f"{tag}.png"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
