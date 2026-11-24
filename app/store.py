"""日样写入与汇总查询的核心逻辑。

汇总方案（月内前缀汇总）：
- summaries(section, year, month, day) = 该断面当月 1 日至当日各档合计干重。
- 查询任意日段 [a, b]（同一自然年内）只需把若干份汇总相加/相减：
    * a、b 同月：P(b) − P(a−1)                                    ≤ 2 份
    * 跨月：左月（月总 − P(a−1)，a 为 1 日则只读月总）
            + 中间整月各 1 份月总（同一年内 ≤ 10 份）
            + 右月 P(b)                                           ≤ 13 份
  单断面最多读 13 份，满足 ≤ 16 份的约束。
- 补录/更正某日样：只读取变动的那份原始日样得到差量 δ，再把当月 [当日, 月末]
  的每份前缀汇总整体加上 δ（一条 upsert 完成，已存在的行加 δ，未存在的行以 δ
  建行——由不变式，未存在的行旧值必为零）。最多写 31 份，满足 ≤ 32 份的约束。
- 日样与汇总在同一事务中提交，一起生效；任何失败整体回滚，原结果仍可查。
"""
import calendar

from .db import GRADE_COLUMNS
from .gradation import N_GRADES

_COLS = ", ".join(GRADE_COLUMNS)
_MARKS = ", ".join("?" for _ in GRADE_COLUMNS)
_SET_EXCLUDED = ", ".join(f"{c} = excluded.{c}" for c in GRADE_COLUMNS)
_ADD_EXCLUDED = ", ".join(f"{c} = {c} + excluded.{c}" for c in GRADE_COLUMNS)

_UPSERT_SAMPLE = (
    f"INSERT INTO daily_samples (section, date, {_COLS}) VALUES (?, ?, {_MARKS}) "
    f"ON CONFLICT (section, date) DO UPDATE SET {_SET_EXCLUDED}"
)
_UPSERT_PREFIX = (
    f"INSERT INTO summaries (section, year, month, day, {_COLS}) "
    f"VALUES (?, ?, ?, ?, {_MARKS}) "
    f"ON CONFLICT (section, year, month, day) DO UPDATE SET {_ADD_EXCLUDED}"
)
_SELECT_SAMPLE = f"SELECT {_COLS} FROM daily_samples WHERE section = ? AND date = ?"
_SELECT_PREFIX = (
    f"SELECT {_COLS} FROM summaries "
    "WHERE section = ? AND year = ? AND month = ? AND day = ?"
)


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _weights_of(row):
    return [row[c] for c in GRADE_COLUMNS]


def submit_daily_sample(conn, section, day, weights):
    """提交（或整份替换）一份日样，并同步维护汇总。日样与汇总同一事务生效。

    返回 {"replaced": bool, "summaries_written": int}。
    """
    year, month, dom = day.year, day.month, day.day
    last_day = _days_in_month(year, month)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 原始日样只查变动的这一份，用于求差量。
        row = conn.execute(_SELECT_SAMPLE, (section, day.isoformat())).fetchone()
        replaced = row is not None
        old = _weights_of(row) if row else [0] * N_GRADES
        delta = [new - old for new, old in zip(weights, old)]

        conn.execute(_UPSERT_SAMPLE, (section, day.isoformat(), *weights))

        written = 0
        if any(delta):
            # 当月 [当日, 月末] 的每份前缀汇总都包含这一天，统一加差量。
            rows = [
                (section, year, month, d, *delta) for d in range(dom, last_day + 1)
            ]
            conn.executemany(_UPSERT_PREFIX, rows)
            written = len(rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"replaced": replaced, "summaries_written": written}


def _fetch_prefix(conn, section, year, month, day):
    """读取一份月内前缀汇总；无行返回 None（按零计，不计入实读份数）。"""
    row = conn.execute(_SELECT_PREFIX, (section, year, month, day)).fetchone()
    return _weights_of(row) if row else None


def query_section_totals(conn, section, start, end):
    """查询某断面 [start, end]（含首尾，同一自然年内）的各档合计干重。

    只读汇总表；返回 (各档合计列表, 实读汇总份数)。
    调用方须把本函数包在一个事务里，保证整次查询取自同一已提交状态。
    """
    totals = [0] * N_GRADES
    reads = 0

    def add(prefix, sign):
        nonlocal reads
        if prefix is None:
            return
        reads += 1
        for i in range(N_GRADES):
            totals[i] += sign * prefix[i]

    year, ma, mb = start.year, start.month, end.month
    if ma == mb:
        add(_fetch_prefix(conn, section, year, ma, end.day), +1)
        if start.day > 1:
            add(_fetch_prefix(conn, section, year, ma, start.day - 1), -1)
    else:
        # 左月：[start, 月末] = 月总 − P(start−1)；start 为 1 日时只读月总。
        add(_fetch_prefix(conn, section, year, ma, _days_in_month(year, ma)), +1)
        if start.day > 1:
            add(_fetch_prefix(conn, section, year, ma, start.day - 1), -1)
        # 中间整月：每月 1 份（月总 = 月末日的前缀）。
        for m in range(ma + 1, mb):
            add(_fetch_prefix(conn, section, year, m, _days_in_month(year, m)), +1)
        # 右月：[月初, end] = P(end)。
        add(_fetch_prefix(conn, section, year, mb, end.day), +1)
    return totals, reads
