"""Pure presentation models for live workflow state and result evidence."""

from html import escape

from ui.diff_view import diff_counts
from ui.execution_graph import node_states
from workflow.models import requires_measured_evidence
from workflow.node_registry import default_registry

ROLES = [
    ("coding", "编码 Agent", "生成完整源码"),
    ("review", "审查 Agent", "问题、证据与需求核对"),
    ("validate", "独立验收", "静态规则与功能用例"),
    ("fix", "修复 Agent", "依据反馈更新源码"),
]
STATE_LABELS = {
    "waiting": "等待", "active": "正在执行", "done": "已提交", "skipped": "无需执行",
    "paused": "已暂停", "blocked": "环境阻塞", "failed": "执行中断", "attention": "待处理",
}


def version(snapshot: dict, version_id: str | None) -> dict:
    return next((v for v in snapshot.get("versions", []) if v.get("id") == version_id), {})


def active_nodes(snapshot: dict) -> set[str]:
    if "active_nodes" in snapshot:
        return set(snapshot.get("active_nodes") or [])
    event = snapshot.get("execution_event") or {}
    if event.get("kind") == "node_start" or snapshot.get("active"):
        return {snapshot.get("active_node") or event.get("node") or snapshot.get("phase")}
    return set()


def role_cards(snapshot: dict) -> list[dict]:
    current = version(snapshot, snapshot.get("current_version"))
    digest = current.get("hash")
    review, validation = snapshot.get("review") or {}, snapshot.get("validation") or {}
    events = snapshot.get("events") or []
    live = active_nodes(snapshot)
    status = snapshot.get("status", "created")
    next_node = (snapshot.get("pending_nodes") or [snapshot.get("next_node")])[0]
    terminal = status in {"completed_static", "completed_verified", "needs_attention", "failed"}
    cards = []
    extension_labels = {s.id: (s.label, s.description) for s in default_registry().all()}
    canvas_states = node_states(snapshot) if snapshot.get("workflow_config", {}).get("canvas") else {}
    roles = [*ROLES, *((node, *extension_labels.get(node, (node, "执行已注册角色的交接合同")))
                      for node in snapshot.get("workflow_config", {}).get("extension_nodes", []))]
    for node, title, purpose in roles:
        state, detail = "waiting", purpose
        done_events = [e for e in events if e.get("node") == node and not e.get("inherited")]
        if node == "coding":
            if snapshot.get("input_mode") == "upload":
                state, detail = "skipped", "已有上传源码，直接进入审查"
            elif snapshot.get("versions"):
                state = "done"
                detail = "已生成原始源码" if done_events else "继承已有源码，本轮未重复编码"
        elif node == "review":
            if review.get("code_hash") == digest and digest:
                state = "done" if review.get("complete") else "attention"
                detail = f"当前版：{len(review.get('findings', []))} 条问题，{len(review.get('pending_findings', []))} 条待确认"
            elif snapshot.get("review_history"):
                detail = "源码已更新，等待当前版本重新审查"
        elif node == "validate":
            if validation.get("code_hash") == digest and digest:
                state = "attention" if validation.get("status") == "error" else "done"
                passed, failed = validation.get("passed_count", 0), validation.get("failed_count", 0)
                detail = f"检查 {passed} 项通过 / {failed} 项未通过；" + ("已执行功能用例" if validation.get("functional_executed") else "功能未执行")
        elif node == "fix":
            records = [r for r in snapshot.get("repair_records", []) if not r.get("inherited")]
            if snapshot.get("termination_reason") == "candidate_rejected":
                state, detail = "attention", "候选复验未通过保护检查，当前源码保留上一版本"
            elif snapshot.get("termination_reason") in {"no_progress", "repeated_code"}:
                state, detail = "attention", "候选未形成新的有效源码版本"
            elif records:
                state, detail = "done", f"本轮已提交 {len(records)} 次修复，改后仍须审查与验收"
            elif terminal:
                state, detail = "skipped", "本轮没有执行修复"
        else:
            output = snapshot.get("node_outputs", {}).get(node, {})
            if output.get("code_hash") == digest and digest:
                summary = output.get("summary", "结构化交接已提交")
                state, detail = "done", summary[:100] + ("…" if len(summary) > 100 else "")
        if node in live:
            state = "active"
            round_no = snapshot.get("fix_round", 0)
            detail = {
                "coding": "正在根据目标生成完整源码，结果尚未提交",
                "review": f"第 {round_no} 轮修复后重新审查" if round_no else "首次审查：检查问题与需求",
                "validate": f"第 {round_no} 轮修复后重新验收" if round_no else "首次独立检查：执行已选验收方式",
                "fix": f"正在执行第 {round_no + 1} 轮修复，候选尚未提交",
            }.get(node, "正在解释当前版本证据，结果尚未提交")
        elif snapshot.get("execution_stage") == "branch_finished" and (snapshot.get("execution_event") or {}).get("node") == node:
            state, detail = "waiting", "分支计算完成，等待汇合后提交结果"
        elif status in {"paused", "blocked", "failed"} and not (status == "paused" and snapshot.get("execution_event")):
            affected = next_node if status == "paused" else snapshot.get("phase")
            branch_blocked = affected == "inspect" and status in {"blocked", "failed"} and (
                (node == "validate" and validation.get("status") == "error")
                or (node == "review" and review.get("code_hash") != digest))
            if affected == node or branch_blocked or canvas_states.get(node) == "paused":
                state = {"paused": "paused", "blocked": "blocked", "failed": "failed"}[status]
                detail = snapshot.get("error") or ("进度已保存，继续时从这里恢复" if status == "paused" else "保留已有源码与结果")
        if snapshot.get("canvas_cursor", {}).get("outcomes", {}).get(node) == "skipped":
            state, detail = "skipped", "条件分支未选择该节点"
        cards.append({"node": node, "title": title, "state": state, "label": STATE_LABELS[state], "detail": detail})
    return cards


