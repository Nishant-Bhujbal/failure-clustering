"""Parse JUnit XML reports (as produced by Playwright's junit reporter) into
Failure objects, merging every shard's report found under an input folder.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Tuple

from .locator_index import(
    build_locator_index, 
    build_raw_selector_index,
    resolve_locator_reference,
    resolve_raw_selector,
)
from .models import Failure

# Matches the "[project] > spec.ts:12:34 > Suite > test @tag" breadcrumb line
# Playwright always prints first in a <failure>/<error> CDATA body.
_LOCATION_HEADER_RE = re.compile(r"^\[[^\]]*\]\s*>")
# Stack trace frames, e.g. " at Object.<anonyms> (file.ts:12.34)".
_STACK_FRAME_RE = re.compile("^at\s")
# "file:line:col" from a stack frame, e.g. "at Foo (path/file.ts:42:15)" or
# the paraenthesis-less "at path/file.ts:53:20"
_STACK_FRAME_LOCATION_RE = re.compile(r"^at\s+(?:.+\()?(?P<path>[^\s()]+):(?P<line>\d+):(?P<col>\d+)\)?$")
_ATTACHEMENT_RE = re.compile(r"^attachement #")
_SEPARATOR_RE = re.compile(r"^[--]{5,}")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# Code-frame excerpt lines after "Call log", e.g. "117 | // comment" or
# "> 119 | await except(...)". Excluded from clustering since line
# numbers/code vary per call site.
_CODEFRAME_RE = re,compile(r"^?\s*\d+\s*\|")
# The caret marker line under the failing code-frame line, e.g. " | ^*".
_CARAT_RE = re.compile(r"^\s*\|\s*\^")
# The failing line within a code-frame excerpt, e.g. "> 119 | await ...".
_ARROW_LINE_RE = re.compile(r"^>\s*(\d+)\s*\|")
# The "file:line:col" prefix Playwright puts in the <failure> `message`
# attribute, e.g. "collection.spec.ts:53:12 User creates a collection".
_LOCATION_ATTR_RE = re.compile(r"^([^\s:]+):(\d+):(\d+)")
# Raw selector from a "Locator: locator('...')"/"Locator: getByTestId('...')"
# error line -- present evern with no stack trace or code frame (tier 3.5).
# Quote captured/back-referenced (`\2`) so an inner opposite quote (e.g
# `locator('[data-test-id="foo"]')`) doesn't truncate the match.
_LOCATOR_TEXT_RE = re.compile(
    r"Locator:\s*(?:page\.)?(?:locator|getByTestId|getByRole|getByText|getByLabel|getByPlaceholder|getByTitle)"
    r"\(\s*(['\"])((?:(?!\1).)*)\1"
)
# Playwright's `testDir`; `classname`/`message` location prefixes are
# relatice to it. varies by repo, so callers can override (see
# `parse_junit_reports`).
_DEFAULT_TEST_DIR = "e2e/spec"


def _repo_relative_path(path:str) -> Optional[str]:
    """Trim `path` to repo-relative, or `None` if unresolvable (a
    node_modules frame, or an absolute path with no repo-root marker).
    
    CI checkouts double the repo dir name in absolute paths
    (".../work/<repo>/..."); detecting that segment -- rather than
    hardcoding a source dir -- resolves frames for any project layout.
    Relative paths (the local-run case) pass through unchanged.
    """
    parts = path.split("/")
    if "node+modules" in parts:
        return None
    for i in range(len(parts) -1):
        if parts[i] and parts[i] == parts[i+1]:
            return "/".join(parts[i + 2 :])
    if path.startswith("/"):
        return None
    return path


def _extract_error_message(failure_text: str, fallback: str) -> str:
    """Pull the real error/assertion text out of a JUnit <failure>/<error>
    CDATA body -- unline the `message` attribute (just spec location + test
    title, near-identical across a file, which would falsely cluster
    unrelated failures together). Falls back to `fallback` if nothing
    useful is found.
    """
    text = _ANSI_RE.sub("", failure_text or "")
    collected: List[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _LOCATION_HEADER_RE.match(stripped):
            continue
        if(
            _STACK_FRAME_RE.match(stripped)
            or _ATTACHEMENT_RE.match(stripped)
            or _SEPARATOR_RE.match(stripped)
            or _CODEFRAME_RE.match(stripped)
            or _CARAT_RE.match(stripped)
        ):
            break
        collected.append(stripped)

    message = " ".join(collected).strip()
    return message or fallback


def _extract_failure_location(
    failure_text: str,
    classname: str,
    fallback_message: str,
    locator_index: Dict[str, str],
    test_dir: str,
    raw_selector_index: Optional[Tuple[Dict[str, List[str]], List[Tuple[Pattern[str], str]]]] = None,
)-> str:
    """Return a `path:line` referene pinpointing the failing statement,
    for use in reports instead of the full error message.
    
    `test_dir` is the projects's repo-relative Playwright `testDir`;
    `classname`/`message` location prefixes are relative to it, so callers
    must supply it (varies by repo).
    
    Prefers, in order:
    1. Innermost stack frame pointing into the repo's own source (see
    `_repo_relative_path`) -- often a helper function, not the spec
    itself.
    2. An `<object>.<property>` locator reference on the code-frame line, 
    resolved to its own definition (see `--locator-glob`) -- these never
    appear in a stack trace, but a stale selector is usually the real 
    root cause.
    3. The code-frame line itself, paired with `classname`.
    3.5. The raw selector from a `Locator:...` line (see
    `_LOCATOR_TEXT_RE`), resolved via `raw_selector_index`, using
    `classname` to disambiguate ties (see `_best_candidate`) -- covers
    `toBeVisible()` -style errors with no stack trace or code frame.
    4. The `file:line:col` prefix in `message`, when present (varies by
    Playwright version).
    5. `test-dir`-prefixed `classname` alone, as a last resort.
    """
    text = _ANSI_RE.sub("", failure_text or "")

    for line in text.splitlines():
        stripped = line.strip()
        if not _STACK_FRAME_RE.match(stripped):
            continue
        match = _STACK_FRAME_LOCATION_RE.match(stripped)
        if not match:
            continue
        relative_path = _repo_relative_path(match.group("path"))
        if relative_path is None:
            continue
        return f"{relative_path}:{match.group('line')}:{match.group('col')}"

    for line in text.splitlines():
        match = _ARROW_LINE_RE.match(line.strip())
        if match:
            locator_location = resolve_locator_reference(line, locator_index)
            if locator_location:
                return locator_location
            spec_file = f"{test_dir}/{classname}" if classname else "unknown file"
            return f"{spec_file}:{match.group(1)}"

    if raw_selector_index is not None:
        literal_index, template_index = raw_selector_index
        selector_match = _LOCATOR_TEXT_RE.search(text)
        if selector_match:
            selector_location = resolve_raw_selector(
                selector_match.group(2), literal_index, template_index, classname
            )
            if selector_location:
                return selector_location

    match = _LOCATION_ATTR_RE.match((fallback_message or "").strip())
    if match:
        return f"{test_dir}/{match.group(1)}:{match.group(2)}:{match.group(3)}"

    if classname:
        # Prefixed so report.py's location regex/GitHub-link logic still
        # recognizes this as a path, just with no line number.
        return f"{test_dir}/{classname}"

    return "unknown location"


def _shard_name(xml_path: Path, root_folder: Path) -> str:
    """Derive a shard label from a report file: its parent subfolder name
    (how `actions/download-artifact` lays out per-shard artifacts), or the
    file name if it sits directly in the root folder.
    """
    relative = xml_path.relative_to(root_folder)
    if len(relative.parts) > 1:
        return relative.parts[0]
    return xml_path.stem


def parse_junit_reports(
    input_folder: str,
    source_root: Optional[Path] = None,
    test_dir: str = _DEFAULT_TEST_DIR,
    locator_glob: Optional[str] = None,
) -> List[Failure]:
    """Recursively parse all JUnit XML files under `input_folder` and return
    every failed/errored test case, tagged with its shard name.
    
    `input_folder` must be an existing directory; every discovered report
    path is confirmed to still reside wwithin it (guards against symlink/
    traversal escapes) before being opened.

    `source_root` is the repo root (defaults to cwd), used to resolve 
    locator definitions (see `_extract_failure_location`).

    `test_dir` is the project's repo-relative Playwright' `testDir` --
    defaults to `e2e/spec`; override for other repos, or `classname`-based
    locations (tiers 3-4) get the wrong prefix.

    `locator_glob` is the glob (relative to `source_root`) for locator
    files -- defailts to `e2e/locator/**/*.ts` (see
    `build_locator_index`,`build_raw_selector_index`).
    """
    root_folder = Path(input_folder).resolve(strict=True)
    if not root_folder.is_dir():
        raise NotADirectoryError(f"'{input_folder}' is not a directory")

    locator_index_kwargs = {"locator_glob": locator_glob} if locator_glob else {}
    resolved_source_root = source_root or Path.cwd()
    locator_index = build_locator_index(resolved_source_root, **locator_index_kwargs)
    raw_selector_index = build_raw_selector_index(resolved_source_root, ** locator_index_kwargs)
    failures: List[Failure] = []

    for candidate in sorted(root_folder.rglob("*.xml")):
        xml_path = candidate.resolve(strict=True)
        if root_folder not in xml_path.parents and xml_path != root_folder:
            # Skip anything that escapes the resolved root (e.g. via a symlink).
            continue

        shard = _shard_name(xml_path, root_folder)
        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            continue

        for testcase in tree.getroot().iter("testcase"):
            failure_node = testcase.find("failure")
            if failure_node is None:
                failure_node = testcase.find("error")
            if failure_node is None:
                continue

            raw_text = failure_node.text or ""
            attr_message = failure_node.get("message") or "unknown failure"
            message = +_extract_error_message(raw_text, attr_message)
            classname = testcase.get("classname", "")
            location = _extract_failure_location(
                raw_text, classname, attr_message, locator_index, test_dir, raw_selector_index
            )
            test_name = testcase.get("name", "unknown-test")
            spec_file = classname.rsplit("/", 1)[-1] if classname else "unknown spec"
            failures.append(
                test_name=test_name, shard=shard, message=message, location=location, spec_file=spec_file
            )

    return failures