# -*- coding: utf-8 -*-
"""
跨校碳中和平台 MVP（依 ISO 14064-2 精神設計）
=================================================
安裝套件（Anaconda Prompt）：
    pip install streamlit pandas
執行方式：
    streamlit run app.py

七大核心模組：
    1. 專案邊界   2. 基準線情境   3. 溫室氣體來源／匯／庫辨識
    4. 監測計畫   5. 減排或增匯量化   6. 不確定性   7. 證據與版本紀錄
另有「專案總覽」頁面，用來建立專案（產生 UUID，串聯所有資料表）。

資料庫：SQLite（與 app.py 同資料夾的 carbon_platform.db，第一次啟動自動建立）
證據檔案：與 app.py 同資料夾的 evidence_files/
"""

import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 0. 基本設定
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "carbon_platform.db"
EVIDENCE_DIR = BASE_DIR / "evidence_files"

# ---------------------------------------------------------------------------
# 1. 資料庫 Schema（與 init_db.sql 內容相同，啟動時自動執行）
#    設計原則：
#    - 所有主鍵皆為 UUID（TEXT），所有業務表以 project_id 串聯專案
#    - 每張表都有 version / created_at / updated_at，每次修改版本 +1
#    - audit_log 記錄每一次新增／修改／刪除的前後內容（JSON 快照）
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

