"""Strip dynamic/noisy substrings from error message so that structually
identical failures compare as equal (or near-equal) regardless of the exact
ids, timestamps or line numbers involved.

Order matters: more specific patterns must run before the generic
catch-all number pattern.
"""

import re
from typing import Optional

_PATTERNS = [
    (re.compile(r"#[0-9a-fA-F]{4,}"), "<ID>"),                                                # element ids / hashes
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b"), "<UUID>"),                          # UUIDs
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?Z?\b"), "<TIMESTAMP>"),     # ISO 8601 timestamps
    # Playwright assertion values, e.g. `Expected string: "..."` / `Received: "..."`.
    # These commonly wrap randomly generated test data (e.g. faker-generated string or
    # other per-run unique values used as fixture data) and must be masked so that otherwise
    # identical assertion failures compare equal.
    (re.compile(r'((?:expected|received)\s*(?:string|pattern|substring|array|value)?\s*:\s*)"[^"]*"', re.IGNORECASE), r'\1"<VALUE>"'),
    # Some repos prefix generated test entity names with a fixed tag plus a short random
    #suffix (e.g. "E2E <5-char alphanumeric>", from something like `` const preix = `E2E ${faker.string.alphanumeric(5)}`) ``
    #in spec code) -- mask any occurrence left outside a quoted assertion value (e.g. in unquoted log text). Harmless
    #no-op for repos that doesn't use this convention; adjust or remove the pattern if not applicable.
    (re.compile(r"\bE2E ?[A-Za-z0-9]{5}\b"), "E2E <RANDID>"),
    (re.compile(r":\d+:\d+\b"), ":<LINE>:<COL>"),                                             # file.ts:123:45
    (re.compile(r"\b\d+\b"), "<NUM>")                                                         # remaining standalone number
    (re.compile(r"\s+"), " "),                                                                # collapse whitespace
]


def normalize_error(message:str) -> str:
    """Return a normalized version of `message` suitable for similarity comparison."""
    normalized = message.strip()
    for pattern, replacement in _PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized.strip().lower()


#Matches the selector passed to a Playwright `locator('...')` call, e.g. the `[data-value="brand"]`
#in `waiting for locator('[data-value="brand"]')` or the `h1.text-headin` in `Locator: locator('h1.text.headin')`.
_LOCATOR_CALL_RE = re.compile(r"locator\(\s*(['\"])(.*?)\1\s*\)")
_LOCATOR_NUM_RE = re.compile(r"\d+")


def extract_locator(message:str) -> Optional[str]:
    """Return the (normalized) selector reference by the first `locator('...')` call found in `message`, or `None`
    if it doesn't reference one.
    
    Two failures can share almost all of their surrounding wording -- timeout phrasing, "Call log: "/"Locator:" 
    labels, etc. -- while actually pointing at completely different elements (e.g. a `click` timeout on a dropdown
    option vs. a `toHaveText` timeout on an unrelated page heading). That shared biolerplate can be enough for a whole-message 
    fuzzy ratio clear the clustering threshold even though the root cause differs, since the differing selector may only be
    a small fraction of a long message. Extracting the selector lets clustering require it to match before two failures
    are folded together, independent of the rest of the message
    """
    match = _LOCATOR_CALL_RE.search(message)
    if not match:
        return None
    #Digits inside the selector (e.g. `tr:nth-child(3)`) are call site specific, not part of the root cause, so 
    #collapse them like the rest of `normalize_error` does.
    selector = _LOCATOR_NUM_RE.sub("<NUM>", match.group(2))
    return selector.strip().lower()


def extract_raw_locator(message:str) -> Optional[str]:
    """Return the selector referenced by the first `locator('...')` call found in `message`, exactly as written 
    (original case, original digits), or `None` if it doesn't reference one.
    
    Unlike `extract_locator`, this doesn't normalize digits or case -- it exists purely for human-readable display
    (e.g. "possibly related clusters" note in `report.py`) , wherr=e showing the normalized `<NUM>` placeholder 
    (e.g. `h<num>.text-headin`) would be more confusing than showing the real selector a developer can search their
    codebase for.
    """
    match = _LOCATOR_CALL_RE.search(message)
    return match.group(2).strip() if match else None