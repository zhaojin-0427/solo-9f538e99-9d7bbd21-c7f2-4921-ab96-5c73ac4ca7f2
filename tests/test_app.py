"""接口与约束的端到端测试。"""
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import GRADE_COLUMNS
from app.gradation import BOUNDARIES_MM, GRADES_MM, N_GRADES
from app.main import app

W1 = [10, 20, 30, 40, 50, 30, 20, 10, 5, 3, 2]  # 总 223
W2 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]         # 总 66
W3 = [0, 0, 0, 100, 100, 0, 0, 0, 0, 0, 0]      # 总 200


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEDIMENT_DB", str(tmp_path / "test.db"))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def submit(client, section, day, weights):
    return client.post("/docs", json={"section": section, "date": day, "weights": weights})


def submit_bins(client, section, day, bins):
    return client.post("/docs", json={"section": section, "date": day, "bins": bins})


def query(client, start, end, *sections):
    return client.get(
        "/query",
        params=[("start", start), ("end", end)] + [("sections", s) for s in sections],
    )


def only_section(resp):
    assert resp.status_code == 200, resp.text
    secs = resp.json()["sections"]
    assert len(secs) == 1
    return secs[0]


def test_swagger_ui_still_at_get_docs(client):
    assert client.get("/docs").status_code == 200


