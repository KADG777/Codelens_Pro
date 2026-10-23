"""Reproduce the labelled, scripted workflow demonstration without an API key."""

import argparse
import json
from pathlib import Path

from workflow import WorkflowRunService
from workflow.examples import ORDER_TASK


def main() -> None:
    parser = argparse.ArgumentParser(description="CodeLens Flow 无密钥预设流程演示")
    parser.add_argument("--db", type=Path, default=Path("data/workflow-demo.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("reports/workflow-demo.json"))
    parser.add_argument("--resume", help="继续已经保存的运行 ID")
    parser.add_argument("--pause-after-review", action="store_true")
    args = parser.parse_args()
    service = WorkflowRunService(args.db)
    run_id = args.resume or service.create_run(
        task=ORDER_TASK,
        mode="demo",
        scenario="orders",
        validation_profile="static_only",
    )
    snapshot = service.execute(
        run_id,
        stop_after="review" if args.pause_after_review else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"运行 ID：{run_id}")
    print(f"状态：{snapshot.get('status')}；修复轮数：{snapshot.get('fix_round', 0)}")
    print(f"记录：{args.output.resolve()}")
    print("模式：预设流程演示，未调用真实模型，未执行被审查源码。")


if __name__ == "__main__":
    main()
