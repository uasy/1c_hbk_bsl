"""The traversal.

One function walks every level. ``parse_file`` does not know whether it is
looking at a configuration, an object or a form: the file states its kind, the
rules of that kind say what to do, and children are found by listing the folder
the rule names — never by assembling a path from a declared name.

Nothing is written to a database here. The crawl returns what it found and what
it reached, so the prototype can subtract the reached files from the files on
disk: what is left over is what no rule accounts for.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .parsers import ParseContext
from .rules import RuleSet
from .xml import (
    child_text,
    declared_kind,
    declared_version,
    elements,
    local_name,
    parse,
    properties_of,
)


@dataclass(slots=True)
class Node:
    """A node of the metadata tree."""

    kind: str
    name: str
    identity: str
    parent: str | None
    file_path: str | None
    node_root: str | None
    version: str
    declared: bool


@dataclass(slots=True)
class Member:
    """A child that lives entirely inside its owner's XML and owns no files."""

    owner: str
    member_kind: str
    name: str


@dataclass(slots=True)
class Ref:
    """An address the export writes down: who points at what, and as what.

    ``ref_kind`` is the last segment of the key that found it — a token of the
    format (``MethodName``, ``DefaultForm``), never a slug of our own, for the
    same reason member tags are: it has to be comparable with the strings the
    platform itself writes.
    """

    owner: str
    ref_kind: str
    target: str


@dataclass(slots=True)
class CrawlResult:
    nodes: list[Node] = field(default_factory=list)
    members: list[Member] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    visited: set[Path] = field(default_factory=set)
    #: kinds a file declared for which no rules were loaded
    missing_rules: Counter[str] = field(default_factory=Counter)
    #: descriptors found in a folder that the owner's composition does not declare
    unmanaged: list[str] = field(default_factory=list)
    #: names declared in the composition with no descriptor on disk
    missing_descriptors: list[str] = field(default_factory=list)
    #: anything the traversal could not make sense of
    anomalies: list[str] = field(default_factory=list)

    def nodes_of(self, kind: str) -> list[Node]:
        return [n for n in self.nodes if n.kind == kind]


