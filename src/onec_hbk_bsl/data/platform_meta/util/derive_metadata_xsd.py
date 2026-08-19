#!/usr/bin/env python3
"""Порождение XSD одного формата выгрузки из корпуса.

Формат задаётся парой ``--namespace`` + ``--root-tag``; поколение выгрузки —
``--version``. Один запуск даёт одну схему и пополняет общие заглушки в
``stubs/``.

Схема ОПИСАТЕЛЬНА: фиксирует встретившееся, а не допустимое формой. Модель
содержимого поэтому разрешительная (``xs:choice`` без порядка и кратностей) —
такой схемой можно находить поля и обнаруживать незнакомое, но нельзя
отбраковывать файлы.

Тип именуется **контекстом** — последними ``CONTEXT`` именами пути. Полный путь
разворачивал бы рекурсию в ленту типов, растущую с корпусом; одно имя сливало бы
``ChildObjects`` справочника с ``ChildObjects`` табличной части. Фиксированный
контекст даёт и сходимость, и раздельность: у ``Table/ChildItems`` свой тип
(колонки), у ``ContextMenu/ChildItems`` свой (кнопки).

Границы артефакта:

- одно пространство имён и одно поколение выгрузки на файл;
- поддеревья чужих пространств не раскрываются: у них свои схемы, здесь
  ``xs:any``. Типы, названные через ``xsi:type``, получают заглушки в
  ``stubs/`` — общие на все форматы, пополняются слиянием;
- ни путей корпуса, ни счётчиков наблюдений: словарь формата, и только.

Схема НАКАПЛИВАЕТСЯ. Прогон читает существующий файл обратно в модель
наблюдений, объединяет с наблюдениями по переданным корням и переписывает.
Прежние выгрузки для этого держать не нужно — источником прежних наблюдений
служит сама схема; ``--fresh`` строит с нуля, отбрасывая накопленное.

Инварианты:

- монотонность — ранее наблюдённое не исчезает;
- идемпотентность — прогон, ничего не добавивший, файла не меняет;
- обратимость — ``read_back(emit(model)) == model``; на ней держатся первые две,
  поэтому всё, что влияет на вывод, обязано читаться назад;
- все файлы формата из корпуса проходят валидацию полученной схемой.

Использование::

    python util/derive_metadata_xsd.py \
        --version 2.20 \
        --namespace 'http://v8.1c.ru/8.3/MDClasses' \
        --root-tag  MetaDataObject \
        --out 2.20/MetaDataObject.xsd \
        /путь/к/конфигурации/src /путь/к/расширению
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    from lxml import etree as ET

    HAVE_LXML = True
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

    HAVE_LXML = False

#: Сколько имён пути задают тип. Тождество типа — это выбор, и он между двумя
#: бедами. Полный путь разворачивает рекурсию в ленту: групп в группах сколько
#: угодно, и типов становится столько же, сколько встретилось сочетаний, — они
#: растут с корпусом и никогда не сходятся. Одно имя, наоборот, сливает
#: `ChildObjects` справочника с `ChildObjects` табличной части. Фиксированный
#: контекст даёт и сходимость, и раздельность семейств: `Table/ChildItems` —
#: колонки, `ContextMenu/ChildItems` — кнопки, `Pages/ChildItems` — страницы.
#: Три имени вдобавок отличают реквизит справочника от реквизита регистра.
CONTEXT = 3

MDC = "http://v8.1c.ru/8.3/MDClasses"
XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def context(path: str, child: str) -> str:
    """Ключ типа для ребёнка: последние ``CONTEXT`` имён пути.

    Рекурсия отдельным понятием не нужна: структура, вернувшаяся к себе, даёт
    тот же ключ и тем самым тот же тип — именно так рекурсивный тип и выражается
    в XSD. Повтор имени контейнера при этом ничего не сворачивает: у
    ``TabularSection/ChildObjects`` ключ свой, а не предка.
    """
    return ".".join(((path.split(".") if path else []) + [child])[-CONTEXT:])


def qname(tag) -> tuple[str, str]:
    """(namespace, local) для тега; для комментариев lxml — ('', '')."""
    if not isinstance(tag, str):
        return ("", "")
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return (ns, local)
    return ("", tag)


class Observation:
    """Наблюдения по одному пути элемента.

    Хранится ровно то, что влияет на вывод, — иначе слияние опиралось бы на
    сведения, которых в схеме нет и которые не прочитать назад. Так, имена
    чужих атрибутов не хранятся: они покрыты `anyAttribute ##other` и в схему
    не попадают. Рекурсия тоже не поле: она выражается тем, что вернувшаяся к
    себе структура даёт тот же ключ типа, а значит и тот же тип.
    """

    __slots__ = ("children", "attributes", "foreign_children", "xsi_typed")

    def __init__(self) -> None:
        self.children: set[str] = set()  # локальные имена детей своего ns
        self.attributes: set[str] = set()  # локальные имена атрибутов без ns
        self.foreign_children: set[str] = set()  # '{ns}local' детей чужих ns
        # тип задаётся в экземпляре через xsi:type; какие именно типы —
        # сведение не путевое, оно собирается в Model.xsi_types
        self.xsi_typed = False

    def absorb(self, other: Observation) -> None:
        self.children |= other.children
        self.attributes |= other.attributes
        self.foreign_children |= other.foreign_children
        self.xsi_typed = self.xsi_typed or other.xsi_typed

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Observation):
            return NotImplemented
        return (
            self.children == other.children
            and self.attributes == other.attributes
            and self.foreign_children == other.foreign_children
            and self.xsi_typed == other.xsi_typed
        )


class Model:
    """Всё, из чего строится схема: наблюдения по путям плюс типы xsi:type.

    `xsi_types` — пространство имён в локальные имена типов. Своё пространство
    даёт типы, объявляемые в самой схеме; чужие — импорты и заглушки. Ключ с
    пустым множеством законен: при чтении схемы назад имена чужих типов взять
    неоткуда (они живут в `stubs/`), а импорт восстановить нужно.
    """

    __slots__ = ("paths", "xsi_types", "root_types")

    def __init__(self) -> None:
        self.paths: dict[str, Observation] = defaultdict(Observation)
        self.xsi_types: dict[str, set[str]] = defaultdict(set)
        # типы своего пространства, названные xsi:type на САМОМ корне:
        # <Rights xsi:type="Rights">, <PredefinedData xsi:type="…Items">.
        # Хранятся отдельно, потому что только для них известна база — тип
        # корня, — и только они поэтому объявляются его расширением
        self.root_types: set[str] = set()

    def absorb(self, other: Model) -> None:
        for path, obs in other.paths.items():
            self.paths[path].absorb(obs)
        for ns, names in other.xsi_types.items():
            self.xsi_types[ns] |= names
        self.root_types |= other.root_types


def inherited_version(path: Path, _cache: dict[str, str] = {}) -> str:
    """Версия ближайшего предка, несущего атрибут.

    Форматы старой линии (MXL, `DataCompositionSchema`) `version` не несут:
    3 454 файла из 27 386 в замеренном корпусе. Брать для них «последнюю
    известную» неверно —
    это подставило бы правила более нового поколения, чем выгрузка. Родительская
    же версия есть факт о происхождении: конфигурация выгружается одним прогоном
    Конфигуратора, и payload пишется тем же прогоном, что и его описатель.
    """
    cur = path.parent
    while cur != cur.parent:
        key = str(cur)
        if key in _cache:
            return _cache[key]
        for cand in (cur.with_suffix(".xml"), cur / "Configuration.xml"):
            if cand.is_file():
                try:
                    v = ET.parse(str(cand)).getroot().get("version") or ""
                except Exception:
                    v = ""
                if v:
                    _cache[key] = v
                    return v
        cur = cur.parent
    return ""


def collect(
    roots: list[Path], version: str, target_ns: str = MDC, target_root: str = "MetaDataObject"
) -> tuple[Model, int]:
    model = Model()
    paths = model.paths
    files = 0

    def walk(elem, path: str) -> None:
        obs = paths[path]
        for name, value in elem.attrib.items():
            ns, local = qname(name)
            # чужие атрибуты (xsi:nil и прочие) покрыты anyAttribute ##other,
            # именами в схему не попадают — и не хранятся
            if not ns:
                obs.attributes.add(local)
            if ns == XSI and local == "type":
                nsmap = getattr(elem, "nsmap", {})
                if ":" in value:
                    pfx, loc = value.split(":", 1)
                    t_ns = nsmap.get(pfx, "")
                else:
                    # без префикса — пространство ПО УМОЛЧАНИЮ, а не пустое.
                    # В формах так записан xsi:type="DynamicList": пространство
                    # там logform, то есть своё же. Пока это читалось как пустое,
                    # тип не объявлялся и 226 форм из 800 не проходили проверку
                    loc, t_ns = value, nsmap.get(None, "")
                obs.xsi_typed = True
                model.xsi_types[t_ns].add(loc)
        for child in elem:
            ns, local = qname(child.tag)
            if not local:
                continue  # комментарий / PI
            if ns == target_ns or ns == "":
                obs.children.add(local)
                # Ключ типа — контекст, последние CONTEXT имён пути. Обход при
                # этом идёт по дереву документа и потому конечен, а вложенность
                # любой глубины ложится на те же типы: вернувшаяся к себе
                # структура даёт тот же ключ
                walk(child, context(path, local))
            else:
                obs.foreign_children.add(f"{{{ns}}}{local}")

    for root_dir in roots:
        for f in root_dir.rglob("*.xml"):
            try:
                root = ET.parse(str(f)).getroot()
            except Exception:
                continue
            ns, local = qname(root.tag)
            if ns != target_ns or local != target_root:
                continue
            # Версия: своя, иначе унаследованная от родителя. Форматы старой
            # линии (MXL, схемы компоновки) атрибута не несут вовсе — для них
            # версия означает «в выгрузке какого поколения файл наблюдался»,
            # а не версию их собственного формата
            file_version = root.get("version") or inherited_version(f)
            if file_version != version:
                continue
            files += 1
            # атрибуты корня учитываем отдельным путём
            robs = paths[target_root]
            for name in root.attrib:
                a_ns, a_local = qname(name)
                if not a_ns:
                    robs.attributes.add(a_local)
                elif a_ns == XSI and a_local == "type":
                    # Корень называет свой тип сам: <Rights xsi:type="Rights">,
                    # <PredefinedData xsi:type="CatalogPredefinedItems">. Тип
                    # своего же пространства, и база у него известна — тип
                    # корня, — поэтому он объявляется расширением, а не
                    # заглушкой: иначе корень пришлось бы объявить anyType, и
                    # проверка формата перестала бы что-либо значить
                    value = root.attrib[name]
                    nsmap = getattr(root, "nsmap", {})
                    if ":" in value:
                        pfx, loc = value.split(":", 1)
                        t_ns = nsmap.get(pfx, "")
                    else:
                        loc, t_ns = value, nsmap.get(None, "")
                    if t_ns == target_ns:
                        model.root_types.add(loc)

            if target_ns == MDC and target_root == "MetaDataObject":
                # У MDClasses корень несёт РОВНО одного ребёнка, и он же вид
                # объекта (проверено: 16 717 из 16 717, аномалий нет).
                children = [c for c in root if isinstance(c.tag, str)]
                if len(children) != 1:
                    continue
                _, kind = qname(children[0].tag)
                robs.children.add(kind)
                walk(children[0], f"{target_root}.{kind}")
            else:
                # У прочих форматов корень — уже само содержимое: у logform:Form
                # это AutoCommandBar, Attributes, ChildItems и прочее. Вида как
                # отдельного уровня нет, обходим корень напрямую
                walk(root, target_root)
    return model, files


_SAFE = re.compile(r"[^0-9A-Za-z_.\-]")


def ns_file(ns: str) -> str:
    return _SAFE.sub("_", ns.replace("http://", "")) + ".xsd"


def emit_stub(ns: str, type_names: set[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!--",
        f"  Заглушки типов пространства {ns}.",
        "  Порождено из корпуса: перечислены только те типы, на которые",
        "  ссылается xsi:type в выгрузках. Содержимое НЕ описано —",
        "  заглушка нужна, чтобы xsi:type разрешался при проверке.",
        "-->",
        f'<xs:schema xmlns:xs="{XSD}" targetNamespace="{ns}"',
        '           elementFormDefault="qualified">',
    ]
    for t in sorted(type_names):
        lines.append(f'  <xs:complexType name="{t}" mixed="true">')
        lines.append("    <xs:sequence>")
        lines.append(
            '      <xs:any namespace="##any" processContents="skip"'
            ' minOccurs="0" maxOccurs="unbounded"/>'
        )
        lines.append("    </xs:sequence>")
        lines.append('    <xs:anyAttribute namespace="##any" processContents="skip"/>')
        lines.append("  </xs:complexType>")
    lines.append("</xs:schema>")
    return "\n".join(lines) + "\n"


def type_name(path: str) -> str:
    return "t_" + _SAFE.sub("_", path)


# Читать модель нельзя через defaultdict: обращение к недостающему пути завело
# бы в неё пустое наблюдение, которого в выводе нет, и обратимость перестала бы
# выполняться. Отсутствующий путь — вот это, и оно неизменяемо по смыслу
_EMPTY = Observation()


def emit(
    model: Model,
    version: str,
    updated: str,
    target_ns: str = MDC,
    root_tag: str = "MetaDataObject",
) -> str:
    paths = model.paths
    out: list[str] = []
    w = out.append
    w('<?xml version="1.0" encoding="UTF-8"?>')
    w("<!--")
    w(f"  Схема пространства имён {target_ns}")
    w(f"  корневой элемент {root_tag}, поколение выгрузки {version}.")
    if target_ns != MDC:
        w("")
        w("  ВНИМАНИЕ: этот формат СВОЕГО атрибута version может не нести —")
        w("  тогда поколение унаследовано от описателя-родителя и означает")
        w("  «в выгрузке какого поколения файл наблюдался», а НЕ версию этого")
        w("  формата.")
    w("")
    w("  ПОРОЖДЕНО из корпуса выгрузок, не является официальной схемой 1С.")
    # Ни путей корпуса, ни счётчиков: пути — это каталоги рабочей машины с
    # именами конкретных конфигураций (см. readme, раздел 1), а счётчики при
    # накоплении означали бы «сколько раз наблюдалось за всю историю прогонов»
    # — число, проверяемое только тем корпусом, которого уже нет
    w(f"  Дата последнего пополнения: {updated}")
    w("")
    w("  СХЕМА НАКАПЛИВАЕТСЯ. Прогон читает этот файл обратно в модель")
    w("  наблюдений, объединяет с наблюдениями по новым выгрузкам и переписывает.")
    w("  Прежние выгрузки для этого не нужны: источником прежних наблюдений")
    w("  служит сама схема. Ранее записанное не удаляется; прогон, ничего не")
    w("  добавивший, файла не меняет.")
    w("")
    w("  ХАРАКТЕР АРТЕФАКТА — ОПИСАТЕЛЬНЫЙ, НЕ НОРМАТИВНЫЙ.")
    w("  Схема фиксирует то, что встретилось в корпусе, а не то, что допускает")
    w("  формат. Модель содержимого намеренно разрешительная (xs:choice без")
    w("  порядка и кратностей). Применение:")
    w("    можно  — находить поля, обнаруживать незнакомые элементы;")
    w("    нельзя — отбраковывать файлы: законный, но не встретившийся в корпусе")
    w("             элемент дал бы ложную ошибку.")
    w("")
    w("  Поддеревья чужих пространств имён (v8:, xr:, logform, DCS, predef…)")
    w("  не раскрываются — у них собственные артефакты; здесь xs:any.")
    w("-->")
    w(f'<xs:schema xmlns:xs="{XSD}"')
    w(f'           xmlns="{target_ns}"')
    w(f'           targetNamespace="{target_ns}"')
    w('           elementFormDefault="qualified">')
    w("")
    # Типы СВОЕГО пространства, названные через xsi:type, объявляются прямо
    # здесь: импортировать собственный targetNamespace в XSD нельзя, а без
    # объявления `xsi:type="DynamicList"` не разрешается и форма не проходит
    own_types = sorted(model.xsi_types.get(target_ns, ()))
    foreign_ns = sorted(ns for ns in model.xsi_types if ns not in ("", XSD, target_ns))
    if foreign_ns:
        w("  <!-- Схема НЕ самодостаточна: экземпляры ссылаются через xsi:type")
        w("       на типы чужих пространств имён. Заглушки этих типов порождаются")
        w("       рядом; настоящие определения — за пределами этого артефакта")
        w("       (у каждого пространства будет свой). -->")
        for ns in foreign_ns:
            w(f'  <xs:import namespace="{ns}" schemaLocation="stubs/{ns_file(ns)}"/>')
        w("")
    w(f'  <xs:element name="{root_tag}" type="{type_name(root_tag)}" nillable="true"/>')
    w("")

    if own_types:
        w("  <!-- Типы своего пространства, названные через xsi:type. Импортировать")
        w("       собственный targetNamespace нельзя, поэтому объявлены здесь. -->")
        for t in own_types:
            if t in model.root_types:
                # Тип, которым корень называет сам себя. База известна — тип
                # корня, — и объявление расширением делает xsi:type законно
                # выводимым, оставляя содержимое под проверкой. Объявить корень
                # anyType было бы проще, но тогда формат не проверялся бы вовсе
                w(f'  <xs:complexType name="{t}" mixed="true">')
                w("    <xs:complexContent>")
                w(f'      <xs:extension base="{type_name(root_tag)}"/>')
                w("    </xs:complexContent>")
                w("  </xs:complexType>")
                continue
            # тип, названный на вложенном элементе: базы у него нет — один и тот
            # же тип встречается у разных элементов, — поэтому разрешительно
            w(f'  <xs:complexType name="{t}" mixed="true">')
            w("    <xs:sequence>")
            w(
                '      <xs:any namespace="##any" processContents="skip"'
                ' minOccurs="0" maxOccurs="unbounded"/>'
            )
            w("    </xs:sequence>")
            w('    <xs:anyAttribute namespace="##any" processContents="skip"/>')
            w("  </xs:complexType>")
        w("")

    for path in sorted(paths):
        obs = paths[path]
        tn = type_name(path)
        mixed = ' mixed="true"' if (obs.children or obs.foreign_children) else ""
        w(f'  <xs:complexType name="{tn}"{mixed}>')
        w("    <xs:annotation>")
        w(f"      <xs:documentation>контекст: {path}</xs:documentation>")
        w("    </xs:annotation>")
        local_attrs = sorted(obs.attributes)

        def attrs(indent: str) -> None:
            for attr in local_attrs:
                w(f'{indent}<xs:attribute name="{attr}" type="xs:string"/>')
            # xsi:nil и прочие чужие атрибуты — разрешительно
            w(f'{indent}<xs:anyAttribute namespace="##other" processContents="skip"/>')

        if obs.children or obs.foreign_children:
            # смешанное содержимое: элемент может нести и текст, и детей
            w("    <xs:sequence>")
            w('      <xs:choice minOccurs="0" maxOccurs="unbounded">')
            for child in sorted(obs.children):
                child_path = context(path, child)
                if paths.get(child_path, _EMPTY).xsi_typed:
                    # тип задаётся в экземпляре через xsi:type; чтобы он был
                    # «validly derived», объявленный тип обязан быть anyType
                    w(f'        <xs:element name="{child}" type="xs:anyType" nillable="true"/>')
                else:
                    w(
                        f'        <xs:element name="{child}" type="{type_name(child_path)}" nillable="true"/>'
                    )
            if obs.foreign_children:
                # список полный, без отсечки: он единственная запись о том, что
                # здесь встречалось, и схема читается назад по нему же
                foreign = ", ".join(sorted(obs.foreign_children))
                w(f"        <!-- чужие пространства имён: {foreign} -->")
                w('        <xs:any namespace="##other" processContents="skip"/>')
            w("      </xs:choice>")
            w("    </xs:sequence>")
            attrs("    ")
        else:
            # лист: текст плюс атрибуты
            w("    <xs:simpleContent>")
            w('      <xs:extension base="xs:string">')
            attrs("        ")
            w("      </xs:extension>")
            w("    </xs:simpleContent>")
        w("  </xs:complexType>")
        w("")
    w("</xs:schema>")
    return "\n".join(out) + "\n"


#: Прежние схемы называли это путём и писали его целиком; нынешние пишут
#: контекст — последние CONTEXT имён. Читаются оба: прочитанное усекается до
#: контекста, и накопленное прежними прогонами переживает смену тождества типа
_DOC_PATH = re.compile(r"^(?:контекст|путь):\s*([^;\s]+)")
_FOREIGN_LIST = re.compile(r"чужие пространства имён:\s*(.*)")
_UPDATED_LINE = re.compile(r"^  Дата (?:последнего пополнения|порождения): .*$", re.M)


def local_type(value: str | None) -> str:
    return (value or "").rsplit(":", 1)[-1]


def documented_path(node) -> str | None:
    """Путь из аннотации типа; None — если тип не путевой."""
    for sub in node.iter():
        if isinstance(sub.tag, str) and qname(sub.tag)[1] == "documentation":
            m = _DOC_PATH.match((sub.text or "").strip())
            return m.group(1) if m else None
    return None


def read_back(text: str, target_ns: str = MDC, root_tag: str = "MetaDataObject") -> Model:
    """Схема обратно в модель наблюдений.

    Это не разбор XSD вообще, а чтение собственного вывода: восстанавливается
    ровно то, из чего `emit` его строил. Обратное преобразование обязано быть
    точным — на нём держится накопление: то, что не прочитается, при следующем
    прогоне пропадёт из схемы.

    Имена типов чужих пространств не восстанавливаются намеренно: в схеме их
    нет, они живут в `stubs/`, где сливаются своим порядком. Восстанавливается
    сам факт импорта — этого достаточно, чтобы импорт пережил слияние.
    """
    if not HAVE_LXML:  # pragma: no cover
        raise RuntimeError("для чтения схемы назад нужен lxml: без него теряются комментарии")
    model = Model()
    truncated = 0
    for node in ET.fromstring(text.encode("utf-8")):
        if not isinstance(node.tag, str):
            continue
        _, tag = qname(node.tag)
        if tag == "import":
            model.xsi_types.setdefault(node.get("namespace") or "", set())
        elif tag == "element" and node.get("name") == root_tag:
            if local_type(node.get("type")) == "anyType":
                # схема прежнего вида: корень объявлен anyType, потому что нёс
                # xsi:type. Какими именно типами он себя называл, там не
                # записано — это восстановит первый же прогон по корпусу
                model.paths[root_tag].xsi_typed = True
        elif tag == "complexType":
            path = documented_path(node)
            if path is None:
                # тип своего пространства, названный через xsi:type; расширение
                # типа корня означает, что так называет себя сам корень
                name = node.get("name") or ""
                model.xsi_types[target_ns].add(name)
                for sub in node.iter():
                    if isinstance(sub.tag, str) and qname(sub.tag)[1] == "extension":
                        if local_type(sub.get("base")) == type_name(root_tag):
                            model.root_types.add(name)
                continue
            path = ".".join(path.split(".")[-CONTEXT:])
            obs = model.paths[path]
            for sub in node.iter():
                if not isinstance(sub.tag, str):  # комментарий
                    m = _FOREIGN_LIST.search((sub.text or "").strip())
                    if m:
                        listed = m.group(1).strip()
                        if listed.endswith("…"):
                            # схема прежнего формата: список был усечён, и то,
                            # что за многоточием, не восстановить
                            truncated += 1
                            listed = listed[:-1].rstrip(", ")
                        obs.foreign_children |= {n for n in listed.split(", ") if n}
                    continue
                _, sub_tag = qname(sub.tag)
                if sub_tag == "attribute":
                    obs.attributes.add(sub.get("name") or "")
                elif sub_tag == "element":
                    child = sub.get("name") or ""
                    obs.children.add(child)
                    if local_type(sub.get("type")) == "anyType":
                        model.paths[context(path, child)].xsi_typed = True
    if truncated:
        print(
            f"внимание: в прежней схеме {truncated} усечённых списков чужих детей —"
            " то, что было за многоточием, накоплению недоступно",
            file=sys.stderr,
        )
    return model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--namespace", default=MDC, help="пространство имён формата (по умолчанию MDClasses)"
    )
    ap.add_argument(
        "--root-tag",
        default="MetaDataObject",
        help="корневой элемент формата (по умолчанию MetaDataObject)",
    )
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="строить с нуля, отбросив накопленное в существующей схеме",
    )
    args = ap.parse_args()

    roots = [r for r in args.roots if r.is_dir()]
    if not roots:
        print("нет ни одного существующего корня", file=sys.stderr)
        return 2

    observed, files = collect(roots, args.version, args.namespace, args.root_tag)
    if not files:
        print(f"не найдено ни одного {args.root_tag} версии {args.version}", file=sys.stderr)
        return 1

    old_text = args.out.read_text(encoding="utf-8") if args.out.is_file() else None
    kept = None
    merged = Model()
    if old_text is not None and not args.fresh:
        kept = read_back(old_text, args.namespace, args.root_tag)
        merged.absorb(kept)
    merged.absorb(observed)

    if kept is not None:
        # что дала выгрузка, видно только по итогу слияния: корпус, где чего-то
        # не встретилось, расходится с накопленным, но ничего к нему не убавляет
        added = set(merged.paths) - set(kept.paths)
        grown = [p for p in kept.paths if merged.paths[p] != kept.paths[p]]
        print(f"накоплено ранее  : {len(kept.paths)} путей")
        print(f"новых путей      : {len(added)}")
        print(f"пополнено путей  : {len(grown)}")

    updated = date.today().isoformat()
    xsd = emit(merged, args.version, updated, args.namespace, args.root_tag)
    # Дата — единственное, что меняется само по себе, поэтому сравнение идёт
    # без неё: прогон, ничего не добавивший, файла не трогает, и дата остаётся
    # днём, когда схема действительно пополнилась
    if old_text is not None and _UPDATED_LINE.sub("", xsd) == _UPDATED_LINE.sub("", old_text):
        print(f"схема не изменилась: {args.out}")
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(xsd, encoding="utf-8")
        print(f"записано         : {args.out}")

    # заглушки типов чужих пространств имён, на которые ссылается xsi:type.
    # Пополняются и тогда, когда сама схема не изменилась: новый тип в уже
    # импортированном пространстве схему не трогает, а заглушку — обязан
    by_ns = {
        ns: names
        for ns, names in observed.xsi_types.items()
        # своё пространство исключаем: его типы объявлены в самой схеме,
        # импортировать собственный targetNamespace нельзя
        if names and ns and ns not in (XSD, args.namespace)
    }
    if by_ns:
        stub_dir = args.out.parent / "stubs"
        stub_dir.mkdir(exist_ok=True)
        touched = 0
        for ns, names in sorted(by_ns.items()):
            path = stub_dir / ns_file(ns)
            # Заглушки общие для всех форматов: каталог один, а прогонов много.
            # Перезапись выкидывала бы типы, нужные другим схемам, — так
            # порождение logform стёрло DesignTimeRef, и MetaDataObject
            # перестал проходить на 13 файлах из 400. Поэтому СЛИЯНИЕ.
            was = path.read_text(encoding="utf-8") if path.is_file() else None
            if was is not None:
                names = names | set(re.findall(r'complexType name="([^"]+)"', was))
            stub = emit_stub(ns, names)
            if stub != was:
                path.write_text(stub, encoding="utf-8")
                touched += 1
        print(f"заглушек чужих ns: {len(by_ns)}, из них переписано {touched}")

    print(f"путей элементов  : {len(merged.paths)}")
    print(f"строк в схеме    : {xsd.count(chr(10))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
