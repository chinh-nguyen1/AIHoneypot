import csv
import json
import pathlib
import random
import sys

csv.field_size_limit(min(sys.maxsize, 10_000_000))

BASE_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_BASE_CSV = BASE_DIR / "data" / "raw" / "dataset.csv"
DEFAULT_STAGING_DIR = BASE_DIR / "data" / "kaggle_staging"
DEFAULT_OUTPUT_CSV = BASE_DIR / "data" / "raw" / "dataset_full.csv"

LABEL_MAP = {
    "normal": "Normal",
    "norm": "Normal",
    "benign": "Normal",
    "valid": "Normal",
    "0": "Normal",
    "sqli": "SQLi",
    "sql": "SQLi",
    "sql injection": "SQLi",
    "sql_injection": "SQLi",
    "xss": "XSS",
    "cross-site scripting": "XSS",
    "cross_site_scripting": "XSS",
    "cmdi": "CmdI",
    "command injection": "CmdI",
    "command_injection": "CmdI",
    "os command injection": "CmdI",
    "pathtraversal": "PathTraversal",
    "path-traversal": "PathTraversal",
    "path_traversal": "PathTraversal",
    "path traversal": "PathTraversal",
    "directory traversal": "PathTraversal",
    "lfi": "PathTraversal",
}

INVALID_TOKENS = {"", "nan", "none", "null", "n/a", "na"}
PARAM_KEYS = ["id", "q", "search", "username", "file", "page", "cmd", "item"]


def wrap_pipeline_json(raw_payload: str, idx: int) -> str:
    text = raw_payload.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and (
                "query_params" in parsed or "body_params" in parsed
            ):
                return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            pass
    key = PARAM_KEYS[idx % len(PARAM_KEYS)]
    obj = {"query_params": {key: text}, "body_params": {}}
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def infer_label_from_filename(filename: str) -> str | None:
    stem = filename.lower()
    for k, v in LABEL_MAP.items():
        if k in {"0", "norm", "sql"}:
            continue
        if k in stem:
            return v
    return None


def open_csv_with_fallback(path: pathlib.Path):
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with path.open("r", encoding=enc, newline="") as f:
                f.read(4096)
            return path.open("r", encoding=enc, errors="replace", newline="")
        except UnicodeDecodeError:
            continue
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def build_clean_dataset(
    base_csv: pathlib.Path = DEFAULT_BASE_CSV,
    staging_dir: pathlib.Path = DEFAULT_STAGING_DIR,
    output_csv: pathlib.Path = DEFAULT_OUTPUT_CSV,
    max_per_class: int = 900,
    seed: int = 42,
) -> None:
    rng = random.Random(seed)
    buckets: dict[str, set[str]] = {
        "Normal": set(),
        "SQLi": set(),
        "XSS": set(),
        "CmdI": set(),
        "PathTraversal": set(),
    }

    if base_csv.exists():
        with open_csv_with_fallback(base_csv) as f:
            for idx, row in enumerate(csv.DictReader(f)):
                payload = (row.get("payload") or "").strip()
                label = LABEL_MAP.get((row.get("label") or "").strip().lower())
                if (
                    payload.lower() not in INVALID_TOKENS
                    and label in buckets
                    and 1 <= len(payload) <= 2000
                ):
                    buckets[label].add(wrap_pipeline_json(payload, idx))

    staging_files = sorted(staging_dir.glob("*.csv")) if staging_dir.exists() else []
    if not staging_files:
        print(f"[INFO] No extra CSV files found in {staging_dir}")
    for csv_file in staging_files:
        file_fallback_label = infer_label_from_filename(csv_file.stem)
        with open_csv_with_fallback(csv_file) as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                continue
            valid_headers = [
                h for h in reader.fieldnames if h and not h.lower().startswith("unnamed")
            ]
            if not valid_headers:
                continue
            cols = {c.lower().strip(): c for c in valid_headers}
            payload_col = next(
                (
                    cols[k]
                    for k in ("payload", "sentence", "query", "value", "text", "request")
                    if k in cols
                ),
                valid_headers[0],
            )
            label_col = next(
                (
                    cols[k]
                    for k in ("label", "attack_type", "type", "class", "category", "attack")
                    if k in cols
                ),
                valid_headers[-1] if len(valid_headers) >= 2 else None,
            )

            added_from_file = 0
            for idx, row in enumerate(reader):
                raw_p = (row.get(payload_col) or "").strip()
                if raw_p.lower() in INVALID_TOKENS or not (1 <= len(raw_p) <= 2000):
                    continue
                raw_l = (row.get(label_col) or "").strip().lower() if label_col else ""
                if raw_l == "1" and file_fallback_label:
                    label = file_fallback_label
                elif raw_l in LABEL_MAP:
                    label = LABEL_MAP[raw_l]
                elif not label_col and file_fallback_label:
                    label = file_fallback_label
                else:
                    label = None

                if label in buckets:
                    before = len(buckets[label])
                    buckets[label].add(wrap_pipeline_json(raw_p, idx))
                    if len(buckets[label]) > before:
                        added_from_file += 1
            print(
                f"[INFO] {csv_file.name}: added {added_from_file} valid rows "
                f"(payload='{payload_col}', label='{label_col}')"
            )

    final_rows: list[tuple[str, str]] = []
    print("\n--- Cleaned & Balanced Class Distribution ---")
    for label, items in buckets.items():
        sorted_items = sorted(items)
        if len(sorted_items) > max_per_class:
            sorted_items = rng.sample(sorted_items, max_per_class)
        print(f"  - {label:14s}: {len(sorted_items)} samples")
        for p in sorted_items:
            final_rows.append((p, label))

    rng.shuffle(final_rows)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["payload", "label"])
        writer.writerows(final_rows)
    print(f"\n[OK] Exported {len(final_rows)} clean rows to {output_csv}")


if __name__ == "__main__":
    build_clean_dataset()
