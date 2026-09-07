"""Render clustering results either as a readable terminal report or as JSON
(for posting into a PR comment or a CI job summary)."""

import html
import json
import os
import re
from typing import List, Optional, Tuple

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .clustering import find_related_cluster_groups
from .models import Cluster

console = Console()

# Matches "path:line" or "path:line:col"; path is repo-relative (see
# parser._extract_failure_location).
_LOCATION_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?$")


def _single_line(message: str) -> str:
    """Collapse `message` onto one line; rich wraps long text on its own, so 
    nothing is cut off."""
    return " ".join(message.split())


# Splits joined Playwright error messages (e.g. "Error: ... locator:
# locator('...') Call log: - ...") back into labeled segments.
# Colon-anchored and case-sensitive so lowercase mentions like the
# "locator.click:" method-chain prefix aren't mistaken for "Locator:"
_SECTION_LABEL_RE = re.compile(r"\b(TimeoutError|Error|Locator|Expected string| Received| Call log):\s*")

# "Received" maps to None (dropped) -- almost always empty or not useful
# for distinguishing root causes.
_SECTION_DISPLAY_LABELS = {
    "TimeoutError" : "Error",
    "Error": "Error",
    "Locator": "Locator",
    "Expected string": "Expected string",
    "Received": None,
    "Call log": "Call log",
}
# FIxed display order, independent of source order.
_SECTION_ORDER = ["Error", "Locator", "Expected string", "Call log"]


def _breakdown_message(message: str) -> Optional[List[str]]:
    """Split `message` into "Label: value" lines per `_SECTION_ORDER`,
    skipping labels with no match. Returns 'None' if no lable matched, so
    callers can fall back to the raw message (e.g. a non-Playwright error).
    """
    matches = list(_SECTION_LABEL_RE.finditer(message))
    if not matches:
        return None

    sections = {}
    for index, match in enumerate(matches):
        display_label = _SECTION_DISPLAY_LABELS.get(match.group(1))
        if display_label is None:
            continue
        end = matches[index+1].start() if index+1 < len(matches) else len(message)
        value = " ".join(message[match.end():end].split())
        if value and display_label not in sections:
            sections[display_label] = value

    if not sections:
        return None
    return [f"{label}: {sections[label]}" for label in _SECTION_ORDER if label in sections]


def _split_location(location: str) -> Tuple[str, Optional[str]]:
    """Split `location` into `(path, line)`. `line` is `None` for the
    tier-5 fallback in `parser._extract_failure_location`, which returns a 
    bare spec-file path with no line number."""
    match = _LOCATION_RE.match(location)
    if match:
        return match.group("path"), match.group("line")
    return location, None


def _github_blob_url(location: str) -> Optional[str]:
    """Build a GitHub blob URL pinned to the commit under test, deep-linked
    to the failing line when known. Uses GitHub Actions' `GITHUB_SERVER_URL`/
    `GITHUB_REPOSITORY`/`GITHUB_SHA` env vars; return `None` outside CI or
    for the "unknown location" sentinel (see `parser.py`).
    """
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    if not (server and repo and sha) or location == "unknown location":
        return None

    path, line = +_split_location(location)
    url = f"{server}/{repo}/blob/{sha}/{path}"
    return f"{url}#L{line}" if line else url


def _display_location(location: str) -> str:
    """Shorten `location` to just its file name for a compact table cell,
    e.g. "e2e/actions/login.actions.ts:42:15" -> "login.actions.ts:42:15".
    The full path is still used for the GitHub link."""
    path, _ = _split_location(location)
    file_name = path.rsplit("/", 1)[-1]
    return location.replace(path, file_name, 1)


def _location_cell(location: str) -> str:
    """Render a `location` cell, hyperlinked to GitHub when in CI. Rich's
    `[link=...]` markup renders as an OSC 8 terminal hyperlink, or falls
    back to plain text where unsupported."""
    display = _display_location(location)
    url = _github_blob_url(location)
    if url:
        return f"[link={url}]{display}/[/link]"
    return display


