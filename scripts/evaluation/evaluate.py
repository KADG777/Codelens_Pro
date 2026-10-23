"""Collect review predictions for human scoring; no unverified accuracy claims."""

import argparse
import json
import sys
from pathlib import Path

from review_agent.agent import ReviewAgent
from review_agent.config import ROOT, Settings
from review_agent.llm import DeepSeekClient, ModelError
from review_agent.models import ReviewInput


def main() -> int:
    parser = argparse.ArgumentParser(
        description="运行 Python / C++ 代码审查评估样例，输出待人工标注的结果。"
    )
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--strategy", choices=["direct", "tools", "full"], default="full")
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "evaluation.json")
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "evals" / "cases.json", help="评估用例 JSON 文件"
    )
    args = parser.parse_args()
    if args.demo and args.strategy == "direct":
        parser.error("--demo 不支持 direct；真实三组对比需要配置 DeepSeek API。")
    client = None
    try:
        settings = Settings.from_env()
        if not args.demo:
            client = DeepSeekClient(settings)
        agent = ReviewAgent(client, settings.model)
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        results = []
        args.output.parent.mkdir(parents=True, exist_ok=True)
        for case in cases:
            print(f"运行 {case['id']}…")
            entry = {
                **case,
                "strategy": args.strategy,
                "mode": "demo" if args.demo else "deepseek",
                "human_review": {
                    "reviewed": False,
                    "matches": [],
                    "false_positive_ids": [],
                    "notes": "",
                },
            }
            try:
                report = agent.review(
                    ReviewInput(
                        code=case["code"], filename=case.get("filename", case["id"] + ".py")
                    ),
                    args.strategy,
                )
                entry["report"] = report.model_dump()
            except ModelError as error:
                entry["error"] = str(error)
            results.append(entry)
            # Save after each case so a partial run remains inspectable.
            args.output.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"已保存 {len(results)} 个样例的结果：{args.output.resolve()}")
        print("请人工匹配报告与 expected，再计算准确率、召回率；离线结果不代表模型能力。")
        return int(any("error" in entry for entry in results))
    except (OSError, ValueError) as error:
        print(f"评估失败：{error}")
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
