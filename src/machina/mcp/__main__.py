"""CLI entrypoint for the Machina MCP server.

Usage:
    python -m machina.mcp --config machina.yaml --transport stdio
    python -m machina.mcp --config machina.yaml --transport streamable-http --port 8000
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Machina MCP Server",
        prog="python -m machina.mcp",
    )
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
    parser.add_argument("--host", default="0.0.0.0", help="Host for HTTP transport")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    args = parser.parse_args()

    from machina.config.loader import load_yaml
    from machina.config.schema import MachinaConfig
    from machina.mcp.server import serve

    try:
        raw = load_yaml(args.config)
    except FileNotFoundError:
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    config = MachinaConfig.model_validate(raw)
    serve(config, transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
