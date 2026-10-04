#!/usr/bin/env python3
"""Fail when the hand-written frontend API types drift from the FastAPI schemas.

`apps/web/src/types/api.ts` intentionally avoids code generation so the shapes
stay readable, but readable types rot silently. This script imports the real
FastAPI application, dumps its OpenAPI document, parses the TypeScript
interfaces in the frontend type module, and compares field names for every
mapped pair. It exits non-zero on any drift so CI (and `npm run check:api`)
catches a renamed or removed schema field before the UI breaks at runtime.

Usage:
    python scripts/check_api_types.py               # check, exit 1 on drift
    python scripts/check_api_types.py --verbose     # also list matching fields
    python scripts/check_api_types.py --json        # machine-readable report

Only field *names* are compared. Nullability, optionality and value types are
left to `tsc` against the real responses, because the server expresses those
with unions and aliases that do not translate one-to-one into TypeScript.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "apps" / "api"
TYPES_FILE = ROOT / "apps" / "web" / "src" / "types" / "api.ts"

# TypeScript interface -> OpenAPI component schema. Every interface in the
# frontend type module that mirrors a server schema belongs here; the script
# errors if a listed interface disappears.
#
# Deliberately absent, because they are frontend-shaped rather than server
# schemas: `Page<T>` (envelope generic), `Plan` and `UsageSummary` (views over
# the dict-typed billing/usage endpoints), `CandidateScoreDetail`, `CropKeyframe`
# and `CaptionStyle` (nested structures read out of `Record<string, unknown>`
# fields), and `Capabilities`' inner objects.
TYPE_MAP: dict[str, str] = {
    "Project": "ProjectOut",
    "ProjectSummary": "ProjectSummary",
    "Video": "VideoOut",
    "Job": "JobOut",
    "Transcript": "TranscriptDetail",
    "TranscriptSegment": "TranscriptSegmentOut",
    "TranscriptWord": "TranscriptWordOut",
    "Candidate": "CandidateOut",
    "Clip": "ClipOut",
    "ClipDetail": "ClipDetail",
    "CaptionWord": "CaptionWord",
    "CaptionCue": "CaptionCue",
    "CaptionResponse": "CaptionResponse",
    "HeadlineSuggestion": "HeadlineSuggestion",
    "HeadlineResponse": "HeadlineResponse",
    "Render": "RenderOut",
    "RenderPreset": "RenderPresetOut",
    "ExportItem": "ExportWithClip",
    "AIProvider": "AIProviderOut",
    "Capabilities": "CapabilityOut",
}

PAGE_TYPES = [
    "Page_CandidateOut_",
    "Page_ClipOut_",
    "Page_ProjectSummary_",
    "Page_RenderOut_",
    "Page_JobOut_",
    "Page_ExportWithClip_",
]


def _ensure_dependencies() -> None:
    """Re-exec under the project virtualenv when FastAPI is not importable.

    `npm run check:api` has no idea which interpreter has the API installed, so
    the script finds `ROOT/.venv` itself instead of demanding a manual
    `source .venv/bin/activate`.
    """
    try:
        import fastapi  # noqa: F401
    except ImportError:
        venv_python = ROOT / ".venv" / "bin" / "python"
        # Compare `sys.prefix`, not `sys.executable.resolve()`: a venv's
        # `bin/python` is a symlink to the system interpreter, so resolved paths
        # compare equal even when we are running outside the venv.
        if venv_python.exists() and Path(sys.prefix) != (ROOT / ".venv"):
            os.execv(str(venv_python), [str(venv_python), *sys.argv])
        print(
            "error: FastAPI is not importable. Create the API environment first "
            "(.venv) or run this script with the interpreter that has the API installed.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def load_openapi() -> dict[str, Any]:
    _ensure_dependencies()
    sys.path.insert(0, str(API_DIR))
    from app.main import app  # noqa: WPS433 (deliberate late import)

    return app.openapi()


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def parse_interfaces(source: str) -> dict[str, list[str]]:
    """Return {interface name: [top-level property names]} from a TS module.

    `extends` chains are flattened so `interface ClipDetail extends Clip` is
    compared against the full inherited shape, matching how the server merges
    Pydantic base models.
    """
    text = _strip_comments(source)
    interfaces: dict[str, list[str]] = {}
    bases: dict[str, list[str]] = {}
    pattern = re.compile(
        r"export\s+interface\s+(\w+)(?:\s*<[^>]*>)?\s*(?:extends\s+([\w<>, ]+?)\s*)?\{"
    )
    for match in pattern.finditer(text):
        name = match.group(1)
        if match.group(2):
            bases[name] = [base.strip().split("<")[0] for base in match.group(2).split(",") if base.strip()]
        depth = 1
        index = match.end()
        body_start = index
        while index < len(text) and depth:
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
        body = text[body_start : index - 1]
        fields: list[str] = []
        inner = 0
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if inner == 0:
                field = re.match(r"^(?:readonly\s+)?(\w+)\s*[?]?\s*:", line)
                if field:
                    fields.append(field.group(1))
                elif re.match(r"^\[.*\]\s*:", line):
                    fields.append("<index>")
            inner += raw_line.count("{") + raw_line.count("(") + raw_line.count("[")
            inner -= raw_line.count("}") + raw_line.count(")") + raw_line.count("]")
            inner = max(inner, 0)
        interfaces[name] = fields

    def flatten(name: str, seen: frozenset[str] = frozenset()) -> list[str]:
        if name in seen or name not in interfaces:
            return []
        fields = list(interfaces[name])
        for base in bases.get(name, []):
            fields = flatten(base, seen | {name}) + fields
        return list(dict.fromkeys(fields))

    return {name: flatten(name) for name in interfaces}


def schema_fields(schemas: dict[str, Any], name: str) -> list[str] | None:
    schema = schemas.get(name)
    if schema is None:
        return None
    properties = schema.get("properties")
    if properties is None:
        return []
    return list(properties.keys())


def resolve_alias(schemas: dict[str, Any], name: str) -> str:
    """Follow a component that is a `$ref`-only alias (the `Page[...]` shapes)."""
    seen = set()
    while name not in seen:
        seen.add(name)
        schema = schemas.get(name) or {}
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            continue
        for candidate in schema.get("allOf", []):
            if "$ref" in candidate:
                name = candidate["$ref"].rsplit("/", 1)[-1]
                break
        else:
            break
    return name


def merged_fields(schemas: dict[str, Any], name: str) -> list[str] | None:
    """Field names for a schema, merging `allOf` members (Pydantic inheritance)."""
    schema = schemas.get(name)
    if schema is None:
        return None
    fields: list[str] = []
    for candidate in [schema, *schema.get("allOf", [])]:
        ref = candidate.get("$ref")
        if ref:
            nested = merged_fields(schemas, ref.rsplit("/", 1)[-1])
            if nested:
                fields.extend(nested)
        fields.extend((candidate.get("properties") or {}).keys())
    # dedupe, keep order
    return list(dict.fromkeys(fields))


def compare(
    interfaces: dict[str, list[str]], schemas: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    report: list[dict[str, Any]] = []
    problems: list[str] = []

    for ts_name, schema_name in sorted(TYPE_MAP.items()):
        if ts_name not in interfaces:
            problems.append(f"interface {ts_name} is listed in TYPE_MAP but missing from {TYPES_FILE.name}")
            continue
        resolved = resolve_alias(schemas, schema_name)
        server = merged_fields(schemas, resolved)
        if server is None:
            problems.append(f"schema {schema_name} (for interface {ts_name}) is missing from the OpenAPI document")
            continue
        client = [field for field in interfaces[ts_name] if field != "<index>"]
        missing = [field for field in server if field not in client]
        extra = [field for field in client if field not in server]
        entry = {
            "interface": ts_name,
            "schema": resolved,
            "server_fields": server,
            "client_fields": client,
            "missing_on_client": missing,
            "not_on_server": extra,
        }
        report.append(entry)
        if missing:
            problems.append(f"{ts_name}: server field(s) missing from the frontend type: {', '.join(missing)}")
        if extra:
            problems.append(f"{ts_name}: frontend field(s) not present on the server: {', '.join(extra)}")

    if "Page" in interfaces and not set(interfaces["Page"]) >= {
        "items",
        "total",
        "limit",
        "offset",
        "has_more",
    }:
        problems.append("Page<T> must keep items/total/limit/offset/has_more")

    for page_schema in PAGE_TYPES:
        if page_schema not in schemas:
            problems.append(f"expected pagination schema {page_schema} is missing from the OpenAPI document")

    return report, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="print every compared interface")
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of text")
    args = parser.parse_args()

    if not TYPES_FILE.exists():
        print(f"error: {TYPES_FILE} does not exist", file=sys.stderr)
        return 2

    schemas = load_openapi()["components"]["schemas"]
    interfaces = parse_interfaces(TYPES_FILE.read_text())
    report, problems = compare(interfaces, schemas)

    if args.json:
        print(json.dumps({"report": report, "problems": problems}, indent=2))
        return 1 if problems else 0

    for entry in report:
        status = "ok" if not (entry["missing_on_client"] or entry["not_on_server"]) else "DRIFT"
        if args.verbose or status != "ok":
            print(f"[{status:5}] {entry['interface']:<24} <- {entry['schema']} ({len(entry['server_fields'])} fields)")

    if problems:
        print()
        for problem in problems:
            print(f"  ! {problem}")
        print(f"\nAPI type check failed with {len(problems)} problem(s).")
        return 1

    print(f"\nAPI types match the server schemas ({len(report)} interfaces compared).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
