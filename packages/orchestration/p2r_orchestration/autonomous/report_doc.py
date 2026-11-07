"""Agent-written report document: fixed sections, embedded figures, figure provenance rules.

The document is model interpretation. The program only checks its shape and that every figure it embeds is a
figure artifact frozen in the same round with declared provenance (a plotting script and its input files).
It never reads meaning into the text: outcome, verdict and metrics stay program-owned in report().
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .security import digest, fail

SCHEMA_VERSION = "paper2run.report_document.v1"
SECTIONS = ("title", "abstract", "background", "setup", "method", "deviations",
            "results", "discussion", "limitations", "conclusion")
SECTION_LIMIT = 16000
TITLE_LIMIT = 300
CAPTION_LIMIT = 600
FIGURE_EXTENSIONS = {".png", ".svg", ".jpg", ".jpeg", ".webp"}
FIGURE_REF = re.compile(r"\{\{figure:([^{}\s]+)\}\}")
SCRIPT_KINDS = {"code", "analysis"}
INPUT_KINDS = {"result", "data", "log"}

TEXT = {"type": "string", "minLength": 1, "maxLength": SECTION_LIMIT}
REPORT_SCHEMA = {
    "type": "object",
    "description": ("Structured experiment report in Simplified Chinese (Markdown). Embed a frozen figure with "
                    "{{figure:<artifact name>}} on its own line."),
    "properties": {name: ({"type": "string", "minLength": 1, "maxLength": TITLE_LIMIT} if name == "title" else TEXT)
                   for name in SECTIONS},
    "required": list(SECTIONS),
    "additionalProperties": False,
}


def figure_fields(args, scope):
    """caption and derived_from for a figure artifact, validated against the round's frozen artifacts."""
    if PurePosixPath(args["name"]).suffix.lower() not in FIGURE_EXTENSIONS:
        fail("INVALID_ARTIFACT", "figure artifacts must be .png, .svg, .jpg, .jpeg or .webp")
    caption = args.get("caption")
    if not isinstance(caption, str) or not caption.strip() or len(caption) > CAPTION_LIMIT:
        fail("INVALID_ARTIFACT", "figure artifacts need a caption (1-600 characters)")
    sources = args.get("derived_from")
    if not isinstance(sources, list) or not sources or not all(isinstance(s, str) for s in sources):
        fail("INVALID_ARTIFACT", "figure artifacts need derived_from: the frozen plotting script and its input files")
    kinds = {a["name"]: a["kind"] for a in scope}
    unknown = [s for s in sources if s not in kinds]
    if unknown:
        fail("INVALID_ARTIFACT", "derived_from must name artifacts frozen in this round: " + ", ".join(unknown))
    if not SCRIPT_KINDS & {kinds[s] for s in sources}:
        fail("INVALID_ARTIFACT", "derived_from must include the plotting script (kind analysis or code)")
    if not INPUT_KINDS & {kinds[s] for s in sources}:
        fail("INVALID_ARTIFACT", "derived_from must include the plotted input (kind result, data or log)")
    return {"caption": caption.strip(), "derived_from": list(dict.fromkeys(sources))}


def references(document):
    """Figure names embedded in the document, in reading order, without repeats."""
    names = []
    for section in SECTIONS:
        for name in FIGURE_REF.findall(document.get(section) or ""):
            if name not in names:
                names.append(name)
    return names


def gate(document, artifacts):
    """Missing report requirements, in a stable order (same style as candidate.gate)."""
    if not isinstance(document, dict):
        return ["report document"]
    limits = {name: TITLE_LIMIT if name == "title" else SECTION_LIMIT for name in SECTIONS}
    missing = [f"report section {name} (empty or over {limits[name]} characters)" for name in SECTIONS
               if not isinstance(document.get(name), str) or not document[name].strip() or len(document[name]) > limits[name]]
    missing += [f"report section {name} (unknown)" for name in document if name not in limits]
    figures = {a["name"] for a in artifacts if a["kind"] == "figure"}
    if not figures:
        missing.append("figure artifact")
    refs = references(document)
    missing += [f"figure reference {name} (not a figure frozen in this round)" for name in refs if name not in figures]
    if figures and not refs:
        missing.append("figure embedded in the report ({{figure:<name>}})")
    return missing


def build(document, artifacts, clean):
    """The frozen document: cleaned sections plus the embedded figures' provenance, numbered in reading order."""
    sections = {name: clean(document[name]) for name in SECTIONS}
    by_name = {a["name"]: a for a in artifacts if a["kind"] == "figure"}
    figures = [{"name": name, "number": i + 1, "caption": by_name[name].get("caption", ""),
                "derived_from": by_name[name].get("derived_from", []), "sha256": by_name[name]["sha256"],
                "bytes": by_name[name].get("bytes")}
               for i, name in enumerate(n for n in references(sections) if n in by_name)]
    result = {"schema_version": SCHEMA_VERSION, "author": "model", "sections": sections, "figures": figures}
    result["sha256"] = digest(result)
    return result
