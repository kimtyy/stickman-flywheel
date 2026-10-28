"""상태 파일 원자적 쓰기 유틸리티 (임시 파일 + os.replace)."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union


def atomic_write_text(path: Union[str, Path], text: str, encoding: str = "utf-8") -> None:
    """같은 디렉터리의 임시 파일에 쓴 뒤 os.replace 로 교체 → 읽는 쪽은 항상 완전한 파일만 본다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Union[str, Path], data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))
