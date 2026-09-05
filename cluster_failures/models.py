"""Data structures sharded across the failure clustering tool."""

from dataclasses import dataclass, field
from typing import List


@dataclass
class Failure:
    """A single failed test case parsed from a JUnit XML report."""

    test_name:str
    shard:str
    message:str
    location:str
    spec_file:str


@dataclass
class Cluster:
    """A group of failures believed to share the same root cause."""

    representative_message:str
    failures: List[Failure] =field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.failures)
