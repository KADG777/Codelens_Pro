"""Trace facts across source versions without converting agent claims to proof."""

import hashlib
from difflib import SequenceMatcher

from ui.progress import result_summary, usage_summary, version


def change_blocks(before: str, after: str) -> list[dict]:
    blocks = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, before.splitlines(True), after.splitlines(True), autojunk=False).get_opcodes():
        if tag != "equal":
            blocks.append({"number": len(blocks) + 1, "kind": tag, "old_start": i1 + 1 if i2 > i1 else None,
                           "old_end": i2 if i2 > i1 else None, "new_start": j1 + 1 if j2 > j1 else None,
                           "new_end": j2 if j2 > j1 else None, "insert_at": i1 + 1})
    return blocks


def _same_kind(left: dict, right: dict) -> bool:
    if left.get("rule_id") and right.get("rule_id"):
        return left["rule_id"] == right["rule_id"]
    return str(left.get("title", "")).strip().casefold() == str(right.get("title", "")).strip().casefold()


def issue_traces(snapshot: dict) -> list[dict]:
    versions = snapshot.get("versions") or []
    by_hash = {v["hash"]: v for v in versions}
    reviews = list(snapshot.get("review_history") or [])
    if snapshot.get("review"):
        reviews.append(snapshot["review"])
    validations = list(snapshot.get("validation_history") or [])
    if snapshot.get("validation"):
        validations.append(snapshot["validation"])
    traces, seen = [], set()
    for review in reviews:
        source = by_hash.get(review.get("code_hash"))
        if not source:
            continue
        for finding in review.get("findings", []):
            identity = hashlib.sha256((finding.get("title", "") + "|" + finding.get("rule_id", "")).encode("utf-8")).hexdigest()[:8]
            key = f"{source['hash']}:{finding.get('id', '?')}:{identity}"
            if key in seen:
                continue
            seen.add(key)
            records = [r for r in snapshot.get("repair_records", []) if r.get("base_code_hash") == source["hash"]
                       and finding.get("id") in r.get("addressed_finding_ids", [])]
            record = records[-1] if records else {}
            candidate = str(record.get("revised_code", "")).replace("\r\n", "\n").replace("\r", "\n")
            target = next((v for v in versions if v.get("parent") == source["id"] and v.get("code") == candidate), {}) if record else {}
            blocks = change_blocks(source["code"], target["code"]) if target else []
            start = int(finding.get("line_start", finding.get("line", 1)))
            end = int(finding.get("line_end", start))
            near_blocks = [b for b in blocks if (b["old_start"] is not None and b["old_start"] <= end and b["old_end"] >= start)
                           or (b["old_start"] is None and start <= b["insert_at"] <= end + 1)]
            verification = next((r for r in reversed(reviews) if target and r.get("code_hash") == target["hash"]), {})
            if not target:
                review_status = "尚未形成关联修复版本" if record else "没有模型声明的修复关联"
            elif not verification:
                review_status = "新版本尚未复查"
            elif any(_same_kind(finding, f) for f in verification.get("findings", [])):
                review_status = "复查仍检出同类问题"
            elif any(_same_kind(finding, f) for f in verification.get("pending_findings", [])):
                review_status = "同类意见仍待确认"
            elif not verification.get("complete"):
                review_status = "复查尚未完整完成"
            else:
                review_status = "复查未再检出同类问题"
            validation = next((v for v in reversed(validations) if target and v.get("code_hash") == target["hash"]), {})
            if not validation:
                validation_status = "无关联新版本的验证结果"
            elif not validation.get("functional_executed"):
                validation_status = "功能未执行；静态结果见独立验证"
            elif validation.get("status") == "passed":
                validation_status = "该版本功能用例通过（非单问题证明）"
            else:
                validation_status = "该版本功能用例未全部通过"
            traces.append({"key": key, "label": f"{finding.get('id', '?')} @ {source['label']} · {finding.get('title', '')}",
                           "source": source, "finding": finding, "record": record, "target": target,
                           "blocks": blocks, "near_blocks": near_blocks, "review_status": review_status,
                           "validation_status": validation_status, "source_run_id": record.get("source_run_id") if record.get("inherited") else snapshot.get("run_id")})
    # Test IDs are stable within a frozen acceptance contract; do not invent source lines.
    for validation in validations:
        source = by_hash.get(validation.get("code_hash"))
        if not source:
            continue
        for check in validation.get("checks", []):
            if check.get("status") != "failed":
                continue
            check_id = str(check.get("id", "check"))
            finding_id = "V-" + check_id
            key = f"{source['hash']}:{finding_id}"
            if key in seen:
                continue
            seen.add(key)
            records = [r for r in snapshot.get("repair_records", []) if r.get("base_code_hash") == source["hash"]
                       and finding_id in r.get("addressed_finding_ids", [])]
            record = records[-1] if records else {}
            candidate = str(record.get("revised_code", "")).replace("\r\n", "\n").replace("\r", "\n")
            target = next((v for v in versions if v.get("parent") == source["id"]
                           and (not record or v.get("code") == candidate)), {})
            outcome = next((v for v in reversed(validations) if target and v.get("code_hash") == target["hash"]), {})
            retest = next((c for c in outcome.get("checks", []) if str(c.get("id")) == check_id), {})
            static = check_id in {"syntax", "ruff", "interface", "preserved_interfaces"}
            if not target:
                status = "未形成修复版本，尚未重新验收"
            elif not retest:
                status = "新版本缺少同一用例的重新验收结果"
            elif retest.get("status") == "passed" and (static or outcome.get("functional_executed")):
                status = "同一用例重新验收通过" + ("（静态检查）" if static else "（已实际执行）")
            elif retest.get("status") == "failed":
                status = "同一用例重新验收仍失败"
            else:
                status = "同一用例未完成有效重新验收（跳过或环境错误不算通过）"
            blocks = change_blocks(source["code"], target["code"]) if target else []
            traces.append({"key": key, "kind": "validation", "label": f"{finding_id} @ {source['label']} · {check.get('name', check_id)}",
                           "source": source, "finding": {"id": finding_id, "title": check.get("name", check_id),
                           "explanation": check.get("detail", ""), "expected": check.get("expected"), "actual": check.get("actual")},
                           "record": record, "target": target, "blocks": blocks, "near_blocks": [],
                           "link_basis": "claim" if record else "lineage", "original_check": check, "retest": retest,
                           "review_status": "按相同用例 ID 与新版本源码哈希核对", "validation_status": status,
                           "source_run_id": record.get("source_run_id", snapshot.get("run_id"))})
    return traces


