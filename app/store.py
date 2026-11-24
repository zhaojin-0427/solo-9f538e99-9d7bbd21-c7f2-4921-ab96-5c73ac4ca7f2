"""日样写入与汇总查询的核心逻辑。

汇总方案（月内前缀汇总）：
- summaries(section, year, month, day) = 该断面当月 1 日至当日的：
    * g0..g10：各内置档合计干重（合并档克重记在其上限档列上）；
    * c0..c9：各内部档界被非零合并档跨过的累计次数（每份日样对其掩码中
      置位的每个档界贡献 1）。
- 查询任意日段 [a, b]（同一自然年内）只需把若干份汇总相加/相减：
    * a、b 同月：P(b) − P(a−1)                                    ≤ 2 份
    * 跨月：左月（月总 − P(a−1)，a 为 1 日则只读月总）
            + 中间整月各 1 份月总（同一年内 ≤ 10 份）
            + 右月 P(b)                                           ≤ 13 份
  单断面最多读 13 份，满足 ≤ 16 份的约束。档界判定所用的计数列就在这些
  汇总行内，不额外产生读写份数。
- 补录/更正某日样：只读取变动的那份原始日样求差量 δ（各档克重之差与
  跨界次数之差），再把当月 [当日, 月末] 的每份前缀汇总整体加上 δ（一条
  upsert：已存在的行加 δ，未存在的行以 δ 建行——由不变式，未存在的行
  旧值必为零）。最多写 31 份，满足 ≤ 32 份的约束。
- 日样与汇总在同一事务中提交，一起生效；任何失败整体回滚，原结果仍可查。
"""
import calendar

from .db import COUNT_COLUMNS, GRADE_COLUMNS
from .gradation import N_BOUNDARIES, N_GRADES

_G_COLS = ", ".join(GRADE_COLUMNS)
_C_COLS = ", ".join(COUNT_COLUMNS)
_ALL_COLS = ", ".join([*GRADE_COLUMNS, *COUNT_COLUMNS])
_G_MARKS = ", ".join("?" for _ in GRADE_COLUMNS)
_C_MARKS = ", ".join("?" for _ in COUNT_COLUMNS)
_ALL_MARKS = ", ".join("?" for _ in [*GRADE_COLUMNS, *COUNT_COLUMNS])
_SET_SAMPLE = ", ".join(
    [*(f"{c} = excluded.{c}" for c in GRADE_COLUMNS), "mask = excluded.mask"]
)
_ADD_EXCLUDED = ", ".join(
    [*(f"{c} = {c} + excluded.{c}" for c in GRADE_COLUMNS),
     *(f"{c} = {c} + excluded.{c}" for c in COUNT_COLUMNS)]
)

_UPSERT_SAMPLE = (
    f"INSERT INTO daily_samples (section, date, {_G_COLS}, mask) "
    f"VALUES (?, ?, {_G_MARKS}, ?) "
    f"ON CONFLICT (section, date) DO UPDATE SET {_SET_SAMPLE}"
)
_UPSERT_PREFIX = (
    f"INSERT INTO summaries (section, year, month, day, {_ALL_COLS}) "
    f"VALUES (?, ?, ?, ?, {_ALL_MARKS}) "
    f"ON CONFLICT (section, year, month, day) DO UPDATE SET {_ADD_EXCLUDED}"
)
_SELECT_SAMPLE = (
    f"SELECT {_G_COLS}, mask FROM daily_samples "
    "WHERE section = ? AND date = ?"
)
_SELECT_PREFIX = (
    f"SELECT {_ALL_COLS} FROM summaries "
    "WHERE section = ? AND year = ? AND month = ? AND day = ?"
)


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _counts_of_mask(mask):
    """一份日样对各档界的跨界贡献：掩码置位档界计 1，否则 0。"""
    return [(mask >> b) & 1 for b in range(N_BOUNDARIES)]


def submit_daily_sample(conn, section, day, weights, mask):
    """提交（或整份替换）一份日样，并同步维护汇总。日样与汇总同一事务生效。

    weights 为长度 N_GRADES 的各内置档克重（合并档克重落在其上限档列），
    mask 为非零合并档跨过的内部档界掩码（标准 11 档上报时为 0）。

    返回 {"replaced": bool, "summaries_written": int}。
    """
    year, month, dom = day.year, day.month, day.day
    last_day = _days_in_month(year, month)
    new_counts = _counts_of_mask(mask)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 原始日样只查变动的这一份，用于求差量。
        row = conn.execute(_SELECT_SAMPLE, (section, day.isoformat())).fetchone()
        replaced = row is not None
        if row:
            old_weights = [row[c] for c in GRADE_COLUMNS]
            old_counts = _counts_of_mask(row["mask"])
        else:
            old_weights = [0] * N_GRADES
            old_counts = [0] * N_BOUNDARIES
        delta_w = [n - o for n, o in zip(weights, old_weights)]
        delta_c = [n - o for n, o in zip(new_counts, old_counts)]

        conn.execute(
            _UPSERT_SAMPLE, (section, day.isoformat(), *weights, mask)
        )

        written = 0
        if any(delta_w) or any(delta_c):
            # 当月 [当日, 月末] 的每份前缀汇总都包含这一天，统一加差量。
            rows = [
                (section, year, month, d, *delta_w, *delta_c)
                for d in range(dom, last_day + 1)
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
    row = conn.execute(
        _SELECT_PREFIX, (section, year, month, day)
    ).fetchone()
    if row is None:
        return None
    return (
        [row[c] for c in GRADE_COLUMNS],
        [row[c] for c in COUNT_COLUMNS],
    )


def query_section_totals(conn, section, start, end):
    """查询某断面 [start, end]（含首尾，同一自然年内）的汇总。

    只读汇总表；返回 (各档合计列表, 各档界跨界次数列表, 实读汇总份数)。
    某档界跨界次数 > 0 即表示所选日样中有非零合并档跨过该内置档界，
    该断面的标准级配与 D50 须暂停。
    调用方须把本函数包在一个事务里，保证整次查询取自同一已提交状态。
    """
    totals = [0] * N_GRADES
    crossed = [0] * N_BOUNDARIES
    reads = 0

    def add(prefix, sign):
        nonlocal reads
        if prefix is None:
            return
        reads += 1
        weights, counts = prefix
        for i in range(N_GRADES):
            totals[i] += sign * weights[i]
        for b in range(N_BOUNDARIES):
            crossed[b] += sign * counts[b]

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
    return totals, crossed, reads
