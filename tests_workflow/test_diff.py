"""Diff output must preserve meaning without rendering source as HTML."""

from ui.diff_view import build_diff_html, build_side_by_side_html, diff_counts


def test_source_and_version_labels_are_escaped_and_changes_have_signs():
    before = 'unchanged = 1\nprint("</td><script>alert(1)</script>")\n'
    after = 'unchanged = 1\nprint("fixed")\n'
    rendered = build_diff_html(before, after, before_label='<img src=x onerror="alert(1)">')
    assert "<script>" not in rendered
    assert "<img src=x" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "&lt;img src=x" in rendered
    assert "删除" in rendered and "新增" in rendered and "保留" in rendered
    assert "−" in rendered and "+" in rendered
    assert diff_counts(before, after) == (1, 1)


def test_last_line_newline_and_empty_files_are_visible_changes():
    assert diff_counts("line\n", "line") == (1, 1)
    assert "行尾无换行" in build_diff_html("line\n", "line")
    assert diff_counts("", "first\nsecond\n") == (2, 0)
    assert diff_counts("first\nsecond\n", "") == (0, 2)
    assert "当前代码与所选版本相同" in build_diff_html("same\n", "same\n")


def test_side_by_side_diff_escapes_source_and_keeps_unchanged_lines_neutral():
    before = 'safe = 1\nprint("<script>old()</script>")\n'
    after = 'safe = 1\nprint("fixed")\n'
    rendered = build_side_by_side_html(before, after, before_label='<img src=x onerror="x()">')
    assert "修改前源码" in rendered and "修改后源码" in rendered
    assert rendered.count("safe = 1") == 2
    assert "<script>" not in rendered and "<img src=x" not in rendered
    assert "&lt;script&gt;" in rendered and "&lt;img src=x" in rendered
    assert '<tr class="removed"' in rendered and '<tr class="added"' in rendered
    assert "−" in rendered and "+" in rendered


def test_identical_side_by_side_versions_show_both_sources_without_fake_changes():
    source = "answer = 42\nprint(answer)\n"
    rendered = build_side_by_side_html(source, source)
    assert "两侧源码相同" in rendered
    assert rendered.count("answer = 42") == 2
    assert rendered.count("print(answer)") == 2
    assert '<tr class="removed"' not in rendered
    assert '<tr class="added"' not in rendered
    assert "行尾无换行" in build_side_by_side_html("last\n", "last")
