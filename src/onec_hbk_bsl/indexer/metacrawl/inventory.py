"""Workspace inventory — what is here and can we read it.

This is a separate mechanism from the crawl, not a mode of it. It answers by
scanning and by self-description, so it works on sources whose generation has no
generated artifacts at all — which is exactly where it is needed most.

It is also the only place that opens an XSD. The crawl never validates; keeping
schema reading here is what makes "indexing does not pay for validation" a
structural fact rather than a default value.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from lxml import etree

from .xml import MDCLASSES, child_named, child_text, elements, properties_of, qname

#: (namespace, root tag) -> schema file inside a generation directory
FORMATS: dict[tuple[str, str], str] = {
    (MDCLASSES, "MetaDataObject"): "MetaDataObject.xsd",
    ("http://v8.1c.ru/8.3/xcf/logform", "Form"): "logform.Form.xsd",
    ("http://v8.1c.ru/8.3/xcf/extrnprops", "Help"): "extrnprops.Help.xsd",
    ("http://v8.1c.ru/8.3/xcf/extrnprops", "ExtPicture"): "extrnprops.ExtPicture.xsd",
    ("http://v8.1c.ru/8.3/xcf/extrnprops", "JobSchedule"): "extrnprops.JobSchedule.xsd",
    ("http://v8.1c.ru/8.3/xcf/extrnprops", "CommandInterface"): "extrnprops.CommandInterface.xsd",
    ("http://v8.1c.ru/8.2/roles", "Rights"): "roles.Rights.xsd",
    ("http://v8.1c.ru/8.3/xcf/predef", "PredefinedData"): "predef.PredefinedData.xsd",
    (
        "http://v8.1c.ru/8.1/data-composition-system/schema",
        "DataCompositionSchema",
    ): "dcs.DataCompositionSchema.xsd",
    ("http://v8.1c.ru/8.2/data/spreadsheet", "document"): "spreadsheet.document.xsd",
    ("http://v8.1c.ru/8.3/xcf/scheme", "GraphicalSchema"): "scheme.GraphicalSchema.xsd",
}

#: root kinds that can never belong to a configuration's composition
EXTERNAL_KINDS = frozenset({"ExternalReport", "ExternalDataProcessor"})

#: directories that cannot hold a source. Only version control internals are
#: listed: skipping by name hides sources, and ``undefined`` is a working case —
#: a root nobody recognises must still be reported, not filtered away.
SKIP_DIRS = frozenset({".git", ".hg", ".svn"})


class SourceKind(StrEnum):
    CONFIG = "config"
    EXTENSION = "extension"
    EXTERNAL = "external"
    UNDEFINED = "undefined"


@dataclass(slots=True)
class Source:
    """One row of the inventory: a source plus what we can say about it."""

    config_root: str | None
    kind: SourceKind
    name: str | None = None
    meta_version: str | None = None
    meta_files_count: int = 0
    meta_schema_exist: bool = False
    valid_meta_files_count: int = 0
    warning_meta_files_count: int = 0
    invalid_meta_files_count: int = 0
    #: needs the crawl result; stays None until it has run, never a silent zero
    unmanaged_meta_files_count: int | None = None
    root_path: str = ""


@dataclass(slots=True)
class _Head:
    """What the first two elements of a file say about it."""

    namespace: str
    root_tag: str
    kind: str
    version: str


def read_head(path: Path) -> _Head | None:
    """Read root tag, declared kind and version without parsing the whole file."""
    try:
        context = etree.iterparse(str(path), events=("start",))
        namespace = root_tag = kind = version = ""
        for index, (_, element) in enumerate(context):
            if index == 0:
                namespace, root_tag = qname(element.tag)
                version = (element.get("version") or "").strip()
                if not (namespace == MDCLASSES and root_tag == "MetaDataObject"):
                    kind = root_tag
                    break
            else:
                kind = qname(element.tag)[1]
                break
        del context
        return _Head(namespace=namespace, root_tag=root_tag, kind=kind, version=version)
    except (etree.XMLSyntaxError, OSError):
        return None


def _is_extension(path: Path) -> bool:
    """Extensions carry ConfigurationExtensionPurpose; base configurations do not."""
    try:
        root = etree.parse(str(path)).getroot()
    except (etree.XMLSyntaxError, OSError):
        return False
    body = next(iter(elements(root)), None)
    if body is None:
        return False
    return child_named(properties_of(body), "ConfigurationExtensionPurpose") is not None


def _name_of(path: Path) -> str:
    try:
        root = etree.parse(str(path)).getroot()
    except (etree.XMLSyntaxError, OSError):
        return ""
    body = next(iter(elements(root)), None)
    return child_text(properties_of(body), "Name") if body is not None else ""


def scan_workspace(
    workspace: Path,
    *,
    schemas_root: Path | None = None,
    validate_limit: int = 0,
) -> list[Source]:
    """Describe every source under ``workspace``.

    ``validate_limit`` caps how many files per source are validated; validation
    costs about 2.5 ms per file, so a full pass over a large source takes about a
    minute and is not something a status call should do unasked.

    The tree is walked once, top down, and a file belongs to the source of the
    directory it sits in. Ownership is therefore inherited from the parent
    directory rather than searched for: a workspace holds hundreds of roots, and
    asking every file which of them contains it costs their product.
    """
    heads: dict[Path, _Head] = {}
    kinds: dict[Path, SourceKind] = {}  # root -> what it is
    descriptors: dict[Path, Path] = {}  # root -> the file that said so
    owner_of: dict[Path, Path | None] = {}  # directory -> its source root
    owned: dict[Path, list[Path]] = defaultdict(list)
    orphans: list[Path] = []

    for current, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        directory = Path(current)
        files = [directory / name for name in sorted(filenames)]

        # a descriptor names the directory it lies in, so one directory is at
        # most one root; a configuration outranks an external report beside it
        root_kind: SourceKind | None = None
        descriptor: Path | None = None
        for path in files:
            if path.suffix.lower() != ".xml":
                continue
            head = read_head(path)
            if head is None:
                continue
            heads[path] = head
            if head.namespace != MDCLASSES or head.root_tag != "MetaDataObject":
                continue
            if path.name == "Configuration.xml" and head.kind == "Configuration":
                root_kind = SourceKind.EXTENSION if _is_extension(path) else SourceKind.CONFIG
                descriptor = path
            elif head.kind in EXTERNAL_KINDS and root_kind is None:
                # position says nothing: an external report may sit inside a
                # configuration root, so the declared kind is the only evidence
                root_kind = SourceKind.EXTERNAL
                descriptor = path

        # a root claims its own directory; everything else inherits, so the
        # deepest root wins without anything having to be sorted by depth
        if root_kind is not None and descriptor is not None:
            owner: Path | None = directory
            kinds[directory] = root_kind
            descriptors[directory] = descriptor
        else:
            owner = owner_of.get(directory.parent)
        owner_of[directory] = owner

        if owner is None:
            orphans.extend(files)
        else:
            owned[owner].extend(files)

    sources: list[Source] = []
    for root in sorted(kinds, key=str):
        kind = kinds[root]
        descriptor = descriptors[root]
        files = owned.get(root, [])
        head = heads.get(descriptor)
        source = Source(
            config_root=str(root) if kind in (SourceKind.CONFIG, SourceKind.EXTENSION) else None,
            kind=kind,
            name=_name_of(descriptor) or None,
            meta_version=(head.version if head and head.version else None),
            meta_files_count=len(files),
            root_path=str(root),
        )
        _fill_validation(source, files, heads, schemas_root, validate_limit)
        sources.append(source)

    for root, files in _group_orphans(workspace, orphans).items():
        sources.append(
            Source(
                config_root=None,
                kind=SourceKind.UNDEFINED,
                meta_files_count=len(files),
                root_path=str(root),
            )
        )
    return sources


def _group_orphans(workspace: Path, orphans: Iterable[Path]) -> dict[Path, list[Path]]:
    """Everything not claimed by a source, grouped by its top directory."""
    groups: dict[Path, list[Path]] = defaultdict(list)
    for path in orphans:
        relative = path.relative_to(workspace)
        top = workspace / relative.parts[0] if len(relative.parts) > 1 else workspace
        groups[top].append(path)
    return groups


def _fill_validation(
    source: Source,
    files: list[Path],
    heads: dict[Path, _Head],
    schemas_root: Path | None,
    limit: int,
) -> None:
    """Validate what the source's own generation describes.

    A generation with no artifacts is reported, not treated as a failure: schemas
    are missing, counters stay empty, and parsing is not forbidden.
    """
    if schemas_root is None or source.meta_version is None:
        return
    # whether the generation has artifacts is a fact about the package, not about
    # this run: it is reported even when nothing is validated
    generation = schemas_root / source.meta_version
    source.meta_schema_exist = generation.is_dir()
    if not source.meta_schema_exist or limit <= 0:
        return

    schemas: dict[str, etree.XMLSchema] = {}
    checked: dict[str, int] = defaultdict(int)
    for path in files:
        head = heads.get(path)
        if head is None:
            continue
        name = FORMATS.get((head.namespace, head.root_tag))
        if name is None:
            continue
        if checked[name] >= limit:
            continue
        schema = schemas.get(name)
        if schema is None:
            schema_path = generation / name
            if not schema_path.is_file():
                continue
            schema = etree.XMLSchema(etree.parse(str(schema_path)))
            schemas[name] = schema
        checked[name] += 1
        try:
            document = etree.parse(str(path))
        except (etree.XMLSyntaxError, OSError):
            source.invalid_meta_files_count += 1
            continue
        if schema.validate(document):
            # a file of an older generation than its source is structurally fine
            # but still worth flagging
            if head.version and head.version != source.meta_version:
                source.warning_meta_files_count += 1
            else:
                source.valid_meta_files_count += 1
        else:
            source.invalid_meta_files_count += 1


__all__ = ["EXTERNAL_KINDS", "FORMATS", "Source", "SourceKind", "read_head", "scan_workspace"]
