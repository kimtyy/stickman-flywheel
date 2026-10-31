"""과거 에피소드 썸네일 소급 생성: 보관된 대본 + 웹툰 컷 5 로 pipeline_outputs/past_thumbnails/ 에 일괄/개별 생성.

  python generate_past_thumbnails.py --all
  python generate_past_thumbnails.py 20261030_091105 20261031_091122
  python scheduler_daemon.py --make-thumbnail [에피소드ID]      # 같은 기능(ID 생략 시 전체)
컷 이미지가 없는 구형(웹툰 렌더러 이전) 에피소드는 건너뛴다.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional

from thumbnail_maker import OUT_DIR, PAST_THUMB_DIR, WEBTOON_DIR, make_thumbnails

EP_RE = re.compile(r"^\d{8}_\d{6}$")


def find_script(ep: str) -> Optional[Path]:
    for p in (OUT_DIR / "approved" / f"{ep}.json", OUT_DIR / "pending" / f"{ep}.json", OUT_DIR / "test_archive" / ep / "script.json"):
        if p.exists():
            return p
    return None


def eligible_episodes() -> List[str]:
    if not WEBTOON_DIR.exists():
        return []
    return sorted(d.name for d in WEBTOON_DIR.iterdir() if d.is_dir() and EP_RE.match(d.name) and (d / "cut_5.png").exists())


def run(episode_ids: Optional[List[str]] = None, out_dir: Path = PAST_THUMB_DIR) -> str:
    ids = episode_ids or eligible_episodes()
    done, skipped = [], []
    for ep in ids:
        sp = find_script(ep)
        if not sp:
            skipped.append(f"{ep}(대본 없음)")
            continue
        try:
            r = make_thumbnails(json.loads(sp.read_text(encoding="utf-8")), out_dir)
            done.append(f"{ep}: [{r['keyword']}] {r['headline']} / {r['punchline']}")
        except Exception as e:
            skipped.append(f"{ep}({type(e).__name__}: {e})")
    msg = f"썸네일 {len(done)}편 생성 -> {out_dir}"
    msg += "".join(f"\n  - {x}" for x in done)
    if skipped:
        msg += "\n  건너뜀: " + ", ".join(skipped)
    return msg


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="과거 에피소드 후킹 썸네일 소급 생성")
    ap.add_argument("episodes", nargs="*", help="에피소드 ID(생략 시 --all)")
    ap.add_argument("--all", action="store_true", help="컷 이미지가 있는 모든 에피소드")
    ap.add_argument("--out", type=Path, default=PAST_THUMB_DIR)
    a = ap.parse_args()
    if not a.episodes and not a.all:
        ap.error("에피소드 ID 를 주거나 --all 을 지정하세요")
    print(run(a.episodes or None, a.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