def build_progress_html(snapshot: dict) -> str:
    cards = role_cards(snapshot)
    live = active_nodes(snapshot)
    parallel = bool((snapshot.get("workflow_config") or {}).get("parallel_checks"))
    status = snapshot.get("status", "created")
    running = "、".join(card["title"] for card in cards if card["node"] in live)
    header = f"正在执行：{running}" if running else {
        "created": "流程已准备", "paused": "进度已保存，等待继续", "blocked": "环境阻塞，保留已有进度",
        "completed_static": "静态流程完成，功能仍需验收", "completed_verified": "功能验收完成",
        "needs_attention": "本次流程结束，仍有事项待处理", "failed": "本次运行中断",
    }.get(status, "正在汇合与保存结果")
    if live and not running:
        header = "正在判断分支并保存结果"
    elif snapshot.get("execution_stage") == "branch_finished" and not live:
        header = "检查分支已完成，正在汇合提交"
    elif status == "paused" and (snapshot.get("execution_event") or {}).get("kind") == "node_end":
        header = "本阶段已提交，准备进入下一环节"
    rendered = "".join(
        f'<div class="flow-card {card["state"]}" data-node="{card["node"]}" data-state="{card["state"]}">'
        f'<div class="role-top"><b>{index:02d}</b><span>{escape(card["label"])}</span></div>'
        f'<h4>{escape(card["title"])}</h4><p>{escape(str(card["detail"]))}</p></div>'
        for index, card in enumerate(cards, 1)
    )
    path = "编码 → 审查 ∥ 独立验收 → 汇合判断" if parallel else "编码 → 审查 → 独立验收 → 判断"
    execution_label = "并行检查" if parallel else "顺序检查"
    if snapshot.get("workflow_config", {}).get("canvas"):
        path = "依据已冻结画布的依赖和条件逐波执行，实际路径见上方运行图"
        execution_label = "画布调度"
    return f"""<style>
.flow-board{{background:#f7fafc;border:1px solid #d8e4ec;border-radius:14px;padding:18px;margin:12px 0 22px;color:#18334b}}
.flow-heading{{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:14px;font-size:14px}}
.flow-heading strong{{font-size:17px}}.flow-heading span{{color:#506779;font-size:12px}}
.flow-grid{{display:grid;grid-template-columns:repeat({min(4, len(cards))},minmax(0,1fr));gap:10px}}
.flow-card{{background:white;border:1px solid #dbe4ea;border-radius:11px;padding:13px;min-height:128px}}
.flow-card h4{{font-size:15px;margin:10px 0 6px;color:#17344d}}.flow-card p{{font-size:12px;line-height:1.6;margin:0;color:#52697b}}
.role-top{{display:flex;justify-content:space-between;font-size:11px;color:#637b8e}}.role-top b{{font-size:13px}}
.flow-card.active{{background:#e9f8f5;border:2px solid #079d8a;box-shadow:0 0 0 3px #079d8a12}}
.flow-card.active .role-top{{color:#047c6c}}.flow-card.done{{border-top:3px solid #2c8c8a}}
.flow-card.skipped{{background:#f0f3f5;opacity:.8}}.flow-card.blocked,.flow-card.attention,.flow-card.paused{{border-top:3px solid #d99523}}
.flow-card.failed{{border-top:3px solid #c94c4c}}.flow-route{{font-size:12px;color:#456376;margin-top:13px;line-height:1.8}}
@media(max-width:750px){{.flow-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.flow-heading{{display:block}}}}
</style><section class="flow-board" aria-label="流程实时状态"><div class="flow-heading"><strong>{escape(header)}</strong><span>修复 {snapshot.get('fix_round', 0)} / {snapshot.get('max_fix_rounds', 2)} 轮 · {execution_label}</span></div><div class="flow-grid">{rendered}</div><div class="flow-route">{path}；有问题 → 修复 → 重新审查与验收。<br>正在执行的状态来自节点开始通知；只有已提交的源码与检查可作为结果证据。</div></section>"""


