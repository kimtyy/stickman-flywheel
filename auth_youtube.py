"""YouTube 채널 연동 도구 루트 래퍼."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FW_DIR = ROOT / "stickman_flywheel"
if str(FW_DIR) not in sys.path:
    sys.path.insert(0, str(FW_DIR))
os.chdir(FW_DIR)

import auth_youtube

if __name__ == "__main__":
    auth_youtube.authenticate_channel()
