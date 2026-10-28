"""CLI is a JSON client of the same owner used by the MCP server."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def parser():
    result = argparse.ArgumentParser(prog="c1ai")
    sub = result.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("owner", help="Run the independent per-user owner")
    owner.add_argument("--serve", action="store_true", required=True)
    sub.add_parser("mcp", help="Run the official SDK's stdio MCP thin client")
    for command in ("context", "images", "read", "adjust", "preview", "operations", "request"):
        item = sub.add_parser(command)
        source = item.add_mutually_exclusive_group()
        source.add_argument("--json", dest="json_text", help="Request or command arguments as a JSON object")
        source.add_argument("--file", type=Path, help="UTF-8 JSON request file")
        item.add_argument("--timeout", type=float, default=120)
        item.add_argument("--no-start", action="store_true", help="Connect without starting an owner")
        item.add_argument("--compact", action="store_true", help="Return decision facts; operations get keeps the full record")
    return result


def main(argv=None):
    # JSON CLI output is UTF-8 even when Windows redirects stdout under a
    # non-UTF-8 locale. Never corrupt Chinese paths or source evidence strings.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.command == "owner":
        from .owner import serve_owner
        return serve_owner()
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return 0
    try:
        text = args.file.read_text(encoding="utf-8-sig") if args.file else args.json_text
        request = json.loads(text) if text else {}
        if not isinstance(request, dict):
            raise ValueError("Request must be a JSON object")
        if args.command != "request":
            if request.get("command", args.command) != args.command:
                raise ValueError("JSON command conflicts with the CLI command")
            request["command"] = args.command
        elif not isinstance(request.get("command"), str):
            raise ValueError("request requires a command field")
        from .client import call
        result = call(request, timeout=args.timeout, autostart=not args.no_start)
        if args.compact:
            from .formatting import compact_response
            result = compact_response(result, command=request.get("command"), action=request.get("action"))
    except (OSError, ValueError) as error:
        result = {"status": "refused", "error": {"code": "INVALID_CLI_REQUEST", "message": str(error)}}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return 1 if result.get("status") in ("refused", "outcome_unknown", "partial") else 0


if __name__ == "__main__":
    raise SystemExit(main())
