"""Kiểm tra một hoặc nhiều file dataset có tương thích để train hay không.

Dùng trước khi train để biết dataset mới có đúng định dạng không, không ghi gì
vào artifacts/releases.

Cách chạy (bên trong container ai_engine hoặc với các thư viện đã cài):
    python check_dataset.py data/raw/dataset.csv [data/raw/dataset_moi.csv ...]
"""
import argparse
import sys

from data_import import DatasetImporter, DatasetImportError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Đường dẫn 1 hoặc nhiều file .csv/.jsonl/.ndjson/.txt")
    args = parser.parse_args()

    try:
        importer = DatasetImporter(args.paths)
        for _batch in importer.iter_batches():
            pass  # chỉ cần tiêu thụ hết để stats được cập nhật đầy đủ
    except DatasetImportError as exc:
        print(f"[LỖI] Dataset không hợp lệ: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"[LỖI] Không tìm thấy file: {exc}", file=sys.stderr)
        return 1

    stats = importer.stats
    print("=== KẾT QUẢ KIỂM TRA DATASET ===")
    print(f"Tổng số dòng đọc được:     {stats.records_read}")
    print(f"Số dòng hợp lệ:            {stats.valid_records}")
    print(f"Số dòng bị từ chối:        {stats.rejected_records}")
    print(f"  - payload rỗng:          {stats.empty_payloads}")
    print(f"  - payload trùng lặp:     {stats.duplicate_payloads}")
    print("Số lượng theo nhãn:")
    for label, count in stats.label_counts.items():
        print(f"  - {label}: {count}")

    print()
    min_count = min(stats.label_counts.values()) if stats.label_counts else 0
    if min_count < 3:
        print("[CẢNH BÁO] Mỗi nhãn cần tối thiểu 3 mẫu hợp lệ để train được. "
              f"Nhãn ít nhất hiện có {min_count} mẫu.")
        return 1
    if stats.rejected_records > 0:
        print(f"[LƯU Ý] Có {stats.rejected_records} dòng bị từ chối — xem chi tiết lý do "
              "bằng cách truyền reject_report_path khi gọi DatasetImporter nếu cần.")

    print("[OK] Dataset có thể dùng để train (đủ điều kiện tối thiểu).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