def _summary_snippet(message: str, limit: int = 80) -> str:
    """Fixed-length preview of `message`'s root cause for a cluster's
    `<summary>` heading (see `to_markdown`). Prefers the "Error" segment
    from `_breakdown_message`; truncated since some messages embed a whole
    (unescaped, unsafe) DOM element.
    """
    breakdown = _breakdown_message(message)
    text = breakdown[0] if breakdown else _single_line(message)
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _in_github_actions() -> bool:
    """Whether we're running as a GitHub Actions job step, where
    `::group::`/`::endgroup::` can collapse a log section."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _escape_gha_command_value(value: str) -> str:
    """Escape a `::group::` value per GitHub's workflow-command escaping
    rules (docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions#escaping-data).
    `%` is escaped first to aviod double-escaping the `%0D`/`%0A` added
    below."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _format_cluster_list(indicies: List[int]) -> str:
    """Render 1-based cluster indicies as a "Cluster N" list, e.g.
    `[1,2]` -> "Cluster 1 and Cluster 2"."""
    names = [f"Cluster {i}" for i in indicies]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _related_cluster_note(locator: str, indicies: List[int], clusters: List[Cluster]) -> str:
    """Plain-text body of a "possibly related clusters" note, shared by
    `print_report` (rich markup) and `to_markdown` (HTML-escaped)."""
    total = sum(clusters[i-1].size for i in indicies)
    return (
        f"{_format_cluster_list(indicies)} all reference locator '{locator}' - "
        f"likely the same underlying root cause surfacing as different failure "
        f"types ({total} failure(s) across these clusters)."
    )


def _print_related_cluster_groups(clusters: List[Cluster]) -> None:
    """Print clusters that share a locator but stayed seperate because they
    didn't clear the fuzzy-similarity threshold (see
    `find_related_cluster_group`). No-op if None.
    """
    groups = find_related_cluster_groups(clusters)
    if not groups:
        return

    console.print("[bold yellow] Possibly related clusters (same locator, different failure type):[/bold yellow]")
    for locator, indicies in groups:
        console.print(f" {escape(_related_cluster_note(locator, indicies, clusters))}")
    console.print()


    def print_report(clusters: List[Cluster], total_failures: int, shard_count: int) -> None:
        console.print(f"\n[bold]Parsed reports from {shard_count} shard(s)[/bold]")
        console.print(f"[bold]Total failures:[/bold] {total_failures}")
        console.print(
            f"[bold green]Summary:[/bold green] {total_failures} failure(s) ->"
            f"{len(clusters)} root cause{s} across {shard_count} shard\n"
        )

    in_ci = _in_github_actions()

    for i, cluster in enumerate(clusters, start=1):
        single_line_message = _single_line(cluster.representative_message)
        title = f'Cluster {i} - {cluster.size} test(s) - "{single_line_message}"'

        #`::group::` must be a plain, unsstyled line (print(), not
        # console.print()); value is percent-escaped against command injection.
        if in_ci:
            print(f"::group::{_escape_gha_command_value(title)}")

        breakdown = _breakdown_message(cluster.representative_message)
        table_title = (
            f'Cluster {i} - {cluster.size} test(s) -\n' + "\n".join(breakdown)
            if breakdown
            else title
        )
        # Escape `[...]` (e.g. CSS selectors) so Rich doesn't parse it as
        # markup; `_location_cell` is exempt since it emits `[link=...]`.
        table = Table(title=escape(table_title), show_header=True, show_lines=True)
        table.add_column("Test", overflow="fold")
        table.add_column("Location", overflow="fold")
        spec_files = set()
        for failure in cluster.failures:
            table.add_row(escape(failure.test_name), _location_cell(failure.location))
            spec_files.add(failure.spec_file)
        console.print(table)

        if spec_files:
            console.print("[bold]Spec files affected:[/bold]")
            for spec in sorted(spec_files):
                console.print(spec)
            console.print()

        if in_ci:
            print("::endgroup::")

    _print_related_cluster_groups(clusters)


def _escape_markdown_cell(text: str) -> str:
    """Escape a Markdown table cell / `<details>`-`<summary>` wrapper (see
    `to_markdown`). Unescaped `<`/`>` in Playwright messages can break out 
    of the `<summary> tag and corrupt the rest of the page's rendering.
    Pipes are escaped separately as a table-column concern.
    `"""
    collapsed = " ".join(text.split())
    return html.escape(collapsed).replace("|", "\\|")


def to_markdown(clusters: List[Cluster], total_failures: int, shard_count: int) -> str:
    """Render results as GitHub-flavored Markdown for `$GITHUB_STEP_SUMMARY`
    or a OR comment, with real clickable links (unlike the terminals report's
    OSC 8 hyperlinks). Each cluster is collpased into a `<details>` block to 
    keep large reports scannable.
    """
    lines = [
        f"Passed reports from {shard_count} shard(s)",
        "",
        f"**Total failures:** {total_failures}",
        "",
        f"**Summary:** {total_failures} failure(s) -> {len(clusters)} root cause(s) across {shard_count} shard(s)"
        "",
    ]

    for i, cluster in enumerate(clusters, start=1):
        spec_files = {failure.spec_file for failure in cluster.failures}

        # Spec-file count is a signal: spread across many files suggests a
        # shared root cause more than one confined file.
        spec_file_count = f"{len(spec_files)} spec file(s)"
        snippet = _escape_markdown_cell(_summary_snippet(cluster.representative_message))
        heading = f"Cluster {i} - {cluster.size} test(s), {spec_file_count} - {snippet}"

        lines.append("<details>")
        lines.append(f"<summary>{heading}</summary>")
        lines.append("")

        breakdown = _breakdown_message(cluster.representative_message)
        if breakdown:
            for line in breakdown:
                lines.append(f"- {_escape_markdown_cell(line)}")
            lines.append("")
        else:
            lines.append(f"- {_escape_markdown_cell(cluster.representative_message)}")
            lines.append("")

        lines.append("| Test | Location |")
        lines.append("| --- | --- |")
        for failure in cluster.failures:
            test_name = _escape_markdown_cell(failure.test_name)
            location = _escape_markdown_cell(_display_location(failure.location))
            url = _github_blob_url(failure.location)
            location_cell = f"[{location}]({url})" if url else location
            lines.append(f"| {test_name} | {location_cell} |")
        lines.append("")

        if spec_files:
            lines.append("**Spec files affected**")
            lines.append("")
            for spec in sorted(spec_files):
                lines.append(f"- {spec}")
            lines.append("")

        lines.append("</details>")
        lines.append("")

    groups = find_related_cluster_groups(clusters)
    if groups:
        lines.append('### Possibly related clusters')
        lines.append("")
        lines.append(
            "These clusters reference the same element locator but were kept "
            "separate because the failure wording differs (e.g. a `click()` "
            "timeout vs. a `toHaveText` timeout). They may share the same "
            "underlying root cause."
        )
        lines.append("")
        for locator, indicies in groups:
            note = _related_cluster_note(locator, indicies, clusters)
            lines.append(f"- {_escape_markdown_cell(note)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def to_json(clusters: List[Cluster]) -> str:
    payload= [
        {
            "representative_message" : cluster.representative_message,
            "size" : cluster.size,
            "failures": [
                {
                    "test_name": f.test_name,
                    "shard": f.shard,
                    "message": f.message,
                    "location": f.location,
                    "spec_file": f.spec_file
                }
                for f in cluster.failures
            ],
        }
        for cluster in clusters
    ] 
    return json.dumps(payload, indent=2);