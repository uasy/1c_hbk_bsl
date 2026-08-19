"""Parsers named by the rules.

A parser answers "what to do with this thing", and rules name it. Two shapes
share one signature through :class:`ParseContext`:

* fragment parsers work on an element inside the owner's XML (``children_inline``);
* file parsers work on a path inside the node root (``node_files``).

Parsers may queue further work, and recursion is ordinary — a parser that gets a
self-describing file just hands it back to the crawl, which finds the rules of
whatever kind that file declares.

``CommandParser`` is the one exception in the whole mechanism: a command has no
descriptor of its own (``Commands/`` holds only directories), so its kind cannot
come from a file and is taken from the declaring tag instead.

A function name carries the generation its algorithm was built on: ``_2_20``
means "written against the 2.20 export", not "usable only with it". The name the
rules use stays the name of the entity (``BslParser``), so rules bind to a
contract and not to an implementation. When a format drifts, a second
implementation appears beside the first, and the registry — not the rules — shows
which generation is read by what.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lxml import etree

from .rules import RuleError
from .xml import child_text, local_name, parse, properties_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .crawl import Crawl, Node


@dataclass(slots=True)
class ParseContext:
    """Everything a parser is given, whatever its shape."""

    crawl: Crawl
    owner: Node
    entry: Any  # RuleEntry; untyped here to keep rules free of parser imports
    element: Any = None  # lxml element for children_inline
    path: Path | None = None  # file for node_files


def parse_member_2_20(ctx: ParseContext) -> None:
    """A child whose body lives in the owner's XML and that owns no files."""
    name = child_text(properties_of(ctx.element), "Name")
    if not name:
        ctx.crawl.result.anomalies.append(f"{ctx.owner.file_path}: {ctx.entry.key} without Name")
        return
    ctx.crawl.add_member(ctx.owner, ctx.entry.member_kind or local_name(ctx.element), name)


def parse_form_member_2_20(ctx: ParseContext) -> None:
    """A member of a form, which names itself with an attribute.

    The two formats differ in one place and it matters: a descriptor of
    ``MDClasses`` puts the name in ``<Properties><Name>``, a form writes
    ``<Attribute name="Объект">``. Nothing else about the member changes, so
    only the source of the name is separate.

    Inside the form itself the spelling is not quite uniform either: content
    typed through ``xsi:type`` — a choice parameter link — names itself with a
    ``<Name>`` child. Reading only the attribute would drop it while the file
    plainly declares a name, so both spellings are read here.
    """
    name = (ctx.element.get("name") or "").strip() or child_text(ctx.element, "Name").strip()
    if not name:
        ctx.crawl.result.anomalies.append(f"{ctx.owner.file_path}: {ctx.entry.key} without name")
        return
    ctx.crawl.add_member(ctx.owner, ctx.entry.member_kind or local_name(ctx.element), name)


def parse_ref_2_20(ctx: ParseContext) -> None:
    """A property whose value is an address of something else.

    The scheduled job names its handler, the event subscription names its own,
    an object names its default form. Nothing in the code calls those — the
    platform does — so without this edge the procedure looks unused and the form
    looks unreferenced.

    The address sits in the element's text or in its ``name`` attribute — a form
    writes per-role visibility as ``<Value name="Role.X">true</Value>``, where
    the text is the setting and the attribute is the address. Both are read: an
    address is an address, and which of the two spellings the format chose says
    nothing about the edge.

    The edge kind is the property name from the key, unless the rule names it:
    where the addressing element is called ``Value``, the key's own tail says
    nothing, and what the edge means is written down in the rule instead.
    """
    target = (ctx.element.text or "").strip()
    if not _ADDRESS.match(target):
        target = (ctx.element.get("name") or "").strip()
    if not target:
        return
    ctx.crawl.add_ref(ctx.owner, ctx.entry.ref_kind or ctx.entry.key.rsplit("/", 1)[-1], target)


#: an address as the platform writes it: ``Constant.X``,
#: ``InformationRegister.Y.StandardAttribute.Z``, ``Catalog.X.Command.Y``. The
#: head is an identifier, so versions (``3.0.202``) and URLs are not addresses.
_ADDRESS = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:\.[^.\s\"'<>/]+)+$")


def parse_file_refs_2_20(ctx: ParseContext) -> None:
    """A file that declares no names of its own and only points at others.

    Role rights, exchange plan content, the command interface of a subsystem —
    each has its own shape (``<object><name>``, ``<Item><Metadata>``, attribute
    ``<Command name=…>``), and none of them is worth teaching separately: an
    address is recognised by its form, wherever it sits. What the file is stays
    with the key, so the edge kind is the key's own name, and the shapes inside
    remain the file's business.
    """
    if ctx.path is None:
        return
    ctx.crawl.visit(ctx.path)
    try:
        root = parse(ctx.path).getroot()
    except etree.XMLSyntaxError as exc:
        ctx.crawl.result.anomalies.append(f"{ctx.path}: unreadable XML ({exc})")
        return
    ref_kind = ctx.entry.key.rsplit("/", 1)[-1]
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for value in (element.text, *element.attrib.values()):
            text = (value or "").strip()
            if _ADDRESS.match(text):
                ctx.crawl.add_ref(ctx.owner, ref_kind, text)


