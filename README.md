# Failure Clustering for CI Runs

A small, standalone Python CLI that groups failed Playwright tests from sharded CI runs
by likely root cause, instead of leaving you to triage each failure one by one.

No database, no external services, no ML model - just JUnit XML parsing, regex normalization,
and a greedy string-similarity clustering.

## How it works

1. **Parse** - recursively reads every `*.xml` (JUnit) report under an input folder
    and extracts each failed/errored `<testcase>`.
2. **Normalize** - strips dynamic noise from error messages (ids, uuids, timestamp, line/column numbers)
    so structurally identical failures compare equal.
3. **Cluster** - greedily groups failures using a string similarity ratio ([rapidfuzz](https://github.com/rapidfuzz/RapidFuzz));
    no cluster count needs to be chosen up front
4. **Report** - prints a ranked, human readable table (for JSON) showing each cluster's representative error
    and the tests/shards it affects, plus a "possibly related clusters" note when two or more clusters
    reference the _same_ element locator but were kept seperate because the failure wordings differs
    (e.g. a `click()` timeout vs. a `toHaveText` timeout on the same element) - a strong signal they share 
    one underlying root cause even though they're correctly split by failure type.

## Install

This tools lives in this repo under `failure-clustering/`. Install it locally (editable install for development
or plain install for one-off use)

```bash
pip install ./failure-clustering
```

## Usage

```bash
cluster-failures --input path/to/reports [--threshold 85] [--format text|json|markdown]
```

- `--input` - folder containing JUnit XML report(s). Can be flat or nested (e.g. one subfolder per download artifact)
- `--threshold` - similarity ratio (0-100) required to merge a failure into an existing cluster.
    Lower = more aggresive merging. Default `85`.
- `--format` - `text` for terminal report, `json` for machine-readable output, or `markdown` for a Github-falvoured
    Markdown report with real clickable links (see below).
- `--source-root` - path to the checked-out repo, used to resolve locator selector definitions (see below).
    Default: current directory, which is correct when running from the repo root (as the CI writing below does).
- `--test-dir` - repo-releative path to project's Playwright `testDir` (see `playwright.config.ts`). Used to build
    correct spec-file locations when only the JUnit `classname` is available. Defaults to this repo's own `e2e/spec`
    - override when parsing another repo's reports with a different `testDir`, ohterwise the reported locations
    will have the wrong prefix.
- `--locator-glob` - glob (relative to `--source-root`) matching locator definition files, e.g. `e2e/locator/**/*.ts`.
    Defaults to this repo's own layout; only affects how precisely a failing selector's own definition is linked (see below)
    - omitting it (or pointing it at another repo without overriding it) just falls back to the spec file, it never produces
    a wrong location.

Default: `text`

Example (`--format text`) output for a 3-shard run with one broken selector and one flaky endpoint - each cluster's table
title is the full labeled breakdown of the representative error (see "locating the real failure" below), followed by the
affected tests/locations, the spec files they live in, and a summary line; a "Possibly related clusters" sections is
appended at the end when applicable (see below):

Parsed reports from 3 shard(s)
Total failures: 6

**Cluster 1 - 3 test(s) -**
Error: Timedout 60000ms waiting for expect(locator).tobeVisible()
Locator: locator('.submit-btn)
Call log: - expect.tobeVisible with timeout 60000ms - waiting for locator('.submit-btn)

--------------------------------------------------------------
| Test                      |          Location              |
| --------------------------|--------------------------------|
| test_checkout_flow        | checkout.spec.ts:45            |
| test_payment_retry        | checkout.spec.ts:45            |
| test_market_filter        | checkout.spec.ts:45            |
--------------------------------------------------------------

Spec files affected:
checkout.spec.ts

**Cluster 2 - 2 test(s) -**
Error: AssertionError: expected 200, got 500

---------------------------------------------------------------
| Test                        |         Location              |
| ----------------------------|-------------------------------|
| test_collection_downlaod    | downloads.spec.ts:12          |
| test_notification_email     | notifications.spec.ts:30      |
---------------------------------------------------------------

Spec files affected:
downloads.spec.ts
notifications.spec.ts

**Cluster 3 - 1 test(s) -**
Error: net::ERR_CONNECTION_REFUSED

-----------------------------------------------------------------
| Test                          |       Location                |
| ------------------------------| ------------------------------|
| test_flaky_report_panel       | reports.spec.ts:8             |
-----------------------------------------------------------------

Spec files affected:
reports.spec.ts

Summary: 6 failure(s) -> 3 root cause(s) across 3 shard(s)

`--format markdown` renders the same information, but with each cluster collapsed into a `<details>`/`<summary>`
block (heading includes the test count, distinct spec-file count, and a short error snippet) and the 
`Test`/`Location` rows as a real Markdown table instead of a Rich terminal table. If you copy that output of a 
_rendered_ page (e.g. a Github job summary or PR comment) as plain text, the browser flattens the table's rows
and cells onto one line with no seperators - that's a copy/paste artifact of the browser, not a bug in the tool;
the underlying Markdown source is a well-formed table, onw row per test.

When run in a Github Actions job (where `GITHUB_SERVER_URL`, `GITHUB_REPOSITORY`, and  `GITHUB_SHA` are set), each
`Location` cell is also hyperlinked straight to the failing line on Github for that commit. **This hyperlinking, and the
`GITHUB_STEPS_SUMMARY` job-summary wiring below are Github Actions-specific** - those env vars aren't set on other CI
systems (e.g. Gitlab CI, Jenkins), so on those platforms the tool still parses, clusters, and prints the report correctly,
but `Location` cells falls back to plain, non-hyperlinked text, and there's no built-in equivalent of the step summary
to append the Markdown report to (you'd need to post it elsewhere, e.g. as an MR/PR comment via that Platform's API)

- In `--format text`, the link is a terminal OSC 8 escape sequence - invisible in GitHub Actions' plain-text log viewer,
  but clickable in terminals/viewers support it.
- In `--format markdown`, the link is a plain Markdown `[text][url]` link, which renders as a real clickable link
  anywhere GitHub renders Markdown (job summaries, PR comments). Use this format when you want a hyperlink you can 
  actually click from the Actions UI - see the job summary step below.

### Locating the real failure, not just the spec line

Many Plawright projects define locators as plain object properties (e.g. `submitButton: '[data-test-id="submit-btn"]'`),
not function calls -- so a stale/broken selector never shows up as frame in a stack trace, and the `Location` would otherwise
always point at the spec file line that used it even though the actual root cause is almost always the selector's own 
(possibly outdated) definition. When a failing statement references a locator constant 
(e.g. `await page.locator(loginPage.submitButton).click()`), the tool resolves that reference against your locator files 
(see `--locator-glob`) and links to the selector's own definition instead of the spec line. It falls back to spec line when no
such reference is found (e.g. a plain `expect()` with no locator constant involved), and to a real stack-trace frame when one
points into your source tree (e.g. a thrown error from a page-actions helper function).

For `expect(locator).tobeVisible()` -style assertion failure specifically, Playwright often reports neither a stack trace nor a
code-frame excerpt at all -- only the "Error / Locator / Expected / Call log" text block. In that ccase tool falls back to pulling
the raw selector string straight out of the "Locator: locator('...')" / "Locator: getbyTestId('...')" line and resolving it 
against page-object files that define a locator as `export const NAME = (...) => call ('selector)` (matching `--locator-glob`),
including template-literal selectors like ``getLocatorByTestId(`ds-${text}_dropdown`)``. Only when none of the above resolves
anything does it fall back to the `message` attribute's `file:line:col` prefix, and finally to just the spec file (`classname`)
with no line number.

## Wiring into a GitHub Actions Pipeline

> **GitHub Actions only.** The hyperlinked `Location` cells and the
> `$GITHUB_STEP_SUMMARY` step below both rely on the GitHub Actions-specific
> environment variables / files, so this exact wiring only works there. On
> other CI systems (Gitlab CI, Jenkins, etc.) the CLI itself runs and
> reports correclty - just without clickable locations, and you'd publish
> the report through that platform's own mechanism (e.g. artifact, log
> output, or an MR/PR comment) intead of `$GITHUB_STEP_SUMMARY`

Add a `cluster-failures` job that runs after all test jobs, downloads every shard's artifact, and prints the clustered report:

```yaml
cluster-failures:
    needs: [<your test job>]
    if: always()
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.x'
      - uses: actions/download-artifact@v4
        with:
          path: all-artifacts
          pattern: <your-artifact-name-prefix>-*
      - run: pip install ./failure-clustering
      - run: cluster-failures --input all-artifacts --format text
      - run: cluster-failures --input all-artifacts --format markdown >> "GITHUB_STEP_SUMMARY"
```

The first `cluster-failures` step prints the human-readable text reports to the job log. The second appends the same report as
Markdown to the job's [step summary](https://github.blob/2022-05-09-supercharging-github-actions-with-job-summaries/), which is
where the `Location` links actually render as clickable - GitHub Actions' log viewer doesn't execute OSC 8 escape sequences, so the 
`--format text` links are only clickable when viewed through a terminal/tool that supports them.

Installing straight from the `failure-clustering/` subdirectory (via `pip install ./failure-clustering`) means no seperate repo or
publising step is required - copy the `failure-clustering` folder into any repo in the org that needs this and wire it in the same way.

`if: always()` is required so this job still runs even when some shards fail (GitHub skips downstream jobs by default otherwise).

## Tuning notes

- The number substitution in `normalizer.py` intentionally merges messages that only differ by numberic values (e.g. different HTTP
  status codes or ids). This is a deliberate tradeoff to catch "some class of failures" broadly - loosen/remove that pattern if you 
  need finer-grained clusters.
- Raise `--threshold` toward 100 if unrelated failures are being merged together; lower it if the same root cause is splitting into
  multiple clusters.