def test_submit_receipt_and_single_day_query(client):
    r = submit(client, "S1", "2026-03-15", W1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replaced"] is False
    assert body["summaries_written"] == 31 - 15 + 1  # 3 月共 31 天
    assert body["sample_total_g"] == sum(W1)

    sec = only_section(query(client, "2026-03-15", "2026-03-15", "S1"))
    assert sec["grade_totals_g"] == W1
    assert sec["total_weight_g"] == sum(W1)
    assert sec["summaries_read"] <= 16
    assert sec["message"] is None


def test_resubmit_same_day_replaces(client):
    submit(client, "S1", "2026-03-15", W1)
    r = submit(client, "S1", "2026-03-15", W2)
    assert r.json()["replaced"] is True

    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["grade_totals_g"] == W2  # 整份替换，不是叠加
    assert sec["total_weight_g"] == sum(W2)


def test_multi_month_range_and_exclusion(client):
    submit(client, "S1", "2026-01-10", W1)
    submit(client, "S1", "2026-02-20", W2)
    submit(client, "S1", "2026-03-05", W3)
    submit(client, "S1", "2026-04-01", W1)  # 在查询范围之外

    expected = [a + b + c for a, b, c in zip(W1, W2, W3)]
    sec = only_section(query(client, "2026-01-05", "2026-03-31", "S1"))
    assert sec["grade_totals_g"] == expected
    assert sec["total_weight_g"] == sum(expected)

    sec = only_section(query(client, "2026-02-01", "2026-02-28", "S1"))
    assert sec["grade_totals_g"] == W2  # 只含 2 月那份

    sec = only_section(query(client, "2026-01-10", "2026-01-10", "S1"))
    assert sec["grade_totals_g"] == W1


def test_cumulative_and_d50(client):
    submit(client, "S1", "2026-05-06", W3)  # 总 200，一半=100，第 3 档(0.5mm)先达到
    sec = only_section(query(client, "2026-05-01", "2026-05-31", "S1"))
    assert sec["d50_mm"] == 0.5
    cum = sec["cumulative_passing_percent"]
    assert cum[0] == 0.0 and cum[2] == 0.0 and cum[3] == 50.0 and cum[4] == 100.0 and cum[-1] == 100.0

    # 奇数总量：总 5，一半 2.5，第 0 档累计 3 首次达到
    submit(client, "S2", "2026-05-06", [3, 0, 2] + [0] * (N_GRADES - 3))
    sec = only_section(query(client, "2026-05-06", "2026-05-06", "S2"))
    assert sec["d50_mm"] == GRADES_MM[0]


def test_zero_total_means_no_sample(client):
    sec = only_section(query(client, "2026-01-01", "2026-12-31", "不存在"))
    assert sec["total_weight_g"] == 0
    assert sec["message"] == "无样"
    assert sec["d50_mm"] is None
    assert sec["cumulative_passing_percent"] is None
    assert sec["summaries_read"] == 0

    submit(client, "S0", "2026-06-01", [0] * N_GRADES)  # 提交了但全零
    sec = only_section(query(client, "2026-06-01", "2026-06-30", "S0"))
    assert sec["message"] == "无样"


def test_multiple_sections_and_comma_form(client):
    submit(client, "S1", "2026-07-01", W1)
    submit(client, "S2", "2026-07-02", W2)
    r = query(client, "2026-07-01", "2026-07-31", "S1", "S2", "S3")
    assert r.status_code == 200, r.text
    secs = {s["section"]: s for s in r.json()["sections"]}
    assert secs["S1"]["grade_totals_g"] == W1
    assert secs["S2"]["grade_totals_g"] == W2
    assert secs["S3"]["message"] == "无样"

    r = client.get("/query", params={"start": "2026-07-01", "end": "2026-07-31",
                                     "sections": "S1,S2"})
    assert [s["section"] for s in r.json()["sections"]] == ["S1", "S2"]


def test_worst_case_read_write_bounds(client):
    # 1 日提交（31 天的月）：写 31 份，不超 32
    r = submit(client, "S1", "2026-01-01", W1)
    assert r.json()["summaries_written"] == 31
    assert r.json()["summaries_written"] <= 32
    # 全年最差日段：1 月 2 日 ~ 12 月 30 日，读 13 份，不超 16
    sec = only_section(query(client, "2026-01-02", "2026-12-30", "S1"))
    assert sec["summaries_read"] <= 16


def test_validation_errors(client):
    bad = [
        [-1] + [0] * (N_GRADES - 1),      # 负数
        [0] * (N_GRADES - 1),             # 档数不足
        [0] * (N_GRADES + 1),             # 档数过多
        [1.5] + [0] * (N_GRADES - 1),     # 非整数
        [True] + [0] * (N_GRADES - 1),    # 布尔不算整数
        ["5"] + [0] * (N_GRADES - 1),     # 字符串不算整数
    ]
    for w in bad:
        assert submit(client, "S1", "2026-03-15", w).status_code == 422, w
    assert submit(client, "  ", "2026-03-15", W1).status_code == 422
    assert submit(client, "S1", "2026-02-30", W1).status_code == 422

    assert query(client, "2026-03-10", "2026-03-01", "S1").status_code == 400
    assert query(client, "2025-12-31", "2026-01-01", "S1").status_code == 400
    assert query(client, "2026-01-01", "2026-12-31", "A", "B", "C", "D", "E").status_code == 400
    assert client.get("/query", params={"start": "2026-01-01", "end": "2026-01-02"}).status_code == 422


def test_failed_write_rolls_back_keeps_old_results(client, tmp_path):
    submit(client, "S1", "2026-03-15", W1)
    # 人为制造写入中途失败：删掉汇总表
    conn = sqlite3.connect(os.environ["SEDIMENT_DB"])
    conn.execute("DROP TABLE summaries")
    conn.commit()
    conn.close()

    r = submit(client, "S1", "2026-03-15", W2)
    assert r.status_code == 500  # 写入失败

    # 日样未被改动：仍是旧样（日样与汇总一起回滚）
    conn = sqlite3.connect(os.environ["SEDIMENT_DB"])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM daily_samples WHERE section='S1' AND date='2026-03-15'"
    ).fetchone()
    conn.close()
    assert [row[c] for c in GRADE_COLUMNS] == W1


# ---------- 合并档（bins）格式 ----------

