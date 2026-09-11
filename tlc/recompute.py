"""参数文件复算 CLI:

    python -m tlc.recompute params.json --image plate.jpg --out spots.csv

读取导出的参数文件,在原图上重跑同一条流水线,验证结果可复算。
"""

import argparse
import csv
import json
import sys

from . import pipeline
from .qc import THRESHOLDS

CSV_FIELDS = ["lane_id", "lane_label", "spot_no", "rf", "center_y_px",
              "center_mm_from_baseline", "area", "height", "pct_lane", "pct_plate",
              "y0", "y1", "saturated_px"]


def recompute(params_path, image_path=None):
    with open(params_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    img = image_path or bundle.get("image_path")
    if not img:
        raise SystemExit("参数文件未记录 image_path,请用 --image 指定原图")
    result = pipeline.compute_bundle(img, bundle)
    return bundle, result


def main(argv=None):
    ap = argparse.ArgumentParser(description="TLC 参数文件复算")
    ap.add_argument("params", help="导出的 params.json")
    ap.add_argument("--image", help="原图路径(默认取参数文件中的 image_path)")
    ap.add_argument("--out", help="输出 CSV 路径(默认打印到 stdout)")
    args = ap.parse_args(argv)

    bundle, result = recompute(args.params, args.image)
    rows = []
    lane_label = {l["id"]: l.get("label", "") for l in bundle.get("lanes", [])}
    for s in sorted(result["spots"], key=lambda s: (s["lane_id"], s.get("spot_no", 0))):
        rows.append({
            "lane_id": s["lane_id"],
            "lane_label": lane_label.get(s["lane_id"], ""),
            "spot_no": s.get("spot_no", ""),
            "rf": f"{s['rf']:.4f}",
            "center_y_px": f"{s['center_y']:.2f}",
            "center_mm_from_baseline": "" if s["center_mm"] is None else f"{s['center_mm']:.2f}",
            "area": f"{s['area']:.1f}",
            "height": f"{s['height']:.1f}",
            "pct_lane": f"{s['pct_lane']:.2f}",
            "pct_plate": f"{s['pct_plate']:.2f}",
            "y0": f"{s['y0']:.1f}",
            "y1": f"{s['y1']:.1f}",
            "saturated_px": s["saturated_px"],
        })
    out = open(args.out, "w", newline="", encoding="utf-8") if args.out else sys.stdout
    try:
        w = csv.DictWriter(out, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    finally:
        if args.out:
            out.close()
    for f in result["flags"]:
        print(f"[{f['level']}] {f['type']}: {f['message']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
