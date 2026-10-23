"""Bounded tool/report loop, compact review, source-bound facts and stage timing."""

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, ValidationError

from review_agent.cpp_tools import CPP_LIMITATIONS
from review_agent.demo import DemoClient
from review_agent.followup import FollowupResult, run_followup
from review_agent.languages import LANGUAGE_LABELS
from review_agent.llm import ChatModel, ModelError
from review_agent.models import (
    AuditBatch,
    Draft,
    Event,
    Metrics,
    ReviewInput,
    ReviewReport,
    StageMetric,
)
from review_agent.prompts import (
    REFLECTION_SYSTEM,
    REVIEW_SYSTEM,
    schema_instruction,
)
from review_agent.semantics import source_facts
from review_agent.tools import TOOL_SCHEMAS, ToolRegistry, compact_tool_result
from review_agent.verification import (
    finalize_candidates,
    reconcile_candidates,
    report_summary,
    validate_audit_ids,
)

Strategy = Literal["direct", "tools", "full"]


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def numbered_source(request: ReviewInput) -> str:
    return "\n".join(f"{index}: {line}" for index, line in enumerate(request.code.splitlines(), 1))


class ReviewAgent:
    MAX_TOOL_ROUNDS = 4
    MAX_TOOL_CALLS = 8
    DRAFT_TOKENS = 3200
    AUDIT_TOKENS = 1600

    def __init__(self, model: ChatModel | None = None, model_name: str = "deepseek-flash"):
        self.model = model if model is not None else DemoClient()
        self.demo = isinstance(self.model, DemoClient)
        self.model_name = "本地规则演示" if self.demo else model_name
        self.stages: list[StageMetric] = []
        self.last_followup: FollowupResult | None = None

    @contextmanager
    def _measure(self, stage: str, kind: str):
        started = time.perf_counter()
        before = self.model.metrics.model_copy(deep=True)
        status = "completed"
        try:
            yield
        except Exception:
            status = "failed"
            raise
        finally:
            self.stages.append(
                StageMetric(
                    stage=stage,
                    kind=kind,
                    status=status,
                    elapsed_seconds=round(time.perf_counter() - started, 3),
                    **{
                        key: getattr(self.model.metrics, key) - getattr(before, key)
                        for key in (
                            "model_calls",
                            "http_attempts",
                            "prompt_tokens",
                            "completion_tokens",
                        )
                    },
                )
            )

    def _call(self, messages, *, stage, tools=None, max_tokens=DRAFT_TOKENS):
        with self._measure(stage, "model"):
            return self.model.complete(
                messages, tools=tools, json_output=True, max_tokens=max_tokens
            )

    def _structured(
        self,
        messages,
        schema: type[BaseModel],
        *,
        stage,
        validate=None,
        response=None,
        max_tokens=DRAFT_TOKENS,
    ):
        for attempt in range(2):
            if response is None:
                response = self._call(messages, stage=stage, max_tokens=max_tokens)
            try:
                parsed = schema.model_validate_json(response.get("content") or "")
                if validate:
                    validate(parsed)
                return parsed
            except (ValidationError, TypeError, ValueError):
                if attempt == 0:
                    messages = [
                        *messages,
                        {
                            "role": "user",
                            "content": "格式无效。仅返回符合 JSON schema 的对象；复核须逐个覆盖全部候选 ID，不得遗漏、重复或新增。",
                        },
                    ]
                    response = None
        raise ModelError("模型连续两次返回不符合报告格式的内容，请重试或缩小代码范围。")

    def review(
        self,
        request: ReviewInput,
        strategy: Strategy = "full",
        on_event: Callable[[Event], None] | None = None,
    ) -> ReviewReport:
        if strategy not in {"direct", "tools", "full"}:
            raise ValueError("不支持的审查策略。")
        if self.demo and strategy == "direct":
            raise ValueError("离线演示没有模型语义能力，不支持直接模型审查基线。")
        started = time.perf_counter()
        initial = self.model.metrics.model_copy(deep=True)
        self.stages = []
        events, warnings = [], list(CPP_LIMITATIONS) if request.language == "cpp" else []
        registry, tool_count = ToolRegistry(request), 0

        def emit(stage, detail):
            event = Event(stage=stage, detail=detail)
            events.append(event)
            if on_event:
                on_event(event)

        def execute(name, arguments):
            nonlocal tool_count
            tool_count += 1
            with self._measure(name, "tool"):
                result = registry.execute(name, arguments)
            emit("工具执行", f"{name} · {'参数或工具错误' if 'error' in result else '结果已返回'}")
            return result

        emit(
            "输入校验",
            f"{request.filename} · {LANGUAGE_LABELS[request.language]} · {len(request.code.splitlines())} 行 · 快照 {request.fingerprint}",
        )
        with self._measure("源码事实核对", "local"):
            facts = source_facts(request.code, request.language)
            registry.results["check_semantics"] = {"facts": facts}
        emit("源码事实核对", f"提取 {len(facts)} 条计算/API 事实；未执行被审查代码。")
        payload = {
            "filename": request.filename,
            "language": request.language,
            "focus": request.focus,
            "numbered_source": numbered_source(request),
            "semantic_facts": facts,
        }
        messages = [
            {"role": "system", "content": REVIEW_SYSTEM + schema_instruction()},
            {"role": "user", "content": encode(payload)},
        ]

        if self.demo:
            for name in ("read_source", "parse_structure", "run_static_checks"):
                execute(name, "{}")
            draft = Draft(
                findings=[],  # Real tool results are collected centrally below.
                limitations=["离线演示只覆盖本地规则，未进行模型语义分析。"],
            )
        elif strategy == "direct":
            draft = self._structured(messages, Draft, stage="直接初审")
        else:
            draft = None
            for round_index in range(self.MAX_TOOL_ROUNDS):
                stage = "工具规划" if round_index == 0 else "工具反馈与初审"
                emit(stage, f"第 {round_index + 1} 轮：可继续调用工具，也可直接输出报告 JSON。")
                available = [
                    schema
                    for schema in TOOL_SCHEMAS
                    if schema["function"]["name"] not in {"read_source", "check_semantics"}
                    and schema["function"]["name"] not in registry.results
                ]
                response = self._call(messages, stage=stage, tools=available)
                calls = response.get("tool_calls") or []
                if not calls:
                    draft = self._structured(
                        messages, Draft, stage="初审格式修复", response=response
                    )
                    break
                if not isinstance(calls, list) or len(calls) > 32:
                    raise ModelError("模型返回了不合法或过多的工具请求。")
                ids = set()
                for call in calls:
                    if (
                        not isinstance(call, dict)
                        or not isinstance(call.get("id"), str)
                        or not call["id"]
                        or call["id"] in ids
                        or not isinstance(call.get("function"), dict)
                        or not isinstance(call["function"].get("name"), str)
                        or not isinstance(call["function"].get("arguments"), str)
                    ):
                        raise ModelError("模型返回了不合法的工具调用结构。")
                    ids.add(call["id"])
                messages.append({**response, "content": None})
                for call in calls:
                    name = call["function"]["name"]
                    result = (
                        {"error": "已达到工具调用上限。"}
                        if tool_count >= self.MAX_TOOL_CALLS
                        else execute(name, call["function"]["arguments"])
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": encode(compact_tool_result(name, result)),
                        }
                    )
                if tool_count >= self.MAX_TOOL_CALLS:
                    warnings.append("达到工具调用上限，基于已收集证据生成报告。")
                    break
            else:
                warnings.append("达到工具规划轮次上限，基于已收集证据生成报告。")
            if draft is None:
                draft = self._structured(messages, Draft, stage="预算结束后初审")

        if strategy != "direct":
            for name in ("parse_structure", "run_static_checks"):
                if name not in registry.results:
                    warnings.append(f"本次模型未完成 {name} 工具，相关检查覆盖不足。")
            for name, status in registry.checks.items():
                if status not in {"completed", "syntax_error"}:
                    warnings.append(f"{name} 状态为 {status}，该部分检查未完成。")
            static_result = registry.results.get("run_static_checks", {})
            if static_result.get("truncated") or static_result.get("ruff", {}).get("truncated"):
                warnings.append(
                    static_result.get("truncation_notice")
                    or "静态检查候选项过多，工具结果已截断，可能遗漏其他问题。"
                )

        with self._measure("证据校验", "local"):
            candidates, records, reconciliation_warnings = reconcile_candidates(
                draft.findings,
                request,
                registry.results,
                facts=facts,
                model_draft=not self.demo,
            )
            warnings.extend(reconciliation_warnings)
        for warning in reconciliation_warnings:
            emit("候选完整性", warning)
        eligible = [
            item
            for item in candidates
            if not item.grounding_error
            and (item.finding.severity in {"high", "medium"} or item.needs_review)
        ]
        emit(
            "证据校验",
            f"{len(eligible)} 条候选需复核（高/中优先级或证据链/工具背书检查）；普通低优先级意见仍可待确认。",
        )
        audit, reflection_status = None, "local_only" if self.demo else "skipped"
        if strategy == "full" and not self.demo and eligible:
            emit("反思复核", "按事实逐条返回简短裁决，仅修正必要字段。")
            audit_payload = {
                **payload,
                "calculations": registry.calculations,
                "candidates": [
                    {
                        "finding_id": item.finding_id,
                        **item.finding.model_dump(),
                        "quality_errors": item.quality_errors,
                        "actual_static": item.static,
                    }
                    for item in eligible
                ],
            }
            audit_messages = [
                {"role": "system", "content": REFLECTION_SYSTEM + schema_instruction(AuditBatch)},
                {"role": "user", "content": encode(audit_payload)},
            ]
            try:
                audit = self._structured(
                    audit_messages,
                    AuditBatch,
                    stage="反思复核",
                    validate=lambda batch: validate_audit_ids(
                        batch, {item.finding_id for item in eligible}
                    ),
                    # Recovered static results may fill the 30-candidate cap.
                    # Keep ordinary reviews unchanged; bound larger audits too.
                    max_tokens=min(6000, max(self.AUDIT_TOKENS, 160 * len(eligible))),
                )
                reflection_status = "completed"
            except ModelError:
                reflection_status = "failed"
                warnings.append("模型复核失败，缺少实际工具支持的候选移入待确认，不计入正式问题。")
                emit("反思复核", "失败；实际静态发现保留，模型推测进入待确认。")
        with self._measure("报告整理", "local"):
            findings, pending, records = finalize_candidates(
                candidates, audit, records, reflection_status=reflection_status, facts=facts
            )
        withheld = sum(record.status == "withheld" for record in records)
        if withheld:
            warnings.append(
                f"{withheld} 条候选因证据不足未展示，原因见校验记录；这不等于证明这些问题不存在。"
            )
        warnings.append(
            "源码事实与静态规则有适用条件；未执行代码或测试，模型复核仍可能误报或漏报。"
        )
        emit("报告完成", f"保留 {len(findings)} 条问题与建议，{len(pending)} 条待确认。")
        metrics = Metrics(
            **{
                key: getattr(self.model.metrics, key) - getattr(initial, key)
                for key in ("model_calls", "http_attempts", "prompt_tokens", "completion_tokens")
            },
            tool_calls=tool_count,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            stages=self.stages,
        )
        return ReviewReport(
            filename=request.filename,
            language=request.language,
            source_hash=request.fingerprint,
            mode="demo" if self.demo else "deepseek",
            model=self.model_name,
            strategy=strategy,
            summary=report_summary(findings, pending),
            findings=findings,
            pending_findings=pending,
            verification_records=records,
            semantic_facts=facts,
            calculations=registry.calculations,
            limitations=list(dict.fromkeys(warnings + draft.limitations)),
            reflection_status=reflection_status,
            checks=registry.checks,
            events=events,
            metrics=metrics,
        )

    def follow_up(
        self,
        request: ReviewInput,
        report: ReviewReport,
        question: str,
        history: list[dict] | None = None,
    ) -> str:
        self.last_followup = None
        if not question.strip() or len(question) > 1500:
            raise ValueError("追问不能为空，且不能超过 1500 字符。")
        if (
            request.fingerprint != report.source_hash
            or request.filename != report.filename
            or request.language != report.language
        ):
            raise ValueError("代码已更改，请重新审查后再追问。")
        if self.demo:
            return "当前为离线演示，追问需要 DeepSeek。请切换到 DeepSeek 模式并重新审查；现在可展开问题卡片查看证据与修改建议。"
        self.last_followup = run_followup(self.model, request, report, question, history or [])
        return self.last_followup.answer