# 三档合并：(..0.25]、(0.25..2.0]、(2.0..64]，克重落在上限档 2、5、10。
BINS3 = [
    {"upper_mm": 0.25, "grams": 30},
    {"upper_mm": 2.0, "grams": 70},
    {"upper_mm": 64.0, "grams": 100},
]
# 档 (..0.25] 覆盖档 0..2，跨 0.063/0.125；档 (0.25,2.0] 覆盖档 3..5，跨
# 0.5/1.0；档 (2.0,64] 覆盖档 6..10，跨 4/8/16/32。
EXPECTED_CROSSED_3 = [0.063, 0.125, 0.5, 1.0, 4.0, 8.0, 16.0, 32.0]
EXPANDED_3 = [0, 0, 30, 0, 0, 70, 0, 0, 0, 0, 100]  # 克重记在上限档列


def test_merged_bins_suspend_gradation_and_list_boundaries(client):
    r = submit_bins(client, "S1", "2026-03-15", BINS3)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged"] is True
    assert body["sample_total_g"] == 200
    assert body["summaries_written"] == 31 - 15 + 1

    sec = only_section(query(client, "2026-03-15", "2026-03-15", "S1"))
    assert sec["total_weight_g"] == 200
    assert sec["crossed_boundaries_mm"] == EXPECTED_CROSSED_3
    assert sec["grade_totals_g"] is None
    assert sec["cumulative_passing_percent"] is None
    assert sec["d50_mm"] is None
    assert sec["message"] == "存在跨档合并档，标准级配与 D50 暂停"
    assert sec["summaries_read"] <= 16


def test_zero_weight_merged_bin_can_be_split(client):
    # 仅 0.25 档有 30 g（覆盖档 0..2，跨 0.063/0.125）；更粗的两档为零，
    # 零重量跨档不跨界，因此 0.25 以上的档界全部确定。
    bins = [
        {"upper_mm": 0.25, "grams": 30},
        {"upper_mm": 2.0, "grams": 0},
        {"upper_mm": 64.0, "grams": 0},
    ]
    r = submit_bins(client, "S1", "2026-03-15", bins)
    assert r.status_code == 200, r.text
    sec = only_section(query(client, "2026-03-15", "2026-03-15", "S1"))
    # 零重量档可拆分：未引入额外跨界；但 30 g 所在的非零档本身跨 0.063/0.125。
    assert sec["crossed_boundaries_mm"] == [0.063, 0.125]
    assert sec["message"] == "存在跨档合并档，标准级配与 D50 暂停"
    assert sec["total_weight_g"] == 30
    assert sec["grade_totals_g"] is None
    assert sec["d50_mm"] is None


def test_zero_weight_only_coarse_does_not_block_finer_known_section(client):
    # 细档逐个上报（档界全部确定），最粗的合并档为零重量：可拆分，照常供数。
    bins = [{"upper_mm": u, "grams": g} for u, g in zip(GRADES_MM[:-1], W1[:-1])]
    bins.append({"upper_mm": 64.0, "grams": 0})
    submit_bins(client, "S1", "2026-03-15", bins)
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["crossed_boundaries_mm"] == []
    assert sec["grade_totals_g"] == W1[:-1] + [0]
    assert sec["d50_mm"] == d50_of(W1[:-1] + [0])
    assert sec["message"] is None


def test_all_boundaries_known_section_serves_normal_gradation(client):
    # 每个合并档都只覆盖一个内置档（上限逐个列出）：等同标准 11 档。
    bins = [{"upper_mm": u, "grams": g} for u, g in zip(GRADES_MM, W1)]
    submit_bins(client, "S1", "2026-03-15", bins)
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["crossed_boundaries_mm"] == []
    assert sec["grade_totals_g"] == W1
    assert sec["d50_mm"] == d50_of(W1)
    assert sec["message"] is None


def d50_of(weights):
    total = sum(weights)
    run = 0
    for w, upper in zip(weights, GRADES_MM):
        run += w
        if 2 * run >= total:
            return upper


