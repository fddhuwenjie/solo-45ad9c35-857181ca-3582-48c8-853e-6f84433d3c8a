"""SQLite 持久化:分析、几何版本(含调整前后留痕)、泳道、峰、异常处置、跨板对照。"""

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
CREATE TABLE IF NOT EXISTS comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  comparison_id INTEGER NOT NULL,
  analysis_id INTEGER NOT NULL,
  std_lane_id INTEGER NOT NULL,        -- 该板的标准品泳道
  position INTEGER NOT NULL DEFAULT 0, -- 并排顺序
  fingerprint TEXT NOT NULL DEFAULT '',-- 加入/最近确认时板数据指纹(几何版本+泳道+峰边界)
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparison_targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  comparison_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rf_ref REAL NOT NULL,                -- 参考 Rf
  rf_tol REAL NOT NULL DEFAULT 0.05,   -- 候选匹配容差
  sort REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comparison_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  comparison_id INTEGER NOT NULL,
  target_id INTEGER NOT NULL,
  member_id INTEGER NOT NULL,
  peak_id INTEGER,                     -- NULL = 已拆开(无匹配)
  lane_id INTEGER,
  status TEXT NOT NULL DEFAULT 'auto', -- auto=程序建议 | manual=用户改绑/拆开
  locked INTEGER NOT NULL DEFAULT 0,   -- 1=锁定确认,重配不再改动
  data_fp TEXT NOT NULL DEFAULT '',    -- 该条关系确认时的板数据指纹;与当前指纹不符即失效
  updated_at TEXT NOT NULL,
  UNIQUE(target_id, member_id)
);
CREATE TABLE IF NOT EXISTS calibrations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  target_name TEXT NOT NULL DEFAULT '',
  target_rf REAL,
  rf_tol REAL NOT NULL DEFAULT 0.08,
  conc_unit TEXT NOT NULL DEFAULT 'ng/uL',
  vol_unit TEXT NOT NULL DEFAULT 'uL',
  model TEXT NOT NULL DEFAULT 'linear',
  current_version INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration_lanes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  calibration_id INTEGER NOT NULL,
  lane_id INTEGER NOT NULL,            -- 单板 lanes.id(当前几何版本)
  role TEXT NOT NULL DEFAULT 'unknown',-- standard | blank | unknown
  peak_id INTEGER,                     -- 绑定斑点(峰 id);空白可为空
  concentration REAL,                  -- 标准点样液浓度(标准用)
  volume REAL,                         -- 进样体积(标准/未知用)
  dilution REAL,                       -- 稀释倍数(未知用)
  excluded INTEGER NOT NULL DEFAULT 0, -- 标准点排除
  exclude_reason TEXT NOT NULL DEFAULT '',
  sort REAL NOT NULL DEFAULT 0,
  UNIQUE(calibration_id, lane_id)
);
CREATE TABLE IF NOT EXISTS calibration_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  calibration_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  model TEXT NOT NULL,
  fit TEXT NOT NULL,                   -- 拟合结果 {slope,intercept,r2}
  snapshot TEXT NOT NULL,              -- 成线时完整快照(几何/边界/斑点ID/面积/标注/反算)
  source_fp TEXT NOT NULL,             -- 来源数据指纹;与当前不符即过期
  is_current INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kinetic_series (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  analysis_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  start_at TEXT NOT NULL DEFAULT '',   -- 显色开始时刻(HH:MM[:SS] 或留空按相对秒)
  window_t0 REAL,                      -- 草稿取值窗口(秒)
  window_t1 REAL,
  current_version INTEGER,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kinetic_frames (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,      -- 登记顺序
  image_path TEXT NOT NULL,
  image_width INTEGER NOT NULL DEFAULT 0,
  image_height INTEGER NOT NULL DEFAULT 0,
  image_sha1 TEXT NOT NULL DEFAULT '',
  taken_at TEXT NOT NULL DEFAULT '',   -- 拍摄时刻(钟点串或相对秒数)
  control_points TEXT NOT NULL,        -- [{fx,fy,rx,ry,kind: corner|check}]
  dx REAL NOT NULL DEFAULT 0,          -- 校正空间配准微调
  dy REAL NOT NULL DEFAULT 0,
  excluded INTEGER NOT NULL DEFAULT 0,
  exclude_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kinetic_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  window_t0 REAL NOT NULL,
  window_t1 REAL NOT NULL,
  snapshot TEXT NOT NULL,              -- 成版时完整快照(帧/时刻/配准/逐帧面积/窗口/统计)
  source_fp TEXT NOT NULL,             -- 来源指纹(照片/时刻/配准/板面几何/积分边界);不符即过期
  is_current INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
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
        # 轻量迁移:旧库 comparison_matches 缺少 data_fp 时补列
        # (旧行 data_fp='' => 与当前指纹不符,匹配显示为失效,重新匹配即可恢复)
        cols = {r[1] for r in con.execute("PRAGMA table_info(comparison_matches)")}
        if "data_fp" not in cols:
            con.execute("ALTER TABLE comparison_matches"
                        " ADD COLUMN data_fp TEXT NOT NULL DEFAULT ''")

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


# ---------- 跨板对照 ----------

def create_comparison(con, name):
    cur = con.execute("INSERT INTO comparisons (name, created_at) VALUES (?,?)",
                      (name, now()))
    return cur.lastrowid


def list_comparisons(con):
    rows = con.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM comparison_members m WHERE m.comparison_id=c.id) AS n_members,
                  (SELECT COUNT(*) FROM comparison_targets t WHERE t.comparison_id=c.id) AS n_targets
           FROM comparisons c ORDER BY c.id DESC""").fetchall()
    return [row_to_dict(r) for r in rows]


def get_comparison(con, cid):
    r = con.execute("SELECT * FROM comparisons WHERE id=?", (cid,)).fetchone()
    return row_to_dict(r) if r else None


def add_member(con, cid, analysis_id, std_lane_id, fingerprint):
    pos = con.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM comparison_members WHERE comparison_id=?",
        (cid,)).fetchone()[0]
    cur = con.execute(
        """INSERT INTO comparison_members
           (comparison_id, analysis_id, std_lane_id, position, fingerprint, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, analysis_id, std_lane_id, pos, fingerprint, now()))
    return cur.lastrowid


