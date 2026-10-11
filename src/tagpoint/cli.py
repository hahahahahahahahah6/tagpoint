"""Command-line interface for tagpoint."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .checker import CheckError, check


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tagpoint",
        description="Verify a PyPI release matches its GitHub tag (name AND content).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="check one package release against its repo tag")
    c.add_argument("package", help="PyPI package name, e.g. requests")
    c.add_argument("--repo", required=True, help="GitHub repo as owner/name, e.g. psf/requests")
    c.add_argument("--shallow", action="store_true",
                   help="skip file-list and tag-date checks (tag existence + sdist version only)")
    c.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"),
                   help="GitHub token (or set GITHUB_TOKEN) to raise API rate limits")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check":
        try:
            report = check(args.package, args.repo, deep=not args.shallow, token=args.token)
        except CheckError as e:
            print(f"tagpoint: error: {e}", file=sys.stderr)
            return 2
        print(report.render())
        return 0 if report.consistent else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
