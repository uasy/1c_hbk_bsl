"""Rule loading.

Rules are data: one file per kind, keyed by the kind the file itself declares.
A rule entry says what to do with one key — which parser, where the children
live — and nothing else; there are no conditions in rules.

Parser names are resolved to functions **at load time** and an unknown name
aborts the load. A name that silently failed to resolve would behave like a
wrong path — an empty result with no error — and would also break go-to-definition
and find-usages on the rules themselves.

Schemas are not read here. The crawl never opens an XSD; checking rule keys
against the schema of their generation belongs to the inventory tool.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SECTIONS: tuple[str, ...] = ("children_inline", "children_external", "node_files")


class RuleError(Exception):
    """Rules are unusable: fail at load, never mid-crawl."""


@dataclass(frozen=True, slots=True)
class RuleEntry:
    """One key of one section.

    ``key`` is either an ``ChildObjects/<Tag>`` selector or a path relative to the
    node root. Paths carry no wildcards: a key is an exact file path, or a folder
    path meaning "everything inside belongs to this node".

    ``children_inline`` holds the keys of the member's own ``<ChildObjects>`` —
    a tabular section's attributes, an operation's parameters, a URL template's
    methods. Such a member has no rule file of its own and cannot have one:
    nothing in the export declares itself a tabular section, the crawl arrives
    there from the owner.
    """

    key: str
    parser_name: str | None
    parser: Callable[..., None] | None
    folder: str | None = None
    member_kind: str | None = None
    #: what an edge from this key means, when the key itself cannot say. The kind
    #: is normally the key's last segment — a token of the format. Where the
    #: addressing element is named ``Value`` that segment says nothing, and the
    #: decision names the token that does: ``UserVisible``, ``Edit``, ``Use``.
    ref_kind: str | None = None
    type_name: str | None = None
    children_inline: Mapping[str, RuleEntry] = field(default_factory=dict)
    #: inside the matched element the enumeration starts over: ``(through, from)``
    #: — where to descend, and which part of the key list applies again. A form
    #: group holds its own items, a subsystem its own subsystems. Two values and
    #: not a flag: re-applying every key would reach a button both as
    #: ``AutoCommandBar/ChildItems/Button`` from the parent and as
    #: ``ChildItems/Button`` from inside the bar. Both may be empty — a subsystem
    #: contains a subsystem by the very key that found it.
    recursive: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class KindRules:
    kind: str
    schema_version: str
    children_inline: Mapping[str, RuleEntry] = field(default_factory=dict)
    children_external: Mapping[str, RuleEntry] = field(default_factory=dict)
    node_files: Mapping[str, RuleEntry] = field(default_factory=dict)

    def section(self, name: str) -> Mapping[str, RuleEntry]:
        return getattr(self, name)


@dataclass(frozen=True, slots=True)
class RuleSet:
    """Rules of one generation, by kind."""

    schema_version: str
    kinds: Mapping[str, KindRules]

    def for_kind(self, kind: str) -> KindRules | None:
        return self.kinds.get(kind)


def _entry_of(
    key: str,
    raw: dict,
    where: str,
    registry: Mapping[str, Callable[..., None]],
    file_name: str,
) -> RuleEntry:
    """One entry, with the keys of the member's own ``<ChildObjects>`` below it."""
    name = raw.get("parser")
    if name is not None and name not in registry:
        raise RuleError(f"{file_name}, {where}: unknown parser {name!r}")
    nested = raw.get("children_inline") or {}
    return RuleEntry(
        key=key,
        parser_name=name,
        parser=registry[name] if name else None,
        folder=raw.get("folder"),
        member_kind=raw.get("member_kind"),
        ref_kind=raw.get("ref_kind"),
        type_name=raw.get("type"),
        children_inline={
            sub: _entry_of(sub, sub_raw, f"{where}/{sub}", registry, file_name)
            for sub, sub_raw in nested.items()
        },
        recursive=_recursion_of(raw.get("recursive"), where, file_name),
    )


def _recursion_of(raw, where: str, file_name: str) -> tuple[str, str] | None:
    """``{"through": …, "from": …}`` -> пара, иначе None."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - {"through", "from"}:
        raise RuleError(f"{file_name}, {where}: recursive ждёт through и from, получено {raw!r}")
    return raw.get("through", ""), raw.get("from", "")


def load_rules(
    rules_dir: Path,
    kinds: list[str],
    registry: Mapping[str, Callable[..., None]],
) -> RuleSet:
    """Load ``kinds`` from ``rules_dir``, resolving parser names via ``registry``."""
    loaded: dict[str, KindRules] = {}
    version = ""
    for kind in kinds:
        path = rules_dir / f"{kind}.json"
        if not path.is_file():
            raise RuleError(f"no rule file for kind {kind!r}: {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        declared = doc.get("kind")
        if declared != kind:
            raise RuleError(f"{path.name}: file declares kind {declared!r}, expected {kind!r}")
        version = version or doc.get("$schema_version", "")

        sections: dict[str, dict[str, RuleEntry]] = {name: {} for name in SECTIONS}
        for section in SECTIONS:
            for key, raw in doc.get(section, {}).items():
                sections[section][key] = _entry_of(
                    key, raw, f"{section}/{key}", registry, path.name
                )
        loaded[kind] = KindRules(
            kind=kind,
            schema_version=doc.get("$schema_version", ""),
            children_inline=sections["children_inline"],
            children_external=sections["children_external"],
            node_files=sections["node_files"],
        )
    return RuleSet(schema_version=version, kinds=loaded)


def available_kinds(rules_dir: Path) -> list[str]:
    """Every kind that has a rule file.

    There is deliberately no "closure from a seed kind" here: the reachable set
    is not derivable from rule keys. ``children_external`` names the child kind in
    its key, but ``node_files`` does not — the key ``Ext/Form.xml`` gives no hint
    that the file behind it declares ``logform.Form``. A parser may also be
    terminal, so the absence of rules for a kind is not evidence either way.
    Scope is therefore a property of the run, not something rules can compute.
    """
    return sorted(p.stem for p in rules_dir.glob("*.json"))
