from __future__ import annotations

import argparse
import getpass
import sys

from .config import default_settings
from .database import Database


def read_password(prompt: str = "Password: ") -> str:
    password = getpass.getpass(prompt)
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match")
    return password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="domain-router-ui-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("username")
    reset = subparsers.add_parser("reset-password")
    reset.add_argument("username")
    subparsers.add_parser("status")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database = Database(default_settings().database_path)
    database.initialize()
    try:
        if args.command == "create":
            user_id = database.create_user(args.username, read_password())
            print(f"Created administrator {args.username} with id {user_id}")
        elif args.command == "reset-password":
            if not database.reset_password(args.username, read_password("New password: ")):
                raise ValueError(f"User not found: {args.username}")
            print(f"Password reset for {args.username}; all sessions revoked")
        else:
            print(f"users: {database.user_count()}")
        return 0
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
