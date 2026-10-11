#!/usr/bin/env python3
"""Check source links and obvious credentials without printing secret values."""
import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "age-identity": re.compile("AGE-SECRET-" + r"KEY-1[A-Z0-9]{58}"),
    "tailscale-key": re.compile(r"tskey-(?:auth|api|client)-[A-Za-z0-9_-]{16,}"),
    "github-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "cloudflare-secret": re.compile(r'''["']TunnelSecret["']\s*:\s*["'][A-Za-z0-9+/=]{32,}["']'''),
    "cloudflare-account-certificate": re.compile("-----BEGIN ARGO " + "TUNNEL TOKEN-----"),
}
BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def secret_types(content):
    found = {name for name, pattern in PATTERNS.items() if pattern.search(content)}
    for match in BASE64.finditer(content):
        try:
            value = json.loads(base64.b64decode(match.group(), validate=True))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(value, dict) and {"a", "s", "t"} <= value.keys():
            found.add("cloudflare-connector-token")
    return found


def links(content):
    for token in MarkdownIt().parse(content):
        for child in token.children or []:
            if child.type == "link_open":
                yield child.attrGet("href")
            elif child.type == "image":
                yield child.attrGet("src")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Also scan reachable Git history")
    args = parser.parse_args()
    failures = []
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
    for name in filter(None, names):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for kind in secret_types(content):
            failures.append(f"Credential pattern: {name} ({kind})")
        if path.suffix == ".md":
            for href in links(content):
                if not href or href.startswith(("/", "#")) or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", href):
                    continue
                from urllib.parse import unquote
                target = unquote(href.split("#", 1)[0].split("?", 1)[0])
                if target and not (path.parent / target).exists():
                    failures.append(f"Broken local link: {name} -> {href}")
    if args.history:
        # Scan every unique historical blob, including files deleted before HEAD.
        objects = git("rev-list", "--objects", "--all").decode().splitlines()
        for entry in objects:
            oid, _, name = entry.partition(" ")
            if not name or git("cat-file", "-t", oid).strip() != b"blob":
                continue
            content = git("cat-file", "blob", oid).decode("utf-8", errors="replace")
            for kind in secret_types(content):
                failures.append(f"Historical credential pattern: {oid[:12]} {name} ({kind})")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        return 1
    print("Local documentation links and credential-pattern scan: OK")
    if args.history:
        print("Reachable Git history credential-pattern scan: OK")
    print("Pattern scanning is not a proof that all possible secrets are absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
