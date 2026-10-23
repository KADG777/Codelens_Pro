"""Recompute metrics from explicit, exhaustive developer annotations, never keywords."""

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path

LABELS = {"matched", "incorrect", "needs_context", "advice", "duplicate"}


def score_run(entries: list[dict], annotations: dict) -> dict:
    if set(annotations) != {entry["id"] for entry in entries}:
        raise ValueError("标注与结果的样例 ID 必须完全一致。")
    counts = {bucket: Counter() for bucket in ("findings", "pending_findings")}
    expected = recalled = formal_recalled = 0
    rows, metrics = [], []
    for entry in entries:
        report = entry.get("report", {})
        labels = annotations[entry["id"]]
        available = {
            f"{bucket}:{i}": bucket
            for bucket in counts
            for i, _ in enumerate(report.get(bucket, []), 1)
        }
        if set(available) != set(labels):
            raise ValueError(f"{entry['id']} 的每条正式/待确认意见都必须且只能标注一次。")
        matches, formal = set(), set()
        row_counts = Counter()
        for key, annotation in labels.items():
            outcome = annotation["outcome"]
            if outcome not in LABELS or not annotation.get("reason", "").strip():
                raise ValueError("每条标注需要合法分类与具体理由。")
            if outcome in {"matched", "duplicate"}:
                index = annotation.get("expected_index")
                if type(index) is not int or not 1 <= index <= len(entry["expected"]):
                    raise ValueError("匹配编号越界。")
                if outcome == "matched":
                    if index in matches:
                        raise ValueError("同一缺陷不能重复计分，应标记 duplicate。")
                    matches.add(index)
                    if available[key] == "findings":
                        formal.add(index)
            counts[available[key]][outcome] += 1
            row_counts[outcome] += 1
        expected += len(entry["expected"])
        recalled += len(matches)
        formal_recalled += len(formal)
        if report:
            metrics.append(report["metrics"])
        rows.append(
            {
                "id": entry["id"],
                "expected": len(entry["expected"]),
                "matched": len(matches),
                "formal_matched": len(formal),
                "missed": len(entry["expected"]) - len(matches),
                "counts": dict(row_counts),
                "failed": not bool(report),
            }
        )
    combined = counts["findings"] + counts["pending_findings"]
    result = {
        "cases": len(entries),
        "completed": len(metrics),
        "expected_defects": expected,
        "recalled": recalled,
        "formal_recalled": formal_recalled,
        "recall": recalled / expected if expected else None,
        "formal_recall": formal_recalled / expected if expected else None,
        "counts": {**{k: dict(v) for k, v in counts.items()}, "all": dict(combined)},
        "visible_items": combined.total(),
        "formal_items": counts["findings"].total(),
        # Advice and unknown-context items remain in this denominator, explicitly.
        "known_defect_fraction": combined["matched"] / combined.total() if combined else None,
        "formal_supported_fraction": counts["findings"]["matched"] / counts["findings"].total()
        if counts["findings"]
        else None,
        "rows": rows,
    }
    for field in ("elapsed_seconds", "model_calls", "prompt_tokens", "completion_tokens"):
        values = [m[field] for m in metrics]
        result[field] = (
            {"mean": round(statistics.mean(values), 3), "min": min(values), "max": max(values)}
            if values
            else None
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按完整开发标注计分，区分已知缺陷、错误、缺少上下文和建议。"
    )
    parser.add_argument(
        "--annotations", type=Path, default=Path("evals/results/quality-annotations.json")
    )
    parser.add_argument("--output", type=Path, default=Path("evals/results/quality-summary.json"))
    args = parser.parse_args()
    annotations = json.loads(args.annotations.read_text(encoding="utf-8"))
    summaries = {}
    for strategy, run in annotations["runs"].items():
        path = args.annotations.parent / run["file"]
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != run["sha256"]:
            raise ValueError(f"{strategy} 原始结果哈希改变，需重新确认标注。")
        summaries[strategy] = score_run(json.loads(raw), run["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
