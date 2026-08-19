"""Prototype runner: inventory a workspace, or crawl one source and report.

    python -m onec_hbk_bsl.indexer.metacrawl inventory <workspace> [--validate N]
    python -m onec_hbk_bsl.indexer.metacrawl crawl <source-root> [--scope Catalogs]

Nothing is written anywhere: the crawl reports what it reached, and the remainder
— files on disk nobody reached — is the check that needs no reference at all.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from importlib import resources
from pathlib import Path

from .crawl import Crawl
from .inventory import scan_workspace
from .parsers import registry_for
from .rules import RuleError, available_kinds, load_rules


def _meta_root() -> Path:
    # platform_meta is data, not a package: resolve through the package that holds it
    return Path(str(resources.files("onec_hbk_bsl.data"))) / "platform_meta"


def _run_inventory(args: argparse.Namespace) -> int:
    schemas_root = _meta_root()
    started = time.perf_counter()
    sources = scan_workspace(
        Path(args.workspace).resolve(),
        schemas_root=schemas_root,
        validate_limit=args.validate,
    )
    elapsed = time.perf_counter() - started

    header = f"{'kind':10} {'version':8} {'files':>7} {'schema':>7} {'valid':>6} {'warn':>5} {'bad':>5}  name / root"
    print(header)
    print("-" * len(header))
    for source in sources:
        name = source.name or Path(source.root_path).name
        print(
            f"{source.kind:10} {source.meta_version or '-':8} {source.meta_files_count:>7} "
            f"{'yes' if source.meta_schema_exist else 'no':>7} "
            f"{source.valid_meta_files_count:>6} {source.warning_meta_files_count:>5} "
            f"{source.invalid_meta_files_count:>5}  {name}"
        )
    counts = Counter(s.kind for s in sources)
    print(f"\nsources: {len(sources)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"elapsed: {elapsed:.1f} s")
    return 0


def _run_crawl(args: argparse.Namespace) -> int:
    root = Path(args.source).resolve()
    descriptor = root / "Configuration.xml"
    if not descriptor.is_file():
        print(f"no Configuration.xml in {root}", file=sys.stderr)
        return 2

    rules_dir = _meta_root() / args.generation / "Rules"
    kinds = available_kinds(rules_dir)
    try:
        rules = load_rules(rules_dir, kinds, registry_for(args.generation))
    except RuleError as exc:
        print(f"rules: {exc}", file=sys.stderr)
        return 2
    print(f"rules loaded: {len(kinds)} kinds")

    started = time.perf_counter()
    crawl = Crawl(rules, last_known_version=args.generation)
    crawl.parse_file(descriptor, owner=None)
    elapsed = time.perf_counter() - started
    result = crawl.result

    by_kind = Counter(n.kind for n in result.nodes)
    by_member = Counter(m.member_kind for m in result.members)

    print(
        f"\nnodes: {len(result.nodes)}  files visited: {len(result.visited)}  "
        f"elapsed: {elapsed:.1f} s"
    )
    print("nodes by kind:  " + "  ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
    print(
        "members:        "
        + "  ".join(f"{k}={v}" for k, v in sorted(by_member.items()))
        + f"   total={len(result.members)}"
    )

    scope = root / args.scope
    if scope.is_dir():
        on_disk = {p for p in scope.rglob("*") if p.is_file()}
        remainder = sorted(on_disk - result.visited)
        print(f"\nremainder under {args.scope}/: {len(remainder)} of {len(on_disk)}")
        for suffix, count in Counter(p.suffix.lower() for p in remainder).most_common(10):
            print(f"    {suffix or '(none)':10} {count}")
        for path in remainder[:10]:
            print(f"    e.g. {path.relative_to(root)}")

    print(f"\nunmanaged descriptors: {len(result.unmanaged)}")
    print(f"declared without descriptor: {len(result.missing_descriptors)}")
    if result.anomalies:
        print(f"anomalies: {len(result.anomalies)}")
        for item in result.anomalies[:5]:
            print(f"    {item}")
    if result.missing_rules:
        print(f"kinds without rules: {len(result.missing_rules)}")
        for kind, count in result.missing_rules.most_common(8):
            print(f"    {kind:34} {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="metacrawl")
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="describe every source in a workspace")
    inventory.add_argument("workspace")
    inventory.add_argument(
        "--validate", type=int, default=0, help="validate up to N files per format per source"
    )
    inventory.set_defaults(func=_run_inventory)

    crawl = sub.add_parser("crawl", help="crawl one source by rules")
    crawl.add_argument("source")
    crawl.add_argument("--generation", default="2.20")
    crawl.add_argument("--scope", default="Catalogs", help="subtree the remainder is computed for")
    crawl.set_defaults(func=_run_crawl)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