def get_member(con, mid):
    r = con.execute("SELECT * FROM comparison_members WHERE id=?", (mid,)).fetchone()
    return row_to_dict(r) if r else None


def list_members(con, cid):
    rows = con.execute(
        "SELECT * FROM comparison_members WHERE comparison_id=? ORDER BY position, id",
        (cid,)).fetchall()
    return [row_to_dict(r) for r in rows]


def update_member(con, mid, std_lane_id=None, fingerprint=None):
    if std_lane_id is not None:
        con.execute("UPDATE comparison_members SET std_lane_id=? WHERE id=?",
                    (std_lane_id, mid))
    if fingerprint is not None:
        con.execute("UPDATE comparison_members SET fingerprint=? WHERE id=?",
                    (fingerprint, mid))


def delete_member(con, mid):
    con.execute("DELETE FROM comparison_matches WHERE member_id=?", (mid,))
    con.execute("DELETE FROM comparison_members WHERE id=?", (mid,))


def add_target(con, cid, name, rf_ref, rf_tol):
    pos = con.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 FROM comparison_targets WHERE comparison_id=?",
        (cid,)).fetchone()[0]
    cur = con.execute(
        "INSERT INTO comparison_targets (comparison_id, name, rf_ref, rf_tol, sort)"
        " VALUES (?,?,?,?,?)",
        (cid, name, rf_ref, rf_tol, pos))
    return cur.lastrowid


def update_target(con, tid, name, rf_ref, rf_tol):
    con.execute("UPDATE comparison_targets SET name=?, rf_ref=?, rf_tol=? WHERE id=?",
                (name, rf_ref, rf_tol, tid))


def delete_target(con, tid):
    con.execute("DELETE FROM comparison_matches WHERE target_id=?", (tid,))
    con.execute("DELETE FROM comparison_targets WHERE id=?", (tid,))


