"""Collect live follow-up outputs on synthetic fixtures for developer review."""

import argparse
import json
import sys
from pathlib import Path

from review_agent.agent import ReviewAgent
from review_agent.config import ROOT, Settings
from review_agent.llm import DeepSeekClient, ModelError
from review_agent.models import ReviewedFinding, ReviewInput


class RecordingClient:
    """Keep synthetic raw drafts in the evaluation artifact only, never in the UI."""

    def __init__(self, client):
        self.client = client
        self.responses = []

    @property
    def metrics(self):
        return self.client.metrics

    def complete(self, messages, **kwargs):
        response = self.client.complete(messages, **kwargs)
        self.responses.append(response)
        return response


def main() -> int:
    parser = argparse.ArgumentParser(
        description="真实 API 追问评估；只发送合成样例，不执行建议代码。"
    )
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "followup.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "quality-followup.json")
    args = parser.parse_args()
    settings = Settings.from_env()
    client = DeepSeekClient(settings)
    results = []
    try:
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for case in cases:
            print(f"运行追问 {case['id']}…", flush=True)
            request = ReviewInput(code=case["code"], filename=case["filename"])
            # This intentionally isolates follow-up quality from stochastic initial reviews.
            report = ReviewAgent().review(request)
            if case.get("legacy_pending"):
                report.pending_findings.append(ReviewedFinding(**case["legacy_pending"]))
            recording = RecordingClient(client)
            agent = ReviewAgent(recording, settings.model)
            entry = {**case, "model": settings.model, "source_hash": request.fingerprint}
            history = list(case.get("history", []))
            entry["turns"] = []
            for question in case["questions"]:
                before = len(recording.responses)
                try:
                    answer = agent.follow_up(request, report, question, history)
                    entry["turns"].append(
                        {
                            "question": question,
                            **agent.last_followup.model_dump(),
                            "raw_candidates": recording.responses[before:],
                        }
                    )
                    history += [
                        {"role": "user", "content": question},
                        {"role": "assistant", "content": answer},
                    ]
                except (ModelError, ValueError) as error:
                    entry["turns"].append({"question": question, "error": str(error)})
            entry["developer_review"] = {"reviewed": False, "passed": None, "notes": ""}
            results.append(entry)
            args.output.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"已保存 {len(results)} 个场景；需逐条检查语义与验证方法，不能只按本地校验通过判分。")
        return int(any("error" in turn for entry in results for turn in entry["turns"]))
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
