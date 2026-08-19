#!/usr/bin/env python3
"""Порождение правил разбора (``KindRules``) по корпусу выгрузок.

Правила отвечают на вопрос «что из найденного разбираем и каким парсером».
Один файл на вид, читается целиком::

    Rules/<Вид>.json

Что откуда берётся::

    перечень ключей   обходом корпуса, регенерация — перезапись
    метки возврата    тем же обходом: где перечень начинается заново
    решения           принимает человек, регенерация — слияние

Корпус — единственный источник перечня, и промежуточного файла между ним и
правилами нет: перечень, который нельзя проверить по выгрузкам, проверить нечем,
а у того, кто перегенерирует правила, выгрузки есть по условию.

Решения живут в самих файлах правил и больше нигде: генератор их не знает и не
подставляет. Прогон читает существующий файл вида, перезаписывает по корпусу
перечень ключей и переносит в него записанные решения.

Состояний у ключа три: имя парсера — разбираем вот чем, ``NopParser`` — решено
не разбирать, ``null`` — решение не принято. Последнее перечисляется при каждой
перегенерации и роняет её: перечень машинный, решение по каждой записи — за
человеком.

В правилах нет счётчиков, нет папок для ``node_files`` (путь и есть ключ) и нет
указания, куда писать: это свойство парсера, а не ключа. Есть ``kind`` и
``$schema_version`` — файл объявляет, что он такое.

Использование::

    python util/derive_kind_rules.py \
        --version 2.20 \
        --out-dir 2.20/Rules \
        /путь/к/конфигурации/src /путь/к/расширению
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import schema_paths
from derive_inventory import build_observations, scan

# ---------------------------------------------------------------------------
# Решений здесь нет: они живут в файлах правил, генератор их только переносит.
#
# Состояний у ключа три, и путать их нельзя:
#   parser: "<Имя>"     решено разбирать, вот чем
#   parser: "NopParser" решено НЕ разбирать: содержимое не интересует
#   parser: null        решение не принято — это дыра, её показывает проверка
#
# Ключ, встретившийся в корпусе впервые, появляется с parser=null у каждого
# вида, где встретился. Одинаковое решение для многих видов записывается в
# каждый из этих файлов: единственный источник решения — файл вида.
# ---------------------------------------------------------------------------

SECTIONS = ("children_inline", "children_external", "node_files")
DECIDED_KEYS = ("parser", "member_kind", "ref_kind", "type")

README = """# Правила разбора

Файл на вид: что у него встречается и какое решение действует.

Перечень порождается обходом корпуса, решения принимает человек.

## Три состояния ключа

| Значение | Смысл |
|---|---|
| `parser: "<Имя>"` | решено разбирать, вот чем |
| `parser: "NopParser"` | решено **не** разбирать: содержимое не интересует |
| `parser: null` | решение **не принято** — дыра, её показывает перегенерация |

Различать второе и третье обязательно: без явного отказа «не смотрели» и
«посмотрели и не нужно» выглядят одинаково, и проверка перестаёт что-либо
значить. `NopParser` при этом не пропускает файл мимо: он посещается и
вычёркивается, поэтому остаток остаётся сигналом.

## Секции

Все дети из `<ChildObjects>` — узлы; различает их то, где лежит тело. Признак
машинный: либо у объявления есть `uuid` и `Properties`, либо только имя текстом.

| Секция | Тело ребёнка | Пример |
|---|---|---|
| `children_inline` | внутри этого файла: `<Тег uuid><Properties><Name>` | `Attribute`, `TabularSection`, `Command` |
| `children_external` | в своём описателе, в родителе только имя текстом | `Form`, `Template` |
| `node_files` | файла никто не объявлял, путь известен точно | `Ext/ObjectModule.bsl` |

`folder` — независимая ось: есть ли у ребёнка свои файлы. В
`children_external` он обязателен (по нему ищут описатель), в `children_inline`
стоит только у тех, у кого файлы есть.

