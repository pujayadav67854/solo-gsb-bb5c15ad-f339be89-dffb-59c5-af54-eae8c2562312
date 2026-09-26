"""访谈字幕时间轴校准 API。

客户端提交以原录音毫秒计时的字幕片段，以及至少两组
"原时间 -> 成片时间" 同步锚点；服务端在相邻锚点间做线性换算，
并对超出锚点覆盖范围、换算后时长非正的片段返回错误。
"""
from __future__ import annotations

from bisect import bisect_right
from fractions import Fraction
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
class Anchor(BaseModel):
    """一组同步锚点：原录音时间 -> 成片时间（单位：毫秒）。"""

    original: int = Field(..., description="原录音时间（毫秒）")
    final: int = Field(..., description="成片时间（毫秒）")


class Segment(BaseModel):
    """一条以原录音毫秒计时的字幕片段。"""

    start: int = Field(..., description="片段原起始时间（毫秒）")
    end: int = Field(..., description="片段原结束时间（毫秒）")
    text: str = Field("", description="字幕文字")


class CalibrateRequest(BaseModel):
    anchors: List[Anchor] = Field(
        ..., description="同步锚点，至少两组；两列时间均须严格递增"
    )
    segments: List[Segment] = Field(..., description="待校准的字幕片段")

    @field_validator("segments")
    @classmethod
    def _segments_not_empty(cls, v: List[Segment]) -> List[Segment]:
        if not v:
            raise ValueError("segments 不能为空")
        return v


class CalibratedSegment(BaseModel):
    index: int = Field(..., description="片段在输入中的位置（从 0 开始）")
    start: int = Field(..., description="换算后起始时间（毫秒，整数）")
    end: int = Field(..., description="换算后结束时间（毫秒，整数）")
    text: str = Field("", description="原字幕文字")


class CalibrateResponse(BaseModel):
    segments: List[CalibratedSegment]


class ErrorItem(BaseModel):
    location: str = Field(..., description="出错位置，如 anchor / segment")
    index: Optional[int] = Field(None, description="出错元素下标（从 0 开始）")
    code: str = Field(..., description="错误代码")
    message: str = Field(..., description="错误说明")


# --------------------------------------------------------------------------- #
# 核心换算逻辑
# --------------------------------------------------------------------------- #
def round_half_up(value: Fraction) -> int:
    """四舍五入到整数（.5 一律向远离零方向进位）。"""
    q, r = divmod(value.numerator, value.denominator)
    if r * 2 >= value.denominator:
        q += 1
    return q


def build_anchor_lines(anchors: List[Anchor]) -> List[tuple]:
    """把相邻锚点预编译为 (o0, f0, slope) 线段，slope 为精确分数。"""
    lines = []
    for a, b in zip(anchors, anchors[1:]):
        slope = Fraction(b.final - a.final, b.original - a.original)
        lines.append((a.original, a.final, slope))
    return lines


def map_time(
    t: int,
    originals: List[int],
    finals: List[int],
    lines: List[tuple],
) -> Optional[Fraction]:
    """把单个原时间线性换算为成片时间；超出锚点覆盖范围返回 None。"""
    first, last = originals[0], originals[-1]
    if t < first or t > last:
        return None
    # 最后一个锚点原样命中；其余时间点归入其左侧锚点所在线段
    i = bisect_right(originals, t) - 1
    if i == len(originals) - 1:
        return Fraction(finals[-1])
    o0, f0, slope = lines[i]
    return f0 + slope * (t - o0)


def validate_anchors(anchors: List[Anchor]) -> List[ErrorItem]:
    """校验锚点数量及两列时间严格递增。"""
    errors: List[ErrorItem] = []
    if len(anchors) < 2:
        errors.append(
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_anchors",
                message="至少需要两组同步锚点",
            )
        )
        return errors

    for i in range(1, len(anchors)):
        if anchors[i].original <= anchors[i - 1].original:
            errors.append(
                ErrorItem(
                    location="anchor.original",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 original 未严格递增：第 {i} 组为 {anchors[i].original}，"
                        f"前一组为 {anchors[i - 1].original}"
                    ),
                )
            )
        if anchors[i].final <= anchors[i - 1].final:
            errors.append(
                ErrorItem(
                    location="anchor.final",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"锚点 final 未严格递增：第 {i} 组为 {anchors[i].final}，"
                        f"前一组为 {anchors[i - 1].final}"
                    ),
                )
            )
    return errors


def calibrate(req: CalibrateRequest) -> tuple[Optional[CalibrateResponse], List[ErrorItem]]:
    """执行校准；成功返回响应，失败返回错误列表。"""
    errors = validate_anchors(req.anchors)
    if errors:
        return None, errors

    lines = build_anchor_lines(req.anchors)
    originals = [a.original for a in req.anchors]
    finals = [a.final for a in req.anchors]
    results: List[CalibratedSegment] = []

    for idx, seg in enumerate(req.segments):
        if seg.end <= seg.start:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_duration",
                    message=(
                        f"原片段时长非正：start={seg.start}，end={seg.end}，要求 end > start"
                    ),
                )
            )
            continue

        start_mapped = map_time(seg.start, originals, finals, lines)
        end_mapped = map_time(seg.end, originals, finals, lines)
        if start_mapped is None or end_mapped is None:
            lo, hi = req.anchors[0].original, req.anchors[-1].original
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="out_of_anchor_range",
                    message=(
                        f"第 {idx} 段 [{seg.start}, {seg.end}] 超出锚点覆盖范围 "
                        f"[{lo}, {hi}]"
                    ),
                )
            )
            continue

        if end_mapped <= start_mapped:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration",
                    message=(
                        f"第 {idx} 段换算后时长非正："
                        f"start={float(start_mapped):.3f}ms，end={float(end_mapped):.3f}ms"
                    ),
                )
            )
            continue

        start_ms = round_half_up(start_mapped)
        end_ms = round_half_up(end_mapped)
        if end_ms <= start_ms:
            errors.append(
                ErrorItem(
                    location="segment",
                    index=idx,
                    code="non_positive_mapped_duration_after_rounding",
                    message=(
                        f"第 {idx} 段四舍五入后时长非正：start={start_ms}，end={end_ms}"
                    ),
                )
            )
            continue

        results.append(
            CalibratedSegment(
                index=idx, start=start_ms, end=end_ms, text=seg.text
            )
        )

    if errors:
        return None, errors
    return CalibrateResponse(segments=results), []


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="访谈字幕时间轴校准 API",
    description=(
        "根据至少两组「原时间 -> 成片时间」同步锚点，"
        "在相邻锚点间线性换算字幕片段的起止时间（毫秒）。"
    ),
    version="1.0.0",
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/calibrate", response_model=CalibrateResponse)
def calibrate_endpoint(req: CalibrateRequest) -> CalibrateResponse:
    resp, errors = calibrate(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "calibration_failed",
                "detail": [e.model_dump() for e in errors],
            },
        )
    return resp  # type: ignore[return-value]


@app.exception_handler(RequestValidationError)
def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": exc.errors()},
    )