def report_markdown(snapshot: dict) -> str:
    current = version(snapshot, snapshot.get("current_version"))
    lines = ["# CodeLens Flow 运行报告", "", f"运行：{snapshot.get('run_id', '')}",
             f"模式：{'预设流程' if snapshot.get('mode') == 'demo' else '真实模型'}", f"目标：{snapshot.get('task', '')}",
             f"状态：{snapshot.get('status', '')}", f"当前源码版本：{current.get('label', '')}",
             f"当前源码 SHA-256：{current.get('hash', '')}", "", "## 本次结果", ""]
    for item in result_summary(snapshot):
        lines.append(f"- {item['name']}：{item['value']}；{item['detail']}")
    parent = snapshot.get("parent_run_id") or snapshot.get("retry_of")
    if parent:
        lines.extend(["", f"来源运行：{parent}；本次使用独立新预算。"])
    plan = snapshot.get("workflow_config") or {}
    lines.extend(["", "## 执行计划", "", f"检查方式：{'并行检查后汇合' if plan.get('parallel_checks') else '顺序执行'}",
                  f"修复循环：{'开启' if plan.get('enable_repair', True) else '关闭'}；最多 {plan.get('max_fix_rounds', snapshot.get('max_fix_rounds', 2))} 轮",
                  f"审查后暂停：{'开启' if plan.get('pause_after_review') else '关闭'}", f"验收合同：{snapshot.get('acceptance_id', '自定义目标')}"])
    usage = usage_summary(snapshot)
    lines.extend(["", "## 用量", "", f"本轮模型调用：{usage['current']['model_calls']}；输入与输出 token：{usage['current']['prompt_tokens'] + usage['current']['completion_tokens']}",
                  f"已记录来源合计模型调用：{usage['total']['model_calls']}；输入与输出 token：{usage['total']['prompt_tokens'] + usage['total']['completion_tokens']}",
                  "", "## 问题、修改与复查", ""])
    for trace in issue_traces(snapshot):
        finding = trace["finding"]
        location = ("验收用例：" + finding["id"]) if trace.get("kind") == "validation" else f"定位：{finding.get('line_start', '?')}–{finding.get('line_end', finding.get('line_start', '?'))} 行"
        lines.extend([f"### {trace['label']}", f"问题版本：{trace['source']['hash']}",
                      location,
                      f"修复 Agent 声明：{trace['record'].get('change_summary', '没有声明关联')}",
                      f"关联版本：{trace['target'].get('label', '未形成新版本')}", f"复查：{trace['review_status']}",
                      f"验证：{trace['validation_status']}", ""])
    lines.extend(["## 节点执行记录", "", "|节点|轮次|耗时（秒）|状态|", "|---|---:|---:|---|"])
    for record in snapshot.get("node_runs", []):
        lines.append(f"|{record['node']}|{record['round']}|{record['duration_seconds']:.3f}|{record['status']}|")
    lines.extend(["", "并行分支与汇合耗时存在重叠，不能直接相加为总耗时。", "", "## 证据边界", "", "红绿差异证明文本变化；模型声明、复查未再检出和实际功能验收是不同证据，不能互相替代。", "报告保留用户目标与问题描述，不导出模型连接配置。"])
    return "\n".join(lines) + "\n"
