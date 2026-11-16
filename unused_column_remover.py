from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Set

from config import MODEL_ROOT, REPORT_NAME


def run_detector() -> None:
    detector_path = Path(__file__).resolve().with_name("unused_column_detector.py")
    result = subprocess.run(
        [sys.executable, str(detector_path), REPORT_NAME],
        capture_output=True,
        text=True,
        cwd=str(detector_path.parent),
    )

    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)

    if result.returncode != 0:
        raise RuntimeError(f"The detector failed to run for {REPORT_NAME}.")


def normalize_name(value: str | None) -> str | None:
    if value is None:
        return None

    value = str(value).strip()
    if not value:
        return None

    value = value.replace("'", "").replace('"', "").strip()
    value = value.replace("[", "").replace("]", "").strip()

    return value or None


def find_latest_usage_report() -> Path:
    matches = sorted(
        Path().glob(f"{REPORT_NAME}_column_usage*.txt"),
        key=lambda p: p.name,
    )

    if not matches:
        raise FileNotFoundError(
            f"No usage report found for {REPORT_NAME}. Run the detector first."
        )

    return matches[-1]


def parse_unused_columns(report_path: Path) -> Dict[str, Set[str]]:
    rows: DefaultDict[str, Set[str]] = defaultdict(set)

    current_table: str | None = None
    current_status: str | None = None

    for line in report_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()

        if stripped.startswith("Table:"):
            current_table = stripped[len("Table:") :].strip()
            current_status = None
            continue

        if stripped == "UNUSED":
            current_status = "UNUSED"
            continue

        if stripped == "RELATIONSHIP_ONLY":
            current_status = "RELATIONSHIP_ONLY"
            continue

        if stripped.startswith("-") and current_table:
            column_name = stripped[1:].strip()
            if current_status == "UNUSED":
                rows[current_table].add(column_name)

    return {table: columns for table, columns in rows.items()}


def get_table_name_from_tmdl(file_path: Path) -> str | None:
    for line in file_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("table "):
            raw_name = stripped[len("table ") :].strip()
            return normalize_name(raw_name) or raw_name
    return None


def build_remove_columns_expression(column_names: Iterable[str]) -> str:
    cleaned = []
    for column in column_names:
        normalized = normalize_name(column)
        if normalized:
            cleaned.append(f'"{normalized.replace("\"", "\"\"")}"')
    return "{" + ", ".join(cleaned) + "}"


def count_keyword_occurrences(line: str, keyword: str) -> int:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])", re.IGNORECASE)
    return len(pattern.findall(line))


def leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def find_power_query_block(lines: List[str]) -> tuple[int, int, int] | None:
    """Find the outer TMDL `source = let ... in` block using the exact source/let/in pattern."""
    for source_idx, line in enumerate(lines):
        if not line.strip().startswith("source ="):
            continue

        let_idx = None
        for j in range(source_idx + 1, len(lines)):
            if lines[j].strip() == "let":
                let_idx = j
                break
        if let_idx is None:
            continue

        let_indent = leading_whitespace(lines[let_idx])
        for k in range(let_idx + 1, len(lines)):
            if lines[k].strip() == "in" and leading_whitespace(lines[k]) == let_indent:
                return source_idx, let_idx, k

        return None

    return None


def remove_columns_from_table(table_file: Path, columns_to_remove: Set[str]) -> bool:
    if not columns_to_remove:
        return False

    lines = table_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    block = find_power_query_block(lines)
    if block is None:
        return False

    _, let_idx, in_idx = block

    for line in lines[let_idx:in_idx + 1]:
        if "Crushed Columns" in line or "Crush Columns" in line:
            return False

    clean_columns = sorted({normalize_name(col) or col for col in columns_to_remove if normalize_name(col)})
    if not clean_columns:
        return False

    prev_step_idx = None
    for i in range(in_idx - 1, let_idx, -1):
        stripped = lines[i].strip()
        if stripped and "=" in stripped and not stripped.startswith("let"):
            prev_step_idx = i
            break

    if prev_step_idx is None:
        return False

    prev_line = lines[prev_step_idx]
    prev_text = prev_line.strip()
    if "=" not in prev_text:
        return False

    prev_indent = leading_whitespace(prev_line)
    let_indent = leading_whitespace(lines[let_idx])
    prev_step_name = prev_text.split("=", 1)[0].strip()
    step_name = '#"Crushed Columns"'

    lines[prev_step_idx] = prev_line.rstrip() + ","

    remove_step = (
        f'{prev_indent}{step_name} = Table.RemoveColumns({prev_step_name}, '
        f'{build_remove_columns_expression(clean_columns)}, MissingField.Ignore)'
    )

    lines.insert(in_idx, remove_step)
    lines[in_idx + 1] = f'{let_indent}in'
    lines[in_idx + 2] = f'{prev_indent}{step_name}'

    table_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def main() -> None:
    run_detector()

    try:
        report_path = find_latest_usage_report()
    except FileNotFoundError as exc:
        print(str(exc))
        print("Run the column detector first to generate a usage report before running the remover.")
        return

    unused_by_table = parse_unused_columns(report_path)

    if not unused_by_table:
        print(f"No unused columns found in {report_path.name}.")
        return

    tables_dir = MODEL_ROOT / "definition" / "tables"
    changed_tables: List[str] = []

    for table_file in sorted(tables_dir.glob("*.tmdl")):
        table_name = get_table_name_from_tmdl(table_file)
        if not table_name:
            continue

        columns_to_remove = unused_by_table.get(table_name, set())
        if not columns_to_remove:
            continue

        if remove_columns_from_table(table_file, columns_to_remove):
            changed_tables.append(table_name)

    print("COLUMN REMOVER SUMMARY")
    print("=" * 60)
    print(f"Usage report: {report_path.name}")
    print(f"Tables updated: {len(changed_tables)}")

    for table_name in sorted(changed_tables):
        print(f"- {table_name}")


if __name__ == "__main__":
    main()
