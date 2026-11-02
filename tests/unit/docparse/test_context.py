from p2r_docparse.context import trim_for_context
from p2r_docparse.markers import ChunkOutcome, merge_outcomes


def test_trim_drops_references_keeps_appendix():
    md = "# 1 Intro\nhello\n# References\n[1] ref text\n# A Appendix\nextra\n"
    out, warn = trim_for_context(md, 10_000)
    assert "ref text" not in out and "References" not in out
    assert "hello" in out and "Appendix" in out and "extra" in out
    assert warn is None


def test_trim_handles_chinese_heading():
    out, _ = trim_for_context("# 引言\n正文\n## 参考文献\n条目\n", 1000)
    assert "条目" not in out and "正文" in out


def test_subsections_of_references_removed_until_same_level():
    md = "## References\n### sub\nxx1\n## Next\nyy2\n"
    out, _ = trim_for_context(md, 1000)
    assert "xx1" not in out and "yy2" in out


def test_trim_truncates_with_page_warning():
    md = merge_outcomes([ChunkOutcome(1, 20, "a" * 100), ChunkOutcome(21, 40, "b" * 100)])
    cut = md.index("b" * 100) + 50
    out, warn = trim_for_context(md, cut)
    assert warn == "仅第 1–20 页发送给模型（超出上下文上限）"
    assert len(out) <= cut


def test_trim_no_markers_generic_warning():
    out, warn = trim_for_context("x" * 100, 40)
    assert warn == "文本超出上下文上限，已截断"
    assert len(out) == 40


def test_trim_keeps_markers_inside_references():
    seg1 = "# 1 Intro\nhello\n# References\n[1] ref entry one\n[2] ref entry two\n"
    seg2 = "# A Appendix\n" + "appendix text " * 20
    md = merge_outcomes([ChunkOutcome(1, 20, seg1), ChunkOutcome(21, 40, seg2)])
    cut = md.index("# A Appendix") + 60
    out, warn = trim_for_context(md, cut)
    assert "<!-- p2r:pages 21-40 -->" in out
    assert "ref entry" not in out
    assert warn == "仅第 1–20 页发送给模型（超出上下文上限）"


def test_trim_does_not_count_missing_pages_as_sent():
    md = merge_outcomes([ChunkOutcome(1, 20, "a" * 100), ChunkOutcome(21, 40, None),
                         ChunkOutcome(41, 60, "c" * 100)])
    cut = md.index("c" * 100) + 50
    out, warn = trim_for_context(md, cut)
    assert warn == "仅第 1–20 页发送给模型（超出上下文上限）"