**Ключ вкладывается в ключ.** У члена бывает свой `<ChildObjects>` — реквизиты
табличной части, параметры операции, методы шаблона URL. Тогда его запись несёт
собственный раздел `children_inline` с теми же тремя состояниями:

```json
"ChildObjects/URLTemplate": {
  "parser": "AttributeParser",
  "member_kind": "url_template",
  "children_inline": {
    "ChildObjects/Method": {"parser": "AttributeParser", "member_kind": "http_method"}
  }
}
```

Своего файла у такого вида нет и быть не может: ничто в выгрузке не объявляет
себя табличной частью или методом — к ним приходят обходом от родителя, а файл
правил называется по виду, который объявляет о себе сам описатель.

**Структура содержит саму себя.** Группа формы держит свои элементы, подсистема —
свои подсистемы, и глубина ничем не ограничена, поэтому путь сворачивается на
повторе имени. Место возврата записывается меткой из двух частей: `through` —
куда спуститься от найденного элемента, `from` — с какого места перечня ключи
действуют там снова. Считается метка тем же обходом, что и ключи: разойдись они,
обход не дошёл бы до содержимого свёрнутых ветвей.

```json
"ChildItems/UsualGroup": {
  "parser": "FormMemberParser",
  "member_kind": "form_item",
  "recursive": {"through": "ChildItems", "from": "ChildItems"}
}
```

Двух частей не избежать: ключи возобновляются с `AutoCommandBar/ChildItems`, а
спускаться от группы кнопок надо всего на `ChildItems`. Обе пустые значат «тем же
ключом, каким нашлась сама» — так вложены подсистемы. Выписанный
`children_inline` метку отменяет: он точнее, потому что называет уровень.

В `children_external` решений нет, только папка: разбор задают правила того
вида, который объявит найденный файл. «Этот вид не разбираем» выражается там же,
в его собственных правилах — `NopParser` у его записей.

## Чего здесь нет

- **счётчиков** — правила говорят, что делать, а не сколько раз это встретилось;
  частоты печатает `util/derive_inventory.py` отдельным отчётом;
- **указания, куда писать** — это свойство парсера, а не ключа;
- **папок для `node_files`** — путь и есть ключ.

`kind` и `$schema_version` есть: файл объявляет, что он такое.

## Где править

Здесь же, в файле вида: он единственный источник решения. Генератор решений не
знает и ничего не подставляет, поэтому решение, одинаковое для многих видов,
записывается в каждый из этих файлов.

## Регенерация

    util/derive_kind_rules.py --version 2.20 --out-dir . <корни выгрузок>

Перечень ключей перезаписывается по корпусу, решения переносятся из прежней
редакции файла. Ключ, по которому решение было принято, но который исчез из
корпуса, помечается `absent_in_corpus: true` — молча не удаляется. Прежние
выгрузки при этом не нужны: решения берутся из правил, а не из корпуса.

## Проверка

