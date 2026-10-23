"""Compare two saved engines on identical source; submitted code is never executed."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


def worker(args):
    sys.path.insert(0, str(args.engine_root.resolve()))
    from review_agent.agent import ReviewAgent
    from review_agent.config import Settings
    from review_agent.files import read_source_file
    from review_agent.llm import DeepSeekClient, ModelError
    from review_agent.models import ReviewInput

    request = ReviewInput(code=read_source_file(args.file), filename=args.file.name)
    settings = Settings.from_env()
    client = DeepSeekClient(settings)
    calls = []
    complete = client.complete

    def measured(messages, **kwargs):
        before = client.metrics.model_copy(deep=True)
        started = time.perf_counter()
        try:
            return complete(messages, **kwargs)
        finally:
            calls.append(
                {
                    "call": len(calls) + 1,
                    "seconds": round(time.perf_counter() - started, 3),
                    "request_characters": len(json.dumps(messages, ensure_ascii=False)),
                    "tools_available": bool(kwargs.get("tools")),
                    **{
                        key: getattr(client.metrics, key) - getattr(before, key)
                        for key in ("prompt_tokens", "completion_tokens", "http_attempts")
                    },
                }
            )

    client.complete = measured
    entry = {
        "source_hash": request.fingerprint,
        "filename": request.filename,
        "engine_root": str(args.engine_root.resolve()),
        "model": settings.model,
    }
    try:
        report = ReviewAgent(client, settings.model).review(request)
        entry["report"] = report.model_dump()
    except ModelError as error:
        entry["error"] = str(error)
    finally:
        client.close()
    entry["calls"] = calls
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "error": entry.get("error"),
                "metrics": entry.get("report", {}).get("metrics", {}),
            },
            ensure_ascii=False,
        )
    )
    return int("error" in entry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, default=ROOT / "reports/baseline_v2")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/performance_v3")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--engine-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    if args.worker:
        return worker(args)
    if not (args.baseline_root / "review_agent/agent.py").is_file():
        parser.error("基线目录须包含改动前的 review_agent 包。")
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for repeat in range(1, args.repeats + 1):
        order = [("before", args.baseline_root), ("after", ROOT)]
        if repeat % 2 == 0:
            order.reverse()
        for label, root in order:
            output = args.output / f"{label}-{repeat}.json"
            print(f"运行 {label} 第 {repeat} 次（同一文件，只审查不执行）…", flush=True)
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--engine-root",
                    str(root),
                    "--file",
                    str(args.file.resolve()),
                    "--output",
                    str(output.resolve()),
                ],
                check=True,
                cwd=ROOT,
            )
            results.append(
                {
                    "version": label,
                    "repeat": repeat,
                    **json.loads(output.read_text(encoding="utf-8")),
                }
            )
            (args.output / "comparison.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
