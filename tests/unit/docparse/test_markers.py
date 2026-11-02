from p2r_docparse.markers import (
    ChunkOutcome,
    ParseResult,
    merge_outcomes,
    missing_warnings,
    split_merged,
)

OUTCOMES = [
    ChunkOutcome(1, 20, "# Title\n\nbody one\n"),
    ChunkOutcome(21, 40, None),
    ChunkOutcome(41, 41, "last page text"),
]


def test_merge_split_roundtrip():
    merged = merge_outcomes(OUTCOMES)
    assert "<!-- p2r:pages 1-20 -->" in merged
    assert "<!-- p2r:pages 21-40 missing -->" in merged
    assert "<!-- p2r:page 41 -->" in merged
    assert split_merged(merged) == OUTCOMES


def test_roundtrip_empty_and_blank_text():
    xs = [ChunkOutcome(1, 2, ""), ChunkOutcome(3, 3, "a\n\n"), ChunkOutcome(4, 5, None)]
    assert split_merged(merge_outcomes(xs)) == xs


def test_missing_warnings():
    assert missing_warnings(OUTCOMES) == ["第 21–40 页解析失败，未包含"]
    assert missing_warnings([ChunkOutcome(3, 3, None)]) == ["第 3 页解析失败，未包含"]


def test_marker_inside_line_not_split():
    md = merge_outcomes([ChunkOutcome(1, 2, "see <!-- p2r:page 9 --> inline text")])
    assert split_merged(md) == [ChunkOutcome(1, 2, "see <!-- p2r:page 9 --> inline text")]


def test_parse_result_fields():
    r = ParseResult("x", "online_agent", "v1", 3, 3, [], [])
    assert r.pages_parsed == 3
