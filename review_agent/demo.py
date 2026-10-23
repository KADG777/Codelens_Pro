"""Deterministic simulator of tool selection. It does not emulate LLM reasoning."""

import json

from review_agent.languages import LANGUAGE_LABELS
from review_agent.models import Draft, Finding, Metrics
from review_agent.selection import select_findings, selection_notice


class DemoClient:
    def __init__(self):
        self.metrics = Metrics()

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_output: bool = False,
        max_tokens: int = 6000,
    ) -> dict:
        if tools:
            used = {
                call["function"]["name"]
                for message in messages
                if message["role"] == "assistant"
                for call in message.get("tool_calls", [])
            }
            for name in ["read_source", "parse_structure", "run_static_checks"]:
                if name not in used:
                    return {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"demo_{name}",
                                "type": "function",
                                "function": {"name": name, "arguments": "{}"},
                            }
                        ],
                    }
            return {"role": "assistant", "content": "离线工具阶段完成。"}
        payload = json.loads(messages[-1]["content"])
        language = payload.get("language", "python")
        result = payload.get("tool_results", {}).get("run_static_checks", {})
        raw = result.get("builtin_findings", []) + result.get("ruff", {}).get("findings", [])
        retained, stats = select_findings((Finding.model_validate(item) for item in raw), 30)
        draft = Draft(
            summary=f"{LANGUAGE_LABELS[language]} 离线检查收到 {stats['unique_count']} 条不同候选问题，保留 {stats['retained_count']} 条；结果来自本地规则，尚未进行模型语义分析。",
            findings=retained,
            limitations=[
                f"演示模式使用固定的工具调用顺序，仅覆盖{'C++ 语法树规则' if language == 'cpp' else 'Python AST 与 Ruff 规则'}，不能判断复杂业务逻辑。",
                "未执行代码或单元测试；没有报告问题不代表代码完全正确。",
            ],
        )
        for notice in (
            result.get("truncation_notice", ""),
            selection_notice(stats, "离线报告候选"),
        ):
            if notice:
                draft.limitations.append(notice)
        return {"role": "assistant", "content": draft.model_dump_json()}
