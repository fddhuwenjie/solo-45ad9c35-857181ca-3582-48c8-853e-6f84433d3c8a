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
from tlc import TOOL, __version__, annotate, background, geometry, pipeline, profile, qc

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
            # 峰搜索限定在 前沿~基线 之间,避免铅笔线等被误检
            det["y_min"] = min(d["front_y"], d["baseline_y"])
            det["y_max"] = max(d["front_y"], d["baseline_y"])
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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
