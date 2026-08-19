#!/usr/bin/env python3
"""Обход корпуса выгрузок: что в нём вообще встречается и сколько раз.

Исчерпывающий перечень: какие теги объявлены у какого вида, в каких папках лежат
дети, какие файлы есть в корне узла, со счётчиками.

Перечень порождается **в память** и оттуда идёт в ``derive_kind_rules.py`` —
файла между корпусом и правилами нет. Отдельный прогон этого скрипта пишет
наблюдения на диск как **отчёт для человека**: счётчики нужны, чтобы решать по
новым ключам, каким парсером их разбирать. Правила от такого отчёта не зависят,
и рантайм его не читает.

ОПИСАТЕЛЬНЫЙ: зафиксировано встретившееся, а не допустимое формой.
Пути корпуса в отчёт не попадают, только количество корней.

Использование::

    python util/derive_inventory.py \
        --version 2.20 \
        --out /tmp/observations-2.20.json \
        /путь/к/конфигурации/src /путь/к/расширению
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

try:
    from lxml import etree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET  # type: ignore

MDC = "http://v8.1c.ru/8.3/MDClasses"

# доля объявлений тега, у которых нашёлся собственный каталог, начиная
# с которой тег считается встроенным узлом, а не членом
INLINE_NODE_SHARE = 0.5

# папки, имена детей в которых порождаются конфигурацией, а не форматом:
# ключом становится сама папка, внутрь генератор не смотрит
DATA_DIRS = {"Items", "_files", "ParentConfigurations"}


def qname(tag) -> tuple[str, str]:
    if not isinstance(tag, str):
        return ("", "")
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return (ns, local)
    return ("", tag)


def child_named(elem, name: str):
    for c in elem:
        if qname(c.tag)[1] == name:
            return c
    return None


class NodeStat:
    __slots__ = (
        "observed",
        "children",
        "members",
        "member_children",
        "inner",
        "inner_marks",
        "ref_props",
        "node_files",
        "unresolved",
        "inline_nodes",
        "inline_declared",
    )

    def __init__(self) -> None:
        self.observed = 0
        # тег ребёнка -> папка -> сколько раз найдено
        self.children: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.members: dict[str, int] = defaultdict(int)
        # у члена бывает свой <ChildObjects>: табличная часть с реквизитами,
        # операция с параметрами, шаблон URL с методами. Тег члена -> тег его
        # ребёнка -> сколько раз. Без этого поля вложенное не видно вовсе:
        # член считался бы одним числом, а его содержимое не осматривалось
        self.member_children: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # путь внутри самоописывающегося файла чужого пространства -> сколько
        # раз. Форма и графическая схема объявляют своё содержимое сами, и без
        # этого поля оно не видно: обход перечня идёт по описателям MDClasses
        self.inner: dict[str, int] = defaultdict(int)
        # ключ -> (through, from): где обход, найдя элемент, начинает перечень
        # заново. Считается тем же проходом, что и пути, — иначе метки и ключи
        # разошлись бы, и содержимое свёрнутых ветвей осталось бы недостижимым
        self.inner_marks: dict[str, tuple[str, str]] = {}
        # свойство описателя -> головной сегмент значения -> сколько раз.
        # Ссылкой свойство признаётся, если голова — известный вид: так
        # отсеиваются маски кода, URL и версии, у которых точка тоже есть
        self.ref_props: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.node_files: dict[str, int] = defaultdict(int)
        # объявлен ребёнок-узел, но описателя на диске не нашлось
        self.unresolved: dict[str, int] = defaultdict(int)
        # объявлен свойствами в родителе, но имеет свой каталог с файлами
        self.inline_nodes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # сколько всего раз тег объявлен свойствами — знаменатель доли
        self.inline_declared: dict[str, int] = defaultdict(int)


def scan(
    roots: list[Path], version: str, entered: set[str] | None = None
) -> tuple[dict[str, NodeStat], int]:
    """``entered`` — виды, в которые обход заходит: у них есть файл правил.

    Только у них осматривается содержимое самоописывающегося файла: иначе в
    правила поехали бы ключи внутри файлов, которые решено не открывать. Список
    передаёт генератор правил, читающий артефакты; сам обход их не знает.

    ``None`` снимает ограничение — так работает отдельный прогон, который пишет
    отчёт: он описательный и показывает всё, что встретилось.
    """
    nodes: dict[str, NodeStat] = defaultdict(NodeStat)
    # кандидаты во встроенные узлы: подтверждаются долей после обхода
    pending: dict[str, NodeStat] = {}
    total = 0

    def subdirs(d: Path) -> list[Path]:
        try:
            return [p for p in d.iterdir() if p.is_dir()]
        except OSError:
            return []

    kind_cache: dict[str, str] = {}

    def descriptor_kind(path: Path) -> str:
        """Вид объекта, объявленный самим файлом (пустая строка — не описатель)."""
        key = str(path)
        if key in kind_cache:
            return kind_cache[key]
        kind = ""
        try:
            root = ET.parse(key).getroot()
            if qname(root.tag) == (MDC, "MetaDataObject"):
                kids = [c for c in root if isinstance(c.tag, str)]
                if len(kids) == 1:
                    kind = qname(kids[0].tag)[1]
        except Exception:
            kind = ""
        kind_cache[key] = kind
        return kind

    def handle(descriptor: Path, node_root: Path | None = None) -> None:
        """*node_root* задаётся только для корня конфигурации — см. ниже."""
        nonlocal total
        try:
            root = ET.parse(str(descriptor)).getroot()
        except Exception:
            return
        ns, local = qname(root.tag)
        if ns != MDC or local != "MetaDataObject":
            return
        if (root.get("version") or "") != version:
            return
        kids = [c for c in root if isinstance(c.tag, str)]
        if len(kids) != 1:
            return
        total += 1
        node_elem = kids[0]
        kind = qname(node_elem.tag)[1]
        stat = nodes[kind]
        stat.observed += 1

        # Свойства описателя, значение которых похоже на адрес: обработчик
        # регламентного задания, подписка на событие, форма по умолчанию.
        # Головной сегмент запоминается, а не проверяется здесь: виды известны
        # только после обхода, отбор идёт в build_observations
        own_props = child_named(node_elem, "Properties")
        for prop in own_props if own_props is not None else ():
            name = qname(prop.tag)[1]
            value = (prop.text or "").strip()
            if name and "." in value and " " not in value:
                stat.ref_props[name][value.split(".", 1)[0]] += 1

        # Корень узла = описатель без '.xml' — правило одно на всех уровнях.
        # Единственное исключение: конфигурация. Её описатель лежит В корне,
        # а не рядом с ним: дети объявлены в <root>/Catalogs/…, а папки
        # <root>/Configuration/ не существует вовсе.
        if node_root is None:
            node_root = descriptor.with_suffix("")
        child_dirs = subdirs(node_root) if node_root.is_dir() else []
        # Две разные вещи, которые нельзя держать одним множеством.
        # named_dirs — папки, занятые детьми, объявленными ИМЕНЕМ: по ним
        # запрещено искать каталог для детей, объявленных свойствами.
        # owned_dirs — папки, содержимое которых принадлежит не этому узлу:
        # они исключаются из сбора его собственных файлов. Папка встроенных
        # узлов попадает только во второе: Commands/ держит всех братьев сразу,
        # и запирать её после первой команды нельзя.
        named_dirs: set[str] = set()
        owned_dirs: set[str] = set()

        declared = child_named(node_elem, "ChildObjects")
        if declared is not None:
            # Два прохода. Сначала дети, объявленные ИМЕНЕМ: у них есть
            # описатель, и вид проверяется по нему — это надёжно. Их папки
            # занимаются. Только потом ищем каталоги для детей, объявленных
            # свойствами: иначе реквизит с именем существующего макета
            # «находится» в Templates, а табличная часть — в Forms.
            ordered = sorted(declared, key=lambda e: not (e.text or "").strip())
            for decl in ordered:
                tag = qname(decl.tag)[1]
                if not tag:
                    continue
                name = (decl.text or "").strip()
                if not name:
                    # Объявление свойствами внутри родителя (<Attribute>,
                    # <TabularSection>, <Command>…). Обычно это член без
                    # файлового представления — но не всегда: у команды есть
                    # каталог Commands/<Имя>/ с модулем, а описателя нет.
                    props = child_named(decl, "Properties")
                    inline_name = ""
                    if props is not None:
                        nm = child_named(props, "Name")
                        inline_name = (nm.text or "").strip() if nm is not None else ""
                    stat.inline_declared[tag] += 1
                    hit = None
                    if inline_name:
                        for d in child_dirs:
                            if d.name in named_dirs:
                                continue  # папка уже принадлежит детям по имени
                            if (d / inline_name).is_dir():
                                hit = d.name
                                break
                    if hit is None:
                        stat.members[tag] += 1
                        # содержимое члена: у табличной части, операции и
                        # шаблона URL есть собственный <ChildObjects>
                        own = child_named(decl, "ChildObjects")
                        for sub_decl in own if own is not None else ():
                            sub_tag = qname(sub_decl.tag)[1]
                            if sub_tag:
                                stat.member_children[tag][sub_tag] += 1
                    else:
                        # узел без описателя: данные в XML родителя, файлы свои
                        stat.inline_nodes[tag][hit] += 1
                        owned_dirs.add(hit)
                        # Вид ещё не создаём: доля считается после обхода, и
                        # одно случайное совпадение имени реквизита с именем
                        # каталога не должно порождать вид из ничего.
                        sub = pending.setdefault(tag, NodeStat())
                        sub.observed += 1
                        sub_root = node_root / hit / inline_name
                        stack = [sub_root]
                        while stack:
                            cur = stack.pop()
                            try:
                                entries = list(cur.iterdir())
                            except OSError:
                                continue
                            for e in entries:
                                if e.is_dir():
                                    stack.append(e)
                                elif e.is_file():
                                    sub.node_files[e.relative_to(sub_root).as_posix()] += 1
                    continue
                # Одного совпадения имени файла мало: планы видов расчёта и
                # регистры расчёта сплошь одноимённые, и первая же подходящая
                # папка даёт неверный ответ. Сверяем вид найденного описателя
                # с объявленным тегом — самоописание файла.
                found = None
                for d in child_dirs:
                    cand = d / f"{name}.xml"
                    if cand.is_file() and descriptor_kind(cand) == tag:
                        found = d.name
                        break
                if found is None:
                    # описателя нет: либо член, записанный текстом, либо пропажа
                    if any((node_root / d.name).is_dir() for d in child_dirs):
                        stat.unresolved[tag] += 1
                    else:
                        stat.members[tag] += 1
                    continue
                stat.children[tag][found] += 1
                named_dirs.add(found)
                owned_dirs.add(found)
                handle(node_root / found / f"{name}.xml")

        # Собственные файлы узла — с тем же правилом «описатель плюс
        # одноимённый каталог = узел», применённым РЕКУРСИВНО. Внутри корня оно
        # работает так же, как снаружи: Ext/Help.xml владеет Ext/Help/,
        # Ext/Form.xml — Ext/Form/, Ext/Picture.xml — Ext/Picture/. Без этого
        # содержимое справки приписывалось бы справочнику, и решение по
        # ru.html пришлось бы принимать в каждом из тринадцати видов со справкой.
        if node_root.is_dir():
            # Собственный описатель узла — не его ребёнок. У обычных видов он
            # лежит снаружи корня (Catalogs/X.xml против Catalogs/X/), а у
            # конфигурации корнем служит папка с описателем, и без явного
            # исключения Configuration.xml попал бы в собственные map_child.
            collect_files(node_root, node_root, stat, owned_dirs, skip_file=descriptor)

    def nested_kind(path: Path) -> str:
        """Вид вложенного узла — из самоописания файла.

        Для MDClasses это дочерний тег MetaDataObject, для прочих пространств —
        «<хвост ns>.<корневой тег>»: logform.Form — это содержимое формы, а не
        вид метаданных Form, и путать их нельзя.
        """
        try:
            root = ET.parse(str(path)).getroot()
        except Exception:
            return ""
        ns, local = qname(root.tag)
        if ns == MDC and local == "MetaDataObject":
            kids = [c for c in root if isinstance(c.tag, str)]
            return qname(kids[0].tag)[1] if len(kids) == 1 else ""
        if not local:
            return ""
        return f"{ns.rstrip('/').rsplit('/', 1)[-1]}.{local}" if ns else local

    def collect_files(
        cur: Path,
        node_root: Path,
        stat: NodeStat,
        skip_top: set[str],
        skip_file: Path | None = None,
    ) -> None:
        try:
            entries = list(cur.iterdir())
        except OSError:
            return
        files = [e for e in entries if e.is_file()]
        dirs = {e.name: e for e in entries if e.is_dir()}
        payload_dirs: set[str] = set()

        for f in files:
            if skip_file is not None and f == skip_file:
                continue
            rel = f.relative_to(node_root)
            if rel.parts and rel.parts[0] in skip_top:
                continue
            stat.node_files[rel.as_posix()] += 1
            if f.suffix != ".xml":
                continue
            kind = nested_kind(f)
            if not kind:
                continue
            sub = nodes[kind]
            sub.observed += 1
            # Содержимое такого файла объявляет он сам, и именованные узлы
            # внутри становятся ключами его вида — но только если обход в этот
            # вид заходит, то есть правила у него есть. Иначе перечислялось бы
            # содержимое файлов, которые решено не открывать: состав выгрузки,
            # командный интерфейс, предопределённые.
            if entered is None or kind in entered:
                counts, marks = named_paths(f)
                for path, n in counts.items():
                    sub.inner[path] += n
                for key, mark in marks.items():
                    sub.inner_marks.setdefault(key, mark)
            # рядом одноимённый каталог — его файлы принадлежат тому же виду
            if f.stem in dirs:
                payload_dirs.add(f.stem)
                sub_root = dirs[f.stem]
                collect_files(sub_root, sub_root, sub, set())

        for name, d in dirs.items():
            rel = d.relative_to(node_root)
            if rel.parts and rel.parts[0] in skip_top:
                continue
            if name in payload_dirs:
                continue  # содержимое принадлежит вложенному узлу
            collect_files(d, node_root, stat, skip_top)

    for root_dir in roots:
        # точки входа — описатели верхнего уровня: Configuration.xml и всё,
        # что лежит рядом; вложенные обходятся рекурсивно из handle()
        for cfg in root_dir.rglob("Configuration.xml"):
            handle(cfg, node_root=cfg.parent)

    # Встроенный узел подтверждён, если хоть у одного родителя доля объявлений
    # с собственным каталогом достаточна. Не подтверждённые кандидаты — это
    # совпадения имён, а не виды: их файлы и счётчики отбрасываются.
    confirmed = {
        tag
        for st in nodes.values()
        for tag, folders in st.inline_nodes.items()
        if st.inline_declared.get(tag, 0)
        and sum(folders.values()) / st.inline_declared[tag] >= INLINE_NODE_SHARE
    }
    for tag, sub in pending.items():
        if tag not in confirmed:
            continue
        target = nodes[tag]
        target.observed += sub.observed
        for rel, n in sub.node_files.items():
            target.node_files[rel] += n
    return nodes, total


def named_paths(path: Path) -> dict[str, int]:
    """Пути именованных узлов внутри файла, со счётчиками.

    Именем считается либо атрибут ``name`` (так объявляет форма), либо дочерний
    ``Name`` — прямой или в ``Properties`` (так объявляют графическая схема и
    предопределённые). Прочие элементы — свойства-скаляры, ключами не
    становятся.

    **Повтор имени сворачивает путь к первому его вхождению.** Группа формы
    содержит ``ChildItems``, в них снова группы, и так на любую глубину —
    перечень абсолютных путей был бы бесконечен. Свёртка идёт по самому пути и
    ни от чего больше не зависит; ровно так же её считает по графу элементов
    ``util/schema_paths.py``, откуда берутся метки рекурсии для правил. Совпасть
    эти две стороны обязаны: перечень задаёт ключи, метки — как обход входит
    внутрь найденного, и разойдись они, ключи ушли бы вглубь.

    Различать ветви свёртка не пытается, и это осознанно: решение по ключу
    зависит от вида элемента, а не от того, под какой панелью он лежит.

    Вместе с путями возвращаются **метки возврата**: где обход, найдя элемент,
    начинает перечень заново. Считаются они тем же проходом и потому не могут
    разойтись с ключами — а разойдись, обход не дошёл бы до содержимого
    свёрнутых ветвей. Метка из двух частей: ``through`` — куда спуститься от
    помеченного элемента, ``from`` — с какого места перечня ключи действуют там
    снова.

    -> (пути со счётчиками, метки: ключ -> (through, from))
    """
    try:
        root = ET.parse(str(path)).getroot()
    except Exception:
        return {}, {}
    out: dict[str, int] = defaultdict(int)
    marks: dict[str, tuple[str, str]] = {}

    def named(el) -> bool:
        if (el.get("name") or "").strip():
            return True
        if qname(el.tag)[1] == "Properties":
            # сам блок свойств не сущность: имя в нём принадлежит владельцу
            return False
        for child in el:
            if not isinstance(child.tag, str):
                continue
            local = qname(child.tag)[1]
            if local == "Name" and (child.text or "").strip():
                return True
            if local == "Properties" and child_named(child, "Name") is not None:
                return True
        return False

    def walk(el, prefix: str, holder: str) -> None:
        segments = prefix.split("/") if prefix else []
        for child in el:
            if not isinstance(child.tag, str):
                continue
            local = qname(child.tag)[1]
            if not local:
                continue
            key = f"{prefix}/{local}" if prefix else local
            is_named = named(child)
            if is_named:
                out[key] += 1
            if local in segments:
                # имя уже было в пути: ниже начинается то же самое, и спуск
                # продолжается от первого его вхождения. Метка садится на
                # ближайший именованный элемент сверху — он и есть ключ правил,
                # а путь от него до места возврата и есть `through`
                anchor = "/".join(segments[: segments.index(local) + 1])
                if holder:
                    marks.setdefault(holder, (key[len(holder) + 1 :], anchor))
                walk(child, anchor, holder)
                continue
            # сворачивается спуск, а не сам элемент: подсистема внутри
            # подсистемы считается своим ключом и лишь детей отдаёт якорю
            walk(child, key, key if is_named else holder)

    walk(root, "", "")
    return dict(out), marks


def fold_node_files(
    raw: dict[str, int],
    decided: set[str] = frozenset(),
    structural: set[str] = frozenset(),
) -> dict[str, int]:
    """Свернуть папки, содержимое которых — данные, а не формат.

    **Подстановок в ключах нет.** Ключ — либо точный путь файла, либо путь
    ПАПКИ, и тогда он значит «всё, что внутри, принадлежит узлу». Папкой ключ
    становится там, где имена детей порождаются конфигурацией: элементы формы,
    вложения справки, имена конфигураций-поставщиков, WSDL и XSD чужого
    сервиса. Перечислять их поимённо нельзя: это не описание формата, а
    содержимое конкретных выгрузок.

    Свёртка идёт по трём правилам:

    1. **По принятому решению** (``decided``) — папка, по которой решение уже
       записано в правилах, остаётся ключом, и внутрь генератор не смотрит.
       Это единственное правило, не зависящее от того, что попало в корпус:
       форму ключа задаёт человек, а не число наблюдений.
    2. **Названная папка данных** — ``DATA_DIRS``. Нужна на корпусе, по которому
       правил ещё нет: без неё первый прогон рассыпал бы элементы формы на
       тысячи ключей с порождёнными именами.
    3. **Страховка от непредусмотренного** — папка, у которой больше одного
       ребёнка, каждый встретился ровно раз и **ни одно имя не известно как
       ключ у других видов**. Последнее существенно: у вида с одним-двумя
       объектами структурные имена (``ManagerModule.bsl``, ``Help.xml``) тоже
       встречаются по разу, и без этой оговорки свёртка прятала бы модули.
    """
    out: dict[str, int] = {}

    # дерево путей с накопленными счётчиками
    tree: dict = {}
    for rel, count in raw.items():
        cur = tree
        for seg in rel.split("/"):
            cur = cur.setdefault(seg, {"$n": 0, "$kids": {}})
            cur["$n"] += count
            cur = cur["$kids"]

    def all_children_unique(kids: dict) -> bool:
        return (
            len(kids) > 1
            and all(not k["$kids"] and k["$n"] == 1 for k in kids.values())
            and not (set(kids) & structural)
        )

    def walk(kids: dict, prefix: str) -> None:
        for seg, node in kids.items():
            path = f"{prefix}/{seg}" if prefix else seg
            if not node["$kids"]:
                out[path] = node["$n"]
            elif path in decided or seg in DATA_DIRS or all_children_unique(node["$kids"]):
                out[path] = node["$n"]  # ключ — папка, внутрь не смотрим
            else:
                walk(node["$kids"], path)

    walk(tree, "")
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def build_observations(
    nodes: dict[str, NodeStat],
    version: str,
    total: int,
    roots: list[Path],
    decided: dict[str, set[str]] | None = None,
) -> dict:
    """Наблюдения целиком: перечень для порождения правил и основание решений.

    Генератор правил берёт эту структуру из памяти. Записанная на диск, она
    остаётся отчётом для человека и ничьим входом не является.

    ``decided`` — ключи-папки, по которым решение уже принято, по видам: они
    остаются одним ключом, что бы ни лежало внутри. Передаёт их генератор
    правил, который единственный читает артефакты; сам по себе этот обход
    решений не знает.
    """
    decided = decided or {}
    # имя структурно, если встречается ключом у нескольких видов: так
    # `ManagerModule.bsl` отличается от `1.xsd`, который есть только у одного
    seen: dict[str, set[str]] = defaultdict(set)
    for kind, stat in nodes.items():
        for rel in stat.node_files:
            seen[rel.rsplit("/", 1)[-1]].add(kind)
    structural = {name for name, kinds in seen.items() if len(kinds) > 1}
    doc: dict = {
        "$note": (
            "Наблюдения по корпусу: перечень того, что вообще встречается, со "
            "счётчиками. Отчёт для человека, принимающего решения по ключам "
            "правил. НЕ вход и НЕ рабочий артефакт: правила порождаются прямо "
            "по корпусу, разбор идёт по Rules/, схемы читает механизм проверки."
        ),
        "$schema_version": version,
        # Пути корпуса намеренно НЕ записываются: это абсолютные пути рабочей
        # машины с именами конкретных конфигураций. Для оценки основания важна
        # широта выборки, а не чьи именно выгрузки в неё вошли
        "$corpus_roots": len(roots),
        "$descriptors": total,
        "$kinds": len(nodes),
        "nodes": {},
    }
    for kind in sorted(nodes):
        stat = nodes[kind]
        node: dict = {"observed": stat.observed}
        if stat.children:
            node["children"] = {
                tag: {
                    "folder": max(f.items(), key=lambda kv: kv[1])[0],
                    "observed": sum(f.values()),
                }
                for tag, f in sorted(stat.children.items())
            }
        inline_out: dict[str, dict] = {}
        members = dict(stat.members)
        for tag, folders in sorted(stat.inline_nodes.items()):
            matched = sum(folders.values())
            declared_n = stat.inline_declared.get(tag, matched)
            share = matched / declared_n if declared_n else 0.0
            if share >= INLINE_NODE_SHARE:
                inline_out[tag] = {
                    "folder": max(folders.items(), key=lambda kv: kv[1])[0],
                    "observed": matched,
                    "declared": declared_n,
                    "share": round(share, 3),
                }
            else:
                members[tag] = members.get(tag, 0) + matched
        if inline_out:
            node["inline_nodes"] = inline_out
        if members:
            node["members"] = dict(sorted(members.items()))
        if stat.member_children:
            node["member_children"] = {
                tag: dict(sorted(kids.items()))
                for tag, kids in sorted(stat.member_children.items())
            }
        # свойство остаётся, только если хоть одно его значение начинается видом:
        # `MethodName = CommonModule.…` остаётся, `CodeMask = @@@` и
        # `Namespace = http://…` отпадают
        ref_props = {
            name: sum(heads.values())
            for name, heads in sorted(stat.ref_props.items())
            if set(heads) & set(nodes)
        }
        if ref_props:
            node["properties"] = ref_props
        if stat.inner:
            node["inner"] = dict(sorted(stat.inner.items()))
        if stat.inner_marks:
            node["inner_recursive"] = {
                key: {"through": through, "from": anchor}
                for key, (through, anchor) in sorted(stat.inner_marks.items())
            }
        if stat.unresolved:
            node["declared_without_descriptor"] = dict(sorted(stat.unresolved.items()))
        files = fold_node_files(stat.node_files, decided.get(kind, frozenset()), structural)
        if files:
            node["node_files"] = files
        doc["nodes"][kind] = node
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    roots = [r for r in args.roots if r.is_dir()]
    if not roots:
        print("нет ни одного существующего корня", file=sys.stderr)
        return 2

    nodes, total = scan(roots, args.version)
    if not total:
        print(f"не найдено описателей версии {args.version}", file=sys.stderr)
        return 1

    inv = build_observations(nodes, args.version, total, roots)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(inv, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"описателей пройдено : {total}")
    print(f"видов узлов         : {len(nodes)}")
    print(f"отчёт               : {args.out} ({args.out.stat().st_size / 1024:.1f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
