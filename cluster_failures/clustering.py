"""Greedy similarity-based clustering of test failures.

NO ML mode needed: each failure's normalized message is compared against
the representative message of every existing cluster using a string
similarity ratio. If the best match clears the threshold, the failure jobs
that cluster; otherwise it starts a new one.
"""

from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from .models import Cluster, Failure
from .normalizer import extract_locator, extract_raw_locator, normalize_error


def cluster_failures(failures: List[Failure], threshold: float = 85.0) -> List[Cluster]:
    """Group `failures` into clusters of (probably) shared root cause.
    
    `threshold` is the minimum rapidfuzz similarity ratio (0-100) required
    for a failure to be folded into an existing cluster. Clusters are returned sorted by size,
    largest (most impactful) first.
    """
    clusters: List[Cluster] = []
    normalized_reps: List[str] = []
    rep_locators: List[Optional[str]] = []

    for failure in failures:
        normalized = normalize_error(failure.message)
        locator = extract_locator(failure.message)

        best_index = -1
        best_score = 0.0
        for index,rep in enumerate(normalized_reps):
            # If both this failure and the candidate cluster reference a 
            # specific element, require the selectors to match. Otherwise
            # failures on unrelated elements (e.g. a dropdown options vs. a
            # page heading) can end up sharing a cluster purely because the
            # surrounding timeout/call-log wording overlaps -- see
            # `extract_locator`'s docstring. Messages with no extractable
            # locator (or a cluster rep without one) fall back to the plain
            # fuzzy comparison below.
            if locator is not None and rep_locators[index] is not None and locator != rep_locators[index]:
                continue
            score = fuzz.ratio(normalized, rep)
            if score > best_score:
                best_score = score
                best_index = index

        if best_index != -1 and best_score >= threshold:
            clusters[best_index].failures.append(failure)
        else:
            clusters.append(Cluster(representative_message=failure.message, failures=[failure]))
            normalized_reps.append(normalized)
            rep_locators.append(locator)

    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters


def find_related_cluster_groups(clusters: List[Cluster]) -> List[Tuple[str, List[int]]]:
    """Find sets of *distinct* clusters that all reference the same element
    locator (see `extract_locator`) despite having kept seperate by 
    `cluster_failures`.
    
    This deliberately catches a blind spot of the locator gate in 
    `cluster_failures`: it only ever *prevents* two failures on different
    elements from merging -- it can't *force* two failures on the very same
    element together when their surrounding wording differs enough (e.g. a
    `.click()` timeout vs. a `toHaveText` timeout on the same heading) to
    miss the fuzzy-ratio threshold. Two clusters sharing a locator are a 
    strong signal they're actually the same underlying root cause (e.g. "This
    element doesn't render/stabilize in time") surfacing as two different
    Playwright API failures, so callers can surface that as a hint even 
    though the clusters themselves are correctly kept sepeate by failure
    *type*.

    Returns a list of `(locator, cluster_indices)` tuples -- `cluster_indices`
    are 1-based positions into `clusters`, matching the numbering used in the
    reports (`report.print_report`/`report.to_markdown`) -- for every locator
    referenced by two or more clusters, sorted by total failure count across
    the group (largest/most impactful first). Clusters with no extractable
    locator, and locators reference the only one cluster, are omitted.

    `locator` is the raw, un-normalized selector text (see
    `extract_raw_locator`) from whichever cluster in the group was scanned
    first, kept human-readable for display; grouping itself is still keyed
    by the normalized form (see `extract_locator`) so that two clusters
    differing only in call-site-specific digits (e.g. `tr:nth-child(2)` vs.
    `tr:nth-child(5)`) are still recognized as the same underlying locator.
    """
    groups_by_key: Dict[str, Tuple[str, List[int]]] = {}
    for index, cluster in enumerate(clusters, start=1):
        key = extract_locator(cluster.representative_message)
        if key is None:
            continue
        display = extract_raw_locator(cluster.representative_message) or key
        _, indices = groups_by_key.setdefault(key, (display, []))
        indices.append(index)

    groups = [(display, indices) for display, indices in groups_by_key.values() if len(indices) > 1]
    groups.sort(key=lambda group: sum(clusters[i-1].size for i in group[1]), reverse=True)
    return groups
