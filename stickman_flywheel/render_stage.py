"""스테이지 모드 렌더 진입점.

  python render_stage.py <episode_id>                    # 기존 승인/대기 대본을 스테이지 모드로 렌더
  python render_stage.py --topic "식당 진상 ..."          # 새 소재로 대본을 만든 뒤 렌더 (코드 수정 없음)
  python render_stage.py <episode_id> --plan-only        # 무대 계획/자산 해결까지만
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from fs_utils import atomic_write_json  # noqa: E402

PLANS_DIR = BASE_DIR / "pipeline_outputs" / "stage_plans"


def _load_script(ep: str):
    from module2_calendar_manager import APPROVED_DIR, PENDING_DIR, load_script
    for d in (APPROVED_DIR, PENDING_DIR):
        p = d / f"{ep}.json"
        if p.exists():
            return load_script(p)
    raise FileNotFoundError(ep)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_id", nargs="?")
    ap.add_argument("--topic", help="새 소재로 대본부터 생성")
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--reuse-plan", action="store_true", help="저장된 무대 계획(stage_plans/<id>.json)이 있으면 LLM 재호출 없이 사용")
    ap.add_argument("--suffix", default="_stage")
    args = ap.parse_args()

    if args.topic:
        from module1_trend_and_script import EmotionalReverseScriptGenerator
        script = EmotionalReverseScriptGenerator().generate(custom_topic=args.topic)
        print(f"[Script] 새 대본 생성: {script.episode_id} — {script.episode_title}", flush=True)
    elif args.episode_id:
        script = _load_script(args.episode_id)
    else:
        ap.error("episode_id 또는 --topic 필요")

    from asset_library import AssetLibrary
    from director import plan_stage
    from stage_engine import StageRuntime

    t0 = time.time()
    plan_path = PLANS_DIR / f"{script.episode_id}.json"
    if args.reuse_plan and plan_path.exists():
        from director_ir import StagePlan, validate_plan
        plan, warns = validate_plan(StagePlan.model_validate_json(plan_path.read_text(encoding="utf-8")), len(script.scenes))
        print(f"[Director] 저장된 계획 재사용: {plan_path.name}", flush=True)
    else:
        plan, warns = plan_stage(script)
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_json(plan_path, plan.model_dump())
    print(f"[Director] 장소={plan.location.id} 세트={[s.id for s in plan.set_pieces]} 소품={[p.id for p in plan.props]} "
          f"비트={[len(c.beats) for c in plan.cuts]} ({time.time() - t0:.0f}s)", flush=True)
    for w in warns:
        print(f"[Director:warn] {w}", flush=True)

    t1 = time.time()
    lib = AssetLibrary(log=lambda m: print(m, flush=True))
    assets = lib.resolve_plan(plan)
    print(f"[Assets] {({k: len(v) for k, v in lib.stats.items()})} ({time.time() - t1:.0f}s)", flush=True)
    print(f"[Assets] cache_hit={lib.stats['cache_hit']} generated={lib.stats['generated']} fallback={lib.stats['fallback']}", flush=True)
    if args.plan_only:
        return 0

    from module3_renderer import StickmanShortRenderer
    rt = StageRuntime(plan, assets)
    r = StickmanShortRenderer()
    r.stage_runtime = rt
    r.output_suffix = args.suffix
    r.episode_setting = plan.location.id
    t2 = time.time()
    from audio_timeline import AudioPlan, build_audio_plan
    audio = AudioPlan.load(script.episode_id) if (args.reuse_plan and AudioPlan.exists(script.episode_id)) else build_audio_plan(script, r)
    path = r.render_episode(script, audio_plan=audio)
    for w in rt.warnings:
        print(f"[Stage:warn] {w}", flush=True)
    print(f"DONE {path} ({time.time() - t2:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
