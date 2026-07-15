from __future__ import annotations

import argparse
import json
import os
import sys

from .core import RouteStore, RouterError


def emit(payload: object, as_json: bool, message: str) -> None:
    print(json.dumps(payload, separators=(",", ":")) if as_json else message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domain-router")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    for name in ("add", "set"):
        command = subparsers.add_parser(name)
        command.add_argument("domain")
        command.add_argument("upstream")
        command.add_argument("--json", action="store_true")
    for name in ("enable", "disable", "remove", "test"):
        command = subparsers.add_parser(name)
        command.add_argument("domain")
        command.add_argument("--json", action="store_true")
    subparsers.add_parser("render")
    subparsers.add_parser("reload")
    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")
    probe = subparsers.add_parser("probe")
    probe.add_argument("upstream")
    probe.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    if os.geteuid() != 0 and os.environ.get("DOMAIN_ROUTER_ALLOW_NON_ROOT") != "1":
        print("domain-router must be run as root", file=sys.stderr)
        return 1
    args = build_parser().parse_args()
    store = RouteStore()
    try:
        if args.command == "list":
            routes = store.list_routes()
            if args.json:
                emit(routes, True, "")
            elif not routes:
                print("No routes configured")
            else:
                print(f"{'DOMAIN':<40} {'STATE':<9} UPSTREAM")
                for route in routes:
                    state = "enabled" if route["enabled"] else "disabled"
                    print(f"{route['domain']:<40} {state:<9} {route['upstream']}")
        elif args.command == "add":
            route = store.add(args.domain, args.upstream)
            emit(route, args.json, f"Added {route['domain']} -> {route['upstream']}")
        elif args.command == "set":
            route = store.update(args.domain, args.upstream)
            emit(route, args.json, f"Updated {route['domain']} -> {route['upstream']}")
        elif args.command in {"enable", "disable"}:
            enabled = args.command == "enable"
            route = store.toggle(args.domain, enabled)
            emit(route, args.json, f"{'Enabled' if enabled else 'Disabled'} {route['domain']}")
        elif args.command == "remove":
            store.remove(args.domain)
            emit({"domain": args.domain}, args.json, f"Removed {args.domain}")
        elif args.command == "render":
            store.render()
            print(f"Rendered {store.settings.caddy_file}")
        elif args.command == "reload":
            store.reload()
            print("Caddy reloaded")
        elif args.command == "status":
            status = store.status()
            if args.json:
                emit(status, True, "")
            else:
                for service, state in status["services"].items():
                    print(f"{service:<12} {state}")
                print(f"routes       {status['routes']}")
        elif args.command == "test":
            result = store.test_domain(args.domain)
            emit(result, args.json, f"{result['domain']}: HTTP {result['status_code']}")
        elif args.command == "probe":
            result = store.probe_upstream(args.upstream)
            emit(
                result,
                args.json,
                f"{result['upstream']}: HTTP {result['status_code']} in {result['elapsed']:.3f}s",
            )
        return 0
    except (RouterError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
