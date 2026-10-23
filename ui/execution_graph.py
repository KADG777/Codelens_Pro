"""One routing picture for both planning and persisted execution evidence."""

from html import escape
from urllib.parse import urlencode

import streamlit as st

from workflow.node_registry import default_registry

LABELS = {"coding": "编码 Agent", "review": "审查 Agent", "validate": "独立验收", "gate": "汇合 / 判断",
          "fix": "修复 Agent", "finish": "完成 / 待处理", "inspect": "并行汇合"}
LABELS.update({s.id: s.label for s in default_registry().all()})
COLORS = {"waiting": ("#f4f7fb", "#9aaebf"), "active": ("#e3f0ff", "#2267d7"),
          "finished": ("#e5f5ef", "#198264"), "blocked": ("#fff0ed", "#c4533e"), "paused": ("#fff6dc", "#ab7b15")}


def node_states(snapshot: dict) -> dict:
    states = {}
    for record in snapshot.get("node_runs", []):
        states[record["node"]] = "blocked" if record.get("error") or record.get("status") == "needs_attention" else "finished"
    for node in snapshot.get("active_nodes", []):
        states[node] = "active"
    if snapshot.get("status") == "paused":
        for node in snapshot.get("pending_nodes", []):
            states[node] = "paused"
    if snapshot.get("status") == "blocked":
        states[snapshot.get("phase")] = "blocked"
    return states


def build_execution_svg(plan: dict, snapshot: dict | None = None, selected: str = "") -> str:
    live = snapshot is not None
    snapshot = snapshot or {}
    parallel = plan.get("parallel_checks", False)
    extensions = plan.get("extension_nodes", [])
    repair = plan.get("enable_repair", True) and plan.get("max_fix_rounds", 2) > 0
    positions = {"coding": (25, 90), "review": (235, 25 if parallel else 90),
                 "validate": (235 if parallel else 445, 155 if parallel else 90),
                 "gate": (655, 90 + 90 * len(extensions)), "fix": (445, 300 + 90 * len(extensions)),
                 "finish": (655, 300 + 90 * len(extensions))}
    for index, node in enumerate(extensions):
        positions[node] = (655, 90 + 90 * index)
    edges = [("coding", "review"), ("review", "validate")] if not parallel else [("coding", "review"), ("coding", "validate")]
    after = [*extensions, "gate"]
    edges += [("validate", after[0])]
    if parallel:
        edges.append(("review", after[0]))
    edges += list(zip(after, after[1:])) + [("gate", "finish")]
    if repair:
        edges += [("gate", "fix"), ("fix", "review")]
        if parallel:
            edges.append(("fix", "validate"))
    states = node_states(snapshot)
    observed = set()
    for record in snapshot.get("node_runs", []):
        if record.get("error"):
            continue
        source, target = record["node"], record.get("output", {}).get("next_node")
        if source == "inspect":
            observed.update((branch, target) for branch in ("review", "validate"))
        elif target == "inspect":
            observed.update((source, branch) for branch in ("review", "validate"))
        else:
            observed.add((source, target))
        if source == "gate" and record.get("status") in {"completed_verified", "completed_static", "needs_attention"}:
            observed.add(("gate", "finish"))
    paths, boxes = [], []
    for source, target in edges:
        sx, sy = positions[source]
        tx, ty = positions[target]
        taken = (source, target) in observed
        active = states.get(target) == "active" and (taken or source in {"coding", "fix"} and states.get(source) == "finished")
        color = "#2267d7" if active else "#198264" if taken else "#bac8d4"
        if source == "fix" and target in {"review", "validate"}:
            d = f"M{sx} {sy+30} H{tx-25} V{ty+30} H{tx}"
        elif abs(sy-ty) > 80 and sx == tx:
            d = f"M{sx+82} {sy+60} V{ty}"
        else:
            d = f"M{sx+165} {sy+30} H{(sx+165+tx)/2} V{ty+30} H{tx}"
        paths.append(f'<path data-edge="{escape(source)}:{escape(target)}" d="{d}" fill="none" stroke="{color}" stroke-width="{3 if taken or active else 1.5}" marker-end="url(#route-arrow)"/>')
    for node, (x, y) in positions.items():
        state = states.get(node, "waiting")
        if node == "finish" and snapshot.get("status") in {"completed_static", "completed_verified", "needs_attention"}:
            state = "blocked" if snapshot["status"] == "needs_attention" else "finished"
        fill, border = COLORS[state]
        skipped = node == "fix" and not repair or node == "coding" and snapshot.get("input_mode") == "upload"
        records = [r for r in snapshot.get("node_runs", []) if r["node"] == node]
        detail = "自动跳过" if skipped else "运行中" if state == "active" else "已暂停 · 待继续" if state == "paused" else "待执行" if live else "计划节点"
        if node == "finish" and state in {"finished", "blocked"}:
            detail = {"completed_verified": "功能验收通过", "completed_static": "静态完成 · 功能未测", "needs_attention": "停止 · 仍需处理"}.get(snapshot.get("status"), detail)
        if records and state != "active":
            detail = f"{sum(r['duration_seconds'] for r in records):.2f} 秒 · {len(records)} 次" + (" · 失败" if state == "blocked" else "")
        error = next((r.get("error", "") for r in reversed(records)), "")
        shape = f'<g opacity="{".45" if skipped else "1"}"><title>{escape(error or detail)}</title><rect x="{x}" y="{y}" width="165" height="64" rx="12" fill="{fill}" stroke="{border}" stroke-width="{4 if selected == node else 2}"/><text x="{x+82}" y="{y+26}" text-anchor="middle" font-size="15" fill="#15364c">{escape(LABELS.get(node,node))}</text><text x="{x+82}" y="{y+48}" text-anchor="middle" font-size="11" fill="#516578">{escape(detail)}</text></g>'
        if live and snapshot.get("run_id"):
            href = "?" + urlencode({"run": snapshot["run_id"], "node": node}) + "#node-handoff"
            shape = f'<a href="{escape(href, quote=True)}" target="_self">{shape}</a>'
        boxes.append(shape)
    title = ("并行" if parallel else "顺序") + ("运行路径" if live else "工作流预览")
    return f'<svg viewBox="0 0 865 {390+90*len(extensions)}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{title}" style="width:100%;max-height:510px"><defs><marker id="route-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0 L8 4 L0 8" fill="#7696a9"/></marker></defs>{"".join(paths)}{"".join(boxes)}</svg>'


