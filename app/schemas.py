"""Pydantic request models.

Times are integer milliseconds.  Every interval in this API is half-open:
``start`` is included, ``end`` is excluded.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceReelCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200, description="原片名称")


class FilmCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200, description="成片名称")


class SubtitleLineIn(StrictModel):
    number: int = Field(ge=0, description="台词编号，在同一原片字幕内唯一")
    start_ms: int = Field(ge=0, description="原片起点毫秒（含）")
    end_ms: int = Field(description="原片终点毫秒（不含）")
    text: str = Field(min_length=1, description="台词文字")

    @model_validator(mode="after")
    def _end_after_start(self) -> "SubtitleLineIn":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class SubtitleDocumentIn(StrictModel):
    lines: list[SubtitleLineIn] = Field(
        default_factory=list, description="已审台词；允许提交空字幕版本"
    )

    @model_validator(mode="after")
    def _unique_numbers(self) -> "SubtitleDocumentIn":
        seen: set[int] = set()
        for line in self.lines:
            if line.number in seen:
                raise ValueError(f"duplicate line number {line.number}")
            seen.add(line.number)
        return self


class EditSegmentIn(StrictModel):
    source_reel_id: int = Field(gt=0, description="原片号")
    source_start_ms: int = Field(ge=0, description="原片起点毫秒（含）")
    source_end_ms: int = Field(description="原片终点毫秒（不含）")
    output_start_ms: int = Field(ge=0, description="成片起点毫秒（含）")

    @model_validator(mode="after")
    def _end_after_start(self) -> "EditSegmentIn":
        if self.source_end_ms <= self.source_start_ms:
            raise ValueError("source_end_ms must be greater than source_start_ms")
        return self


class EditTableIn(StrictModel):
    segments: list[EditSegmentIn] = Field(
        min_length=1, description="剪辑表各段；顺序不限，成片上必须互不重叠"
    )
