"""Console entry point."""

from __future__ import annotations

import argparse
import sys

from . import inspect, refresh, unload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tableau-hyper-refresher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--datasource-id", required=True)
    inspect_parser.add_argument(
        "--force-full-load", choices=("true", "false"), default="false"
    )

    unload_parser = subparsers.add_parser("unload")
    unload_parser.add_argument("--table-text", required=True)
    unload_parser.add_argument("--table-type", choices=("table", "text"), required=True)
    unload_parser.add_argument("--table-key", required=True)
    unload_parser.add_argument("--datasource-ids")
    unload_parser.add_argument("--s3-bucket")
    unload_parser.add_argument("--s3-prefix")
    unload_parser.add_argument("--refresh-config")

    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--datasource-id", required=True)
    refresh_parser.add_argument("--s3-bucket")
    refresh_parser.add_argument("--format", choices=("csv", "parquet"), default="csv")
    refresh_parser.add_argument("--refresh-config")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    forwarded: list[str] = []
    for key, value in vars(args).items():
        if key == "command" or value is None:
            continue
        forwarded.extend((f"--{key.replace('_', '-')}", str(value)))

    original_argv = sys.argv
    try:
        sys.argv = [f"tableau-hyper-refresher {args.command}", *forwarded]
        {"inspect": inspect.main, "unload": unload.main, "refresh": refresh.main}[
            args.command
        ]()
    finally:
        sys.argv = original_argv