def test_formats_interchangeable_same_day_replacement(client):
    # 先合并档上报（带跨界），再用标准格式整份替换：跨界状态须消失。
    submit_bins(client, "S1", "2026-03-15", BINS3)
    r = submit(client, "S1", "2026-03-15", W2)
    assert r.status_code == 200
    assert r.json()["replaced"] is True
    assert r.json()["merged"] is False
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["grade_totals_g"] == W2
    assert sec["crossed_boundaries_mm"] == []
    assert sec["d50_mm"] == d50_of(W2)

    # 反向：标准格式 → 合并档，同日再替换，跨界重新出现。
    r = submit_bins(client, "S1", "2026-03-15", BINS3)
    assert r.json()["replaced"] is True
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["crossed_boundaries_mm"] == EXPECTED_CROSSED_3
    assert sec["total_weight_g"] == 200


def test_merged_then_standard_on_other_days_range_aggregation(client):
    # 10 日合并档 (..2.0] 覆盖档 0..5（跨 0.063..1.0），20 日标准档。
    submit_bins(client, "S1", "2026-03-10", [
        {"upper_mm": 2.0, "grams": 50}, {"upper_mm": 64.0, "grams": 0},
    ])
    submit(client, "S1", "2026-03-20", W1)
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    # 零重量粗档不跨界；非零档 (..2.0] 跨档界 0..4（上限 0.063..1.0）。
    assert sec["crossed_boundaries_mm"] == [0.063, 0.125, 0.25, 0.5, 1.0]
    assert sec["total_weight_g"] == 50 + sum(W1)
    assert sec["message"] is not None

    # 只查 20 日之后：不含合并档，档界全部确定，按标准档供数。
    sec = only_section(query(client, "2026-03-20", "2026-03-31", "S1"))
    assert sec["crossed_boundaries_mm"] == []
    assert sec["grade_totals_g"] == W1


def test_merged_all_zero_is_no_sample(client):
    bins = [
        {"upper_mm": 0.25, "grams": 0},
        {"upper_mm": 2.0, "grams": 0},
        {"upper_mm": 64.0, "grams": 0},
    ]
    submit_bins(client, "S0", "2026-06-01", bins)
    sec = only_section(query(client, "2026-06-01", "2026-06-30", "S0"))
    assert sec["message"] == "无样"
    assert sec["total_weight_g"] == 0
    assert sec["crossed_boundaries_mm"] == []
    assert sec["grade_totals_g"] is None


def test_merged_bins_validation_errors(client):
    bad_bins = [
        # 上限不是内置档
        [{"upper_mm": 0.3, "grams": 1}, {"upper_mm": 64.0, "grams": 0}],
        # 上限未升序
        [{"upper_mm": 2.0, "grams": 1}, {"upper_mm": 1.0, "grams": 1},
         {"upper_mm": 64.0, "grams": 0}],
        # 最粗不是 64
        [{"upper_mm": 0.25, "grams": 1}, {"upper_mm": 32.0, "grams": 1}],
        # 空 bins
        [],
        # 克重为负 / 浮点 / 布尔 / 字符串
        [{"upper_mm": 0.25, "grams": -1}, {"upper_mm": 64.0, "grams": 0}],
        [{"upper_mm": 0.25, "grams": 1.5}, {"upper_mm": 64.0, "grams": 0}],
        [{"upper_mm": 0.25, "grams": True}, {"upper_mm": 64.0, "grams": 0}],
        [{"upper_mm": 0.25, "grams": "5"}, {"upper_mm": 64.0, "grams": 0}],
        # 上限为布尔
        [{"upper_mm": True, "grams": 1}, {"upper_mm": 64.0, "grams": 0}],
    ]
    for bins in bad_bins:
        assert submit_bins(client, "S1", "2026-03-15", bins).status_code == 422, bins

    # 两种格式同时给 / 都不给
    r = client.post("/docs", json={
        "section": "S1", "date": "2026-03-15",
        "weights": W1, "bins": BINS3,
    })
    assert r.status_code == 422
    r = client.post("/docs", json={"section": "S1", "date": "2026-03-15"})
    assert r.status_code == 422


