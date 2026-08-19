"""Rules-driven metadata crawl.

The crawl walks a 1C configuration export from ``Configuration.xml`` down, taking
every decision from generated rules (``data/platform_meta/<generation>/Rules``)
rather than from hardcoded folder tables. Each file states its own kind, so the
traversal never needs to know what level it is on.

The crawl does not read XSD schemas: validation lives in :mod:`inventory`, which
is a separate mechanism exposed as a tool.
"""

from __future__ import annotations

from .crawl import Crawl, CrawlResult, Member, Node
from .inventory import Source, SourceKind, scan_workspace
from .parsers import registry_for
from .rules import KindRules, RuleEntry, RuleError, RuleSet, load_rules

__all__ = [
    "Crawl",
    "CrawlResult",
    "KindRules",
    "Member",
    "Node",
    "RuleEntry",
    "RuleError",
    "RuleSet",
    "Source",
    "SourceKind",
    "load_rules",
    "registry_for",
    "scan_workspace",
]
