import json

from p2r_orchestration.autonomous.tools import SYSTEM_PROMPT, TOOLS


def test_progress_notes_are_requested_in_simplified_chinese():
    assert "Simplified Chinese" in SYSTEM_PROMPT
    assert "Simplified Chinese" in json.dumps(TOOLS, default=str)
