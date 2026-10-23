"""Render a source comparison from saved versions, never from model HTML."""

from difflib import SequenceMatcher
from html import escape
from itertools import zip_longest


def diff_counts(before: str, after: str) -> tuple[int, int]:
    """Return added/deleted line counts, including line-ending changes."""
    added = deleted = 0
    matcher = SequenceMatcher(None, before.splitlines(True), after.splitlines(True), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            added += j2 - j1
        if tag in {"delete", "replace"}:
            deleted += i2 - i1
    return added, deleted


def build_diff_html(
    before: str,
    after: str,
    *,
    before_label: str = "修改前",
    after_label: str = "修改后",
    context: int | None = 3,
) -> str:
    """Return an escaped, read-only unified diff with both line numbers."""
    old = before.splitlines(True)
    new = after.splitlines(True)
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    groups = (
        [matcher.get_opcodes()]
        if context is None
        else list(matcher.get_grouped_opcodes(max(0, context)))
    )
    rows: list[str] = []

    def row(kind: str, old_no: int | None, new_no: int | None, text: str) -> str:
        sign = {"removed": "−", "added": "+", "same": " "}[kind]
        label = {"removed": "删除", "added": "新增", "same": "保留"}[kind]
        content = escape(text.rstrip("\r\n"), quote=True)
        ending = ""
        if text and not text.endswith(("\n", "\r")):
            ending = '<span class="ending"> ⏎ 行尾无换行</span>'
        return (
            f'<tr class="{kind}" aria-label="{label}">'
            f'<td class="num">{old_no or ""}</td>'
            f'<td class="num">{new_no or ""}</td>'
            f'<td class="sign" title="{label}">{sign}</td>'
            f'<td class="code">{content}{ending}</td></tr>'
        )

    if before == after:
        rows.append('<tr><td class="empty" colspan="4">当前代码与所选版本相同。</td></tr>')
    else:
        for group_no, group in enumerate(groups):
            if group_no or (group and (group[0][1] or group[0][3])):
                rows.append('<tr class="gap"><td colspan="4">⋯ 未修改的代码已折叠 ⋯</td></tr>')
            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    rows.extend(row("same", i + 1, j + 1, old[i]) for i, j in zip(range(i1, i2), range(j1, j2)))
                else:
                    if tag in {"delete", "replace"}:
                        rows.extend(row("removed", i + 1, None, old[i]) for i in range(i1, i2))
                    if tag in {"insert", "replace"}:
                        rows.extend(row("added", None, j + 1, new[j]) for j in range(j1, j2))
        if groups and (groups[-1][-1][2] < len(old) or groups[-1][-1][4] < len(new)):
            rows.append('<tr class="gap"><td colspan="4">⋯ 未修改的代码已折叠 ⋯</td></tr>')

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
body{{margin:0;color:#1f2937;background:#fff;font-family:system-ui,sans-serif;font-size:13px}}
.legend{{padding:12px 14px;border:1px solid #dce3eb;border-bottom:0;border-radius:8px 8px 0 0;background:#f8fafc}}
.legend span{{display:inline-block;margin-right:18px}}.legend .del{{color:#9b1c1c}}.legend .add{{color:#126331}}
.scroll{{overflow:auto;max-height:520px;border:1px solid #dce3eb;border-radius:0 0 8px 8px}}
table{{border-collapse:collapse;min-width:100%;font:13px/1.65 Consolas,'Courier New',monospace}}
th{{text-align:left;padding:6px 9px;font:12px system-ui,sans-serif;color:#64748b;background:#f8fafc}}
td{{vertical-align:top}}.num{{min-width:34px;padding:0 8px;text-align:right;user-select:none;color:#64748b;border-right:1px solid #e5e7eb}}
.sign{{width:18px;min-width:18px;text-align:center;user-select:none}}.code{{white-space:pre;padding:0 12px 0 4px;width:100%;tab-size:4}}
.removed{{background:#ffebe9;color:#82071e}}.added{{background:#dafbe1;color:#116329}}.same{{background:#fff;color:#1f2937}}
.gap td{{padding:7px 14px;color:#64748b;background:#f1f5f9;font:12px system-ui,sans-serif}}
.empty{{padding:28px;color:#64748b;text-align:center;font:14px system-ui,sans-serif}}
.ending{{color:#64748b;font:11px system-ui,sans-serif}}
</style></head><body>
<div class="legend"><span class="del">− {escape(before_label, quote=True)}</span><span class="add">+ {escape(after_label, quote=True)}</span><span>普通背景：保留</span></div>
<div class="scroll"><table aria-label="源代码更改对比"><thead><tr><th>原行</th><th>新行</th><th></th><th>源码</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
</body></html>"""


def build_side_by_side_html(
    before: str,
    after: str,
    *,
    before_label: str = "原始版本",
    after_label: str = "当前版本",
    context: int | None = 3,
    focus_old_range: tuple[int, int] | None = None,
) -> str:
    """Show aligned original/current code even when a repair made no change."""
    old, new = before.splitlines(True), after.splitlines(True)
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    block_numbers = {opcode: index for index, opcode in enumerate((o for o in matcher.get_opcodes() if o[0] != "equal"), 1)}
    groups = (
        [matcher.get_opcodes()]
        if context is None or before == after
        else list(matcher.get_grouped_opcodes(max(0, context)))
    )
    if focus_old_range and context is not None:
        start = max(0, focus_old_range[0] - 1 - context)
        end = min(len(old), focus_old_range[1] + context)
        selected = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                low, high = max(start, i1), min(end, i2)
                if low < high:
                    selected.append((tag, low, high, j1 + low - i1, j1 + high - i1))
            elif (i1 < end and i2 > start) or (i1 == i2 and start <= i1 <= end):
                selected.append((tag, i1, i2, j1, j2))
        groups = [selected] if selected else groups
    left: list[str] = []
    right: list[str] = []

    def row(kind: str, number: int | None = None, text: str = "", *, old_side: bool = False) -> str:
        sign = {"removed": "−", "added": "+", "same": "", "blank": ""}[kind]
        label = {"removed": "删除", "added": "新增", "same": "保留", "blank": "无对应行"}[kind]
        ending = '<span class="ending"> ⏎ 行尾无换行</span>' if text and not text.endswith(("\n", "\r")) else ""
        focused = old_side and number is not None and focus_old_range and focus_old_range[0] <= number <= focus_old_range[1]
        classes = f"{kind} focus" if focused else kind
        return (
            f'<tr class="{classes}" aria-label="{label}"' + (f' id="old-L{number}"' if old_side and number else "") + ">"
            f'<td class="num">{number or ""}</td><td class="sign">{sign}</td>'
            f'<td class="code">{escape(text.rstrip(chr(10) + chr(13)), quote=True)}{ending}</td></tr>'
        )

    def gap() -> None:
        gap_row = '<tr class="gap"><td colspan="3">⋯ 未修改的代码已折叠 ⋯</td></tr>'
        left.append(gap_row)
        right.append(gap_row)

    for group_number, group in enumerate(groups):
        if group_number or (group and (group[0][1] or group[0][3])):
            gap()
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    left.append(row("same", i + 1, old[i], old_side=True))
                    right.append(row("same", j + 1, new[j]))
            else:
                number = block_numbers.get((tag, i1, i2, j1, j2), "")
                old_range = f"{i1 + 1}–{i2}" if i2 > i1 else "无"
                new_range = f"{j1 + 1}–{j2}" if j2 > j1 else "无"
                heading = f'<tr class="hunk" data-change-block="{number}"><td colspan="3">修改块 {number} · 原 {old_range} 行 → 新 {new_range} 行</td></tr>'
                left.append(heading)
                right.append(heading)
                for i, j in zip_longest(range(i1, i2), range(j1, j2)):
                    left.append(row("removed", i + 1, old[i], old_side=True) if i is not None else row("blank"))
                    right.append(row("added", j + 1, new[j]) if j is not None else row("blank"))
    if groups and groups[-1] and (groups[-1][-1][2] < len(old) or groups[-1][-1][4] < len(new)):
        gap()
    if not left:
        empty = '<tr class="same"><td class="empty" colspan="3">空文件</td></tr>'
        left.append(empty)
        right.append(empty)
    same_notice = '<div class="notice">两侧源码相同，当前没有可标红或标绿的修改；下面保留完整源码供核对。</div>' if before == after else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
body{{margin:0;background:#fff;color:#1f2937;font:13px system-ui,sans-serif}}
.legend{{padding:9px 12px;color:#475569}}.legend span{{margin-right:18px}}.del{{color:#9b1c1c}}.add{{color:#126331}}
.notice{{padding:10px 12px;background:#f1f5f9;border:1px solid #dce3eb;border-radius:7px;margin-bottom:8px}}
.headers,.split{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
.headers{{border:1px solid #dce3eb;border-radius:7px 7px 0 0;background:#f8fafc;font-weight:650}}
.headers div{{padding:10px 12px}}.headers div+div,.pane+.pane{{border-left:1px solid #cbd5e1}}
.scroll{{max-height:530px;overflow-y:auto;border:1px solid #dce3eb;border-top:0;border-radius:0 0 7px 7px}}
.pane{{overflow-x:auto;min-width:0}}table{{border-collapse:collapse;min-width:100%;font:13px/1.75 Consolas,'Courier New',monospace}}
td{{height:23px;box-sizing:border-box;vertical-align:top}}.num{{min-width:34px;padding:0 7px;text-align:right;color:#64748b;user-select:none;border-right:1px solid #e5e7eb}}
.sign{{min-width:18px;text-align:center;user-select:none}}.code{{white-space:pre;padding:0 12px 0 4px;width:100%;tab-size:4}}
.removed{{background:#ffebe9;color:#82071e}}.added{{background:#dafbe1;color:#116329}}
.same,.blank{{background:#fff;color:#1f2937}}.blank td{{color:transparent}}
.focus .num{{background:#dbeafe;color:#174a78;font-weight:bold;border-left:3px solid #1875a9}}
.gap td{{height:29px;padding:4px 10px;color:#64748b;background:#f1f5f9;font:12px system-ui,sans-serif}}
.hunk td{{height:29px;padding:4px 10px;color:#215f83;background:#edf6fc;font:12px system-ui,sans-serif}}
.ending{{color:#64748b;font:11px system-ui,sans-serif}}.empty{{padding:18px;color:#64748b}}
</style></head><body>
{same_notice}<div class="legend"><span class="del">− 红色：删除 / 改前内容</span><span class="add">+ 绿色：新增 / 改后内容</span><span>普通背景：未修改</span></div>
<div class="headers"><div>修改前 · {escape(before_label, quote=True)}</div><div>修改后 · {escape(after_label, quote=True)}</div></div>
<div class="scroll"><div class="split"><section class="pane" aria-label="修改前源码"><table><tbody>{''.join(left)}</tbody></table></section><section class="pane" aria-label="修改后源码"><table><tbody>{''.join(right)}</tbody></table></section></div></div>
</body></html>"""
