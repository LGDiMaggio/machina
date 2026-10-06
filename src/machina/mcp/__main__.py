"""CLI entrypoint for the Machina MCP server.

Usage:
    python -m machina.mcp --config machina.yaml --transport stdio
    python -m machina.mcp --config machina.yaml --transport streamable-http --port 8000

The same options are available as ``machina mcp serve`` (see :mod:`machina.cli`),
which delegates to :func:`add_serve_arguments` and :func:`run_serve` here so the
two entry points cannot drift.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the MCP server options to ``parser``.

    Shared by ``python -m machina.mcp`` and ``machina mcp serve``.

    Args:
        parser: The parser (or subparser) to extend.
    """
    parser.add_argument(
        "--config",
        required=True,
        help="Path to machina.yaml configuration file",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            f"Bind address for streamable-http (default: {DEFAULT_HOST}; "
            "use 0.0.0.0 to listen on every interface, e.g. inside a container)"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port for streamable-http (default: {DEFAULT_PORT})",
    )


def run_serve(args: argparse.Namespace) -> int:
    """Load the configuration named by ``args`` and run the MCP server.

    Blocks until the server stops.

    Args:
        args: Parsed arguments produced by a parser extended with
            :func:`add_serve_arguments`.

    Returns:
        Process exit code: ``0`` after a clean shutdown, ``1`` when the
        configuration file does not exist.
    """
    from machina.config.loader import load_yaml
    from machina.config.schema import MachinaConfig
    from machina.mcp import server

    try:
        raw = load_yaml(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        return 1

    config = MachinaConfig.model_validate(raw)
    server.serve(config, transport=args.transport, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> None:
    """Run ``python -m machina.mcp``.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).
    """
    parser = argparse.ArgumentParser(
        description="Machina MCP Server",
        prog="python -m machina.mcp",
    )
    add_serve_arguments(parser)
    exit_code = run_serve(parser.parse_args(argv))
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
