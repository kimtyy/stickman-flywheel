"""렌더러 선택기: 일일 자동 렌더링이 쓰는 단일 진입점.

  webtoon : 이미지 생성 모델 웹툰형 쇼츠 (기본, 메인 렌더러)
  stage   : 스틱맨 스테이지 엔진 (백업)
  legacy  : 초기 스틱맨 렌더러 (최후 백업, 외부 이미지 API 불필요)

선택 우선순위: 항목의 "renderer" 필드 > 환경변수 DAEMON_RENDERER > 기본값(webtoon).
실패 시 DAEMON_RENDER_FALLBACK(쉼표 구분, 기본 "legacy") 순서로 대체 렌더를 시도한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RENDERER_NAMES = ("webtoon", "stage", "legacy")
DEFAULT_RENDERER = "webtoon"


def renderer_chain(preferred: Optional[str] = None) -> List[str]:
    first = (preferred or os.getenv("DAEMON_RENDERER", "") or DEFAULT_RENDERER).strip().lower()
    if first not in RENDERER_NAMES:
        raise ValueError(f"알 수 없는 렌더러: {first} (가능: {', '.join(RENDERER_NAMES)})")
    chain = [first]
    for n in os.getenv("DAEMON_RENDER_FALLBACK", "legacy").split(","):
        n = n.strip().lower()
        if n in RENDERER_NAMES and n not in chain:
            chain.append(n)
    return chain


def render_with(name: str, script, bgm_path=None) -> Tuple[Path, Dict[str, Any]]:
    """지정 렌더러로 렌더하고 (mp4 경로, 정보) 를 돌려준다. 결과는 항상 pipeline_outputs/rendered/<episode_id>.mp4."""
    if name == "webtoon":
        from audio_timeline import build_audio_plan
        from render_webtoon import render_webtoon
        res = render_webtoon(script, audio_plan=build_audio_plan(script), bgm_path=bgm_path)
        return res.path, {"renderer": "webtoon", "vlm_pass": res.report.get("vlm_pass"),
                          "needs_review": res.report.get("needs_review")}
    if name == "stage":
        from render_stage import render_stage
        return Path(render_stage(script, suffix="", bgm_path=bgm_path)), {"renderer": "stage"}
    if name == "legacy":
        from module3_renderer import StickmanShortRenderer
        return Path(StickmanShortRenderer().render_episode(script, bgm_path=bgm_path)), {"renderer": "legacy"}
    raise ValueError(name)


def render_episode_auto(script, preferred: Optional[str] = None, bgm_path=None, log=print) -> Tuple[Path, Dict[str, Any]]:
    """선택된 렌더러로 시도하고, 실패하면 폴백 체인을 따라 대체 렌더한다. 모두 실패하면 마지막 예외를 올린다."""
    errors: List[str] = []
    last: Optional[Exception] = None
    for name in renderer_chain(preferred):
        try:
            path, info = render_with(name, script, bgm_path)
            if errors:
                info["fallback_from"] = errors
            return path, info
        except Exception as e:  # noqa: BLE001
            last = e
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            log(f"[Renderer] {name} 렌더 실패 → 다음 렌더러 시도: {errors[-1]}")
    raise RuntimeError("모든 렌더러 실패: " + " | ".join(errors)) from last
