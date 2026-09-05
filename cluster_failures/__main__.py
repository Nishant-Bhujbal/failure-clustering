"""CLI entry point.

Usage:
    python -m cluster_failures --input <folders> [--threshold N] [--format text|json]
"""

import argparse
import sys
from pathlib import Path

from .clustering import cluster_failures
from .parser import parse_junit_reports
from .report import print_report, to_json, to_markdown


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cluster CI test failures across sharded JUnit XML reports by root cause."
    )
    parser.add_argument(
        "--source-root",
        default=".",
        help=(
            "Path to the checked-out repo, used to resolve locator selector"
            "definitions so the report can link to the failing selector's own"
            "definition instead of just the spec file. Default: current directory."
        ),
    )
    parser.add_argument(
        "--test-dir",
        default="e2e/spec",
        help=(
            "Repo-releative path to the project's Playwright testDir (see playwright.config.ts)."
            "Used to build correct spec-file locations when only the JUnit 'classname' is available"
            "Must match the repo under -- this defaults to this repo's own testDir, so ovveride"
            "it when parsing another repo's reports. Default: e2e/spec."
        ),
    )
    parser.add_argument(
        "--locator-glob",
        default=None,
        help=(
            "Glob (releative to --source root) matching Typescript locator/selector definition files,"
            "e.g. 'e2e/locator/**/*.ts'. Only used to resolve a failing selector's own definition;"
            "if omitted or no matches are found, the report fails back to the spec file's location instead."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=85.0,
        help="Similarity ratio (0-100) above which failures are considered the same root cause. Default: 85.0"
    )
    parser.add_argument(
        "--format",
        choices=["text","json", "markdown"],
        default="text",
        help=(
            "Ouput format. 'markdown' renders GitHub-flavored Markdown with real clickable links"
            "(e.g. for $GITHUB_STEP_SUMMARY or a PR comment) instead of the terminal report's OSC 8"
            "hyperlinks, whic don't render in GitHub Actions' plain-text log viewer. Default: text."
        ),
    )
    args = parser.parse_args()

    input_folder = Path(args.input)
    if not input_folder.isdir():
        print(f"Error: '{args.input}' is not a directory", file=sys.stderr)
        return 1

    try:
        failures = parse_junit_reports(
            args.input,
            source_root=Path(args.source_root),
            test_dir=args.test_dir,
            locator_glob=args.locator_glob
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not failures:
        print(f"No failures found in the provided reports.")
        return 0

    clusters = cluster_failures(failures, threshold=args.threshold)
    shard_count = len({f.shard for f in failures})

    if args.format == "json":
        print(to_json(clusters))
    elif args.format == "markdown":
        print(to_markdown(clusters, total_failures=len(failures), shard_count=shard_count))
    else:
        print_report(clusters, total_failures=len(failures), shard_count=shard_count)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())