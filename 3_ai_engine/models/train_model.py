import sys
from pathlib import Path


AI_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(AI_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ENGINE_ROOT))

from retrain import retrain_model


if __name__ == "__main__":
    retrain_model()