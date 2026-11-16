from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import unquote

import csv
import json
import re

from config import MODEL_ROOT, REPORT_ROOT, REPORT_NAME


SEMANTIC_MODEL_ROOT = MODEL_ROOT


@dataclass
class ColumnUsage:
    table: str
    column: str
    status: str
    locations: List[str]


def read_text(path: Path) -> str:
    return path.read_text(
        encoding="utf-8",
        errors="ignore",
    )


def normalize_name(value: str | None) -> str | None:
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    value = value.replace("'", "").replace('"', "").strip()
    value = value.replace("[", "").replace("]", "").strip()

    return value or None


def walk_json(
    obj,
    pairs: set[tuple[str, str]],
    aliases: dict[str, str],
) -> None:

    if isinstance(obj, dict):

        if (
            isinstance(obj.get("Name"), str)
            and isinstance(obj.get("Entity"), str)
        ):
            aliases[obj["Name"]] = obj["Entity"]

        def register_ref(source_ref, property_name):
            if not isinstance(source_ref, dict):
                return
            if not isinstance(property_name, str):
                return

            if "Entity" in source_ref:
                pairs.add(
                    (
                        source_ref["Entity"],
                        property_name,
                    )
                )
            elif "Source" in source_ref:
                entity = aliases.get(source_ref["Source"])
                if entity:
                    pairs.add(
                        (
                            entity,
                            property_name,
                        )
                    )

        # Handle report JSON where Column/Measure refs are split across nested
        # objects like:
        #   { "Column": { "Expression": { "SourceRef": { "Entity": ... } }, "Property": "Account" } }
        for key in ("Column", "Measure"):
            ref = obj.get(key)
            if isinstance(ref, dict):
                expr = ref.get("Expression")
                source_ref = expr.get("SourceRef") if isinstance(expr, dict) else None
                register_ref(source_ref, ref.get("Property"))

        source_ref = obj.get("SourceRef")
        property_name = obj.get("Property")
        register_ref(source_ref, property_name)

        for value in obj.values():
            walk_json(
                value,
                pairs,
                aliases,
            )

    elif isinstance(obj, list):

        for item in obj:
            walk_json(
                item,
                pairs,
                aliases,
            )


def extract_report_column_pairs() -> set[tuple[str, str]]:

    pairs: set[tuple[str, str]] = set()

    for file in REPORT_ROOT.rglob("*.json"):

        try:
            data = json.loads(read_text(file))
        except Exception:
            continue

        aliases: dict[str, str] = {}

        walk_json(
            data,
            pairs,
            aliases,
        )

    return pairs


def extract_model_tmdl_references() -> set[tuple[str, str]]:
    """Collect column references that appear in model DAX/TMDL definitions.

    We intentionally ignore a column's own declaration block when there is no
    expression, and we skip self-references in a computed column/measure block.
    We also normalize DAX-style references such as 'Date Hierarchy'[Date] and
    TableName[ColumnName] so names with spaces are matched correctly.
    """
    refs: set[tuple[str, str]] = set()
    tables_dir = SEMANTIC_MODEL_ROOT / "definition" / "tables"

    if not tables_dir.exists():
        return refs

    pattern = re.compile(
        r"(?:'(?P<table_quoted>[^']+)'|(?P<table_unquoted>[A-Za-z_][A-Za-z0-9_]*))\s*\[\s*(?P<column>[^\]]+?)\s*\]",
        re.IGNORECASE,
    )

    for table_file in tables_dir.glob("*.tmdl"):
        table_name = unquote(table_file.stem)
        lines = read_text(table_file).splitlines()
        i = 0

        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped.startswith(("measure ", "column ")):
                i += 1
                continue

            current_declared_name: str | None = None
            if stripped.startswith("column "):
                name_segment = stripped[len("column ") :].strip()
                if " = " in name_segment or name_segment.endswith("="):
                    current_declared_name = normalize_name(name_segment.split("=", 1)[0].strip())

            if " = " not in stripped and not stripped.endswith("="):
                i += 1
                continue

            indent = len(lines[i]) - len(lines[i].lstrip())
            j = i + 1

            while j < len(lines):
                next_line = lines[j]
                next_stripped = next_line.strip()

                if not next_stripped:
                    j += 1
                    continue

                next_indent = len(next_line) - len(next_line.lstrip())
                if next_indent <= indent and next_stripped.startswith(("measure ", "column ", "partition ", "hierarchy ", "calculationGroup ", "table ")):
                    break

                j += 1

            block_text = "\n".join(lines[i:j])

            for match in pattern.finditer(block_text):
                raw_table = match.group("table_quoted") or match.group("table_unquoted") or ""
                raw_column = match.group("column") or ""

                table_ref = normalize_name(raw_table)
                column_ref = normalize_name(raw_column)

                if not table_ref or not column_ref:
                    continue

                if (
                    current_declared_name
                    and table_ref == table_name
                    and column_ref == current_declared_name
                ):
                    continue

                refs.add((table_ref, column_ref))

            i = j

    return refs


