"""Pydantic models for the configuration payload and evaluation API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

# ---------------------------------------------------------------------------
# Configuration upload models
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrereqSpec(StrictModel):
    flag: str
    expect: bool = True


class ConditionSpec(StrictModel):
    # ``attr`` is required for attribute operators; a segment reference
    # (``op == "segment"``) names its target in ``value`` and needs no attr.
    attr: Optional[str] = None
    op: str
    value: Any = None

    @model_validator(mode="after")
    def _attr_required_unless_segment_ref(self) -> "ConditionSpec":
        if self.op != "segment" and self.attr is None:
            raise ValueError("attr is required unless op is 'segment'")
        return self


class SegmentSpec(StrictModel):
    """A reusable audience group: all conditions must hold (AND)."""

    conditions: list[ConditionSpec] = Field(default_factory=list)


class RuleSpec(StrictModel):
    id: Optional[str] = None
    conditions: list[ConditionSpec] = Field(default_factory=list)
    value: Any = True


class RolloutSpec(StrictModel):
    percentage: int
    key: str = "user_id"
    value: Any = True


class FlagSpec(StrictModel):
    enabled: bool = True
    default: Any = False
    prerequisites: list[PrereqSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    rollout: Optional[RolloutSpec] = None


class ConfigSpec(StrictModel):
    segments: dict[str, SegmentSpec] = Field(default_factory=dict)
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


class PublishConfigRequest(StrictModel):
    version: str = Field(min_length=1)
    segments: dict[str, SegmentSpec] = Field(default_factory=dict)
    flags: dict[str, FlagSpec] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PublishResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str


class VersionInfo(BaseModel):
    version: str
    flag_count: int
    created_at: str


class ConfigResponse(BaseModel):
    version: str
    flag_count: int
    created_at: str
    config: ConfigSpec


class ChainNode(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    prerequisite_of: Optional[str] = None
    expected: Optional[bool] = None


class EvalResult(BaseModel):
    flag: str
    value: Any
    hit: bool
    reason: str
    rule_id: Optional[str] = None
    rollout: Optional[dict[str, Any]] = None
    chain: list[ChainNode]


class BatchEvalResponse(BaseModel):
    version: str
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]


# ---------------------------------------------------------------------------
# Version comparison models
# ---------------------------------------------------------------------------


class ConfigComparisonRequest(StrictModel):
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)


class ImpactedFlag(BaseModel):
    flag: str
    path: list[str]


class FlagImpact(BaseModel):
    flag: str
    impacted: list[ImpactedFlag]


class ConfigComparisonResponse(BaseModel):
    from_version: str
    to_version: str
    added: list[str]
    removed: list[str]
    modified: list[str]
    impacts: list[FlagImpact]


class ErrorDetail(BaseModel):
    code: str
    message: str
    errors: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Evaluation request models
# ---------------------------------------------------------------------------


class BatchEvalRequest(StrictModel):
    version: str = Field(min_length=1)
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Environment pointer models
# ---------------------------------------------------------------------------


class EnvironmentUpsertRequest(StrictModel):
    version: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class EnvironmentResponse(BaseModel):
    name: str
    version: str
    revision: int
    created_at: str
    updated_at: str


class EnvironmentEvalRequest(StrictModel):
    flags: list[str] = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EnvironmentEvalResponse(BaseModel):
    name: str
    version: str
    revision: int
    context: dict[str, Any]
    results: list[EvalResult]
    missing_flags: list[str]


# ---------------------------------------------------------------------------
# Two-version evaluation preview models
# ---------------------------------------------------------------------------


class PreviewSubject(StrictModel):
    # ``subject_id`` identifies the row in the preview output (unique within
    # one request); ``user_id`` drives stable percentage rollouts.
    subject_id: str = Field(min_length=1)
    user_id: Optional[Any] = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EvaluationPreviewRequest(StrictModel):
    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)
    flags: list[str] = Field(min_length=1, max_length=50)
    subjects: list[PreviewSubject] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _subject_ids_must_be_unique(self) -> "EvaluationPreviewRequest":
        seen: set[str] = set()
        duplicates: list[str] = []
        for subject in self.subjects:
            if subject.subject_id in seen and subject.subject_id not in duplicates:
                duplicates.append(subject.subject_id)
            seen.add(subject.subject_id)
        if duplicates:
            raise PydanticCustomError(
                "duplicate_subject_id",
                "subject_id values must be unique within one request; "
                "duplicates: {duplicates}",
                {"duplicates": duplicates},
            )
        return self


class PreviewSide(BaseModel):
    exists: bool
    value: Any = None
    reason: str
    rule_id: Optional[str] = None


class PreviewChangedItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subject_id: str
    flag: str
    from_: PreviewSide = Field(alias="from")
    to: PreviewSide


class PreviewFlagSummary(BaseModel):
    flag: str
    changed: int
    unchanged: int


class EvaluationPreviewResponse(BaseModel):
    from_version: str
    to_version: str
    changed: list[PreviewChangedItem]
    summary: list[PreviewFlagSummary]
