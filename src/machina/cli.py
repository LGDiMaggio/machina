"""Command-line entry point for Machina.

Usage::

    machina describe          # human-readable self-description
    machina describe --json   # JSON form (identical to docs/capabilities.json)

The ``describe`` subcommand calls :func:`machina.introspect.describe` and prints
a readable text summary of the framework's connectors, their capabilities (with
configurable markers), the orphaned-capability gaps, and the extension seams.
With ``--json`` it prints the same structured dict that ``docs/capabilities.json``
carries, serialized via :func:`machina.introspect.render_llms.render_json` so the
two are byte-for-byte equivalent in shape.

``describe()`` is sync-safe and reads capabilities off connector *classes* (no
instantiation, no event loop, no heavy optional dependency import), so the CLI
needs no asyncio runtime.
"""

from __future__ import annotations

import argparse
import json
import sys

from machina.introspect import Spine, describe
from machina.introspect.render_llms import render_json


def _bool_label(value: bool | None) -> str:
    """Render an optional boolean as yes / no / n/a."""
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _format_describe(spine: Spine) -> str:
    """Build the human-readable text summary of a :class:`Spine`."""
    lines: list[str] = ["Machina self-description (code-derived)", ""]

    # --- Connectors x capabilities -------------------------------------
    lines.append("Connectors")
    lines.append("==========")
    for conn in spine.connectors:
        extra = conn.requires_extra or "core"
        installed = _bool_label(conn.extra_installed)
        header = f"- {conn.type} ({conn.class_name}) [extra: {extra}, installed: {installed}]"
        if conn.degraded:
            header += " DEGRADED"
        lines.append(header)
        if conn.degraded and conn.error:
            lines.append(f"    error: {conn.error}")
        if not conn.capabilities:
            lines.append("    (no class-readable capabilities)")
        for cap in conn.capabilities:
            marker = " (configurable)" if cap.configurable else ""
            lines.append(f"    - {cap.capability} -> {cap.method}(){marker}")
    lines.append("")

    # --- Orphaned-capability gaps --------------------------------------
    lines.append("Capability gaps")
    lines.append("===============")
    if spine.gaps.orphaned_capabilities:
        lines.append("Orphaned (no registered connector provides these):")
        by_value = {c.value: c for c in spine.capabilities}
        for value in spine.gaps.orphaned_capabilities:
            note = by_value[value].orphan_note if value in by_value else ""
            lines.append(f"  - {value}" + (f" — {note}" if note else ""))
    else:
        lines.append("None — every capability has a registered provider.")
    if spine.gaps.unmapped_capabilities:
        lines.append(
            "Unmapped (absent from CAPABILITY_TO_METHOD — a guard signal): "
            + ", ".join(spine.gaps.unmapped_capabilities)
        )
    lines.append("")

    # --- Extension seams ------------------------------------------------
    lines.append("Extension seams")
    lines.append("===============")
    lines.append(f"Add a connector at: {spine.seams.add_connector_template}")
    lines.append("")
    lines.append("Protocol seams:")
    for proto in spine.seams.protocols:
        lines.append(f"  - {proto.name} ({proto.location})")
        if proto.doc:
            lines.append(f"      {proto.doc}")
        for method in proto.methods:
            qualifier = "async " if method.is_async else ""
            doc = f" — {method.doc}" if method.doc else ""
            lines.append(f"      * {qualifier}{method.name}(...){doc}")
    lines.append("")
    lines.append("Convention seams:")
    for conv in spine.seams.conventions:
        lines.append(f"  - {conv.name} — {conv.note}")
        lines.append(f"      location: {conv.location_template}")

    return "\n".join(lines)


def _write_stdout(text: str) -> None:
    """Write text to stdout, tolerating a non-UTF-8 console encoding.

    The spine may carry non-ASCII characters (e.g. an em dash in an orphan
    note or a scrubbed docstring). On a Windows console whose default codec is
    cp1252, ``print`` would raise ``UnicodeEncodeError``; encoding to the
    stream's own charset with ``errors="replace"`` keeps the CLI portable.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    data = text.encode(encoding, errors="replace").decode(encoding)
    sys.stdout.write(data)


def _cmd_describe(args: argparse.Namespace) -> int:
    """Run the ``describe`` subcommand."""
    spine = describe()
    if args.json:
        # JSON is ASCII-safe by default (ensure_ascii=True), so it prints
        # cleanly on any console codec.
        json.dump(render_json(spine), sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        _write_stdout(_format_describe(spine) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="machina",
        description="Machina — AI agents for industrial maintenance.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    describe_parser = subparsers.add_parser(
        "describe",
        help="Print a code-derived self-description (connectors, capabilities, seams).",
    )
    describe_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the JSON form (identical in shape to docs/capabilities.json).",
    )
    describe_parser.set_defaults(func=_cmd_describe)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (``0`` on success). An unknown or missing subcommand
        causes :mod:`argparse` to print usage and raise ``SystemExit(2)``.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