Перегенерация перечисляет ключи без решения и завершается ненулевым кодом, пока
такие есть: перечень порождается машинно, а решение по каждой записи обязан
принять человек. `--allow-undecided` снимает отказ, но не перечисление.
"""


def collect(doc: dict) -> tuple[dict, dict, dict]:
    """-> (все ключи по секциям, ключи по видам)."""
    all_keys: dict[str, set[str]] = {s: set() for s in SECTIONS}
    per_kind: dict[str, dict[str, set[str]]] = {}
    # папка ребёнка — наблюдение, а не решение; едет в записи children_*
    folders: dict[tuple[str, str], str] = {}
    # ключи внутри члена: (вид, ключ) -> вложенные ключи
    nested: dict[tuple[str, str], set[str]] = {}
    # ключ -> (через, якорь): куда спуститься внутри найденного элемента и с
    # какого места перечня ключи действуют там снова
    recursive: dict[tuple[str, str], tuple[str, str]] = {}
    for kind, node in doc["nodes"].items():
        keys: dict[str, set[str]] = {s: set() for s in SECTIONS}
        # Тело внутри родителя. Файлы при этом у ребёнка могут быть (команда),
        # а могут не быть (реквизит) — это вторая, независимая ось
        for tag in node.get("members", {}):
            keys["children_inline"].add(f"ChildObjects/{tag}")
        # у члена бывает свой <ChildObjects>: табличная часть с реквизитами,
        # операция с параметрами, шаблон URL с методами. Ключ вкладывается в
        # ключ — так же, как элемент вложен в элемент
        for tag, kids in node.get("member_children", {}).items():
            key = f"ChildObjects/{tag}"
            keys["children_inline"].add(key)
            nested[(kind, key)] = {f"ChildObjects/{sub}" for sub in kids}
        for tag, info in node.get("inline_nodes", {}).items():
            key = f"ChildObjects/{tag}"
            keys["children_inline"].add(key)
            folders[(kind, key)] = info["folder"]
        # Содержимое самоописывающегося файла чужого пространства: именованные
        # узлы формы и графической схемы. Путь абсолютный и свёрнут только в
        # точке возврата, которую назвала схема, — потому перечень конечен, а
        # разные дороги к одному элементу остаются разными ключами
        for path in node.get("inner", {}):
            keys["children_inline"].add(path)
        # метка возврата — из того же обхода, что и ключи: где обход, найдя
        # элемент, начинает перечень заново
        for key, mark in node.get("inner_recursive", {}).items():
            recursive[(kind, key)] = (mark.get("through", ""), mark.get("from", ""))
        # Свойство описателя, значение которого — адрес: обработчик задания,
        # подписка на событие, форма по умолчанию. Ключ — путь до свойства,
        # секция та же: обход разбирает путь и не знает, чем сегмент был
        for prop in node.get("properties", {}):
            keys["children_inline"].add(f"Properties/{prop}")
        # Тело в отдельном описателе
        for tag, info in node.get("children", {}).items():
            key = f"ChildObjects/{tag}"
            keys["children_external"].add(key)
            folders[(kind, key)] = info["folder"]
        for rel in node.get("node_files", {}):
            keys["node_files"].add(rel)
        per_kind[kind] = keys
        for s in SECTIONS:
            all_keys[s] |= keys[s]
    return all_keys, per_kind, folders, nested, recursive


def carry_decision(prev: dict, entry: dict) -> bool:
    """Перенести решение из прежней редакции записи. -> перенесено ли."""
    decided = {k: v for k, v in prev.items() if k in DECIDED_KEYS and v is not None}
    if not decided:
        return False
    entry.update(decided)
    return True


def build_kind(
    kind: str,
    keys: dict[str, set[str]],
    old: dict,
    version: str,
    folders: dict,
    nested: dict,
    recursive: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> tuple[dict, int, int]:
    """Файл вида целиком: что встречается и какое решение действует.

    Перечень ключей — из корпуса, решения — из прежней редакции этого же файла.
    Ключ, которого в ней не было, появляется без решения.

    -> (правило, сохранено решений, записей вне корпуса)
    """
    kept = gone = 0
    recursive = recursive or {}
    # Файл объявляет, что он такое, — тот же принцип, по которому вид и версия
    # берутся из самого XML выгрузки, а не из его пути. Имя файла — это
    # расположение, а не данные; при расхождении с ним верно содержимое, а само
    # расхождение — сигнал. Для составных видов (extrnprops.Help) это ещё и
    # снимает разбор имени файла: вид сам содержит точку.
    rule: dict = {"$schema_version": version, "kind": kind}
    for section in SECTIONS:
        old_sec = old.get(section, {}) if isinstance(old.get(section), dict) else {}

        out: dict = {}
        for key in sorted(keys[section]):
            entry: dict = {}
            folder = folders.get((kind, key))
            if section == "children_external":
                # тело ребёнка в своём файле: нужна только папка. Решения здесь
                # нет вовсе — разбор задают правила того вида, который объявит
                # найденный файл, а «не разбирать» выражается там же: parser
                # null у его записей
                entry["folder"] = folder or ""
            else:
                entry["parser"] = None
                # перечень внутри этого ключа начинается заново, и схема
                # называет оба нужных факта: `through` — куда спуститься от
                # найденного элемента, `from` — с какого места перечня ключи
                # там действуют снова. У подсистемы оба пусты: она содержит
                # подсистему тем же ключом, что и корень
                if (kind, key) in recursive:
                    through, anchor = recursive[(kind, key)]
                    entry["recursive"] = {"through": through, "from": anchor}
                # у ребёнка с телом внутри файлы всё равно могут быть: команда
                # разбирается здесь, а её модуль лежит в Commands/<Имя>/
                if folder:
                    entry["folder"] = folder
            # решение, записанное в файл, переживает регенерацию
            prev = old_sec.get(key, {})
            if carry_decision(prev, entry):
                kept += 1
            # содержимое члена — те же ключи этажом ниже, с теми же тремя
            # состояниями. Отдельного файла вида у него нет и быть не может:
            # ничего в выгрузке не объявляет себя табличной частью, к ней
            # приходят обходом от родителя
            sub_keys = nested.get((kind, key), set())
            prev_sub = prev.get("children_inline")
            prev_sub = prev_sub if isinstance(prev_sub, dict) else {}
            sub_out: dict = {}
            for sub_key in sorted(sub_keys):
                sub_entry: dict = {"parser": None}
                if carry_decision(prev_sub.get(sub_key, {}), sub_entry):
                    kept += 1
                sub_out[sub_key] = sub_entry
            for sub_key, sub_prev in prev_sub.items():
                if sub_key in sub_out:
                    continue
                if any(sub_prev.get(k) is not None for k in DECIDED_KEYS):
                    sub_out[sub_key] = {**sub_prev, "absent_in_corpus": True}
                    gone += 1
            if sub_out:
                entry["children_inline"] = dict(sorted(sub_out.items()))
            out[key] = entry

        # запись пропала из корпуса, но решение по ней было — не удаляем молча
        for key, prev in old_sec.items():
            if key in out:
                continue
            if any(prev.get(k) is not None for k in DECIDED_KEYS):
                out[key] = {**prev, "absent_in_corpus": True}
                gone += 1

        if out:
            rule[section] = dict(sorted(out.items()))
    return rule, kept, gone


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+", type=Path, help="корни выгрузок корпуса")
    ap.add_argument("--version", required=True, help="версия схемы выгрузки, например 2.20")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument(
        "--schema-dir",
        type=Path,
        help="каталог схем поколения (по умолчанию — родительский для --out-dir)",
    )
    ap.add_argument(
        "--allow-undecided",
        action="store_true",
        help="не падать на ключах без решения (перечислены будут всё равно)",
    )
    args = ap.parse_args()

    roots = [r for r in args.roots if r.is_dir()]
    if not roots:
        print("нет ни одного существующего корня", file=sys.stderr)
        return 2

    # виды, в которые обход заходит: у них есть файл правил. Внутрь
    # самоописывающегося файла перечень смотрит только у них
    entered = {p.stem for p in args.out_dir.glob("*.json")}
    nodes, total = scan(roots, args.version, entered)
    if not total:
        print(f"не найдено описателей версии {args.version}", file=sys.stderr)
        return 1
    # Форму ключа-папки задаёт принятое решение, а не число наблюдений: папка,
    # по которой решение записано, остаётся одним ключом. Иначе она то
    # сворачивается, то рассыпается на порождённые имена — в зависимости от
    # того, сколько объектов этого вида попало в корпус
    decided_folders: dict[str, set[str]] = {}
    for path in sorted(args.out_dir.glob("*.json")):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        keys = {k for k, e in (old.get("node_files") or {}).items() if e.get("parser")}
        if keys:
            decided_folders[old.get("kind", path.stem)] = keys

    doc = build_observations(nodes, args.version, total, roots, decided_folders)
    version = args.version
    print(f"корпус: корней {len(roots)}, описателей {total}, видов {len(nodes)}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_keys, per_kind, folders, nested, recursive = collect(doc)

    kept = gone = 0
    decided_count = 0
    undecided: dict[str, list[str]] = {}
    for kind, keys in sorted(per_kind.items()):
        if not any(keys.values()):
            continue
        kpath = args.out_dir / f"{kind}.json"
        old_kind = json.loads(kpath.read_text(encoding="utf-8")) if kpath.is_file() else {}
        rule, k, g = build_kind(kind, keys, old_kind, version, folders, nested, recursive)
        kept += k
        gone += g
        for section in SECTIONS:
            for key, e in rule.get(section, {}).items():
                # вложенные ключи считаются наравне: решение по ним такое же
                for path, entry in [(key, e)] + [
                    (f"{key}/{sub}", se) for sub, se in (e.get("children_inline") or {}).items()
                ]:
                    if entry.get("parser"):
                        decided_count += 1
                    elif section != "children_external":
                        # в children_external парсера нет по устройству: разбор
                        # задают правила того вида, который объявит найденный файл
                        undecided.setdefault(kind, []).append(f"{section}/{path}")
        kpath.write_text(json.dumps(rule, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (args.out_dir / "README.md").write_text(README, encoding="utf-8")

    # Схема поколения ключей не задаёт — их задаёт корпус, и только он видит
    # содержимое, закрытое в схеме заглушкой ``xsi:type``. Она отвечает на
    # другой вопрос: подтверждает ли формат путь, по которому принято решение.
    # Неподтверждённый путь законен (заглушка), но его стоит видеть: так же
    # выглядел бы ключ, доставшийся от другого поколения
    schema_dir = args.schema_dir or args.out_dir.parent
    unconfirmed: dict[str, list[str]] = {}
    for kind, keys in sorted(per_kind.items()):
        paths = schema_paths.for_kind(schema_dir, kind)
        if paths is None:
            continue
        missing = sorted(k for k in keys["children_inline"] if k not in paths.named)
        if missing:
            unconfirmed[kind] = missing
    if unconfirmed:
        count = sum(len(v) for v in unconfirmed.values())
        print(f"схемой не подтверждено  : {count} ключей в {len(unconfirmed)} видах")

    # children_external из счёта решений исключена: парсера там нет по
    # устройству, и включать её значило бы объявить нерешённым то, что решать
    # негде. Считаются записи, а не различные ключи: один и тот же ключ у двух
    # видов — два решения
    total = sum(len(v) for k, v in all_keys.items() if k != "children_external")
    undecided_count = sum(len(v) for v in undecided.values())
    print(f"различных ключей       : {total}")
    print(f"  решение принято      : {decided_count} записей")
    print(f"  ждут решения (null)  : {undecided_count} записей")
    print(f"видов с перечнем       : {sum(1 for k in per_kind.values() if any(k.values()))}")
    if kept or gone:
        print(f"слияние: сохранено {kept}, вне корпуса {gone}")
    print(f"записано в             : {args.out_dir}")

    if undecided:
        # перечень порождается машинно, решение по каждой записи принимает
        # человек: неназванное здесь молча не разбирается и ничем не заметно
        sys.stdout.flush()  # отчёт идёт в stderr — иначе он обгонит сводку
        count = sum(len(v) for v in undecided.values())
        print(f"\nБЕЗ РЕШЕНИЯ: {count} ключей в {len(undecided)} видах", file=sys.stderr)
        for kind, keys in sorted(undecided.items()):
            for key in sorted(keys):
                print(f"    {kind:30} {key}", file=sys.stderr)
        print(
            "\nкаждому нужен парсер либо NopParser — «посмотрели, содержимое не нужно»",
            file=sys.stderr,
        )
        if not args.allow_undecided:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