def list_targets(con, cid):
    rows = con.execute(
        "SELECT * FROM comparison_targets WHERE comparison_id=? ORDER BY sort, id",
        (cid,)).fetchall()
    return [row_to_dict(r) for r in rows]


def list_matches(con, cid):
    rows = con.execute(
        "SELECT * FROM comparison_matches WHERE comparison_id=?", (cid,)).fetchall()
    return [row_to_dict(r) for r in rows]


def upsert_match(con, cid, target_id, member_id, peak_id, lane_id, status, locked,
                 data_fp=""):
    con.execute(
        """INSERT INTO comparison_matches
           (comparison_id, target_id, member_id, peak_id, lane_id, status, locked,
            data_fp, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(target_id, member_id)
           DO UPDATE SET peak_id=excluded.peak_id, lane_id=excluded.lane_id,
                         status=excluded.status, locked=excluded.locked,
                         data_fp=excluded.data_fp, updated_at=excluded.updated_at""",
        (cid, target_id, member_id, peak_id, lane_id, status, 1 if locked else 0,
         data_fp, now()))


def set_match_lock(con, target_id, member_id, locked):
    con.execute(
        "UPDATE comparison_matches SET locked=?, updated_at=?"
        " WHERE target_id=? AND member_id=?",
        (1 if locked else 0, now(), target_id, member_id))


# ---------- 校准曲线 ----------

def create_calibration(con, analysis_id, name, target_name, target_rf, rf_tol,
                       conc_unit, vol_unit):
    cur = con.execute(
        """INSERT INTO calibrations
           (analysis_id, name, target_name, target_rf, rf_tol, conc_unit, vol_unit,
            model, current_version, created_at)
           VALUES (?,?,?,?,?,?,?,?,NULL,?)""",
        (analysis_id, name, target_name, target_rf, rf_tol, conc_unit, vol_unit,
         "linear", now()))
    return cur.lastrowid


def get_calibration(con, cal_id):
    r = con.execute("SELECT * FROM calibrations WHERE id=?", (cal_id,)).fetchone()
    return row_to_dict(r) if r else None


