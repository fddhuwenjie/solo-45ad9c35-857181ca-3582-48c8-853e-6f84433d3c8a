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
from tlc import TOOL, __version__, annotate, background, compare, geometry, pipeline, profile, qc

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
        """为成员生成/刷新自动匹配(不动 manual/locked),并把指纹刷新为当前数据。"""
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
        existing = {mt["target_id"]: mt for mt in db.list_matches(c, cid)
                    if mt["member_id"] == mid}
        for t in targets:
            mt = existing.get(t["id"])
            if mt and (mt["locked"] or mt["status"] == "manual"):
                continue
            cand = best.get(t["id"])
            db.upsert_match(c, cid, t["id"], mid,
                            cand["peak_id"] if cand else None,
                            cand["lane_id"] if cand else None, "auto", 0)
        db.update_member(c, mid, fingerprint=d["fingerprint"])

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
            if d["fingerprint"] != m["fingerprint"]:
                info["stale"] = True
                info["problems"].append({
                    "type": "stale",
                    "message": "该板的几何/泳道/积分边界在加入对照后已修改,"
                               "引用它的匹配已失效,请重新匹配"})
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

        # 次序颠倒检查(对当前已确认关系) + 汇总
        for info in infos:
            d = info["data"]
            if not d:
                continue
            mid = info["member"]["id"]
            mmatches = [mt for mt in matches if mt["member_id"] == mid and mt["peak_id"]]
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
            out_members.append({
                "id": m["id"], "analysis_id": m["analysis_id"],
                "analysis_name": a["name"] if a else f"#{m['analysis_id']}",
                "geometry_version": d["bundle"]["geometry_version"] if d else None,
                "std_lane_id": m["std_lane_id"],
                "std_lane_label": std_lane["label"] if std_lane else "",
                "valid": not info["problems"], "stale": info["stale"],
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

        # 输出匹配(附斑点详情与候选)
        info_by_mid = {i["member"]["id"]: i for i in infos}
        out_matches = []
        for mt in matches:
            info = info_by_mid.get(mt["member_id"])
            d = info["data"] if info else None
            spot = None
            if d and mt["peak_id"] is not None:
                spot = next((s for s in d["spots"] if s["peak_id"] == mt["peak_id"]), None)
            out_matches.append({
                "target_id": mt["target_id"], "member_id": mt["member_id"],
                "peak_id": mt["peak_id"], "lane_id": mt["lane_id"],
                "status": mt["status"], "locked": bool(mt["locked"]),
                "spot": ({k: spot[k] for k in
                          ("peak_id", "lane_id", "lane_label", "spot_no", "rf",
                           "area", "center_y", "y0", "y1")} if spot else None),
                "candidates": (info["candidates"].get(mt["target_id"], [])
                               if info else []),
            })

        # 汇总:只纳入通过质控的成员
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
            db.upsert_match(c, cid, tid, mid, peak_id, lane_id, "manual",
                            bool(cur and cur["locked"]))
            db.update_member(c, mid, fingerprint=d["fingerprint"])
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
                              "problems", "coef", "offset", "response", "standards")}
                            for m in st["members"]],
                "matches": [{k: mt[k] for k in
                             ("target_id", "member_id", "peak_id", "lane_id",
                              "status", "locked", "spot")}
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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
