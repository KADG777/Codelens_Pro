"""Record real API/tool/report runs using only the bundled synthetic examples."""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from review_agent.agent import ReviewAgent
from review_agent.config import ROOT, Settings
from review_agent.llm import DeepSeekClient, ModelError
from review_agent.models import ReviewInput
from review_agent.reporting import to_markdown

CASES = [
    {
        "id": "python_defaults_and_zero",
        "filename": "synthetic.py",
        "code": "def remember(value, entries=[]):\n    entries.append(value)\n    return entries\n\ndef broken_ratio():\n    return 10 / 0\n",
        "expected_rules": ["AST002", "AST005"],
    },
    {
        "id": "cpp_delete_mismatch",
        "filename": "synthetic.cpp",
        "code": "void release() {\n    int* values = new int[4];\n    delete values;\n}\n",
        "expected_lines": [3],
    },
]


class RecordingClient:
    """Record public protocol fields, excluding credentials/hidden reasoning."""

    def __init__(self, client):
        self.client = client
        self.calls = []

    @property
    def metrics(self):
        return self.client.metrics

    def complete(self, messages, **kwargs):
        record = {
            "call": len(self.calls) + 1,
            "offered_tools": [t["function"]["name"] for t in kwargs.get("tools") or []],
            "tool_feedback": [
                {k: m[k] for k in ("role", "tool_call_id", "content") if k in m}
                for m in messages
                if m["role"] == "tool"
            ],
            "max_tokens": kwargs.get("max_tokens"),
        }
        self.calls.append(record)
        try:
            response = self.client.complete(messages, **kwargs)
        except ModelError:
            record["status"] = "failed"
            raise
        record["status"] = "completed"
        record["response"] = {
            key: response[key] for key in ("role", "content", "tool_calls") if key in response
        }
        return response


def save_record(path: Path, data: dict, key: str) -> None:
    # Normally no credential reaches this data: it is only sent in HTTP headers.
    # Keep a final exact-match safeguard for accidental changes to the recorder.
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    if key:
        serialized = serialized.replace(key, "[REDACTED]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized + "\n", encoding="utf-8")


def runtime_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted([ROOT / "app.py", *(ROOT / "review_agent").glob("*.py")]):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 DeepSeek 演示记录，只发送内置合成代码。")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "live-demo.json")
    args = parser.parse_args()
    settings = Settings.from_env()
    output = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": settings.model,
        "runtime_sha256": runtime_digest(),
        "python": sys.version.split()[0],
        "scope": "两份合成代码的真实 API 工具循环与复核；未执行被审查代码，不代表通用准确率。",
        "runs": [],
    }
    client = None
    try:
        client = DeepSeekClient(settings)
        for case in CASES:
            recording = RecordingClient(client)
            entry = {**case, "protocol": recording.calls}
            output["runs"].append(entry)
            print(f"运行 {case['id']}…", flush=True)
            try:
                request = ReviewInput(filename=case["filename"], code=case["code"])
                report = ReviewAgent(recording, settings.model).review(request)
                entry["report"] = report.model_dump()
                entry["markdown"] = to_markdown(report, request)
                rules = {f.rule_id for f in report.findings}
                lines = {f.line_start for f in report.findings}
                tools = {
                    call["function"]["name"]
                    for round_ in recording.calls
                    for call in round_.get("response", {}).get("tool_calls", [])
                }
                entry["checks"] = {
                    "actual_static_tool_called": "run_static_checks" in tools,
                    "reflection_completed": report.reflection_status == "completed",
                    "expected_found": set(case.get("expected_rules", [])).issubset(rules)
                    and set(case.get("expected_lines", [])).issubset(lines),
                }
            except ModelError as error:
                entry["error"] = str(error)
            save_record(args.output, output, settings.api_key)
        return int(any("error" in run or not all(run["checks"].values()) for run in output["runs"]))
    except (ValueError, ModelError) as error:
        print(f"演示未完成：{error}")
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
