"""接口与约束的端到端测试。"""
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.gradation import GRADES_MM, N_GRADES
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
    row = conn.execute(
        "SELECT * FROM daily_samples WHERE section='S1' AND date='2026-03-15'"
    ).fetchone()
    conn.close()
    assert list(row[2:]) == W1