def parse_command_2_20(ctx: ParseContext) -> None:
    """A child declared in the owner's XML that owns a directory but no descriptor.

    The kind cannot be read from a file here, so the rules of ``Command`` are
    applied directly. Everything below that point is ordinary again.
    """
    name = child_text(properties_of(ctx.element), "Name")
    if not name:
        ctx.crawl.result.anomalies.append(f"{ctx.owner.file_path}: Command without Name")
        return
    folder = ctx.entry.folder
    node_root = Path(ctx.owner.node_root) / folder / name if folder else None
    ctx.crawl.enter_node(
        kind="Command",
        name=name,
        owner=ctx.owner,
        file_path=None,
        node_root=node_root,
        version=ctx.owner.version,
        declared=True,
    )


def parse_self_describing_2_20(ctx: ParseContext) -> None:
    """A node file that states its own kind: hand it back to the crawl."""
    if ctx.path is not None:
        ctx.crawl.parse_file(ctx.path, owner=ctx.owner, owner_version=ctx.owner.version)


def parse_opaque_2_20(ctx: ParseContext) -> None:
    """A node file we deliberately do not read: visited, counted, not parsed."""
    if ctx.path is not None:
        ctx.crawl.visit(ctx.path)


#: Implementations by generation. Rules name the key, an unknown name aborts rule
#: loading; the value carries which generation the implementation was built on, so
#: a fork shows up here and the rules of neither generation change.
_REGISTRIES: dict[str, dict[str, Callable[..., None]]] = {
    "2.20": {
        # "we looked and we do not want what is inside". A decision, not a gap:
        # ``parser: null`` means nobody has decided yet, and only this name says
        # the deciding was done. The file is still visited and struck off, so the
        # remainder keeps meaning what it means.
        "NopParser": parse_opaque_2_20,
        "AttributeParser": parse_member_2_20,
        "TabularSectionParser": parse_member_2_20,
        # у формы имя члена — атрибут, а не <Properties><Name>
        "FormMemberParser": parse_form_member_2_20,
        "CommandParser": parse_command_2_20,
        # свойство описателя, значение которого — адрес: обработчик задания,
        # подписка на событие, форма по умолчанию
        "RefParser": parse_ref_2_20,
        # файл, который сам ничего не объявляет, а только ссылается: права роли,
        # состав плана обмена, командный интерфейс
        "FileRefParser": parse_file_refs_2_20,
        # unlike NopParser this one is temporary: code is visited but not read,
        # because the prototype checks reachability and not content
        "BslParser": parse_opaque_2_20,
        # payloads with their own format that declare it themselves. A template is
        # here too: its payload format varies with the template kind, and an HTML
        # template turns out to declare extrnprops.Help — which owns the directory
        # beside it. Treating it as terminal loses that directory.
        "HelpParser": parse_self_describing_2_20,
        "LogFormParser": parse_self_describing_2_20,
        "TemplateParser": parse_self_describing_2_20,
        # a picture descriptor is the same shape: it declares extrnprops.ExtPicture
        # and owns the directory beside it, where the image itself lies. The name is
        # the entity's, not the file's — one picture is Ext/Picture.xml, the splash
        # screen and the main section picture are the same thing under other names.
        "PictureParser": parse_self_describing_2_20,
        # карта маршрута бизнес-процесса объявляет себя scheme.GraphicalSchema:
        # внутри и точки маршрута, адресуемые из кода, и линии связи с
        # оформлением, которые адресовать нельзя, — различать их будут ключи
        # того вида, а не этот парсер
        "FlowchartParser": parse_self_describing_2_20,
        # terminal payload: known file, nothing extracted yet
        "PredefinedParser": parse_opaque_2_20,
    },
}


def registry_for(generation: str) -> dict[str, Callable[..., None]]:
    """Parsers of one generation — the ones its own rules are allowed to name.

    The key is the generation the *rules* come from, not the version an individual
    file declares: rules are loaded a generation at a time, and the parsers that
    go with them are the other half of that same set.

    A generation is supported as a whole or not at all, and an unsupported one
    fails here — at load, like an unknown parser name. There is deliberately no
    falling back to a newer set: that would read an older export with a later
    format, which is the direction of error the version rules rule out.
    """
    try:
        return _REGISTRIES[generation]
    except KeyError:
        known = ", ".join(sorted(_REGISTRIES)) or "none"
        raise RuleError(f"no parsers for generation {generation!r}; known: {known}") from None
