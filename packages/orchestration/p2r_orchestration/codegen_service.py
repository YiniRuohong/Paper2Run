"""Provider-backed code generation for paper-to-code reproduction.

Generated code is a *reviewed artifact*, never a trusted input: this service
only (1) asks a provider for a self-contained standard-library Python script,
(2) enforces a static AST policy on the returned source, and (3) stores the
bytes content-addressed. The script is executed later — if at all — by the
separate sandbox task, never here. No secret crosses this boundary: the
provider object arrives injected with its own secret resolver, and nothing
except the safe projection below is returned or persisted.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from .providers import CompletionRequest, Message, NormalizedEvent, ProviderError

CODEGEN_PHASE = "paper_codegen"
RESPONSE_SCHEMA_VERSION = "codegen.v1"
CODEGEN_MAX_OUTPUT_TOKENS = 32_000
MAX_CODE_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 128 * 1024

# Standard-library-only import allowlist for generated scripts. Anything else
# (os/sys/socket/subprocess/pathlib/...) is rejected before storage.
IMPORT_ALLOWLIST = frozenset({
    "math", "random", "json", "csv", "statistics", "itertools", "collections",
    "functools", "operator", "datetime", "time", "hashlib", "re", "string",
    "typing", "dataclasses", "abc", "decimal", "fractions", "heapq", "bisect",
    "array",
})

# Dynamic-execution escapes: a call node naming any of these is rejected.
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "globals"})

# Write-ish open() modes. Any of these characters in the mode makes the call
# a *write*, which is only permitted to a literal relative path in the
# current working directory.
_WRITE_MODE_CHARS = frozenset({"w", "a", "x"})

REQUIRED_OUTPUT_STRINGS = ("predictions.json", "labels.json")

CODEGEN_JSON_SCHEMA = {
    "name": "codegen",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"code": {"type": "string"}, "notes": {"type": "string"}},
        "required": ["code", "notes"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = (
    "You are a careful reproduction engineer. Read the article extraction "
    "and the experiment spec, then return one JSON object that validates "
    "against the provided JSON schema with exactly two string fields: "
    "\"code\" and \"notes\". The \"code\" field must contain ONE "
    "self-contained Python 3 script that: uses ONLY the Python standard "
    "library modules from this list — math, random, json, csv, statistics, "
    "itertools, collections, functools, operator, datetime, time, hashlib, "
    "re, string, typing, dataclasses, abc, decimal, fractions, heapq, "
    "bisect, array; fixes every random seed so repeated runs are "
    "deterministic; performs no network access and downloads nothing; reads "
    "and writes files ONLY in the current working directory using relative "
    "paths; synthesizes or derives its dataset locally (no external data "
    "files); MUST write the test-set predictions to \"predictions.json\" as "
    "a JSON array of {\"id\": str, \"prediction\": int} and the ground-truth "
    "labels to \"labels.json\" as a JSON object mapping id to int (the "
    "sandbox only collects output files under 256 KB each, so keep the test "
    "set to AT MOST 2000 items and write compact JSON); and MUST "
    "end by printing exactly one line starting with \"SUMMARY\" followed by "
    "one JSON object summarizing the outcome. The script's first lines must "
    "be comments stating the method implemented and the data source. Never "
    "call eval, exec, compile, __import__, or globals. If the paper's "
    "experiment cannot be approximated by a self-contained "
    "standard-library script, still generate the closest faithful baseline "
    "and document the limitations in comments and in \"notes\". The \"notes\" "
    "field must summarize method, data provenance, and any deviations from "
    "the paper. Keep the script COMPACT: at most about 80 lines and no "
    "docstring longer than two lines — long generations exceed the gateway's "
    "response window."
)


class CodegenPolicyError(ValueError):
    """Generated code violated the static safety policy.

    Deliberately a ValueError subclass so route layers can distinguish it
    from provider failures without importing anything private.
    """


def _policy(problem: str) -> CodegenPolicyError:
    return CodegenPolicyError(problem)


def _static_path(node: ast.AST) -> str | None:
    """Best-effort resolution of a path expression to a literal string.

    Returns None when the expression cannot be statically resolved; callers
    treat that conservatively (write mode requires a resolvable literal).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                inner = _static_path(value.value)
                if inner is None:
                    return None
                parts.append(inner)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_path(node.left)
        right = _static_path(node.right)
        if left is None or right is None:
            return None
        return left + right
    return None


