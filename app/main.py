"""FastAPI 接口层。

路由：
- GET  /docs   Swagger 交互文档（FastAPI 默认），可在页面里直接 Try it out 提交。
- POST /docs   按断面和日期提交日样；支持标准 11 档 weights 与相邻内置档合并的
                bins 两种格式（可互换，同断面同日重交整份替换）。
- GET  /query  任选同一自然年内起止日（含首尾）与 1～4 个断面，查各档合计干重、
                累计过筛率与 D50；所选日样的非零合并档跨过内置档界时，暂停该
                断面的标准级配与 D50 并列出被跨过的档界；总干重为零提示"无样"。
- GET  /grades 查看内置统一筛级。
- GET  /healthz 健康检查。
"""
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from . import db, store
from .gradation import (
    BOUNDARIES_MM,
    GRADES_MM,
    N_GRADES,
    BinParseError,
    crossed_boundaries,
    cumulative_from_fine,
    d50_mm,
    parse_merged_bins,
)


@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(
    title="疏浚泥沙合样级配系统",
    version="2.0.0",
    lifespan=lifespan,
)

# 非负整数克：strict 拒绝字符串/浮点/布尔等隐式转换。
NonNegGrams = Annotated[int, Field(strict=True, ge=0)]


class BinIn(BaseModel):
    """一个相邻内置档合并档：档上限（mm，取内置档上限）与该档干重（克）。"""

    upper_mm: Annotated[float, Field(strict=True)] = Field(
        description="合并档上限，须取内置筛级上限之一，按升序排列，最粗为 64 mm"
    )
    grams: NonNegGrams


class DailySampleIn(BaseModel):
    section: str = Field(min_length=1, max_length=64, examples=["S1"])
    date: date
    weights: list[NonNegGrams] | None = Field(
        default=None,
        description=(
            f"标准格式：各内置筛级干重（克），须恰好 {N_GRADES} 档，"
            "顺序与 /grades 一致；与 bins 二选一"
        ),
    )
    bins: list[BinIn] | None = Field(
        default=None,
        description=(
            "合并档格式：相邻内置档合并后的各档（档上限 + 非负整数克重），"
            "上限升序且最粗为 64 mm；与 weights 二选一，两种格式可互换"
        ),
    )

    @field_validator("section")
    @classmethod
    def _strip_section(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("断面名不能为空")
        return v

    @model_validator(mode="after")
    def _exactly_one_format(self):
        if self.weights is None and self.bins is None:
            raise ValueError("须提供 weights（标准 11 档）或 bins（合并档）之一")
        if self.weights is not None and self.bins is not None:
            raise ValueError("weights 与 bins 只能二选一，不可同时提供")
        if self.weights is not None and len(self.weights) != N_GRADES:
            raise ValueError(f"weights 须恰好 {N_GRADES} 档")
        return self

    def normalized(self):
        """统一解析为 (11 档克重, 跨界掩码)。"""
        if self.weights is not None:
            return list(self.weights), 0
        try:
            return parse_merged_bins([(b.upper_mm, b.grams) for b in self.bins])
        except BinParseError as exc:
            raise ValueError(str(exc)) from exc


class SubmitReceipt(BaseModel):
    section: str
    date: date
    replaced: bool = Field(description="是否整份替换了同日旧样")
    summaries_written: int = Field(description="本次实写汇总份数")
    sample_total_g: int = Field(description="本份日样总干重（克）")
    merged: bool = Field(description="本次是否以合并档（bins）格式上报")


class SectionResult(BaseModel):
    section: str
    summaries_read: int = Field(description="本断面实读汇总份数")
    total_weight_g: int
    grade_totals_g: list[int] | None = Field(
        description="各内置档合计干重（克）；档界未全部确定时为 null"
    )
    cumulative_passing_percent: list[float] | None = Field(
        description="由细到粗的累计过筛率（%）；无样或级配暂停时为 null"
    )
    d50_mm: float | None = Field(
        description="从细到粗累计首次达到总干重一半的筛级上限；无样或暂停时为 null"
    )
    crossed_boundaries_mm: list[float] = Field(
        default_factory=list,
        description="被所选日样的非零合并档跨过的内置档界（mm，升序）；非空时级配暂停",
    )
    message: str | None


class QueryResult(BaseModel):
    start: date
    end: date
    year: int
    grades_mm: list[float]
    boundaries_mm: list[float] = Field(description="内置档界（相邻内置档的分界，mm）")
    sections: list[SectionResult]


@app.post("/docs", response_model=SubmitReceipt, summary="提交/替换一份日样")
def submit_daily_sample(sample: DailySampleIn):
    # 合并档语义校验在解析阶段完成；非法输入归为 422。
    try:
        weights, mask = sample.normalized()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    conn = db.connect()
    try:
        outcome = store.submit_daily_sample(
            conn, sample.section, sample.date, weights, mask
        )
    finally:
        conn.close()
    return SubmitReceipt(
        section=sample.section,
        date=sample.date,
        replaced=outcome["replaced"],
        summaries_written=outcome["summaries_written"],
        sample_total_g=sum(weights),
        merged=sample.bins is not None,
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
    for section, totals, crossed, reads in rows:
        total = sum(totals)
        if total == 0:
            # 无样（或所选日样干重全为零）：级配无从谈起。
            results.append(
                SectionResult(
                    section=section,
                    summaries_read=reads,
                    total_weight_g=0,
                    grade_totals_g=None,
                    cumulative_passing_percent=None,
                    d50_mm=None,
                    crossed_boundaries_mm=[],
                    message="无样",
                )
            )
            continue

        boundaries = crossed_boundaries(_mask_of_counts(crossed))
        if boundaries:
            # 有非零合并档跨过内置档界：标准级配与 D50 暂停，仅列档界与总干重。
            results.append(
                SectionResult(
                    section=section,
                    summaries_read=reads,
                    total_weight_g=total,
                    grade_totals_g=None,
                    cumulative_passing_percent=None,
                    d50_mm=None,
                    crossed_boundaries_mm=boundaries,
                    message="存在跨档合并档，标准级配与 D50 暂停",
                )
            )
        else:
            # 档界全部确定：各档合计可信，照常供数。
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
                    crossed_boundaries_mm=[],
                    message=None,
                )
            )
    return QueryResult(
        start=start,
        end=end,
        year=start.year,
        grades_mm=GRADES_MM,
        boundaries_mm=BOUNDARIES_MM,
        sections=results,
    )


def _mask_of_counts(counts):
    """把各档界跨界次数（>0 即被跨过）折叠回掩码。"""
    mask = 0
    for b, c in enumerate(counts):
        if c > 0:
            mask |= 1 << b
    return mask


@app.get("/grades", summary="查看内置统一筛级")
def list_grades():
    return {
        "count": N_GRADES,
        "grades_mm": GRADES_MM,
        "boundaries_mm": BOUNDARIES_MM,
        "coarsest_mm": GRADES_MM[-1],
    }


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
