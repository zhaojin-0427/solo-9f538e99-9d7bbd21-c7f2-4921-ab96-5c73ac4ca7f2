"""SQLite 连接与建表。

- 日样表 daily_samples：每个（断面, 日期）一份，各档干重（克，非负整数）。
- 汇总表 summaries：每个（断面, 年, 月, 日）一份，记录该断面当月 1 日至当日
  这一连续日段的各档合计干重（月内前缀汇总）。某月最后一日的前缀即该月合计。
  不变式：某（月, 日）无行 ⟺ 当月该日及之前没有任何已提交日样，查询时按零计。
"""
import os
import sqlite3

from .gradation import N_GRADES

GRADE_COLUMNS = [f"g{i}" for i in range(N_GRADES)]

_COL_DEFS = ", ".join(f"{c} INTEGER NOT NULL" for c in GRADE_COLUMNS)

_DDL = f"""
CREATE TABLE IF NOT EXISTS daily_samples (
    section TEXT NOT NULL,
    date    TEXT NOT NULL,  -- ISO 格式 YYYY-MM-DD
    {_COL_DEFS},
    PRIMARY KEY (section, date)
);
CREATE TABLE IF NOT EXISTS summaries (
    section TEXT NOT NULL,
    year    INTEGER NOT NULL,
    month   INTEGER NOT NULL,
    day     INTEGER NOT NULL,
    {_COL_DEFS},
    PRIMARY KEY (section, year, month, day)
);
"""


def db_path():
    return os.environ.get("SEDIMENT_DB", "/data/sediment.db")


def connect():
    """每次请求一个连接；autocommit 模式，事务由代码显式控制。"""
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    path = db_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(_DDL)
        # WAL：读事务不阻塞写，读事务期间看到同一已提交快照。
        conn.execute("PRAGMA journal_mode = WAL")
    finally:
        conn.close()
