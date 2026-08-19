"""XML helpers shared by the crawl and the inventory.

The export mixes namespaces freely, so everything here works on local names and
treats the namespace as data rather than as something to hardcode.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from lxml import etree

MDCLASSES = "http://v8.1c.ru/8.3/MDClasses"


def qname(tag: Any) -> tuple[str, str]:
    """-> (namespace, local name). Comments and PIs have non-string tags."""
    if not isinstance(tag, str):
        return ("", "")
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        return (namespace, local)
    return ("", tag)


def local_name(element: Any) -> str:
    return qname(element.tag)[1]


def elements(parent: Any) -> Iterator[Any]:
    """Child elements only: comments and processing instructions are skipped."""
    for child in parent:
        if isinstance(child.tag, str):
            yield child


def child_named(parent: Any, name: str) -> Any:
    if parent is None:
        return None
    for child in elements(parent):
        if local_name(child) == name:
            return child
    return None


def child_text(parent: Any, name: str) -> str:
    child = child_named(parent, name)
    if child is None:
        return ""
    return (child.text or "").strip()


def properties_of(element: Any) -> Any:
    return child_named(element, "Properties")


def parse(path: Path) -> Any:
    """Parse a descriptor. Raises ``etree.XMLSyntaxError`` on malformed input."""
    return etree.parse(str(path))


def declared_kind(root: Any) -> str:
    """The kind a file states about itself.

    ``MetaDataObject`` wraps exactly one element and that element names the kind
    (``Catalog``, ``Form``, ``ExternalReport``). Other formats name themselves by
    their own root: ``logform:Form`` becomes ``logform.Form``, so a kind is always
    a single string and a rule file name never has to be assembled from parts.
    """
    namespace, local = qname(root.tag)
    if namespace == MDCLASSES and local == "MetaDataObject":
        kids = list(elements(root))
        return local_name(kids[0]) if len(kids) == 1 else ""
    if not local:
        return ""
    if not namespace:
        return local
    return f"{namespace.rstrip('/').rsplit('/', 1)[-1]}.{local}"


def declared_version(root: Any) -> str:
    """Schema version the file states about itself, empty when it states none."""
    return (root.get("version") or "").strip()