def list_calibrations(con, analysis_id):
    rows = con.execute(
        """SELECT ca.*,
                  (SELECT COUNT(*) FROM calibration_lanes cl
                   WHERE cl.calibration_id=ca.id AND cl.role='standard') AS n_standard,
                  (SELECT COUNT(*) FROM calibration_versions cv
                   WHERE cv.calibration_id=ca.id) AS n_version
           FROM calibrations ca WHERE analysis_id=? ORDER BY ca.id DESC""",
        (analysis_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


def update_calibration(con, cal_id, fields):
    """更新名称/目标/单位/模型(模型也可由 fit 切换)。fields 中 None 键跳过。"""
    allowed = ("name", "target_name", "target_rf", "rf_tol", "conc_unit", "vol_unit", "model")
    sets, vals = [], []
    for k in allowed:
        if k in fields and fields[k] is not None:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    if sets:
        vals.append(cal_id)
        con.execute(f"UPDATE calibrations SET {', '.join(sets)} WHERE id=?", vals)


def delete_calibration(con, cal_id):
    con.execute("DELETE FROM calibration_versions WHERE calibration_id=?", (cal_id,))
    con.execute("DELETE FROM calibration_lanes WHERE calibration_id=?", (cal_id,))
    con.execute("DELETE FROM calibrations WHERE id=?", (cal_id,))


def list_cal_lanes(con, cal_id):
    rows = con.execute(
        "SELECT * FROM calibration_lanes WHERE calibration_id=? ORDER BY sort, id",
        (cal_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


def replace_cal_lanes(con, cal_id, lanes, live_lane_ids=None, remove_lane_ids=()):
    """全量替换标注泳道(role/绑定/浓度/体积/稀释/排除)。

    live_lane_ids:当前几何版本存在的泳道 id。未在提交列表中的活泳道删除标注;
    已失效的旧泳道(不在此集合)默认保留,供来源追溯,除非其 id 出现在
    remove_lane_ids(用户显式清除失效标注)。
    """
    rows = con.execute(
        "SELECT id, lane_id FROM calibration_lanes WHERE calibration_id=?",
        (cal_id,)).fetchall()
    new_ids = {l["lane_id"] for l in lanes}
    forced = set(remove_lane_ids or ())
    existing = set()
    for r in rows:
        lid = r["lane_id"]
        if lid in new_ids:
            existing.add(lid)
            continue
        if lid in forced or live_lane_ids is None or lid in live_lane_ids:
            con.execute("DELETE FROM calibration_lanes WHERE id=?", (r["id"],))
    for i, l in enumerate(lanes):
        if l["lane_id"] in existing:
            con.execute(
                """UPDATE calibration_lanes SET role=?, peak_id=?, concentration=?, volume=?,
                   dilution=?, excluded=?, exclude_reason=?, sort=?
                   WHERE calibration_id=? AND lane_id=?""",
                (l["role"], l.get("peak_id"), l.get("concentration"), l.get("volume"),
                 l.get("dilution"), 1 if l.get("excluded") else 0,
                 l.get("exclude_reason", ""), i, cal_id, l["lane_id"]))
        else:
            con.execute(
                """INSERT INTO calibration_lanes
                   (calibration_id, lane_id, role, peak_id, concentration, volume, dilution,
                    excluded, exclude_reason, sort)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (cal_id, l["lane_id"], l["role"], l.get("peak_id"),
                 l.get("concentration"), l.get("volume"), l.get("dilution"),
                 1 if l.get("excluded") else 0, l.get("exclude_reason", ""), i))


def get_cal_version(con, vid):
    r = con.execute("SELECT * FROM calibration_versions WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    d = row_to_dict(r)
    d["fit"] = json.loads(d["fit"])
    d["snapshot"] = json.loads(d["snapshot"])
    return d


def list_cal_versions(con, cal_id):
    rows = con.execute(
        "SELECT id, calibration_id, version, model, fit, source_fp, is_current, created_at"
        " FROM calibration_versions WHERE calibration_id=? ORDER BY version", (cal_id,)).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["fit"] = json.loads(d["fit"])
        out.append(d)
    return out


def add_cal_version(con, cal_id, model, fit, snapshot, source_fp):
    """旧版本失效(is_current=0),写入新版本并把 calibrations.current_version 指过去。"""
    old = con.execute(
        "SELECT MAX(version) AS v FROM calibration_versions WHERE calibration_id=?",
        (cal_id,)).fetchone()
    version = (old["v"] or 0) + 1
    con.execute("UPDATE calibration_versions SET is_current=0 WHERE calibration_id=?", (cal_id,))
    cur = con.execute(
        """INSERT INTO calibration_versions
           (calibration_id, version, model, fit, snapshot, source_fp, is_current, created_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        (cal_id, version, model, json.dumps(fit), json.dumps(snapshot, ensure_ascii=False),
         source_fp, now()))
    vid = cur.lastrowid
    con.execute("UPDATE calibrations SET current_version=?, model=? WHERE id=?",
                (vid, model, cal_id))
    return vid, version


# ---------- 显色时间序列 ----------

def create_kinetic_series(con, aid, name="", start_at=""):
    cur = con.execute(
        "INSERT INTO kinetic_series (analysis_id, name, start_at, created_at)"
        " VALUES (?,?,?,?)", (aid, name, start_at, now()))
    return cur.lastrowid


def get_kinetic_series(con, sid):
    r = con.execute("SELECT * FROM kinetic_series WHERE id=?", (sid,)).fetchone()
    return row_to_dict(r) if r else None


def list_kinetic_series(con, aid):
    rows = con.execute(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM kinetic_frames f WHERE f.series_id=s.id) AS n_frames,
                  (SELECT COUNT(*) FROM kinetic_versions v WHERE v.series_id=s.id) AS n_version
           FROM kinetic_series s WHERE analysis_id=? ORDER BY s.id DESC""",
        (aid,)).fetchall()
    return [row_to_dict(r) for r in rows]


def update_kinetic_series(con, sid, fields):
    allowed = ("name", "start_at", "window_t0", "window_t1")
    sets, vals = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    if sets:
        vals.append(sid)
        con.execute(f"UPDATE kinetic_series SET {', '.join(sets)} WHERE id=?", vals)


def delete_kinetic_series(con, sid):
    con.execute("DELETE FROM kinetic_versions WHERE series_id=?", (sid,))
    con.execute("DELETE FROM kinetic_frames WHERE series_id=?", (sid,))
    con.execute("DELETE FROM kinetic_series WHERE id=?", (sid,))


def list_kinetic_frames(con, sid):
    rows = con.execute(
        "SELECT * FROM kinetic_frames WHERE series_id=? ORDER BY seq, id",
        (sid,)).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["control_points"] = json.loads(d["control_points"]) if d["control_points"] else []
        out.append(d)
    return out


def add_kinetic_frame(con, sid, seq, image_path, w, h, sha1, taken_at,
                      control_points, dx=0.0, dy=0.0):
    cur = con.execute(
        """INSERT INTO kinetic_frames
           (series_id, seq, image_path, image_width, image_height, image_sha1,
            taken_at, control_points, dx, dy, excluded, exclude_reason, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,'',?)""",
        (sid, seq, image_path, w, h, sha1, taken_at,
         json.dumps(control_points, ensure_ascii=False),
         float(dx), float(dy), now()))
    return cur.lastrowid


def get_kinetic_frame(con, fid):
    r = con.execute("SELECT * FROM kinetic_frames WHERE id=?", (fid,)).fetchone()
    if not r:
        return None
    d = row_to_dict(r)
    d["control_points"] = json.loads(d["control_points"]) if d["control_points"] else []
    return d


def update_kinetic_frame(con, fid, fields):
    allowed = ("taken_at", "dx", "dy", "excluded", "exclude_reason", "seq")
    sets, vals = [], []
    for k in allowed:
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    if "control_points" in fields:
        sets.append("control_points=?")
        vals.append(json.dumps(fields["control_points"], ensure_ascii=False))
    if sets:
        vals.append(fid)
        con.execute(f"UPDATE kinetic_frames SET {', '.join(sets)} WHERE id=?", vals)


def delete_kinetic_frame(con, fid):
    con.execute("DELETE FROM kinetic_frames WHERE id=?", (fid,))


def add_kinetic_version(con, sid, t0, t1, snapshot, source_fp):
    """旧版本失效(is_current=0),写入新版本并把 series.current_version 指过去。"""
    version = (con.execute(
        "SELECT COALESCE(MAX(version),0)+1 FROM kinetic_versions WHERE series_id=?",
        (sid,)).fetchone()[0])
    con.execute("UPDATE kinetic_versions SET is_current=0 WHERE series_id=?", (sid,))
    cur = con.execute(
        """INSERT INTO kinetic_versions
           (series_id, version, window_t0, window_t1, snapshot, source_fp,
            is_current, created_at)
           VALUES (?,?,?,?,?,?,1,?)""",
        (sid, version, t0, t1, json.dumps(snapshot, ensure_ascii=False),
         source_fp, now()))
    vid = cur.lastrowid
    con.execute("UPDATE kinetic_series SET current_version=?, window_t0=?, window_t1=?"
                " WHERE id=?", (vid, t0, t1, sid))
    return vid, version


def get_kinetic_version(con, vid):
    r = con.execute("SELECT * FROM kinetic_versions WHERE id=?", (vid,)).fetchone()
    if not r:
        return None
    d = row_to_dict(r)
    d["snapshot"] = json.loads(d["snapshot"])
    return d


def list_kinetic_versions(con, sid):
    rows = con.execute(
        "SELECT id, series_id, version, window_t0, window_t1, source_fp, is_current,"
        " created_at FROM kinetic_versions WHERE series_id=? ORDER BY version",
        (sid,)).fetchall()
    return [row_to_dict(r) for r in rows]
