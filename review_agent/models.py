"""Shared schemas for model output, exported reports, and evaluation."""

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_agent.config import MAX_FOCUS_CHARS, MAX_SOURCE_BYTES, MAX_SOURCE_LINES
from review_agent.languages import LanguageName, detect_language


class ReviewInput(BaseModel):
    code: str
    filename: str = "review.py"
    focus: str = "重点检查潜在 Bug、边界条件、异常处理和可维护性。"

    @model_validator(mode="after")
    def validate_input(self) -> "ReviewInput":
        self.code = self.code.replace("\r\n", "\n").replace("\r", "\n")
        if not self.code.strip():
            raise ValueError("请输入或上传需要审查的 Python 或 C++ 代码。")
        if "\x00" in self.code:
            raise ValueError("代码包含空字节，请上传文本形式的源代码文件。")
        if len(self.code.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError(f"单文件最多 {MAX_SOURCE_BYTES // 1000} KB（UTF-8）。")
        if len(self.code.splitlines()) > MAX_SOURCE_LINES:
            raise ValueError(f"单文件最多 {MAX_SOURCE_LINES} 行，请缩小审查范围。")
        if len(self.focus) > MAX_FOCUS_CHARS:
            raise ValueError(f"审查目标最多 {MAX_FOCUS_CHARS} 个字符。")
        self.filename = self.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if len(self.filename) > 150:
            raise ValueError("文件名不能超过 150 字符。")
        detect_language(self.filename)
        if any(ord(char) < 32 for char in self.filename):
            raise ValueError("文件名不能包含控制字符。")
        return self

    @property
    def language(self) -> LanguageName:
        return detect_language(self.filename)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8")).hexdigest()[:12]


EvidenceRole = Literal["related", "training", "inference", "condition", "consumer", "source"]
TriggerBasis = Literal["unspecified", "local_input", "external_condition"]


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: EvidenceRole = "related"
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)
    evidence: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def ordered_range(self) -> "EvidenceReference":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    severity: Literal["high", "medium", "low"]
    category: Literal["Bug", "异常处理", "可维护性", "安全性"]
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)
    explanation: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=2000)
    rule_id: str = Field(default="", max_length=50)
    evidence_role: EvidenceRole = "related"
    related_evidence: list[EvidenceReference] = Field(default_factory=list, max_length=4)
    trigger: str = Field(default="", max_length=400)
    trigger_basis: TriggerBasis = "unspecified"

    @model_validator(mode="after")
    def ordered_range(self) -> "Finding":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(default="审查完成", min_length=1, max_length=2000)
    findings: list[Finding] = Field(max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class AuditDecision(BaseModel):
    """One verdict per candidate; the primary citation stays immutable."""

    model_config = ConfigDict(extra="forbid")
    finding_id: int = Field(ge=1, strict=True)
    verdict: Literal["supported", "uncertain", "rejected"]
    reason: str = Field(min_length=1, max_length=240)
    basis: Literal["local_code", "external_assumption"]
    impact_kind: Literal[
        "runtime_failure",
        "wrong_result",
        "resource_or_security",
        "maintainability",
        "diagnostic",
        "style",
    ]
    severity: Literal["high", "medium", "low"] | None = None
    correction: str = Field(
        default="", max_length=400, description="仅在原解释需修正时填写，含具体条件与影响"
    )
    suggestion: str = Field(default="", max_length=400, description="仅在原建议错误时填写替代建议")
    title: str | None = Field(
        default=None, min_length=1, max_length=160, description="仅收窄混合问题的标题"
    )
    trigger: str | None = Field(default=None, max_length=400)
    trigger_basis: TriggerBasis | None = None
    evidence_role: EvidenceRole | None = None
    related_evidence: list[EvidenceReference] | None = Field(default=None, max_length=4)


class AuditBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: list[AuditDecision] = Field(max_length=30)


class ReviewedFinding(Finding):
    # Assigned by the application, never accepted as a model-supplied trust label.
    verification: Literal["static", "reviewed", "pending"] = "pending"
    review_note: str = ""
    impact: str = ""
    verification_hint: str = ""


class VerificationRecord(BaseModel):
    finding_id: int
    title: str
    status: Literal[
        "accepted",
        "pending",
        "rejected",
        "withheld",
        "relocated",
        "rule_removed",
        "duplicate",
        "recovered",
        "truncated",
    ]
    reason: str


class Event(BaseModel):
    stage: str
    detail: str


class StageMetric(BaseModel):
    stage: str
    kind: Literal["model", "tool", "local"]
    elapsed_seconds: float
    model_calls: int = 0
    http_attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    status: Literal["completed", "failed"] = "completed"


class Metrics(BaseModel):
    model_calls: int = 0
    http_attempts: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0
    stages: list[StageMetric] = Field(default_factory=list)


class ReviewReport(BaseModel):
    schema_version: int = 5
    filename: str
    language: LanguageName = "python"
    source_hash: str
    mode: Literal["demo", "deepseek"]
    model: str
    strategy: Literal["direct", "tools", "full"]
    summary: str
    findings: list[ReviewedFinding]
    pending_findings: list[ReviewedFinding] = Field(default_factory=list)
    verification_records: list[VerificationRecord] = Field(default_factory=list)
    semantic_facts: list[dict] = Field(default_factory=list)
    calculations: list[dict] = Field(default_factory=list)
    limitations: list[str]
    reflection_status: Literal["completed", "failed", "skipped", "local_only"]
    checks: dict[str, str]
    events: list[Event]
    metrics: Metrics
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def normalize_draft(draft: Draft, request: ReviewInput) -> Draft:
    """Validate locations and remove exact duplicates, without claiming semantic proof."""
    lines = request.code.splitlines()
    kept, seen = [], set()
    limitations = list(draft.limitations)
    for finding in draft.findings:
        if finding.line_end > len(lines):
            limitations.append(f"已剔除位置无效的问题：{finding.title}，需人工确认。")
            continue
        key = (finding.line_start, finding.line_end, finding.title.strip().casefold())
        if key not in seen:
            seen.add(key)
            kept.append(finding)
    order = {"high": 0, "medium": 1, "low": 2}
    kept.sort(key=lambda item: (order[item.severity], item.line_start))
    return Draft(
        summary=draft.summary,
        findings=kept,
        limitations=list(dict.fromkeys(limitations))[:20],
    )
