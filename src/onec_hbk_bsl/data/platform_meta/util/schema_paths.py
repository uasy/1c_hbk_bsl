#!/usr/bin/env python3
"""Словарь путей одного поколения — по схеме, а не по корпусу.

Схема уже содержит то, ради чего перечень ходил по выгрузкам второй раз: какие
элементы вложены друг в друга и где структура возвращается к себе. Брать это
оттуда лучше по одной причине — схема хранит пути **абсолютными**, а сворачивает
только хвост, точку возврата. Перечень, свёрнутый по повторяющемуся сегменту,
слил бы ``ChildItems/Button`` и ``AutoCommandBar/ChildItems/Button`` в один ключ,
и один и тот же элемент нашёлся бы двумя дорогами.

Что отсюда выводится:

    именованные пути    у элемента есть ``name``, ``Name`` или ``Properties/Name``
    точки возврата      имя элемента уже встречалось в пути — и якорь, откуда
                        перечень продолжается

Свёртка здесь **своя, для правил**, и по именам типов не считается. Схема
именует типы контекстом и различает `Table/ChildItems` и `ContextMenu/ChildItems`
— ей это нужно, чтобы проверять файлы. Правилам различать их незачем: решение
зависит от вида элемента (`form_item`, `form_attribute`, ссылка), а не от ветки,
в которой он найден. Поэтому путь сворачивается на повторе имени элемента, и
перечень ключей остаётся обозримым.

Чего отсюда не выводится — какие пути несут адреса. ``Properties/DefaultForm``
это элемент как элемент; что его текст — ссылка, видно по значению, а значений в
схеме нет намеренно, вместе со счётчиками. Такие ключи остаются наблюдением по
корпусу.

Рекурсия в схеме — настоящий возврат структуры к себе, а не совпадение имени
контейнера: тип именуется контекстом из нескольких имён, и повтор типа означает,
что тот же контекст встретился снова. Поэтому у ``TabularSection/ChildObjects``
тип свой, а не предка, и точкой рекурсии он не считается.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

XS = "{http://www.w3.org/2001/XMLSchema}"


@dataclass(slots=True)
class SchemaPaths:
    """Пути одной схемы."""

    #: путь -> имя типа; только те, у кого есть собственное имя
    named: dict[str, str] = field(default_factory=dict)
    #: путь, где структура вернулась к себе -> ключ-якорь, откуда продолжать
    recursion: dict[str, str] = field(default_factory=dict)


def _types(root) -> dict[str, tuple[list[tuple[str, str]], set[str]]]:
    """Именованные типы схемы: (дети, атрибуты)."""
    out = {}
    for ct in root.findall(f"{XS}complexType"):
        kids = [(e.get("name"), e.get("type")) for e in ct.iter(f"{XS}element") if e.get("name")]
        attrs = {a.get("name") for a in ct.iter(f"{XS}attribute")}
        out[ct.get("name")] = (kids, attrs)
    return out


def read(xsd: Path) -> SchemaPaths:
    """Пути и точки рекурсии одной схемы.

    Обход идёт от корневых элементов вглубь по типам. Тип, уже встреченный на
    пути, вглубь не раскрывается: это и есть точка возврата, и вместо неё
    записывается якорь — путь, где тот же тип встретился впервые.
    """
    root = etree.parse(str(xsd)).getroot()
    types = _types(root)

    def named(element: str, type_name: str) -> bool:
        """Есть ли у элемента собственное имя.

        Написаний три, по одному на линию форматов: атрибут ``name`` у формы и
        графической схемы, прямой ``<Name>``/``<name>`` у предопределённых,
        компоновки, прав и табличного документа, ``Properties/Name`` у члена и
        описателя метаданных. Различает их парсер, а перечню важно одно — у
        элемента есть имя, значит на него можно сослаться.

        ``Properties`` исключён: прямой ``<Name>`` у него есть, но сущность не
        он, а его владелец — имя принадлежит владельцу.
        """
        kids, attrs = types.get(type_name, ([], set()))
        by_name = dict(kids)
        if "name" in attrs:
            return True
        if element != "Properties" and ("Name" in by_name or "name" in by_name):
            return True
        inner = types.get(by_name.get("Properties", ""), ([], set()))[0]
        return "Name" in dict(inner)

    out = SchemaPaths()

    def walk(type_name: str, path: str) -> None:
        segments = path.split("/") if path else []
        for child, child_type in types.get(type_name, ([], set()))[0]:
            here = f"{path}/{child}" if path else child
            if child_type not in types:
                continue
            if named(child, child_type):
                out.named[here] = child_type
            if child in segments:
                # Имя уже было в пути: ниже начинается то же самое, и перечень
                # продолжается с первого его вхождения. Вглубь не идём — иначе
                # перечень зависел бы от того, насколько глубоко вложены группы
                # в попавшихся выгрузках
                out.recursion[here] = "/".join(segments[: segments.index(child) + 1])
                continue
            walk(child_type, here)

    for element in root.findall(f"{XS}element"):
        # Корень, содержимое которого меняется по ``xsi:type``, прежде объявляли
        # ``xs:anyType``; тело при этом описано и лежит в типе, названном по
        # корню. Схемы нынешнего вида объявляют корень своим типом, но читаются
        # и прежние
        type_name = element.get("type")
        if type_name not in types:
            type_name = f"t_{element.get('name')}"
        if type_name in types:
            walk(type_name, "")
    return out


def for_kind(schema_dir: Path, kind: str) -> SchemaPaths | None:
    """Пути вида: своя схема, либо ветка ``MetaDataObject`` по имени вида.

    Схема называется по формату, а не по виду, и совпадают они не всегда:
    ``logform.Form.xsd`` — это вид ``logform.Form``, а все виды метаданных
    описаны одним ``MetaDataObject.xsd``, где вид — первый сегмент пути. Это то
    же устройство, по которому обход берёт тело описателя: ``MetaDataObject``
    оборачивает ровно один элемент, и он называется видом.
    """
    own = schema_dir / f"{kind}.xsd"
    if own.is_file():
        return read(own)

    common = schema_dir / "MetaDataObject.xsd"
    if not common.is_file():
        return None
    whole = read(common)
    prefix = f"{kind}/"

    def strip(path: str) -> str:
        # якорь, равный самому виду, — это тело описателя: продолжать оттуда
        # значит применить ключи верхнего уровня. Так устроена вложенность
        # подсистем: подсистема содержит подсистему тем же ключом
        return path[len(prefix) :] if path.startswith(prefix) else ""

    branch = SchemaPaths(
        named={strip(p): t for p, t in whole.named.items() if p.startswith(prefix)},
        recursion={strip(p): strip(a) for p, a in whole.recursion.items() if p.startswith(prefix)},
    )
    return branch if branch.named else None


def markers(paths: SchemaPaths) -> dict[str, tuple[str, str]]:
    """Точки рекурсии, переложенные на ключи правил: ключ -> (через, якорь).

    Возврат к типу-предку случается на безымянном контейнере
    (``ChildItems/UsualGroup/ChildItems``), а ключ правил — это именованный
    элемент. Метка потому садится на ближайший именованный путь сверху: внутри
    группы снова применяются ключи из ``ChildItems``. Если именована сама точка
    (``ChildObjects/Subsystem``), метка садится на неё же.

    Фактов в метке два, и одного не хватает. ``через`` — путь от помеченного
    элемента вниз до места, где структура возобновилась: у группы формы это
    ``ChildItems``, у предопределённого элемента — ``ChildItems/Item``. ``якорь``
    — с какого места перечня ключи действуют снова. Совпадают они не всегда:
    ключи возобновляются с ``AutoCommandBar/ChildItems``, а спускаться от группы
    кнопок надо всего на ``ChildItems``.
    """
    out: dict[str, tuple[str, str]] = {}
    for point, anchor in paths.recursion.items():
        key = point if point in paths.named else ""
        if not key:
            above = [p for p in paths.named if point.startswith(f"{p}/")]
            if not above:
                continue
            key = max(above, key=len)
        through = point[len(key) + 1 :] if point != key else ""
        # ближе к точке — точнее: у ключа один якорь, а не набор
        if key not in out or len(anchor) > len(out[key][1]):
            out[key] = (through, anchor)
    return out


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("schema", type=Path, help="файл .xsd")
    args = ap.parse_args()
    paths = read(args.schema)
    print(f"именованных путей: {len(paths.named)}")
    print(f"точек рекурсии   : {len(paths.recursion)}")
    for path in sorted(paths.named):
        print(f"  {path}")
    for path, anchor in sorted(paths.recursion.items()):
        print(f"  {path} -> продолжать ключами из {anchor!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
