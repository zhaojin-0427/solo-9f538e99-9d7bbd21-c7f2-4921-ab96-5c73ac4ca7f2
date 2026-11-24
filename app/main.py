"""FastAPI 接口层。

路由：
- GET  /docs   Swagger 交互文档（FastAPI 默认），可在页面里直接 Try it out 提交。
- POST /docs   按断面和日期提交各筛级干重；同断面同日重交整份替换。
- GET  /query  任选同一自然年内起止日（含首尾）与 1～4 个断面，查各档合计干重、
               累计过筛率与 D50；总干重为零提示"无样"。
- GET  /grades 查看内置统一筛级。
- GET  /healthz 健康检查。
"""
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from . import db, store
from .gradation import GRADES_MM, N_GRADES, cumulative_from_fine, d50_mm


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(
    title="疏浚泥沙合样级配系统",
    version="1.0.0",
    lifespan=lifespan,
)

# 非负整数克：strict 拒绝字符串/浮点/布尔等隐式转换。
NonNegGrams = Annotated[int, Field(strict=True, ge=0)]


class DailySampleIn(BaseModel):
    section: str = Field(min_length=1, max_length=64, examples=["S1"])
    date: date
    weights: list[NonNegGrams] = Field(
        min_length=N_GRADES,
        max_length=N_GRADES,
        description=f"各筛级干重（克），须恰好 {N_GRADES} 档，顺序与 /grades 一致",
    )

    @field_validator("section")
    @classmethod
    def _strip_section(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("断面名不能为空")
        return v


class SubmitReceipt(BaseModel):
    section: str
    date: date
    replaced: bool = Field(description="是否整份替换了同日旧样")
    summaries_written: int = Field(description="本次实写汇总份数")
    sample_total_g: int = Field(description="本份日样总干重（克）")


class SectionResult(BaseModel):
    section: str
    summaries_read: int = Field(description="本断面实读汇总份数")
    total_weight_g: int
    grade_totals_g: list[int]
    cumulative_passing_percent: list[float] | None = Field(
        description="由细到粗的累计过筛率（%）；无样时为 null"
    )
    d50_mm: float | None = Field(
        description="从细到粗累计首次达到总干重一半的筛级上限；无样时为 null"
    )
    message: str | None


class QueryResult(BaseModel):
    start: date
    end: date
    year: int
    grades_mm: list[float]
    sections: list[SectionResult]


@app.post("/docs", response_model=SubmitReceipt, summary="提交/替换一份日样")
def submit_daily_sample(sample: DailySampleIn):
    conn = db.connect()
    try:
        outcome = store.submit_daily_sample(
            conn, sample.section, sample.date, sample.weights
        )
    finally:
        conn.close()
    return SubmitReceipt(
        section=sample.section,
        date=sample.date,
        replaced=outcome["replaced"],
        summaries_written=outcome["summaries_written"],
        sample_total_g=sum(sample.weights),
    )


@app.get("/query", response_model=QueryResult, summary="查询日段级配汇总")
def query_gradation(
    sections: Annotated[
        list[str],
        Query(min_length=1, description="1～4 个断面；可重复传参或逗号分隔"),
    ],
    start: Annotated[date, Query(description="起始日（含），须与截止日同一年")],
    end: Annotated[date, Query(description="截止日（含）")],
):
    if start > end:
        raise HTTPException(400, "起始日不能晚于截止日")
    if start.year != end.year:
        raise HTTPException(400, "起止日须在同一自然年内")

    # 兼容 ?sections=A&sections=B 与 ?sections=A,B 两种写法；去空白、去重。
    flat = []
    for item in sections:
        flat.extend(s.strip() for s in item.split(",") if s.strip())
    section_list = list(dict.fromkeys(flat))
    if not 1 <= len(section_list) <= 4:
        raise HTTPException(400, "断面数量须为 1～4 个")

    # 整次查询包在一个只读事务里：取自同一已提交状态。
    conn = db.connect()
    try:
        conn.execute("BEGIN")
        rows = [
            (s, *store.query_section_totals(conn, s, start, end))
            for s in section_list
        ]
        conn.execute("COMMIT")
    finally:
        conn.close()

    results = []
    for section, totals, reads in rows:
        total = sum(totals)
        if total == 0:
            results.append(
                SectionResult(
                    section=section,
                    summaries_read=reads,
                    total_weight_g=0,
                    grade_totals_g=totals,
                    cumulative_passing_percent=None,
                    d50_mm=None,
                    message="无样",
                )
            )
        else:
            cum = cumulative_from_fine(totals)
            results.append(
                SectionResult(
                    section=section,
                    summaries_read=reads,
                    total_weight_g=total,
                    grade_totals_g=totals,
                    cumulative_passing_percent=[
                        round(100.0 * c / total, 2) for c in cum
                    ],
                    d50_mm=d50_mm(totals),
                    message=None,
                )
            )
    return QueryResult(
        start=start, end=end, year=start.year, grades_mm=GRADES_MM, sections=results
    )


@app.get("/grades", summary="查看内置统一筛级")
def list_grades():
    return {"count": N_GRADES, "grades_mm": GRADES_MM}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index():
    return {
        "service": "疏浚泥沙合样级配系统",
        "interactive_docs": "/docs",
        "submit": "POST /docs",
        "query": "/query?start=YYYY-MM-DD&end=YYYY-MM-DD&sections=S1&sections=S2",
        "grades": "/grades",
    }
