"""访谈字幕时间轴校准 API。

客户端提交以原录音毫秒计时的字幕片段，以及至少两组
"原时间 -> 成片时间" 同步锚点；服务端在相邻锚点间做线性换算，
并对超出锚点覆盖范围、换算后时长非正的片段返回错误。
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

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


class SubtitleLine(BaseModel):
    """一条字幕：起始时间（整数毫秒）与文字。"""

    start: int = Field(..., description="片段起始时间（毫秒）")
    text: str = Field("", description="字幕文字")


class SuggestAnchorsRequest(BaseModel):
    original: List[SubtitleLine] = Field(
        ..., description="原片字幕序列，start 必须严格递增"
    )
    final: List[SubtitleLine] = Field(
        ..., description="成片字幕序列，start 必须严格递增"
    )


class SuggestedAnchor(Anchor):
    """一组由字幕匹配得到的同步锚点，并附来源下标。"""

    original_index: int = Field(..., description="原片字幕下标（从 0 开始）")
    final_index: int = Field(..., description="成片字幕下标（从 0 开始）")


class AnchorSuggestResponse(BaseModel):
    anchors: List[SuggestedAnchor]


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
# 锚点自动建议
# --------------------------------------------------------------------------- #
def normalize_text(text: str) -> str:
    """去首尾空白，并把连续空白（含制表/换行）统一为单个空格。"""
    return " ".join(text.split())


def validate_subtitle_track(
    lines: List[SubtitleLine], track: str
) -> List[ErrorItem]:
    """校验单条字幕轨道：start 非负且严格递增、文字去空白后非空。"""
    errors: List[ErrorItem] = []
    for i, line in enumerate(lines):
        if line.start < 0:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=i,
                    code="negative_time",
                    message=f"{track} 轨道第 {i} 条 start 为负：{line.start}",
                )
            )
        if i > 0 and line.start <= lines[i - 1].start:
            errors.append(
                ErrorItem(
                    location=f"{track}.start",
                    index=i,
                    code="not_strictly_increasing",
                    message=(
                        f"{track} 轨道 start 未严格递增：第 {i} 条为 {line.start}，"
                        f"前一条为 {lines[i - 1].start}"
                    ),
                )
            )
        if not normalize_text(line.text):
            errors.append(
                ErrorItem(
                    location=f"{track}.text",
                    index=i,
                    code="empty_text",
                    message=f"{track} 轨道第 {i} 条 text 去空白后为空",
                )
            )
    return errors


def _better(c: int, d: int, c2: int, d2: int) -> bool:
    """先比匹配数（多者优），再比序号差绝对值之和（少者优）。"""
    return c > c2 or (c == c2 and d < d2)


def _build_match_dp(
    a: List[str], b: List[str]
) -> Tuple[List[array], List[array]]:
    """后缀 LCS 动态规划。

    cnt[i][j]：仅使用 a[i:] 与 b[j:] 时的最大匹配数；
    dst[i][j]：达到该匹配数的前提下，最小的配对序号差绝对值之和。
    匹配仅允许发生在规范化后文字完全相等的条目之间，允许跳过任意片段。
    """
    n, m = len(a), len(b)
    cnt: List[array] = [array("i", [0]) * (m + 1) for _ in range(n + 1)]
    dst: List[array] = [array("i", [0]) * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        row_c, row_d = cnt[i], dst[i]
        down_c, down_d = cnt[i + 1], dst[i + 1]
        for j in range(m - 1, -1, -1):
            # 跳过 a[i] 或跳过 b[j]
            best_c, best_d = down_c[j], down_d[j]
            if _better(row_c[j + 1], row_d[j + 1], best_c, best_d):
                best_c, best_d = row_c[j + 1], row_d[j + 1]
            # 配对 a[i] 与 b[j]
            if a[i] == b[j]:
                mc = 1 + down_c[j + 1]
                md = abs(i - j) + down_d[j + 1]
                if _better(mc, md, best_c, best_d):
                    best_c, best_d = mc, md
            row_c[j], row_d[j] = best_c, best_d
    return cnt, dst


def _reconstruct_pairs(
    a: List[str],
    b: List[str],
    cnt: List[array],
    dst: List[array],
) -> List[Tuple[int, int]]:
    """在匹配数与序号差之和均最优的解中，取配对序号序列字典序最小者。

    每个状态按 (p, q) 字典序扫描可配的文字相等条目，选第一个与后缀
    DP 完全吻合的；相等时配对产生的首对序号一定不晚于任何跳过方案，
    因此字典序最优解必然包含该对，可贪心前移。
    """
    # 成片中每种规范化文字出现的下标（有序），支持快速枚举候选配对
    positions: Dict[str, List[int]] = {}
    for j, text in enumerate(b):
        positions.setdefault(text, []).append(j)

    pairs: List[Tuple[int, int]] = []
    i, j = 0, 0
    n, m = len(a), len(b)
    while i < n and j < m:
        total_c, total_d = cnt[i][j], dst[i][j]
        if total_c == 0:
            break
        chosen: Optional[Tuple[int, int]] = None
        for p in range(i, n):
            found = None
            occ = positions.get(a[p])
            if occ:
                # 下标升序，同一 p 下按 q 字典序；失败的候选不会再被后续状态访问
                for q in occ[bisect_left(occ, j):]:
                    if (
                        1 + cnt[p + 1][q + 1] == total_c
                        and abs(p - q) + dst[p + 1][q + 1] == total_d
                    ):
                        found = (p, q)
                        break
            if found is not None:
                chosen = found
                break
        if chosen is None:  # 理论上不可达：DP 已证明存在最优解
            break
        pairs.append(chosen)
        i, j = chosen[0] + 1, chosen[1] + 1
    return pairs


def suggest_anchors(
    req: SuggestAnchorsRequest,
) -> Tuple[Optional[AnchorSuggestResponse], List[ErrorItem]]:
    """根据两条字幕序列的文字匹配，全局最优地推导同步锚点。"""
    errors = validate_subtitle_track(req.original, "original")
    errors += validate_subtitle_track(req.final, "final")
    if errors:
        return None, errors

    a = [normalize_text(line.text) for line in req.original]
    b = [normalize_text(line.text) for line in req.final]
    cnt, dst = _build_match_dp(a, b)

    if cnt[0][0] < 2:
        return None, [
            ErrorItem(
                location="anchors",
                index=None,
                code="not_enough_matches",
                message=(
                    f"可用匹配仅 {cnt[0][0]} 对，至少需要 2 对才能生成同步锚点"
                ),
            )
        ]

    pairs = _reconstruct_pairs(a, b, cnt, dst)
    return (
        AnchorSuggestResponse(
            anchors=[
                SuggestedAnchor(
                    original=req.original[p].start,
                    final=req.final[q].start,
                    original_index=p,
                    final_index=q,
                )
                for p, q in pairs
            ]
        ),
        [],
    )


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


@app.post("/anchors/suggest", response_model=AnchorSuggestResponse)
def suggest_anchors_endpoint(req: SuggestAnchorsRequest) -> AnchorSuggestResponse:
    resp, errors = suggest_anchors(req)
    if errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "anchor_suggestion_failed",
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