def _open_is_write(call: ast.Call) -> tuple[bool, str | None]:
    """Return (is_write, path_literal) for an open(...) call node.

    ``path_literal`` is None when the path cannot be statically resolved.
    An unresolvable *mode* is treated as a write (conservative).
    """
    args = call.args
    path_node: ast.AST | None = None
    mode_node: ast.AST | None = None
    if len(args) >= 1:
        path_node = args[0]
    if len(args) >= 2:
        mode_node = args[1]
    for keyword in call.keywords:
        if keyword.arg == "file" and path_node is None:
            path_node = keyword.value
        elif keyword.arg == "mode" and mode_node is None:
            mode_node = keyword.value
    if mode_node is None:
        return False, (_static_path(path_node) if path_node is not None else None)
    mode = _static_path(mode_node)
    if mode is None:
        is_write = True  # unknown mode: assume it writes
    else:
        is_write = bool(_WRITE_MODE_CHARS & set(mode))
    path = _static_path(path_node) if path_node is not None else None
    return is_write, path


def _check_open_call(call: ast.Call) -> None:
    is_write, path = _open_is_write(call)
    if not is_write:
        return
    if path is None:
        raise _policy("open() in write mode requires a static literal relative path")
    if path.startswith("/"):
        raise _policy("open() write to absolute path is forbidden")
    if path.startswith("~"):
        raise _policy("open() write to home-relative path is forbidden")
    if ".." in path.replace("\\", "/").split("/"):
        raise _policy("open() write traversing out of the working directory is forbidden")
    if not path:  # empty literal: nothing sensible to write
        raise _policy("open() write requires a non-empty relative path")


def _check_call(call: ast.Call) -> None:
    func = call.func
    name: str | None = None
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        # obj.eval(...) / builtins.exec(...) style indirection is refused too.
        name = func.attr
    if name is not None and name in FORBIDDEN_CALLS:
        raise _policy(f"call to {name}() is forbidden")
    if isinstance(func, ast.Name) and func.id == "open":
        _check_open_call(call)