class Crawl:
    """Rules-driven traversal of one source."""

    def __init__(self, rules: RuleSet, *, last_known_version: str = "") -> None:
        self.rules = rules
        self.last_known_version = last_known_version or rules.schema_version
        self.result = CrawlResult()

    # ------------------------------------------------------------------ state

    def visit(self, path: Path) -> None:
        """Mark a file as reached. The remainder is what nobody reached."""
        self.result.visited.add(path)

    def add_member(self, owner: Node, member_kind: str, name: str) -> None:
        self.result.members.append(Member(owner=owner.identity, member_kind=member_kind, name=name))

    def add_ref(self, owner: Node, ref_kind: str, target: str) -> None:
        self.result.refs.append(Ref(owner=owner.identity, ref_kind=ref_kind, target=target))

    def enter_node(
        self,
        *,
        kind: str,
        name: str,
        owner: Node | None,
        file_path: Path | None,
        node_root: Path | None,
        version: str,
        declared: bool,
    ) -> Node:
        parent = owner.identity if owner else None
        identity = f"{parent}/{kind}:{name}" if parent else f"{kind}:{name}"
        node = Node(
            kind=kind,
            name=name,
            identity=identity,
            parent=parent,
            file_path=str(file_path) if file_path else None,
            node_root=str(node_root) if node_root else None,
            version=version,
            declared=declared,
        )
        self.result.nodes.append(node)
        if node_root is not None:
            self._walk_node_files(node, node_root, kind)
        return node

    # ------------------------------------------------------------------ crawl

    def parse_file(
        self,
        path: Path,
        *,
        owner: Node | None = None,
        owner_version: str | None = None,
        declared: bool = True,
    ) -> Node | None:
        """Parse one descriptor. Levels are not distinguished."""
        self.visit(path)
        try:
            tree = parse(path)
        except etree.XMLSyntaxError as exc:
            self.result.anomalies.append(f"{path}: unreadable XML ({exc})")
            return None
        root = tree.getroot()

        kind = declared_kind(root)
        if not kind:
            self.result.anomalies.append(f"{path}: root declares no kind")
            return None

        # own version, else inherited from the owning descriptor, else last known
        version = declared_version(root) or owner_version or self.last_known_version

        # A kind without rules is still a node: the file exists and says what it
        # is. Rules answer "what to do with its children", not "does it exist" —
        # and the generator emits no rule file for a kind that has no children,
        # no members and no node files, so "no rules" is not evidence of a gap.
        kind_rules = self.rules.for_kind(kind)
        if kind_rules is None:
            self.result.missing_rules[kind] += 1

        body = self._body_of(root)
        name = child_text(properties_of(body), "Name") or path.stem
        node_root = self._node_root(path, kind)

        node = Node(
            kind=kind,
            name=name,
            identity=(f"{owner.identity}/{kind}:{name}" if owner else f"{kind}:{name}"),
            parent=owner.identity if owner else None,
            file_path=str(path),
            node_root=str(node_root),
            version=version,
            declared=declared,
        )
        self.result.nodes.append(node)
        if kind_rules is None:
            return node

        # тело описателя — начало отсчёта для ключей-путей обеих секций элементов
        self._walk_inline(node, kind_rules, body)
        self._walk_external(node, kind_rules, body, node_root, version)
        self._walk_node_files(node, node_root, kind)
        return node

    # ------------------------------------------------------------------ parts

    @staticmethod
    def _body_of(root):
        """The element carrying Properties: MetaDataObject wraps it, others are it."""
        if local_name(root) == "MetaDataObject":
            kids = list(elements(root))
            return kids[0] if len(kids) == 1 else root
        return root

    @staticmethod
    def _node_root(path: Path, kind: str) -> Path:
        """Node root is the descriptor minus ``.xml``.

        One exception: a configuration's descriptor lies *inside* its root, not
        beside it — there is no ``Configuration/`` directory at all.
        """
        if kind == "Configuration":
            return path.parent
        return path.with_suffix("")

    @staticmethod
    def _resolve(anchor, key: str):
        """Элементы по ключу-пути, отсчитанному от ``anchor``.

        Ключ — путь по именам элементов: ``ChildObjects/Attribute``,
        ``Properties/MethodName``, ``Attributes/Attribute`` у формы. Обход не
        знает, какой первый сегмент «правильный», — где искать, сказано в самом
        ключе, и потому одинаково достижимо всё, что объявлено внутри файла.
        """
        current = [anchor]
        for segment in key.split("/"):
            current = [c for el in current for c in elements(el) if local_name(c) == segment]
            if not current:
                return ()
        return current

    def _walk_inline(self, node: Node, kind_rules, anchor) -> None:
        """Whatever the rules address inside this same file."""
        self._walk_inline_entries(node, kind_rules.children_inline, anchor)

    def _walk_inline_entries(self, node: Node, entries, anchor, root=None) -> None:
        """Entries against the element they are counted from.

        A member may hold keys of its own — a tabular section's attributes, an
        operation's parameters. They live nested in the same entry, and the level
        below is this same walk counted from the member. Descent does not depend
        on the owner's parser: every key, at any depth, carries its own decision.

        Where the structure contains itself, the entry names the anchor its keys
        start over from, and only that subtree applies again. A prefix, not a
        flag: re-applying every key would reach a button both as
        ``AutoCommandBar/ChildItems/Button`` from the parent and as
        ``ChildItems/Button`` from inside the bar, and the same element would be
        extracted twice. Written-out nested keys win over the anchor — they say
        which level a key belongs to, and the anchor cannot.
        """
        if anchor is None:
            return
        root = entries if root is None else root
        for key, entry in entries.items():
            for target in self._resolve(anchor, key):
                if entry.parser is not None:
                    entry.parser(ParseContext(crawl=self, owner=node, entry=entry, element=target))
                if entry.children_inline:
                    self._walk_inline_entries(node, entry.children_inline, target, root)
                elif entry.recursive is not None:
                    self._walk_inline_entries(node, self._resumed(root, entry), target, root)

    @staticmethod
    def _resumed(root, entry) -> dict:
        """The keys that apply again inside a recursive element, re-based on it.

        Keys are paths from the file body, so they cannot be re-applied as they
        stand: a button group sits two segments deep, and the keys that resume
        inside it start one segment lower. The entry says both — where to
        descend (``through``) and which part of the list resumes (``from``) —
        and each key is rewritten as the descent plus the key's own tail. The
        entries themselves are untouched: a decision belongs to a key, and the
        key is the same one, met one level deeper.
        """
        through, anchor = entry.recursive
        prefix = f"{anchor}/" if anchor else ""
        out = {}
        for key, sub in root.items():
            if anchor and key == anchor:
                tail = ""
            elif key.startswith(prefix):
                tail = key[len(prefix) :]
            else:
                continue
            path = "/".join(p for p in (through, tail) if p)
            if path:
                out[path] = sub
        return out

    def _walk_external(self, node: Node, kind_rules, anchor, node_root: Path, version: str) -> None:
        """Children with their own descriptor: list the folder, not the names.

        Declared names are read only to tell a declared child from an undeclared
        one — the path itself is never assembled from a name.
        """
        for key, entry in kind_rules.children_external.items():
            names = {text for el in self._resolve(anchor, key) if (text := (el.text or "").strip())}
            folder = node_root / entry.folder if entry.folder else None
            found: set[str] = set()
            if folder is not None and folder.is_dir():
                for descriptor in sorted(folder.glob("*.xml")):
                    found.add(descriptor.stem)
                    is_declared = descriptor.stem in names
                    if not is_declared:
                        self.result.unmanaged.append(str(descriptor))
                    self.parse_file(
                        descriptor, owner=node, owner_version=version, declared=is_declared
                    )
            for missing in sorted(names - found):
                self.result.missing_descriptors.append(f"{node.file_path}: {key} {missing}")

    def _walk_node_files(self, node: Node, node_root: Path, kind: str) -> None:
        """Files in the node root that nobody declared.

        A key is an exact path or a folder; a folder key means everything inside
        belongs to this node, which is how directories named by configuration
        data are addressed without wildcards.
        """
        kind_rules = self.rules.for_kind(kind)
        if kind_rules is None:
            return
        for key, entry in kind_rules.node_files.items():
            target = node_root / key
            if target.is_dir():
                for item in target.rglob("*"):
                    if item.is_file():
                        self.visit(item)
                continue
            if not target.is_file():
                continue
            self.visit(target)
            if entry.parser is not None:
                entry.parser(ParseContext(crawl=self, owner=node, entry=entry, path=target))


__all__ = ["Crawl", "CrawlResult", "Member", "Node"]
