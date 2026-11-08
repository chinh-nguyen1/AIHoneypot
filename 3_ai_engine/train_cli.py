"""Huấn luyện lại mô hình Local ML từ một hoặc nhiều file dataset.

Gọi trực tiếp training_pipeline.run_training_pipeline() đã có sẵn trong dự án.
Tạo một release mới trong artifacts/releases/<version>/, KHÔNG ghi đè release cũ.

Cách chạy:
    python train_cli.py data/raw/dataset.csv [data/raw/them.csv ...]
    python train_cli.py data/raw/dataset.csv --seed 123
"""
import argparse
import json
import sys

from training_pipeline import run_training_pipeline, TrainingConfig, TrainingPipelineError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Đường dẫn 1 hoặc nhiều file dataset")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (mặc định 42)")
    parser.add_argument("--version", default=None, help="Tên version tùy chỉnh (mặc định tự sinh)")
    args = parser.parse_args()

    config = TrainingConfig(random_seed=args.seed)

    try:
        result = run_training_pipeline(args.paths, config=config, version=args.version)
    except TrainingPipelineError as exc:
        print(f"[LỖI] Train thất bại: {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(f"[LỖI] Release đã tồn tại, chọn --version khác: {exc}", file=sys.stderr)
        return 1

    print("=== HUẤN LUYỆN HOÀN TẤT ===")
    print(f"Release mới: {result.release_path}")
    comparison = result.metadata.get("comparison", {})
    print(f"Mô hình tốt hơn (theo macro F1): {comparison.get('higher_macro_f1')}")
    print(json.dumps(comparison.get("per_class", {}), indent=2, ensure_ascii=False))
    print()
    print("Để dùng release này, cập nhật trong .env:")
    print(f"  AI_MODEL_RELEASE={result.release_path.name}")
    print(f"  AI_MODEL_CANDIDATE=baseline   # hoặc alternative, xem per_class ở trên để chọn")
    print("Sau đó chạy: docker compose up -d --build ai_engine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
