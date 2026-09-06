"""Resolve locator/selector references to the `path:lint` where they're
actually *defined* inside the project's source, instead of just the spec
line that *used* them.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Tuple

# A top-level `export const <name> = { ... }` object literal declaration, 
# e.g. "export const loginPageLocators = {"
_EXPORT_CONST_RE = re.compile(r"^export\s+const\s+(\w+)\s*(?::[^=]+)?=\s*\{")
# A direct property of that object literal, e.g. `submitButton: ' ... '` or
# the method-shorthand form `openMenu(page: Page) {`/`async foo() {`.
_PROPERTY_RE = re.compile(r"^(?:async\s+)?(\w+)\s*[:(]")
# An `<object>.<property>` reference anywhere on a line, e.g.
# "loginPageLocators.submitButton" inside
# "page.locator(loginPageLocators.submitButton).click()".
_REFERENCE_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\b")

# Any Playwright-locator-producing call with a quoted string arg, e.g.
# `page.getByTestId('foo')`, `.locator('foo)`, or a project's own wrapper
# (e.g. `getLocatorByTestId('foo)`). Matches by name -- containing
# "locator" (case-insensitive) or starting with "getBy" -- rather than an
# exact list, since repos commonly wrap Playwright calls in their own
# helpers, but Playwright's own "Locator: ..." error text always names its
# *own* method regardless of which wrapper built it. Not anchored to line
# start (unlike `_PROPERTY_RE`) since these calls can appear anywhere e.g.
# inside an object-literal property's arrow function.
# The quote char is captured and back-reference ('\2) so selector
# containing the *other* quote type isn't truncated at that inner quote,
# e.g. `page.locator('[data-value="brand"]')`.
_LOCATOR_CALL_LITERAL_RE = re.compile(
    r"\b\w*(?:[Ll]ocator\w*|getBy\w+)\(\s*`([^`]*\$\{[^}]*\}[^`]*)"
)

# same call shapes, but with a template-literal argument containing exactly
# one `${...}` interpolation, e.g.
# "page.locator(`[data-test-id=\"shared-dilters-checkbox-${type}\"]`)".
_LOCATOR_CALL_TEMPLATE_RE = re.compile(
    r"\b\w*(?:[Ll]ocator\w*|getBy\w+)\(\s*`([^`]*\$\{[^}]*\}[^`]*)`"
)


def build_locator_index(source_root: Path, locator_glob: str = "e2e/locator/**/*.ts") -> Dict[str, str]:
    """Scan every `*.ts` file matching `locator_glob` under `source_root` and
    return a `{"<exportedConst>.<property>": "e2e/locator/.../file.ts:<line>"}`
    map for every property defined inside a top-level
    `export const X = { ... }` object literal.

    Best-effort uses simple brace-depth tracking rather than a full
    Typescript parser, so unusually formatted files could throw off brace
    counting -- acceptable since this only ever degrades to "no match found"
    (failing back to the spec line), never produces a wrong link.
    """
    index: Dict[str, str] = {}

    for ts_file in sorted(source_root.glob(locator_glob)):
        try:
            text = ts_file.read_text(encoding="utf-8")
        except OSError:
            continue
        relative = ts_file.relative_to(source_root).as_posix()

        current_object: Optional[str] = None
        depth = 0
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()

            if current_object is None:
                match = _EXPORT_CONST_RE.match(line)
                if match:
                    current_object = match.group(1)
                    depth = line.count("{") - line.count("}")
                continue

            if depth == 1:
                prop_match = _PROPERTY_RE.match(line)
                if prop_match:
                    key = f"{current_object}.{prop_match.group(1)}"
                    index.setdefault(key, f"{relative}:{line_no}")

            depth += line.count("{") - line.count("}")
            if depth <= 0:
                current_object = None

    return index

def resolve_locator_reference(line: str, index: Dict[str, str]) -> Optional[str]:
    """find the first `<object>.<property>` reference on `line` that matches
    a known locator definition in `index`, returning its `path:line`
    (or `None` if nothing on the line resolves)."""
    for obj_name, prop_name in _REFERENCE_RE.findall(line):
        location = index.get(f"{obj_name}.{prop_name}")
        if location:
            return location
    return None


def _template_to_pattern(template: str) -> Pattern[str]:
    """Convert a template-literal locator body (e.g. `ds-${text}_dropdown`,
    already stripped of its surrounding backticks) into a regex that matches
    any string a call with that template cound have produced at runtime --
    every `${...}` interpolation becomes a `.+` wildcard, and everything 
    else is escaped and matched literally."""
    parts = re.split(r"\$\{[^}]*\}", template)
    return re.compile("^" + ".+".join(re.escape(part) for part in parts) + "$")


def build_raw_selector_index(
    source_root: Path, locator_glob: str = "e2e/locator/**/*.ts"
) -> Tuple[Dict[str, List[str]], List[Tuple[Pattern[str], str]]]:
    """Scan every `*.ts` file matching `locator_glob` for Playwright locator
    calls (see `_LOCATOR_CALL_LITERAL_RE`/`_LOCATOR_CALL_TEMPLATE_RE`) and 
    return two lookup structures for `resolve_raw_selector`:
    
    1. `{"literal selector": ["path:lint", ...]}` --- every matching
    definintion is kept, not just the first, so tries (multiple files
    defining the same literal selector) can be disambiguated later.
    2. `[(compiled_pattern, "path:line"), ...]` for template-literal
    selectors with exactly one `${...}` interpolation -- inherently a 
    heuristic since the runtime string then depends on a parameter (see
    `_template_to_pattern`), so more than one pattern any match.

    Matches anywhere on a line, not just `export const NAME = ...`: a
    common convention is an object-literal property whose arrow function 
    calls `page.<method>(...)` directly (e.g. `filterButton: (page:Page)
    => page.getByROle('button', '...')`). A line with multiple calls (e.g.
    charined `locator(a).locator(b)`) contrinutes an entry for each.
    """
    literal_index = Dict[str, List[str]] = {}
    template_index = List[Tuple[Pattern[str], str]] = []

    for ts_file in sorted(source_root.glob(locator_glob)):
        try:
            text = ts_file.read_text(encoding="utf-8")
        except OSError:
            continue
        relative = ts_file.relative_to(source_root).as_posix()

        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            location = f"{relative}:{line_no}"

            for literal_match in _LOCATOR_CALL_LITERAL_RE.finditer(raw_line):
                literal_index.setdefault(literal_match.group(2), []).append(location)

            for template_match in _LOCATOR_CALL_TEMPLATE_RE.finditer(raw_line):
                template_index.append((_template_to_pattern(template_match.group(1)), location))

    return literal_index, template_index


# Path/classname tokens too generic to mean anything when scoring candidate
# locations against the falling spec file's name (see `_best_candidate`) --
# without these ,near every candidate would tie on e.g. ""page"/"locator"/
# "spec", the exact scenario `_best_candidate` exists to break.
_GENERIC_NAME_TOKENS = {
    "e2e", "spec", "specs", "test", "page", "pages", "locator",
    "locators", "component", "components", "index", "src", "ts", "tsx",
}


def _name_tokens(text: str) -> set:
    """Split `text` (a file path or JUnit `classname`) into lowercase word
    tokens, dropping ones too generic to be a meaningful signal (see
    `_GENERIC_NAME_TOKENS`) -- used by `_best_candidate` to score how
    plausible a candidate definition's own file corresponds to the page/ 
    feature the failing test actually exercises."""
    return{
        token
        for token in re.split(r"[^A-Za-z0-9]+", text.lower())
        if token and token not in _GENERIC_NAME_TOKENS
    }


def _best_candidate(candidates: List[str], classname: Optional[str]) -> str:
    """Pick the most plausible `path:line` among candidates that all
    matched the same runtime selector string -- happens when more than one
    page/locatpr file defines a same-shaped selector (e.g. two pages each 
    with their own `${x}-title-text` template), which the runtime string
    alone can't disambiguate
    
    Scores each candidate by how many meaningful name tokens (see
    `_name_tokens`) its file path shares with `classname` (the failing
    spec's file) --- locator files are usually named after the page/feature
    they cover, same as the spec exercisisng it (e.g. `checkout.spec.ts` is
    far more likely caused by `checkout.locator.ts` than any unrelated 
    `dashboard.locators.ts`).
    """
    if len(candidates) == 1 or not classname:
        return candidates[0]

    wanted = _name_tokens(classname)
    if not wanted:
        return candidates[0]

    best = max(candidates, key=lambda location: len(wanted & _name_tokens(location.rsplit(":",1)[0])))
    best_score = len(wanted & _name_tokens(best.rsplit(":", 1)[0]))
    return best if best_score > 0 else candidates[0]


def resolve_raw_selector(
    selector: str,
    literal_index: Dict[str, List[str]],
    template_index: List[Tuple[Pattern[str], str]],
    classname: Optional[str] = None
) -> Optional[str]:
    """Resolve a raw selector string (e.g. pulled from Playwright error's
    "Locator: locator('...')" line) to its own `path:line` definition,
    preferring an exact literal match before falling back to template 
    patterns (see `build_raw_selector_index`). WHen more than one
    definition matches, disambiguates using `classname`(the spec file the
    failing test lives in) via `_best_candidates` instead of always taking
    whichever definition happened to be scanned first."""
    literal_candidates = literal_index.get(selector)
    if literal_candidates:
        return _best_candidate(literal_candidates, classname)

    template_matches = [location for pattern, location in template_index if pattern.match(selector)]
    if template_matches: 
        return _best_candidate(template_matches, classname)
    return None