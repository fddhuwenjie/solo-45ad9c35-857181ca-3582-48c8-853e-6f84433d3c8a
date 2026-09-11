"""SQLite 持久化:分析、几何版本(含调整前后留痕)、泳道、峰、异常处置。"""

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  image_path TEXT NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  image_sha1 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geometry_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  params TEXT NOT NULL,            -- 用户输入:四角/基线/前沿/标尺
  derived TEXT NOT NULL,           -- 派生量:校正尺寸/基线y/前沿y/px_per_mm
  previous_params TEXT,            -- 上一版输入(调整前)
  previous_derived TEXT,           -- 上一版派生量(调整前)
  is_current INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL,
  geometry_version INTEGER NOT NULL,
  x0 REAL NOT NULL,
  x1 REAL NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  sort REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS peaks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lane_id INTEGER NOT NULL,
  y0 REAL NOT NULL,
  y1 REAL NOT NULL,
  origin TEXT NOT NULL DEFAULT 'manual',   -- auto | manual | split
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS flag_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL,
  geometry_version INTEGER NOT NULL,
  flag_key TEXT NOT NULL,
  kept INTEGER NOT NULL DEFAULT 1,         -- 1=保留异常结果(需理由) 0=撤销保留
  reason TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL,
  UNIQUE(analysis_id, geometry_version, flag_key)
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with connect(db_path) as con:
        con.executescript(SCHEMA)


def row_to_dict(r):
    return {k: r[k] for k in r.keys()}


def get_analysis(con, aid):
    r = con.execute("SELECT * FROM analyses WHERE id=?", (aid,)).fetchone()
    return row_to_dict(r) if r else None


def current_geometry(con, aid):
    r = con.execute(
        "SELECT * FROM geometry_versions WHERE analysis_id=? AND is_current=1", (aid,)).fetchone()
    if not r:
        return None
    d = row_to_dict(r)
    d["params"] = json.loads(d["params"])
    d["derived"] = json.loads(d["derived"])
    if d["previous_params"]:
        d["previous_params"] = json.loads(d["previous_params"])
        d["previous_derived"] = json.loads(d["previous_derived"])
    return d


def list_versions(con, aid):
    rows = con.execute(
        "SELECT * FROM geometry_versions WHERE analysis_id=? ORDER BY version", (aid,)).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        for k in ("params", "derived", "previous_params", "previous_derived"):
            if d[k]:
                d[k] = json.loads(d[k])
        out.append(d)
    return out


def set_geometry(con, aid, params, derived):
    """写入新几何版本:旧版本失效,调整前后参数双双留痕。"""
    old = current_geometry(con, aid)
    version = (old["version"] + 1) if old else 1
    if old:
        con.execute("UPDATE geometry_versions SET is_current=0 WHERE id=?", (old["id"],))
    cur = con.execute(
        """INSERT INTO geometry_versions
           (analysis_id, version, params, derived, previous_params, previous_derived,
            is_current, created_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        (aid, version, json.dumps(params), json.dumps(derived),
         json.dumps(old["params"]) if old else None,
         json.dumps(old["derived"]) if old else None,
         now()))
    return version, cur.lastrowid


def list_lanes(con, aid, version):
    rows = con.execute(
        "SELECT * FROM lanes WHERE analysis_id=? AND geometry_version=? ORDER BY sort, id",
        (aid, version)).fetchall()
    lanes = [row_to_dict(r) for r in rows]
    for lane in lanes:
        peaks = con.execute(
            "SELECT * FROM peaks WHERE lane_id=? ORDER BY y0", (lane["id"],)).fetchall()
        lane["peaks"] = [row_to_dict(p) for p in peaks]
    return lanes


def replace_lanes(con, aid, version, lanes):
    """以前端提交的全量列表为准做 upsert,未出现的旧泳道删除(其峰一并删除)。"""
    existing = {r["id"] for r in con.execute(
        "SELECT id FROM lanes WHERE analysis_id=? AND geometry_version=?", (aid, version))}
    kept = set()
    for i, lane in enumerate(lanes):
        if lane.get("id") in existing:
            lid = lane["id"]
            con.execute("UPDATE lanes SET x0=?, x1=?, label=?, sort=? WHERE id=?",
                        (lane["x0"], lane["x1"], lane.get("label", ""), i, lid))
        else:
            cur = con.execute(
                "INSERT INTO lanes (analysis_id, geometry_version, x0, x1, label, sort)"
                " VALUES (?,?,?,?,?,?)",
                (aid, version, lane["x0"], lane["x1"], lane.get("label", ""), i))
            lid = cur.lastrowid
        kept.add(lid)
    for lid in existing - kept:
        con.execute("DELETE FROM peaks WHERE lane_id=?", (lid,))
        con.execute("DELETE FROM lanes WHERE id=?", (lid,))
    return list_lanes(con, aid, version)


def replace_peaks(con, lane_id, peaks):
    existing = {r["id"] for r in con.execute("SELECT id FROM peaks WHERE lane_id=?", (lane_id,))}
    kept = set()
    for p in peaks:
        if p.get("id") in existing:
            pid = p["id"]
            con.execute("UPDATE peaks SET y0=?, y1=?, origin=?, note=? WHERE id=?",
                        (p["y0"], p["y1"], p.get("origin", "manual"), p.get("note", ""), pid))
        else:
            cur = con.execute(
                "INSERT INTO peaks (lane_id, y0, y1, origin, note) VALUES (?,?,?,?,?)",
                (lane_id, p["y0"], p["y1"], p.get("origin", "manual"), p.get("note", "")))
            pid = cur.lastrowid
        kept.add(pid)
    for pid in existing - kept:
        con.execute("DELETE FROM peaks WHERE id=?", (pid,))
    rows = con.execute("SELECT * FROM peaks WHERE lane_id=? ORDER BY y0", (lane_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


def get_decisions(con, aid, version):
    rows = con.execute(
        "SELECT * FROM flag_decisions WHERE analysis_id=? AND geometry_version=?",
        (aid, version)).fetchall()
    return {r["flag_key"]: row_to_dict(r) for r in rows}


def set_decision(con, aid, version, flag_key, kept, reason):
    con.execute(
        """INSERT INTO flag_decisions (analysis_id, geometry_version, flag_key, kept, reason, decided_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(analysis_id, geometry_version, flag_key)
           DO UPDATE SET kept=excluded.kept, reason=excluded.reason, decided_at=excluded.decided_at""",
        (aid, version, flag_key, 1 if kept else 0, reason, now()))
