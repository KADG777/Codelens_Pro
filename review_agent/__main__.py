"""Command-line entry point: python -m review_agent examples/buggy_order.py --demo."""

import argparse
import sys
from pathlib import Path

from review_agent.agent import ReviewAgent
from review_agent.config import Settings
from review_agent.files import read_source_file
from review_agent.llm import DeepSeekClient, ModelError
from review_agent.models import ReviewInput
from review_agent.reporting import to_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description="Python / C++ 代码审查 Agent")
    parser.add_argument("file", type=Path, help="要审查的单个 Python 或 C++ 源文件 / 头文件")
    parser.add_argument("--demo", action="store_true", help="不调用 API，使用离线规则演示")
    parser.add_argument("--strategy", choices=["direct", "tools", "full"], default="full")
    parser.add_argument("--focus", default="重点检查潜在 Bug、边界情况和异常处理。")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--output", type=Path, help="可选，保存报告到指定文件")
    args = parser.parse_args()
    client = None
    try:
        request = ReviewInput(
            code=read_source_file(args.file), filename=args.file.name, focus=args.focus
        )
        settings = Settings.from_env()
        if not args.demo:
            client = DeepSeekClient(settings)
        report = ReviewAgent(client, settings.model).review(
            request,
            args.strategy,
            on_event=lambda event: print(f"[{event.stage}] {event.detail}", file=sys.stderr),
        )
        content = (
            report.model_dump_json(indent=2)
            if args.format == "json"
            else to_markdown(report, request)
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
            print(f"报告已保存：{args.output.resolve()}", file=sys.stderr)
        else:
            print(content)
        return 0
    except (OSError, ValueError, ModelError) as error:
        print(f"审查失败：{error}", file=sys.stderr)
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