def render_node_details(snapshot: dict) -> None:
    nodes = ["coding", "review", "validate", *snapshot.get("workflow_config", {}).get("extension_nodes", []), "gate", "fix"]
    selected = st.query_params.get("node", st.session_state.get(f"selected_node_{snapshot['run_id']}", snapshot.get("phase", "review")))
    if selected not in nodes:
        selected = "review"
    st.markdown('<div id="node-handoff"></div>', unsafe_allow_html=True)
    st.markdown("**点击节点查看交接内容**")
    columns = st.columns(3)
    for index, node in enumerate(nodes):
        if columns[index % 3].button(LABELS.get(node, node), key=f"inspect_{snapshot['run_id']}_{node}",
                                    type="primary" if node == selected else "secondary", use_container_width=True):
            st.session_state[f"selected_node_{snapshot['run_id']}"] = node
            st.query_params["node"] = node
            st.rerun()
    records = [r for r in snapshot.get("node_runs", []) if r["node"] == selected]
    with st.container(border=True):
        st.write(f"**{LABELS.get(selected, selected)} · 交接记录**")
        if not records:
            st.caption("这个节点尚无已保存的交接记录。旧版本运行不会补造历史耗时。")
            return
        choices = list(range(len(records)))
        index = st.selectbox("选择执行轮次", choices, index=len(records)-1,
            format_func=lambda i: f"第 {i+1} 次 · 修复轮 {records[i]['round']} · {records[i]['duration_seconds']:.2f} 秒",
            key=f"node_attempt_{snapshot['run_id']}_{selected}")
        record = records[index]
        st.caption(f"开始 {record['started_at']} · 完成 {record['finished_at']} · 节点耗时 {record['duration_seconds']:.3f} 秒")
        if record.get("error"):
            st.error(record["error"])
        elif record.get("status") == "needs_attention":
            st.warning("节点已停止，仍需处理：" + str(record.get("output", {}).get("termination_reason", "查看输出中的未解决事项")))
        left, right = st.columns(2)
        with left:
            st.markdown("**输入：接到了什么**")
            st.json(record["input"], expanded=False)
        with right:
            st.markdown("**输出：交给下一步什么**")
            st.json(record["output"], expanded=False)
        output = record["output"].get("extension_output") or {}
        if output:
            summary = output.get("summary", "")
            st.write(summary[:220] + ("…" if len(summary) > 220 else ""))
            with st.expander("查看完整解读、风险与下一步"):
                st.write(summary)
                for risk in output.get("risks", []):
                    st.write("• " + risk)
                for step in output.get("next_steps", []):
                    st.write("下一步：" + step)
            st.caption("这是 Agent 对现有证据的解读；功能通过以独立验收记录为准。")
    st.caption("绿色连线是实际完成的路由，蓝色表示当前执行节点，红色表示节点错误。并行节点耗时不能直接相加为总耗时。")
