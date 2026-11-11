"""`python -m ktv_bridge` runs the MCP server on stdio.

`approvals`, `approve` and `reject` are for humans only, which is why they are not MCP tools.
"""
import argparse
import json
import sys

from .storage import BridgeError, Store, default_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ktv_bridge")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server on stdio (the default)")
    sub.add_parser("approvals", help="list approval requests waiting for a human")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"{name} a pending approval request")
        p.add_argument("approval_id")
        p.add_argument("--by", required=True, help="your name, recorded in the audit log")
        p.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    if args.cmd in (None, "serve"):
        from .server import mcp
        mcp.run()
        return 0

    store = Store(default_db_path())
    try:
        if args.cmd == "approvals":
            result = store.list_approvals()
        else:
            result = store.decide_approval(args.approval_id, args.cmd == "approve", args.by, args.reason)
    except BridgeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