def validate_generated_code(code: str) -> None:
    """Static safety policy for provider-generated scripts.

    Raises :class:`CodegenPolicyError` on the first violation; returns None
    when the code passes every rule. Purely static — runtime enforcement
    (network isolation, resource limits) belongs to the sandbox, never here.
    """
    if not isinstance(code, str) or not code.strip():
        raise _policy("code must be a non-empty string")
    encoded = code.encode("utf-8", errors="strict")
    if len(encoded) > MAX_CODE_BYTES:
        raise _policy(f"code exceeds the {MAX_CODE_BYTES}-byte limit")
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as exc:
        raise _policy(f"code is not parseable Python: {exc}") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in IMPORT_ALLOWLIST:
                    raise _policy(f"import of {alias.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                raise _policy("relative imports are not allowed")
            root = (node.module or "").split(".")[0]
            if root not in IMPORT_ALLOWLIST:
                raise _policy(f"import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Call):
            _check_call(node)
    for required in REQUIRED_OUTPUT_STRINGS:
        if required not in code:
            raise _policy(f"code must reference {required!r}")


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


class CodegenService:
    """Turn a paper extraction + spec into a stored, policy-checked script."""

    def __init__(self, provider: Any, artifact_store: Any, *, max_context_bytes: int = MAX_CONTEXT_BYTES) -> None:
        if type(max_context_bytes) is not int or max_context_bytes < 1:
            raise ValueError("max_context_bytes must be a positive integer")
        self._provider = provider
        self._store = artifact_store
        self._max_context_bytes = max_context_bytes

    async def generate(
        self,
        *,
        project_id: str,
        experiment_id: str,
        revision_id: str | None,
        paper_sha256: str,
        extraction_payload: dict[str, Any],
        spec: dict[str, Any],
        provider_config_id: str,
        model_id: str,
        call_id: str,
        prompt_version: str,
        policy_version: str,
    ) -> dict[str, Any]:
        context = self._build_context(extraction_payload, spec)
        context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
        user_message = (
            "REPRODUCTION REQUEST\n"
            f"project_id: {project_id}\n"
            f"experiment_id: {experiment_id}\n"
            f"revision_id: {revision_id or 'none'}\n"
            f"paper_sha256: {paper_sha256.lower()}\n"
            "---- extraction and spec follow ----\n"
            f"{context}"
        )
        request = CompletionRequest(
            provider_config_id=provider_config_id,
            model_id=model_id,
            phase=CODEGEN_PHASE,
            call_id=call_id,
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=user_message),
            ],
            context_hash=context_hash,
            prompt_version=prompt_version,
            policy_version=policy_version,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            # Measured gateway latency for full-script generation regularly
            # exceeds 120s, and reasoning models think before writing code:
            # use the full 300s request cap. The prompt still caps the script
            # at ~80 lines, so non-reasoning models stop far below the budget.
            timeout_seconds=300.0,
            max_output_tokens=CODEGEN_MAX_OUTPUT_TOKENS,
            json_schema=CODEGEN_JSON_SCHEMA,
        )

        text_payload: str | None = None
        async for event in self._provider.complete(request):
            if not isinstance(event, NormalizedEvent):
                raise ProviderError("invalid_response")
            if event.type == "completed":
                if event.text is None or not isinstance(event.text, str):
                    raise ProviderError("invalid_response")
                text_payload = event.text
        if text_payload is None:
            raise ProviderError("invalid_response")

        try:
            payload = json.loads(text_payload, parse_constant=_reject_json_constant)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise ProviderError("invalid_response") from None
        if not isinstance(payload, dict):
            raise ProviderError("invalid_response")
        code = payload.get("code")
        notes = payload.get("notes")
        # Lenient: notes are optional commentary; only the code is required.
        if notes is None:
            notes = ""
        if isinstance(code, str):
            fenced = re.fullmatch(r"\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?```\s*", code, re.DOTALL)
            if fenced:
                code = fenced.group(1) + "\n"
        if not isinstance(code, str) or not code.strip() or not isinstance(notes, str):
            raise ProviderError("invalid_response")

        # The generated script is untrusted model output: it may only be
        # stored after the static policy accepts it, and it is never run here.
        validate_generated_code(code)

        encoded = code.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        address = self._store.put_bytes(encoded)
        if address.sha256 != digest:  # content-addressed store must agree
            raise ProviderError("invalid_response")

        # Safe projection: full code for human review plus generator
        # metadata. No secret, transport payload, or raw provider response.
        return {
            "project_id": project_id,
            "experiment_id": experiment_id,
            "revision_id": revision_id,
            "paper_sha256": paper_sha256.lower(),
            "code": code,
            "notes": notes,
            "code_sha256": digest,
            "code_artifact_id": digest,
            "artifact_bytes": address.bytes,
            "generator": {
                "provider_config_id": provider_config_id,
                "model_id": model_id,
                "call_id": call_id,
                "prompt_version": prompt_version,
                "policy_version": policy_version,
            },
        }

    def _build_context(self, extraction_payload: dict[str, Any], spec: dict[str, Any]) -> str:
        try:
            document = json.dumps(
                {"extraction": extraction_payload, "spec": spec},
                ensure_ascii=False, sort_keys=True,
            )
        except (TypeError, ValueError):
            raise ProviderError("invalid_response") from None
        return document[: self._max_context_bytes]