-- 專案主檔（對應舊系統 pb_investigate / pb_company / pb_factorylist）
CREATE TABLE IF NOT EXISTS projects (
    project_id    TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    organization  TEXT,
    description   TEXT,
    gwp_version   TEXT DEFAULT 'AR6',
    period_start  TEXT,
    period_end    TEXT,
    status        TEXT DEFAULT '規劃中',
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 模組1：專案邊界
CREATE TABLE IF NOT EXISTS project_boundaries (
    boundary_id    TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    boundary_type  TEXT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT,
    is_included    INTEGER NOT NULL DEFAULT 1,
    justification  TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 模組2：基準線情境
CREATE TABLE IF NOT EXISTS baseline_scenarios (
    baseline_id               TEXT PRIMARY KEY,
    project_id                TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    scenario_name             TEXT NOT NULL,
    description               TEXT,
    alternatives_considered   TEXT,
    selection_rationale       TEXT,
    baseline_year             INTEGER,
    activity_data             REAL NOT NULL DEFAULT 0,
    activity_unit             TEXT,
    emission_factor           REAL NOT NULL DEFAULT 0,   -- kgCO2e / 活動數據單位
    ef_source                 TEXT,
    baseline_emission_tco2e   REAL NOT NULL DEFAULT 0,   -- = 活動數據 x 係數 / 1000
    version                   INTEGER NOT NULL DEFAULT 1,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

-- 模組3：溫室氣體來源／匯／庫（SSR）辨識
CREATE TABLE IF NOT EXISTS ghg_items (
    item_id        TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    boundary_id    TEXT REFERENCES project_boundaries(boundary_id) ON DELETE SET NULL,
    item_type      TEXT NOT NULL CHECK (item_type IN ('來源','匯','庫')),
    ssr_category   TEXT,
    scenario_scope TEXT,
    name           TEXT NOT NULL,
    gas_type       TEXT,
    description    TEXT,
    is_included    INTEGER NOT NULL DEFAULT 1,
    justification  TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 模組4-a：監測計畫
CREATE TABLE IF NOT EXISTS monitoring_plans (
    plan_id         TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    item_id         TEXT REFERENCES ghg_items(item_id) ON DELETE SET NULL,
    parameter_name  TEXT NOT NULL,
    unit            TEXT,
    data_source     TEXT,
    method          TEXT,
    instrument      TEXT,
    frequency       TEXT,
    responsible     TEXT,
    qa_qc           TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 模組4-b：監測數據紀錄（實際監測量，供量化使用）
CREATE TABLE IF NOT EXISTS monitoring_records (
    record_id        TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    plan_id          TEXT NOT NULL REFERENCES monitoring_plans(plan_id) ON DELETE CASCADE,
    period_label     TEXT NOT NULL,                      -- 例：2026-Q1
    activity_value   REAL NOT NULL DEFAULT 0,
    activity_unit    TEXT,
    emission_factor  REAL NOT NULL DEFAULT 0,            -- kgCO2e / 活動數據單位
    ef_source        TEXT,
    measured_at      TEXT,
    emission_tco2e   REAL NOT NULL DEFAULT 0,            -- = 活動數據 x 係數 / 1000
    note             TEXT,
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 模組5：減排或增匯量化（保存當下基準線的快照，確保歷史結果不被事後改動影響）
CREATE TABLE IF NOT EXISTS quantifications (
    quant_id                 TEXT PRIMARY KEY,
    project_id               TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    baseline_id              TEXT REFERENCES baseline_scenarios(baseline_id) ON DELETE SET NULL,
    period_label             TEXT NOT NULL,
    baseline_emission_tco2e  REAL NOT NULL DEFAULT 0,
    project_emission_tco2e   REAL NOT NULL DEFAULT 0,
    leakage_tco2e            REAL NOT NULL DEFAULT 0,
    reduction_tco2e          REAL NOT NULL DEFAULT 0,    -- = 基準線 - 專案 - 洩漏
    calc_mode                TEXT,
    note                     TEXT,
    version                  INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

-- 模組6：不確定性（對應舊系統 pb_ref_co2_rpt_4_8 的活動數據／係數上下限概念）
CREATE TABLE IF NOT EXISTS uncertainty_items (
    uncertainty_id        TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    item_name             TEXT NOT NULL,
    applies_to            TEXT,
    emission_tco2e        REAL NOT NULL DEFAULT 0,
    activity_uncert_pct   REAL NOT NULL DEFAULT 0,
    ef_uncert_pct         REAL NOT NULL DEFAULT 0,
    combined_uncert_pct   REAL NOT NULL DEFAULT 0,       -- = sqrt(a^2 + e^2)
    abs_uncert_tco2e      REAL NOT NULL DEFAULT 0,       -- = 排放量 x 合併不確定性 / 100
    method                TEXT,
    source_description    TEXT,
    mitigation            TEXT,
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

-- 模組7-a：證據（檔案或 URL，可連結到任一模組的任一筆紀錄）
CREATE TABLE IF NOT EXISTS evidence (
    evidence_id    TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    module_table   TEXT,
    ref_record_id  TEXT,
    title          TEXT NOT NULL,
    evidence_type  TEXT,
    file_name      TEXT,
    file_path      TEXT,
    file_sha256    TEXT,
    file_size      INTEGER,
    url            TEXT,
    description    TEXT,
    uploaded_by    TEXT,
    version        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 模組7-b：稽核／版本紀錄（對應舊系統 Event_Log；不設外鍵，專案刪除後紀錄仍保留）
CREATE TABLE IF NOT EXISTS audit_log (
    log_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT,
    table_name   TEXT NOT NULL,
    record_id    TEXT NOT NULL,
    action       TEXT NOT NULL CHECK (action IN ('INSERT','UPDATE','DELETE')),
    old_value    TEXT,
    new_value    TEXT,
    operator     TEXT,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_boundaries_project   ON project_boundaries(project_id);
CREATE INDEX IF NOT EXISTS idx_baseline_project     ON baseline_scenarios(project_id);
CREATE INDEX IF NOT EXISTS idx_ghg_project          ON ghg_items(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_project        ON monitoring_plans(project_id);
CREATE INDEX IF NOT EXISTS idx_records_project      ON monitoring_records(project_id, period_label);
CREATE INDEX IF NOT EXISTS idx_quant_project        ON quantifications(project_id);
CREATE INDEX IF NOT EXISTS idx_uncert_project       ON uncertainty_items(project_id);
CREATE INDEX IF NOT EXISTS idx_evidence_project     ON evidence(project_id);
CREATE INDEX IF NOT EXISTS idx_audit_project        ON audit_log(project_id, table_name, record_id);
"""


# ---------------------------------------------------------------------------
# 2. 資料庫共用函式
# ---------------------------------------------------------------------------
def now_str() -> str:
    """取得目前時間字串（ISO 格式，精確到秒）。"""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


@contextmanager
def db():
    """開啟連線的 context manager：成功自動 commit、失敗自動 rollback，最後一定關閉。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row            # 讓查詢結果可用欄位名稱取值
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite 預設不啟用外鍵，需每個連線開啟
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """啟動時自動檢查並建立資料表（CREATE TABLE IF NOT EXISTS，可重複執行）。"""
    EVIDENCE_DIR.mkdir(exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA_SQL)


def fetch_all(sql: str, params: tuple = ()) -> list:
    """查詢多筆資料，回傳 list[dict]。"""
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def write_audit(conn, project_id, table, record_id, action, old, new, operator) -> None:
    """寫入稽核紀錄：保存修改前後的完整 JSON 快照，作為版本追蹤依據。"""
    conn.execute(
        "INSERT INTO audit_log (project_id, table_name, record_id, action, old_value, new_value, operator, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            project_id, table, record_id, action,
            json.dumps(old, ensure_ascii=False, default=str) if old is not None else None,
            json.dumps(new, ensure_ascii=False, default=str) if new is not None else None,
            operator or "unknown", now_str(),
        ),
    )


def insert_record(cfg: dict, project_id, vals: dict, operator: str) -> str:
    """新增一筆資料（自動產生 UUID、版本=1、時間戳記，並寫入 audit_log）。"""
    ts = now_str()
    rid = str(uuid.uuid4())
    row = {cfg["pk"]: rid}
    if cfg["scoped"]:
        row["project_id"] = project_id
    row.update(vals)
    if cfg.get("compute"):
        row.update(cfg["compute"](vals))          # 計算欄位（例如排放量）
    row.update(version=1, created_at=ts, updated_at=ts)
    cols = ", ".join(row.keys())
    marks = ", ".join("?" for _ in row)
    with db() as conn:
        conn.execute(f"INSERT INTO {cfg['table']} ({cols}) VALUES ({marks})", tuple(row.values()))
        write_audit(conn, project_id if cfg["scoped"] else rid, cfg["table"], rid, "INSERT", None, row, operator)
    return rid


def update_record(cfg: dict, rid: str, project_id, vals: dict, operator: str) -> None:
    """修改一筆資料：版本 +1、更新時間，並記錄修改前後內容。"""
    table, pk = cfg["table"], cfg["pk"]
    with db() as conn:
        old = dict(conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (rid,)).fetchone())
        new_vals = dict(vals)
        if cfg.get("compute"):
            new_vals.update(cfg["compute"](vals))
        new_vals["version"] = old["version"] + 1
        new_vals["updated_at"] = now_str()
        sets = ", ".join(f"{k}=?" for k in new_vals)
        conn.execute(f"UPDATE {table} SET {sets} WHERE {pk}=?", (*new_vals.values(), rid))
        new = dict(conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (rid,)).fetchone())
        write_audit(conn, project_id if cfg["scoped"] else rid, table, rid, "UPDATE", old, new, operator)


def delete_record(cfg: dict, rid: str, project_id, operator: str) -> None:
    """刪除一筆資料；刪除前的完整內容會保存在 audit_log。"""
    table, pk = cfg["table"], cfg["pk"]
    with db() as conn:
        old = dict(conn.execute(f"SELECT * FROM {table} WHERE {pk}=?", (rid,)).fetchone())
        conn.execute(f"DELETE FROM {table} WHERE {pk}=?", (rid,))
        write_audit(conn, project_id if cfg["scoped"] else rid, table, rid, "DELETE", old, None, operator)


# ---------------------------------------------------------------------------
# 3. 各模組的欄位設定（泛用 CRUD 會依此自動產生表單與表格）
# ---------------------------------------------------------------------------
def fk_options(table: str, pk: str, label_sql: str):
    """產生「外鍵下拉選單」的選項函式：回傳 [(id, 顯示文字), ...]。"""
    def _opts(project_id):
        rows = fetch_all(
            f"SELECT {pk} AS id, {label_sql} AS label FROM {table} WHERE project_id=? ORDER BY created_at",
            (project_id,),
        )
        return [(r["id"], r["label"]) for r in rows]
    return _opts


GAS_OPTIONS = ["CO2", "CH4", "N2O", "HFCs", "PFCs", "SF6", "NF3", "多種氣體"]

CFG_PROJECTS = dict(
    table="projects", pk="project_id", scoped=False, label_field="name",
    fields=[
        dict(name="name", label="專案名稱", type="text", required=True),
        dict(name="organization", label="執行單位／學校", type="text"),
        dict(name="description", label="專案說明", type="area"),
        dict(name="gwp_version", label="GWP 版本", type="select", options=["AR4", "AR5", "AR6"], default="AR6"),
        dict(name="period_start", label="減碳期間起", type="date"),
        dict(name="period_end", label="減碳期間迄", type="date"),
        dict(name="status", label="專案狀態", type="select", options=["規劃中", "執行中", "已完成", "已終止"]),
    ],
)

CFG_BOUNDARY = dict(
    table="project_boundaries", pk="boundary_id", scoped=True, label_field="name",
    fields=[
        dict(name="boundary_type", label="邊界類型", type="select",
             options=["組織邊界", "地理邊界", "營運邊界", "技術／設備邊界", "時間邊界"]),
        dict(name="name", label="邊界項目名稱", type="text", required=True),
        dict(name="description", label="範圍描述", type="area"),
        dict(name="is_included", label="納入專案邊界", type="bool", default=True),
        dict(name="justification", label="納入／排除理由", type="area"),
    ],
)

CFG_BASELINE = dict(
    table="baseline_scenarios", pk="baseline_id", scoped=True, label_field="scenario_name",
    fields=[
        dict(name="scenario_name", label="基準線情境名稱", type="text", required=True),
        dict(name="description", label="情境描述（若無本專案時會發生什麼）", type="area"),
        dict(name="alternatives_considered", label="考慮過的替代情境", type="area"),
        dict(name="selection_rationale", label="選定此基準線的理由", type="area"),
        dict(name="baseline_year", label="基準年", type="int", default=date.today().year, min=1990, max=2100),
        dict(name="activity_data", label="基準線活動數據", type="number", min=0.0, step=1.0),
        dict(name="activity_unit", label="活動數據單位（如 kWh）", type="text"),
        dict(name="emission_factor", label="排放係數（kgCO2e／單位）", type="number", min=0.0, step=0.001, format="%.6f"),
        dict(name="ef_source", label="係數來源", type="text"),
    ],
    computed_cols={"baseline_emission_tco2e": "基準線排放量 (tCO2e)"},
    # 排放量(tCO2e) = 活動數據 x 排放係數(kgCO2e/單位) / 1000
    compute=lambda v: {"baseline_emission_tco2e": round(v["activity_data"] * v["emission_factor"] / 1000, 6)},
)

CFG_GHG = dict(
    table="ghg_items", pk="item_id", scoped=True, label_field="name",
    fields=[
        dict(name="item_type", label="類別（來源／匯／庫）", type="select", options=["來源", "匯", "庫"]),
        dict(name="ssr_category", label="ISO 14064-2 SSR 分類", type="select",
             options=["受控 (Controlled)", "相關 (Related)", "受影響 (Affected)"]),
        dict(name="scenario_scope", label="適用情境", type="select", options=["基準線情境", "專案情境", "兩者皆有"]),
        dict(name="name", label="名稱（如：校園空調用電）", type="text", required=True),
        dict(name="gas_type", label="主要溫室氣體", type="select", options=GAS_OPTIONS),
        dict(name="boundary_id", label="所屬邊界項目", type="fk",
             options=fk_options("project_boundaries", "boundary_id", "name")),
        dict(name="description", label="說明", type="area"),
        dict(name="is_included", label="納入量化", type="bool", default=True),
        dict(name="justification", label="納入／排除理由", type="area"),
    ],
)

CFG_PLAN = dict(
    table="monitoring_plans", pk="plan_id", scoped=True, label_field="parameter_name",
    fields=[
        dict(name="parameter_name", label="監測參數（如：用電量）", type="text", required=True),
        dict(name="item_id", label="對應的來源／匯／庫", type="fk",
             options=fk_options("ghg_items", "item_id", "item_type || '：' || name")),
        dict(name="unit", label="單位", type="text"),
        dict(name="data_source", label="數據來源", type="text"),
        dict(name="method", label="監測／量測方法", type="area"),
        dict(name="instrument", label="儀器設備", type="text"),
        dict(name="frequency", label="監測頻率", type="select",
             options=["連續", "每日", "每週", "每月", "每季", "每年", "其他"]),
        dict(name="responsible", label="負責人員／單位", type="text"),
        dict(name="qa_qc", label="品保／品管措施（校正、查核）", type="area"),
    ],
)

CFG_RECORD = dict(
    table="monitoring_records", pk="record_id", scoped=True, label_field="period_label",
    fields=[
        dict(name="plan_id", label="對應的監測參數", type="fk", required=True,
             options=fk_options("monitoring_plans", "plan_id", "parameter_name")),
        dict(name="period_label", label="監測期間（如 2026-Q1）", type="text", required=True),
        dict(name="activity_value", label="實際監測活動量", type="number", min=0.0, step=1.0),
        dict(name="activity_unit", label="活動量單位", type="text"),
        dict(name="emission_factor", label="排放係數（kgCO2e／單位）", type="number", min=0.0, step=0.001, format="%.6f"),
        dict(name="ef_source", label="係數來源", type="text"),
        dict(name="measured_at", label="量測日期", type="date"),
        dict(name="note", label="備註", type="area"),
    ],
    computed_cols={"emission_tco2e": "監測排放量 (tCO2e)"},
    compute=lambda v: {"emission_tco2e": round(v["activity_value"] * v["emission_factor"] / 1000, 6)},
)

CFG_QUANT = dict(  # 量化頁面為客製化表單，這裡只提供資料表資訊給 insert/delete 使用
    table="quantifications", pk="quant_id", scoped=True, label_field="period_label", fields=[],
)

CFG_UNCERT = dict(
    table="uncertainty_items", pk="uncertainty_id", scoped=True, label_field="item_name",
    fields=[
        dict(name="item_name", label="不確定性項目名稱", type="text", required=True),
        dict(name="applies_to", label="影響對象", type="select", options=["基準線", "專案", "洩漏", "其他"]),
        dict(name="emission_tco2e", label="該項目排放量 (tCO2e)", type="number", min=0.0, step=1.0),
        dict(name="activity_uncert_pct", label="活動數據不確定性 (±%)", type="number", min=0.0, step=0.5),
        dict(name="ef_uncert_pct", label="排放係數不確定性 (±%)", type="number", min=0.0, step=0.5),
        dict(name="method", label="評估方法", type="select",
             options=["誤差傳遞", "儀器規格", "專家判斷", "蒙地卡羅模擬", "其他"]),
        dict(name="source_description", label="不確定性來源說明", type="area"),
        dict(name="mitigation", label="降低不確定性的措施", type="area"),
    ],
    computed_cols={
        "combined_uncert_pct": "合併不確定性 (±%)",
        "abs_uncert_tco2e": "絕對不確定性 (±tCO2e)",
    },
    # 乘法關係（活動數據 x 係數）：合併不確定性 = sqrt(a^2 + e^2)
    compute=lambda v: {
        "combined_uncert_pct": round(math.sqrt(v["activity_uncert_pct"] ** 2 + v["ef_uncert_pct"] ** 2), 4),
        "abs_uncert_tco2e": round(
            v["emission_tco2e"] * math.sqrt(v["activity_uncert_pct"] ** 2 + v["ef_uncert_pct"] ** 2) / 100, 6),
    },
)

CFG_EVIDENCE = dict(table="evidence", pk="evidence_id", scoped=True, label_field="title", fields=[])

# 模組名稱對照（證據連結、稽核紀錄篩選時使用）
MODULE_TABLES = {
    "project_boundaries": ("① 專案邊界", CFG_BOUNDARY),
    "baseline_scenarios": ("② 基準線情境", CFG_BASELINE),
    "ghg_items": ("③ 來源／匯／庫", CFG_GHG),
    "monitoring_plans": ("④-a 監測計畫", CFG_PLAN),
    "monitoring_records": ("④-b 監測數據", CFG_RECORD),
    "quantifications": ("⑤ 減排量化", CFG_QUANT),
    "uncertainty_items": ("⑥ 不確定性", CFG_UNCERT),
}
TABLE_LABELS = {"projects": "專案", "evidence": "⑦ 證據", **{k: v[0] for k, v in MODULE_TABLES.items()}}


# ---------------------------------------------------------------------------
# 4. 泛用 CRUD 元件（依欄位設定自動產生：新增表單 / 資料表 / 修改刪除）
# ---------------------------------------------------------------------------
def flash(msg: str) -> None:
    """暫存提示訊息：st.rerun() 之後仍能顯示一次。"""
    st.session_state["_flash"] = msg


def show_flash() -> None:
    msg = st.session_state.pop("_flash", None)
    if msg:
        st.success(msg)


def is_empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def render_field(f: dict, current, key: str, project_id):
    """依欄位型別畫出對應的輸入元件；current 為既有值（修改時帶入）。"""
    t = f["type"]
    label = f["label"] + (" *" if f.get("required") else "")

    if t == "text":
        return st.text_input(label, value=current or "", key=key)
    if t == "area":
        return st.text_area(label, value=current or "", key=key)
    if t == "number":
        v = current if current is not None else f.get("default", 0.0)
        return st.number_input(label, value=float(v), min_value=f.get("min"), step=f.get("step", 1.0),
                               format=f.get("format", "%.4f"), key=key)
    if t == "int":
        v = int(current) if current is not None else int(f.get("default", 0))
        return st.number_input(label, value=v, min_value=f.get("min"), max_value=f.get("max"),
                               step=1, format="%d", key=key)
    if t == "select":
        opts = f["options"]
        default = current if current in opts else f.get("default", opts[0])
        return st.selectbox(label, opts, index=opts.index(default), key=key)
    if t == "fk":
        pairs = f["options"](project_id)                     # [(id, 顯示文字)]
        ids = [p[0] for p in pairs]
        names = {p[0]: p[1] for p in pairs}
        if not f.get("required"):
            ids = [None] + ids
            names[None] = "（無）"
        if not ids:
            st.warning(f"「{f['label']}」尚無可選資料，請先到對應頁面建立。")
            return None
        idx = ids.index(current) if current in ids else 0
        return st.selectbox(label, ids, index=idx, format_func=lambda x: names[x], key=key)
    if t == "date":
        try:
            d = date.fromisoformat(current) if current else date.today()
        except ValueError:
            d = date.today()
        return st.date_input(label, value=d, key=key).isoformat()
    if t == "bool":
        v = bool(current) if current is not None else f.get("default", True)
        return 1 if st.checkbox(label, value=v, key=key) else 0
    raise ValueError(f"未知欄位型別：{t}")


def validate(fields: list, vals: dict) -> list:
    """檢查必填欄位，回傳錯誤訊息清單。"""
    return [f"「{f['label']}」為必填" for f in fields if f.get("required") and is_empty(vals.get(f["name"]))]


def build_display_df(cfg: dict, rows: list, project_id) -> pd.DataFrame:
    """把資料列轉成適合顯示的 DataFrame：欄位改中文、外鍵改顯示名稱、布林改圖示。"""
    df = pd.DataFrame(rows)
    order, rename = [], {}
    for f in cfg["fields"]:
        col = f["name"]
        order.append(col)
        rename[col] = f["label"].split("（")[0]
        if f["type"] == "fk":
            names = {p[0]: p[1] for p in f["options"](project_id)}
            df[col] = df[col].map(lambda x, n=names: n.get(x, "") if x else "")
        elif f["type"] == "bool":
            df[col] = df[col].map(lambda x: "✅" if x else "❌")
    for col, label in cfg.get("computed_cols", {}).items():
        order.append(col)
        rename[col] = label
    order += ["version", "updated_at"]
    rename.update(version="版本", updated_at="最後更新")
    return df[order].rename(columns=rename)


def crud_section(cfg: dict, project_id, operator: str) -> None:
    """泛用 CRUD：新增表單 → 資料表 → 修改／刪除。"""
    table, pk = cfg["table"], cfg["pk"]
    fields = cfg["fields"]

    # ---- (1) 新增表單 ----
    with st.expander("➕ 新增資料", expanded=True):
        with st.form(f"{table}_add_form", clear_on_submit=True):
            vals = {f["name"]: render_field(f, None, f"{table}_add_{f['name']}", project_id) for f in fields}
            submitted = st.form_submit_button("新增")
        if submitted:
            errors = validate(fields, vals)
            if errors:
                st.error("；".join(errors))
            else:
                insert_record(cfg, project_id, vals, operator)
                flash("✅ 已新增資料，並寫入稽核紀錄")
                st.rerun()

    # ---- (2) 既有資料列表 ----
    if cfg["scoped"]:
        rows = fetch_all(f"SELECT * FROM {table} WHERE project_id=? ORDER BY created_at", (project_id,))
    else:
        rows = fetch_all(f"SELECT * FROM {table} ORDER BY created_at")
    st.subheader("📋 既有資料")
    if not rows:
        st.info("目前沒有資料。")
        return
    st.dataframe(build_display_df(cfg, rows, project_id), hide_index=True)

    # ---- (3) 修改／刪除 ----
    with st.expander("✏️ 修改 / 🗑️ 刪除"):
        by_id = {r[pk]: r for r in rows}
        rid = st.selectbox(
            "選擇要處理的資料", list(by_id.keys()), key=f"{table}_pick",
            format_func=lambda x: f"{by_id[x][cfg['label_field']]}（v{by_id[x]['version']}｜{x[:8]}）",
        )
        cur = by_id[rid]
        with st.form(f"{table}_edit_form_{rid}"):   # key 含 rid：切換資料時表單會重新帶入預設值
            new_vals = {f["name"]: render_field(f, cur.get(f["name"]), f"{table}_edit_{rid}_{f['name']}", project_id)
                        for f in fields}
            save = st.form_submit_button("💾 儲存修改（版本 +1）")
        if save:
            errors = validate(fields, new_vals)
            if errors:
                st.error("；".join(errors))
            else:
                update_record(cfg, rid, project_id, new_vals, operator)
                flash(f"✅ 已更新，版本升為 v{cur['version'] + 1}")
                st.rerun()

        st.divider()
        confirm = st.checkbox("我確認要刪除這筆資料（刪除前內容會保留在稽核紀錄）", key=f"{table}_delconf_{rid}")
        if st.button("🗑️ 刪除", key=f"{table}_delbtn_{rid}"):
            if confirm:
                delete_record(cfg, rid, project_id, operator)
                flash("🗑️ 已刪除資料")
                st.rerun()
            else:
                st.warning("請先勾選確認。")


# ---------------------------------------------------------------------------
# 5. 各頁面
# ---------------------------------------------------------------------------
def page_overview(pid, operator):
    st.header("🏠 專案總覽")
    st.caption("先在此建立專案（系統自動產生 UUID 作為專案識別碼），其他 7 個模組的資料都會綁定在所選專案底下。")
    crud_section(CFG_PROJECTS, pid, operator)

    if pid:   # 目前專案的各模組資料筆數
        st.divider()
        st.subheader("📊 目前專案資料統計")
        st.caption(f"專案 UUID：`{pid}`")
        tables = list(MODULE_TABLES.keys()) + ["evidence"]
        cols = st.columns(4)
        for i, t in enumerate(tables):
            n = fetch_all(f"SELECT COUNT(*) AS n FROM {t} WHERE project_id=?", (pid,))[0]["n"]
            cols[i % 4].metric(TABLE_LABELS[t], n)


def page_boundary(pid, operator):
    st.header("① 專案邊界 (Project Boundary)")
    st.caption("定義專案涵蓋的組織、地理、營運、技術與時間範圍，並說明納入／排除理由。")
    crud_section(CFG_BOUNDARY, pid, operator)


def page_baseline(pid, operator):
    st.header("② 基準線情境 (Baseline Scenario)")
    st.caption("描述「若沒有本減碳專案」最可能發生的情境，並以「活動數據 × 排放係數」估算基準線排放量。")
    crud_section(CFG_BASELINE, pid, operator)


def page_ghg(pid, operator):
    st.header("③ 溫室氣體來源／匯／庫辨識 (GHG Sources / Sinks / Reservoirs)")
    st.caption("辨識與專案相關的 SSR，並依 ISO 14064-2 分為受控、相關、受影響三類。")
    crud_section(CFG_GHG, pid, operator)


def page_monitoring(pid, operator):
    st.header("④ 監測計畫 (Monitoring Plan)")
    tab1, tab2 = st.tabs(["📐 監測計畫（要監測什麼、怎麼監測）", "📈 監測數據紀錄（實際量測結果）"])
    with tab1:
        crud_section(CFG_PLAN, pid, operator)
    with tab2:
        st.caption("這裡輸入的實際監測量，會在「⑤ 減排量化」頁面彙總為專案排放量。")
        crud_section(CFG_RECORD, pid, operator)


def page_quantification(pid, operator):
    st.header("⑤ 減排或增匯量化 (Quantification)")
    st.info("計算公式：**實際減排量 = 基準線排放量 − 專案實際監測排放量 − 洩漏量**（單位：tCO2e）")

    baselines = fetch_all("SELECT * FROM baseline_scenarios WHERE project_id=? ORDER BY created_at", (pid,))
    if not baselines:
        st.warning("請先到「② 基準線情境」建立基準線。")
    else:
        # --- 選項放在 form 外面，才能即時顯示預覽 ---
        bmap = {b["baseline_id"]: b for b in baselines}
        bid = st.selectbox("選擇基準線情境", list(bmap.keys()),
                           format_func=lambda x: f"{bmap[x]['scenario_name']}（{bmap[x]['baseline_emission_tco2e']:,.3f} tCO2e）")
        base_val = bmap[bid]["baseline_emission_tco2e"]

        periods = fetch_all(
            "SELECT period_label, ROUND(SUM(emission_tco2e), 6) AS total, COUNT(*) AS n "
            "FROM monitoring_records WHERE project_id=? GROUP BY period_label ORDER BY period_label", (pid,))
        modes = ["自動彙總該期間的監測數據", "手動輸入專案排放量"]
        mode = st.radio("專案排放量來源", modes, horizontal=True)

        period_label, project_val = None, 0.0
        if mode == modes[0]:
            if not periods:
                st.warning("尚無監測數據，請到「④ 監測計畫 → 監測數據紀錄」輸入，或改用手動輸入。")
            else:
                pmap = {p["period_label"]: p for p in periods}
                period_label = st.selectbox(
                    "選擇監測期間", list(pmap.keys()),
                    format_func=lambda x: f"{x}（{pmap[x]['n']} 筆，合計 {pmap[x]['total']:,.3f} tCO2e）")
                project_val = pmap[period_label]["total"]

        c1, c2, c3 = st.columns(3)
        c1.metric("基準線排放量 (tCO2e)", f"{base_val:,.3f}")
        if mode == modes[0]:
            c2.metric("監測排放量 (tCO2e)", f"{project_val:,.3f}")
            c3.metric("預覽減排量（未含洩漏）", f"{base_val - project_val:,.3f}")

        with st.form("quant_form", clear_on_submit=True):
            if mode == modes[1]:
                period_label = st.text_input("期間標籤（如 2026-Q1）*")
                project_val = st.number_input("專案實際排放量 (tCO2e)", min_value=0.0, step=1.0, format="%.4f")
            leakage = st.number_input("洩漏量 (tCO2e)（專案導致邊界外增加的排放）", min_value=0.0, step=1.0, format="%.4f")
            note = st.text_area("備註")
            ok = st.form_submit_button("計算並儲存")
        if ok:
            if is_empty(period_label):
                st.error("請填寫或選擇期間")
            else:
                reduction = round(base_val - project_val - leakage, 6)
                vals = dict(
                    baseline_id=bid, period_label=period_label.strip(),
                    baseline_emission_tco2e=base_val,      # 保存當下基準線快照
                    project_emission_tco2e=project_val, leakage_tco2e=leakage,
                    reduction_tco2e=reduction, calc_mode=mode, note=note,
                )
                insert_record(CFG_QUANT, pid, vals, operator)
                flash(f"✅ 已儲存：{period_label} 實際減排量 {reduction:,.3f} tCO2e")
                st.rerun()

    # --- 結果列表、指標、圖表 ---
    rows = fetch_all("SELECT * FROM quantifications WHERE project_id=? ORDER BY created_at", (pid,))
    st.subheader("📋 量化結果")
    if not rows:
        st.info("目前沒有量化結果。")
        return
    df = pd.DataFrame(rows)
    show = df[["period_label", "baseline_emission_tco2e", "project_emission_tco2e", "leakage_tco2e",
               "reduction_tco2e", "calc_mode", "note", "version", "updated_at"]]
    show.columns = ["期間", "基準線 (tCO2e)", "專案 (tCO2e)", "洩漏 (tCO2e)", "減排量 (tCO2e)",
                    "計算方式", "備註", "版本", "最後更新"]
    st.dataframe(show, hide_index=True)

    tb, tp, tr = df["baseline_emission_tco2e"].sum(), df["project_emission_tco2e"].sum(), df["reduction_tco2e"].sum()
    m1, m2, m3 = st.columns(3)
    m1.metric("累計基準線排放量", f"{tb:,.3f} tCO2e")
    m2.metric("累計專案排放量", f"{tp:,.3f} tCO2e")
    m3.metric("累計實際減排量", f"{tr:,.3f} tCO2e",
              delta=f"{tr / tb * 100:.1f}% of 基準線" if tb else None)
    if (df["reduction_tco2e"] < 0).any():
        st.warning("部分期間的減排量為負值（專案排放高於基準線），請檢查數據。")

    chart = df.groupby("period_label")[["baseline_emission_tco2e", "project_emission_tco2e", "reduction_tco2e"]].sum()
    chart.columns = ["基準線", "專案", "減排量"]
    st.bar_chart(chart)

    with st.expander("🗑️ 刪除量化結果"):
        by_id = {r["quant_id"]: r for r in rows}
        qid = st.selectbox("選擇要刪除的結果", list(by_id.keys()), key="quant_del_pick",
                           format_func=lambda x: f"{by_id[x]['period_label']}（{by_id[x]['reduction_tco2e']:,.3f} tCO2e｜{x[:8]}）")
        ok_del = st.checkbox("我確認要刪除", key="quant_del_conf")
        if st.button("刪除", key="quant_del_btn"):
            if ok_del:
                delete_record(CFG_QUANT, qid, pid, operator)
                flash("🗑️ 已刪除量化結果")
                st.rerun()
            else:
                st.warning("請先勾選確認。")


def page_uncertainty(pid, operator):
    st.header("⑥ 不確定性 (Uncertainty)")
    st.caption("為每個排放項目輸入活動數據與排放係數的不確定性；系統以「平方和開根號」法（假設各項獨立）合併。")
    crud_section(CFG_UNCERT, pid, operator)

    rows = fetch_all("SELECT * FROM uncertainty_items WHERE project_id=?", (pid,))
    if not rows:
        return
    st.divider()
    st.subheader("🎯 整體不確定性評估")
    total_abs = math.sqrt(sum(r["abs_uncert_tco2e"] ** 2 for r in rows))   # 各項獨立：RSS 合併
    reduction = fetch_all("SELECT COALESCE(SUM(reduction_tco2e),0) AS s FROM quantifications WHERE project_id=?",
                          (pid,))[0]["s"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("累計減排量 (tCO2e)", f"{reduction:,.3f}")
    m2.metric("合併不確定性 (±tCO2e)", f"{total_abs:,.3f}")
    m3.metric("相對不確定性 (±%)", f"{total_abs / abs(reduction) * 100:,.1f}%" if reduction else "—")
    m4.metric("保守減排量 (tCO2e)", f"{max(reduction - total_abs, 0):,.3f}",
              help="累計減排量扣除合併不確定性（最低為 0），可作為較保守的申報參考")
    st.bar_chart(pd.DataFrame({"絕對不確定性 (±tCO2e)": {r["item_name"]: r["abs_uncert_tco2e"] for r in rows}}))
    st.caption("※ 此為簡化的誤差傳遞估算，僅供 MVP 展示；正式申報請依所採方法學調整。")


def page_evidence(pid, operator):
    st.header("⑦ 證據與版本紀錄 (Evidence & Versioning)")
    tab1, tab2, tab3 = st.tabs(["📎 證據管理", "🕘 稽核紀錄 (Audit Log)", "🔍 單筆資料版本歷史"])

    # ---------------- 證據管理 ----------------
    with tab1:
        # 連結對象放在 form 外面，才能依模組動態載入紀錄清單
        mod = st.selectbox("證據對應的模組", ["（不指定）"] + list(MODULE_TABLES.keys()),
                           format_func=lambda x: x if x == "（不指定）" else MODULE_TABLES[x][0])
        ref_id = None
        if mod != "（不指定）":
            c = MODULE_TABLES[mod][1]
            recs = fetch_all(f"SELECT {c['pk']} AS id, {c['label_field']} AS label FROM {mod} WHERE project_id=?", (pid,))
            if recs:
                names = {r["id"]: r["label"] for r in recs}
                ref_id = st.selectbox("對應的資料筆", [None] + list(names.keys()),
                                      format_func=lambda x: "（整個模組）" if x is None else f"{names[x]}｜{x[:8]}")
            else:
                st.info("該模組目前沒有資料，證據將只連結到模組。")

        with st.form("evidence_form", clear_on_submit=True):
            title = st.text_input("證據標題 *")
            desc = st.text_area("說明")
            up = st.file_uploader("上傳證據檔案（可選）")
            url = st.text_input("或填寫證據 URL（可選）")
            ok = st.form_submit_button("新增證據")
        if ok:
            if is_empty(title):
                st.error("請填寫證據標題")
            elif up is None and is_empty(url):
                st.error("請上傳檔案或填寫 URL（至少一項）")
            else:
                f_name = f_path = sha = None
                f_size = None
                if up is not None:
                    data = up.getvalue()
                    sha = hashlib.sha256(data).hexdigest()           # 檔案雜湊值，用來證明檔案未被竄改
                    safe = re.sub(r'[\\/:*?"<>|]', "_", Path(up.name).name)   # 移除 Windows 不允許的字元
                    folder = EVIDENCE_DIR / pid
                    folder.mkdir(parents=True, exist_ok=True)
                    target = folder / f"{uuid.uuid4().hex[:8]}_{safe}"
                    target.write_bytes(data)
                    f_name, f_path, f_size = up.name, str(target), len(data)
                etype = "檔案＋URL" if (up is not None and not is_empty(url)) else ("檔案" if up is not None else "URL")
                insert_record(CFG_EVIDENCE, pid, dict(
                    module_table=None if mod == "（不指定）" else mod, ref_record_id=ref_id,
                    title=title.strip(), evidence_type=etype, file_name=f_name, file_path=f_path,
                    file_sha256=sha, file_size=f_size, url=(url or "").strip() or None,
                    description=desc, uploaded_by=operator), operator)
                flash("✅ 已新增證據")
                st.rerun()

        rows = fetch_all("SELECT * FROM evidence WHERE project_id=? ORDER BY created_at", (pid,))
        st.subheader("📋 證據清單")
        if not rows:
            st.info("目前沒有證據。")
        else:
            df = pd.DataFrame(rows)
            df["module_table"] = df["module_table"].map(lambda x: TABLE_LABELS.get(x, "") if x else "")
            show = df[["title", "evidence_type", "module_table", "file_name", "url", "file_sha256",
                       "uploaded_by", "version", "created_at"]]
            show.columns = ["標題", "類型", "對應模組", "檔名", "URL", "SHA-256", "上傳者", "版本", "建立時間"]
            st.dataframe(show, hide_index=True)

            by_id = {r["evidence_id"]: r for r in rows}
            eid = st.selectbox("選擇證據", list(by_id.keys()),
                               format_func=lambda x: f"{by_id[x]['title']}｜{x[:8]}")
            ev = by_id[eid]
            if ev["file_path"] and Path(ev["file_path"]).exists():
                st.download_button("⬇️ 下載檔案", Path(ev["file_path"]).read_bytes(),
                                   file_name=ev["file_name"], key=f"dl_{eid}")
            if ev["url"]:
                st.markdown(f"🔗 [{ev['url']}]({ev['url']})")
            ck = st.checkbox("我確認要刪除此證據紀錄（磁碟上的檔案會保留）", key=f"evdel_ck_{eid}")
            if st.button("🗑️ 刪除證據", key=f"evdel_{eid}"):
                if ck:
                    delete_record(CFG_EVIDENCE, eid, pid, operator)
                    flash("🗑️ 已刪除證據紀錄")
                    st.rerun()
                else:
                    st.warning("請先勾選確認。")

    # ---------------- 稽核紀錄 ----------------
    with tab2:
        st.caption("每一次新增、修改、刪除都會自動記錄操作者與時間。")
        c1, c2 = st.columns(2)
        f_table = c1.selectbox("資料表", ["（全部）"] + list(TABLE_LABELS.keys()),
                               format_func=lambda x: x if x == "（全部）" else TABLE_LABELS[x])
        f_action = c2.selectbox("動作", ["（全部）", "INSERT", "UPDATE", "DELETE"])
        sql, params = "SELECT * FROM audit_log WHERE project_id=?", [pid]
        if f_table != "（全部）":
            sql += " AND table_name=?"
            params.append(f_table)
        if f_action != "（全部）":
            sql += " AND action=?"
            params.append(f_action)
        logs = fetch_all(sql + " ORDER BY log_id DESC LIMIT 500", tuple(params))
        if logs:
            df = pd.DataFrame(logs)
            df["table_name"] = df["table_name"].map(lambda x: TABLE_LABELS.get(x, x))
            show = df[["log_id", "created_at", "operator", "action", "table_name", "record_id"]]
            show.columns = ["編號", "時間", "操作者", "動作", "資料表", "紀錄 ID"]
            st.dataframe(show, hide_index=True)
            # utf-8-sig：讓 Windows 的 Excel 開啟 CSV 時中文不會亂碼
            st.download_button("⬇️ 匯出稽核紀錄 CSV", df.to_csv(index=False).encode("utf-8-sig"),
                               file_name="audit_log.csv", mime="text/csv")
        else:
            st.info("沒有符合條件的紀錄。")

    # ---------------- 單筆版本歷史 ----------------
    with tab3:
        st.caption("選擇一筆資料，查看它每個版本改了什麼。")
        t = st.selectbox("資料表", list(TABLE_LABELS.keys()), key="hist_table",
                         format_func=lambda x: TABLE_LABELS[x])
        recs = fetch_all("SELECT DISTINCT record_id FROM audit_log WHERE project_id=? AND table_name=?", (pid, t))
        if not recs:
            st.info("此資料表尚無紀錄。")
        else:
            rid = st.selectbox("紀錄 ID", [r["record_id"] for r in recs], key="hist_rid")
            hist = fetch_all("SELECT * FROM audit_log WHERE table_name=? AND record_id=? ORDER BY log_id",
                             (t, rid))
            for h in hist:
                old = json.loads(h["old_value"]) if h["old_value"] else {}
                new = json.loads(h["new_value"]) if h["new_value"] else {}
                ver = (new or old).get("version", "?")
                with st.expander(f"v{ver}｜{h['action']}｜{h['created_at']}｜{h['operator']}"):
                    if h["action"] == "UPDATE":
                        diffs = [{"欄位": k, "舊值": old.get(k), "新值": new.get(k)}
                                 for k in sorted(set(old) | set(new))
                                 if old.get(k) != new.get(k) and k not in ("version", "updated_at")]
                        st.dataframe(pd.DataFrame(diffs), hide_index=True)
                    else:
                        st.json(new or old)


# ---------------------------------------------------------------------------
# 6. 主程式：側邊欄導覽
# ---------------------------------------------------------------------------
PAGES = {
    "🏠 專案總覽（建立專案）": page_overview,
    "① 專案邊界": page_boundary,
    "② 基準線情境": page_baseline,
    "③ 來源／匯／庫辨識": page_ghg,
    "④ 監測計畫": page_monitoring,
    "⑤ 減排或增匯量化": page_quantification,
    "⑥ 不確定性": page_uncertainty,
    "⑦ 證據與版本紀錄": page_evidence,
}


def main():
    st.set_page_config(page_title="跨校碳中和平台 MVP", page_icon="🌱", layout="wide")
    init_db()   # 每次執行都確認資料表存在

    st.sidebar.title("🌱 跨校碳中和平台")
    st.sidebar.caption("ISO 14064-2 減碳專案管理 MVP")
    operator = st.sidebar.text_input("操作者（寫入稽核紀錄）", value="user", key="operator")
    page = st.sidebar.radio("功能選單", list(PAGES.keys()))

    # 選擇目前專案（所有模組的資料都依 project_id 過濾）
    projects = fetch_all("SELECT project_id, name FROM projects ORDER BY created_at")
    pid = None
    if projects:
        names = {p["project_id"]: p["name"] for p in projects}
        pid = st.sidebar.selectbox("目前專案", list(names.keys()), format_func=lambda x: names[x])
        st.sidebar.caption(f"UUID：{pid[:8]}…")

    show_flash()
    if pid is None and page != "🏠 專案總覽（建立專案）":
        st.info("請先到「🏠 專案總覽」建立一個專案。")
        st.stop()
    PAGES[page](pid, operator)


if __name__ == "__main__":
    main()