def get_table_columns() -> Dict[str, Set[str]]:

    tables_dir = (
        SEMANTIC_MODEL_ROOT
        / "definition"
        / "tables"
    )

    results: Dict[str, Set[str]] = {}

    column_pattern = re.compile(
        r"^\s*column\s+['\"]?(.+?)['\"]?(?:\s*=.*)?$",
        re.IGNORECASE,
    )

    for table_file in tables_dir.glob("*.tmdl"):

        table_name = unquote(table_file.stem)

        columns: Set[str] = set()

        for line in read_text(table_file).splitlines():

            match = column_pattern.match(line)

            if not match:
                continue

            column_name = normalize_name(match.group(1))

            if column_name:
                columns.add(column_name)

        results[table_name] = columns

    return results


def get_relationship_text() -> str:

    relationship_file = (
        SEMANTIC_MODEL_ROOT
        / "definition"
        / "relationships.tmdl"
    )

    if not relationship_file.exists():
        return ""

    return read_text(relationship_file)


def classify_columns() -> List[ColumnUsage]:

    table_columns = get_table_columns()
    report_pairs = extract_report_column_pairs()
    model_pairs = extract_model_tmdl_references()
    relationship_text = get_relationship_text().lower()

    results: List[ColumnUsage] = []

    for table_name in sorted(table_columns):

        print(f"Scanning table: {table_name}")

        for column_name in sorted(table_columns[table_name]):

            found_in_report = (
                table_name,
                column_name,
            ) in report_pairs

            found_in_model = (
                table_name,
                column_name,
            ) in model_pairs

            found_in_relationship = (
                table_name.lower() in relationship_text
                and column_name.lower() in relationship_text
            )

            locations: List[str] = []

            if found_in_report:
                locations.append(".Report")

            if found_in_model:
                locations.append("model.tmdl")

            if found_in_relationship:
                locations.append("relationships.tmdl")

            if found_in_report:
                status = "USED_BY_REPORT"
            elif found_in_model:
                status = "USED_IN_MODEL"
            elif found_in_relationship:
                status = "RELATIONSHIP_ONLY"
            else:
                status = "UNUSED"

            results.append(
                ColumnUsage(
                    table=table_name,
                    column=column_name,
                    status=status,
                    locations=locations,
                )
            )

    return results


def write_summary_report(usages: List[ColumnUsage]) -> None:

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")

    output_file = Path(
        f"{REPORT_NAME}_column_usage_{timestamp}.txt"
    )

    grouped: Dict[str, Dict[str, List[str]]] = {}
    for usage in usages:
        if usage.status not in {"UNUSED", "RELATIONSHIP_ONLY"}:
            continue

        grouped.setdefault(usage.table, {"UNUSED": [], "RELATIONSHIP_ONLY": []})
        grouped[usage.table][usage.status].append(usage.column)

    with output_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        file.write(
            "POWER BI COLUMN USAGE REPORT\n"
        )

        file.write(
            "=" * 80 + "\n\n"
        )

        file.write(
            f"Report Name: {REPORT_NAME}\n"
        )
        file.write(
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        )

        if not grouped:
            file.write("No unused or relationship-only columns found.\n")
            print(f"\nWrote {output_file}")
            return

        for table_name in sorted(grouped):
            file.write(f"Table: {table_name}\n")
            file.write("-" * 40 + "\n")

            for status in ("UNUSED", "RELATIONSHIP_ONLY"):
                file.write(f"{status}\n")
                columns = sorted(grouped[table_name].get(status, []))

                if columns:
                    for column in columns:
                        file.write(f"- {column}\n")
                else:
                    file.write("None\n")

                file.write("\n")

            file.write("\n")

    print(f"\nWrote {output_file}")


def print_summary(usages: List[ColumnUsage]) -> None:

    statuses = [
        "USED_BY_REPORT",
        "USED_IN_MODEL",
        "RELATIONSHIP_ONLY",
        "UNUSED",
    ]

    total_columns = len(usages)

    print("\n" + "=" * 80)
    print("COLUMN ANALYSIS")
    print("=" * 80)

    print(f"TOTAL_COLUMNS: {total_columns}")

    for status in statuses:

        count = sum(
            1
            for usage in usages
            if usage.status == status
        )

        print(f"{status}: {count}")

    print("\n" + "=" * 80)
    print("UNUSED COLUMNS")
    print("=" * 80)

    unused_count = sum(
        1
        for usage in usages
        if usage.status == "UNUSED"
    )

    print(f"Total unused columns: {unused_count}")


def main():

    print("\n" + "=" * 80)
    print("UNUSED COLUMN DETECTOR")
    print("=" * 80)

    print(f"Report Name : {REPORT_NAME}")
    print(f"Report Path : {REPORT_ROOT}")
    print(f"Model Path  : {SEMANTIC_MODEL_ROOT}")

    usages = classify_columns()

    write_summary_report(usages)

    print_summary(usages)


if __name__ == "__main__":
    main()