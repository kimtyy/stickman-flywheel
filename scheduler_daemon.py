"""스틱맨 플라이휠 무인 스케줄러 데몬 래퍼 (루트 디렉터리 실행 지원)."""
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
FW_DIR = ROOT_DIR / "stickman_flywheel"

if str(FW_DIR) not in sys.path:
    sys.path.insert(0, str(FW_DIR))

# stickman_flywheel 내부 상대경로 의존성을 안전하게 보장
os.chdir(FW_DIR)

import scheduler_daemon

if __name__ == "__main__":
    sys.exit(scheduler_daemon.main())
