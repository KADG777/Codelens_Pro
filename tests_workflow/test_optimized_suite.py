"""Check manually specified frozen oracles before spending any API budget."""

import hashlib
import json
from pathlib import Path

import pytest

from compare_agent_value import summarize
from compare_workflows import audit_status, load_suite
from workflow.validation import audit_execute

ROOT = Path(__file__).resolve().parents[1]
REFERENCES = [
'''def merge_ranges(ranges):
    if not isinstance(ranges, list):
        raise ValueError()
    items = []
    for pair in ranges:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError()
        a, b = pair
        if type(a) is not int or type(b) is not int or a > b:
            raise ValueError()
        if a != b:
            items.append([a, b])
    result = []
    for a, b in sorted(items):
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([a, b])
    return result
''',
r'''def decode_runs(text):
    if not text:
        return ""
    i, total, parts = 0, 0, []
    while i < len(text):
        start = i
        while i < len(text) and text[i] in "0123456789":
            i += 1
        number = text[start:i]
        if not number or number[0] == "0" or len(number) > 3 or int(number) > 100:
            raise ValueError()
        if i >= len(text) or text[i] != ":":
            raise ValueError()
        i += 1
        if i >= len(text):
            raise ValueError()
        ch = text[i]
        i += 1
        if ch == "\\":
            if i >= len(text) or text[i] not in "\\,:":
                raise ValueError()
            ch = text[i]
            i += 1
        elif ch in ",:":
            raise ValueError()
        total += int(number)
        if total > 1000:
            raise ValueError()
        parts.append(ch * int(number))
        if i == len(text):
            return "".join(parts)
        if text[i] != "," or i + 1 == len(text):
            raise ValueError()
        i += 1
    return "".join(parts)
''',
'''def window_admit(events, limit, window):
    if type(limit) is not int or type(window) is not int or limit <= 0 or window <= 0:
        raise ValueError()
    if not isinstance(events, list) or any(type(t) is not int or t < 0 for t in events):
        raise ValueError()
    if any(a > b for a,b in zip(events, events[1:])):
        raise ValueError()
    accepted, output = [], []
    for t in events:
        accepted = [x for x in accepted if x > t - window]
        allowed = len(accepted) < limit
        output.append(allowed)
        if allowed:
            accepted.append(t)
    return output
''',
r'''import re
from urllib.parse import unquote_to_bytes
def parse_query(text):
    def decode(value):
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError()
        try:
            return unquote_to_bytes(value.replace("+", " ")).decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise ValueError() from None
    result = {}
    if text.startswith("?"):
        text = text[1:]
    for part in text.split("&"):
        if part:
            key, _, value = part.partition("=")
            result.setdefault(decode(key), []).append(decode(value))
    return result
''']


@pytest.mark.parametrize("index", range(4))
def test_frozen_public_and_hidden_oracles(monkeypatch, index):
    from workflow import registry
    monkeypatch.setattr(registry, "_TASKS", dict(registry._TASKS))
    suite, _ = load_suite(ROOT / "evals/optimized_cases.json")
    task_id = suite["cases"][index]["task_id"]
    for holdout in (False, True):
        outcome = audit_execute(REFERENCES[index], task_id, holdout=holdout)
        assert outcome["status"] == "passed", outcome


def test_published_results_keep_negative_evidence_and_shared_budget():
    suite_hash = hashlib.sha256((ROOT / "evals/optimized_cases.json").read_bytes()).hexdigest()
    first_raw = (ROOT / "evals/results/optimized-live.json").read_bytes()
    second_raw = (ROOT / "evals/results/optimized-retest.json").read_bytes()
    first, second = json.loads(first_raw), json.loads(second_raw)
    final = json.loads((ROOT / "evals/results/oracle-guard-check.json").read_bytes())
    for report in (first, second):
        assert report["completed_at"] and report["suite_sha256"] == suite_hash
        assert report["summary"] == summarize(report["entries"])
        assert len(report["entries"]) == 12
        assert {e["case_id"] for e in report["entries"]} == {e["case_id"] for e in first["entries"]}
    assert second["engineering_retest"]["prior_sha256"] == hashlib.sha256(first_raw).hexdigest()
    assert final["primary_sha256"] == hashlib.sha256(second_raw).hexdigest()
    assert final["deterministic_replay"]["source_record_preserved"] is True
    assert final["deterministic_replay"]["adopted"] is True
    assert audit_status(final["deterministic_replay"]) == "passed"
    assert final["live_outcome"] == audit_status(final["live"])
    assert final["combined_usage"]["calls"] <= final["authorized_max_calls"] == 140
    assert final["combined_usage"]["charged_tokens"] <= final["authorized_max_tokens"] == 200_000
    assert final["combined_usage"]["calls"] == second["budget_usage"]["calls"] + final["live"]["metrics"]["model_calls"]