def result_summary(snapshot: dict) -> list[dict]:
    current = version(snapshot, snapshot.get("current_version"))
    original = version(snapshot, snapshot.get("original_version"))
    validation = snapshot.get("validation") or {}
    valid = bool(current.get("hash")) and validation.get("code_hash") == current.get("hash")
    added, removed = diff_counts(original.get("code", ""), current.get("code", ""))
    changed = bool(added or removed)
    static_checks = [c for c in validation.get("checks", []) if c.get("id") in {"syntax", "ruff", "interface"}]
    if not valid:
        static, static_detail = "待检查", "尚无当前版本对应的静态结果"
    elif any(c.get("status") in {"failed", "error"} for c in static_checks):
        static, static_detail = "未通过", "查看独立验证中的具体检查"
    elif static_checks and all(c.get("status") == "passed" for c in static_checks):
        static, static_detail = "已通过", f"{len(static_checks)} 项静态检查通过，源码未因此执行"
    else:
        static, static_detail = "未确认", "缺少可核对的静态检查项"
    executed = valid and validation.get("functional_executed")
    measured_goal = requires_measured_evidence(snapshot.get("task", ""))
    if executed:
        functional = "用例已通过" if validation.get("status") == "passed" else "用例未全通过"
        functional_detail = "当前源码已有实际用例记录" + ("；准确率或性能效果仍未测量" if measured_goal else "")
    else:
        functional, functional_detail = "尚未实测", "代码有变化不代表业务效果已达成"
    pending_items = (snapshot.get("review") or {}).get("pending_findings", [])
    unresolved = list(dict.fromkeys([str(i) for i in snapshot.get("unresolved", []) if i] + [str(i.get("title", "待确认意见")) for i in pending_items]))
    if snapshot.get("status") in {"created", "running", "paused", "blocked"}:
        review = snapshot.get("review") or {}
        if review.get("code_hash") == current.get("hash"):
            unresolved.extend(f.get("title", "审查问题") for f in review.get("findings", []) if f.get("severity") in {"high", "medium"})
        if valid:
            unresolved.extend(c.get("detail", "验收失败") for c in validation.get("checks", []) if c.get("status") in {"failed", "error"})
        unresolved = list(dict.fromkeys(unresolved))
    pending = len(pending_items)
    if measured_goal and not unresolved:
        unresolved.append("目标效果尚未进行同条件前后测量")
    return [
        {"name": "源码改动", "value": "已保存修改" if changed else ("尚未修改" if current else "等待源码"), "detail": f"新增 {added} 行 / 删除 {removed} 行" if changed else "保留真实源码，没有制造变化", "tone": "good" if changed else "neutral"},
        {"name": "静态检查", "value": static, "detail": static_detail, "tone": "good" if static == "已通过" else "neutral"},
        {"name": "功能与效果", "value": functional, "detail": functional_detail, "tone": "good" if executed and functional == "用例已通过" and not measured_goal else "pending"},
        {"name": "待确认事项", "value": f"{len(unresolved)} 项" if unresolved else (f"{pending} 条意见" if pending else "无额外待办"), "detail": unresolved[0] if unresolved else ("仍需核对待确认意见" if pending else "请结合本次验收范围使用结果"), "tone": "pending" if unresolved or pending else "neutral"},
    ]


