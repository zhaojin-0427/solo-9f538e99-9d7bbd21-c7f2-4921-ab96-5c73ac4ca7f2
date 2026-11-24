"""SQLite 连接、建表与旧卷迁移。

- 日样表 daily_samples：每个（断面, 日期）一份。g0..g10 为各内置档干重
  （克，非负整数；合并档的克重记在其档上限所属内置档列上），mask 为该日样
  非零合并档跨过的内部档界掩码（标准 11 档上报时为 0）。
- 汇总表 summaries：每个（断面, 年, 月, 日）一份，记录该断面当月 1 日至当日
  这一连续日段的各档合计干重（月内前缀汇总），以及 c0..c9 各内部档界被
  非零合并档跨过的前缀次数。某月最后一日的前缀即该月合计。
  不变式：某（月, 日）无行 ⟺ 当月该日及之前没有任何已提交日样，查询时按零计。

旧 SQLite 卷（只有 g 列）通过 ALTER TABLE ADD COLUMN 平滑升级：补出的
mask / c 列默认 0，旧样均为标准 11 档、不跨任何档界，语义不变。
"""
import os
import sqlite3

from .gradation import N_BOUNDARIES, N_GRADES

GRADE_COLUMNS = [f"g{i}" for i in range(N_GRADES)]
COUNT_COLUMNS = [f"c{b}" for b in range(N_BOUNDARIES)]

_COL_DEFS = ", ".join(f"{c} INTEGER NOT NULL" for c in GRADE_COLUMNS)
_COUNT_DEFS = ", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in COUNT_COLUMNS)

_DDL = f"""
CREATE TABLE IF NOT EXISTS daily_samples (
    section TEXT NOT NULL,
    date    TEXT NOT NULL,  -- ISO 格式 YYYY-MM-DD
    {_COL_DEFS},
    mask    INTEGER NOT NULL DEFAULT 0,  -- 非零合并档跨过的内部档界掩码
    PRIMARY KEY (section, date)
);
CREATE TABLE IF NOT EXISTS summaries (
    section TEXT NOT NULL,
    year    INTEGER NOT NULL,
    month   INTEGER NOT NULL,
    day     INTEGER NOT NULL,
    {_COL_DEFS},
    {_COUNT_DEFS},
    PRIMARY KEY (section, year, month, day)
);
"""

# 旧卷升级：补齐新版本引入的列（已存在则跳过）。
_MIGRATIONS = {
    "daily_samples": [("mask", "INTEGER NOT NULL DEFAULT 0")],
    "summaries": [(c, "INTEGER NOT NULL DEFAULT 0") for c in COUNT_COLUMNS],
}


def db_path():
    return os.environ.get("SEDIMENT_DB", "/data/sediment.db")


def connect():
    """每次请求一个连接；autocommit 模式，事务由代码显式控制。"""
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _existing_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(_DDL)
        # 旧卷迁移：只补缺失列，不动既有数据。
        for table, additions in _MIGRATIONS.items():
            existing = _existing_columns(conn, table)
            for name, decl in additions:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # WAL：读事务不阻塞写，读事务期间看到同一已提交快照。
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()