def test_merged_write_and_read_bounds(client):
    # 1 日提交合并档：写 31 份（≤ 32），计数列随汇总一起写。
    r = submit_bins(client, "S1", "2026-01-01", BINS3)
    assert r.json()["summaries_written"] == 31
    # 含该日样的日段：跨界照常报出，且读份数 ≤ 16。
    sec = only_section(query(client, "2026-01-01", "2026-12-30", "S1"))
    assert sec["crossed_boundaries_mm"] == EXPECTED_CROSSED_3
    assert sec["summaries_read"] <= 16
    # 全年最差日段（1 月 2 日起，不含 1 日样）：读 13 份（≤ 16），
    # 档界判定所用计数就在这些汇总行内，不额外增份。
    sec = only_section(query(client, "2026-01-02", "2026-12-30", "S1"))
    assert sec["summaries_read"] <= 16
    assert sec["crossed_boundaries_mm"] == []
    assert sec["message"] == "无样"


def test_legacy_sqlite_volume_migrates_and_still_serves(tmp_path, monkeypatch):
    # 手工构造旧版卷：只有 g0..g10，没有 mask / c 列。
    db_file = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_file)
    g_cols = ", ".join(GRADE_COLUMNS)
    legacy.execute(
        f"CREATE TABLE daily_samples (section TEXT, date TEXT, {g_cols}, "
        "PRIMARY KEY (section, date))"
    )
    legacy.execute(
        "CREATE TABLE summaries (section TEXT, year INTEGER, month INTEGER, "
        f"day INTEGER, {g_cols}, PRIMARY KEY (section, year, month, day))"
    )
    legacy.execute(
        f"INSERT INTO daily_samples VALUES ('S1','2026-03-15',{','.join('?' * N_GRADES)})",
        W1,
    )
    legacy.execute(
        f"INSERT INTO summaries VALUES ('S1',2026,3,15,{','.join('?' * N_GRADES)})",
        W1,
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setenv("SEDIMENT_DB", str(db_file))
    with TestClient(app, raise_server_exceptions=False) as c:
        # 旧数据可读：旧样不跨任何档界，照常给出标准级配。
        # 查 3/1–3/15 直接命中旧前缀 P(15)，无需月末日行。
        sec = only_section(query(c, "2026-03-01", "2026-03-15", "S1"))
        assert sec["grade_totals_g"] == W1
        assert sec["crossed_boundaries_mm"] == []
        assert sec["d50_mm"] == d50_of(W1)
        # 新格式可写入旧卷（会自动补齐 3/16..3/31 的前缀）。
        r = submit_bins(c, "S2", "2026-03-16", BINS3)
        assert r.status_code == 200, r.text
        sec = only_section(query(c, "2026-03-16", "2026-03-16", "S2"))
        assert sec["crossed_boundaries_mm"] == EXPECTED_CROSSED_3

    # 迁移确实补出了新列。
    migrated = sqlite3.connect(db_file)
    sample_cols = {r[1] for r in migrated.execute("PRAGMA table_info(daily_samples)")}
    summary_cols = {r[1] for r in migrated.execute("PRAGMA table_info(summaries)")}
    migrated.close()
    assert "mask" in sample_cols
    assert {f"c{i}" for i in range(len(BOUNDARIES_MM))} <= summary_cols


def test_merged_rollback_keeps_old_sample_and_boundaries(client):
    submit_bins(client, "S1", "2026-03-15", BINS3)
    # 人为制造写入中途失败：在汇总表上加触发器拒绝写入（表仍可读）。
    conn = sqlite3.connect(os.environ["SEDIMENT_DB"])
    conn.execute(
        "CREATE TRIGGER fail_summary_insert BEFORE INSERT ON summaries "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    conn.commit()
    conn.close()

    r = submit(client, "S1", "2026-03-15", W2)  # 试图换成标准档
    assert r.status_code == 500

    # 回滚后仍是原合并档：查询仍报跨界暂停。
    sec = only_section(query(client, "2026-03-01", "2026-03-31", "S1"))
    assert sec["crossed_boundaries_mm"] == EXPECTED_CROSSED_3
    assert sec["total_weight_g"] == 200