def build_summary_html(snapshot: dict) -> str:
    cards = "".join(
        f'<div class="result-card {item["tone"]}"><span>{escape(item["name"])}</span><strong>{escape(item["value"])}</strong><p>{escape(item["detail"])}</p></div>'
        for item in result_summary(snapshot)
    )
    return f"""<style>.result-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0}}
.result-card{{padding:16px;border:1px solid #dbe5eb;border-radius:12px;background:white;color:#17344d}}
.result-card span{{font-size:12px;color:#617687}}.result-card strong{{display:block;font-size:20px;margin:6px 0}}
.result-card p{{font-size:12px;line-height:1.5;color:#536b7c;margin:0}}.result-card.good{{background:#f0faf7;border-color:#c7e8df}}
.result-card.pending{{background:#fffaf0;border-color:#f0e1bf}}@media(max-width:750px){{.result-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}</style><section aria-label="本次结果摘要" class="result-grid">{cards}</section>"""


def usage_summary(snapshot: dict) -> dict:
    keys = ("model_calls", "http_attempts", "prompt_tokens", "completion_tokens", "tool_calls", "active_seconds")
    current = {key: (snapshot.get("metrics") or {}).get(key, 0) for key in keys}
    inherited = {key: (snapshot.get("inherited_total_metrics") or snapshot.get("inherited_metrics") or {}).get(key, 0) for key in keys}
    return {"current": current, "inherited": inherited, "total": {key: current[key] + inherited[key] for key in keys}}


def branch_overlap_evidence(snapshot: dict) -> list[dict]:
    """Only measured intervals of matching source hashes can show overlap."""
    timings = snapshot.get("branch_timings") or []
    evidence = []
    for review in timings:
        if review.get("node") != "review":
            continue
        for validation in timings:
            if validation.get("node") != "validate" or validation.get("code_hash") != review.get("code_hash"):
                continue
            if not all(isinstance(item.get(key), (int, float)) for item in (review, validation) for key in ("start_clock", "end_clock")):
                continue
            overlap = min(review["end_clock"], validation["end_clock"]) - max(review["start_clock"], validation["start_clock"])
            if overlap > 0:
                evidence.append({"code_hash": review.get("code_hash", ""), "overlap_seconds": overlap,
                                 "review_seconds": review["end_clock"] - review["start_clock"],
                                 "validation_seconds": validation["end_clock"] - validation["start_clock"]})
    return evidence
