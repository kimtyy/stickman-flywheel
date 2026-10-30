"""무인 자율 루프 데몬 (APScheduler).

09:00 후보 서빙 → 15:00 리마인드 → 16:00 타임아웃 자동승인 → 승인 직후 백그라운드 렌더링
→ 17:30 유튜브 업로드 큐 소모. 실행: `python scheduler_daemon.py` (`--once` 로 1회 tick, `--run <job>` 로 단일 작업).

모든 단계는 멱등이며 시각/상태로 스스로 게이트하므로, 데몬이 중간에 재시작되어도 tick 이 밀린 작업을 따라잡는다.
시각은 module2 와 동일하게 이 PC 의 로컬 시간을 기준으로 한다.
대시보드는 이 데몬이 기록하는 daemon_status.json 과 golden_time_queue.json 을 읽기만 한다.
"""
import argparse
import os
import sys
import threading
import time as _time
import traceback
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fs_utils import atomic_write_json
from module2_calendar_manager import (
    APPROVED_DIR, CANDIDATE_SERVING_FILE, CalendarManager, get_golden_time_queue,
    is_autopilot_enabled, load_script, parse_serving_date, update_golden_queue_item,
)

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "pipeline_outputs"
RENDERED_DIR = OUT_DIR / "rendered"
DAEMON_STATUS_FILE = OUT_DIR / "daemon_status.json"

SERVE_TIME = time(9, 0)
REMIND_TIME = time(15, 0)
TIMEOUT_TIME = time(16, 0)
PUBLISH_TIME = time(17, 30)
HEARTBEAT_SEC = 15
ALIVE_THRESHOLD_SEC = 60
TICK_MINUTES = 5
ANALYTICS_HOURS = 6
MAX_RENDER_ATTEMPTS = 2
MAX_UPLOAD_ATTEMPTS = 3
UPLOAD_RETRY_DELAY = timedelta(minutes=10)

_JOB_LOCK = threading.Lock()  # 서빙/렌더/업로드가 상태 파일을 동시에 만지지 않도록 직렬화
_status: Dict[str, Any] = {"pid": os.getpid(), "started_at": datetime.now().isoformat()}
_status_lock = threading.Lock()


# ---------------------------------------------------------------- 상태 기록 / 조회
def _write_status(**fields: Any) -> None:
    with _status_lock:
        _status.update(fields)
        _status["heartbeat"] = datetime.now().isoformat()
        try:
            atomic_write_json(DAEMON_STATUS_FILE, _status)
        except OSError as e:  # 대시보드가 읽는 중 등 일시 오류는 다음 하트비트에서 복구
            print(f"[Daemon] 상태 기록 실패: {e}")


def read_daemon_status() -> Dict[str, Any]:
    """대시보드용 데몬 상태. alive=하트비트가 ALIVE_THRESHOLD_SEC 이내."""
    import json
    try:
        data = json.loads(DAEMON_STATUS_FILE.read_text(encoding="utf-8"))
        age = (datetime.now() - datetime.fromisoformat(data["heartbeat"])).total_seconds()
    except (OSError, ValueError, KeyError):
        return {"alive": False, "age_sec": None}
    data["age_sec"] = int(age)
    data["alive"] = age <= ALIVE_THRESHOLD_SEC
    return data


def _log(msg: str) -> None:
    print(f"[Daemon {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _guarded(name: str, fn: Callable[[], str]) -> Callable[[], None]:
    """작업 예외가 스케줄러를 죽이지 않도록 감싸고, 결과를 daemon_status 에 남긴다."""
    def run() -> None:
        with _JOB_LOCK:
            _write_status(current_job=name)
            try:
                result = fn()
                _log(f"{name}: {result}")
                _write_status(last_job=name, last_job_result=result, last_job_at=datetime.now().isoformat(),
                              last_error=None, current_job=None)
            except Exception as e:
                traceback.print_exc()
                _write_status(last_job=name, last_job_result=f"ERROR {type(e).__name__}: {e}",
                              last_job_at=datetime.now().isoformat(), last_error=str(e), current_job=None)
    return run


# ---------------------------------------------------------------- 단계 구현
def _today_serving() -> Optional[Dict[str, Any]]:
    import json
    if not CANDIDATE_SERVING_FILE.exists():
        return None
    try:
        data = json.loads(CANDIDATE_SERVING_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if parse_serving_date(data) == date.today() else None


def step_serve() -> str:
    """09:00 오늘자 3대 후보 큐레이션 → 캘린더 서빙. 오늘자 서빙이 이미 있으면 건너뜀."""
    if _today_serving():
        return "오늘자 서빙이 이미 존재 → 건너뜀"
    from module1_trend_and_script import run_curation_batch
    candidates = run_curation_batch(count=3)
    served = CalendarManager().serve_candidate_buffet(candidates=candidates, target_date=date.today())
    if not served.get("calendar_synced"):
        why = served.get("calendar_error") or "캘린더 미연동(Dry-Run)"
        return f"WARN 후보 {len(candidates)}건 생성됨, 그러나 캘린더 등록 실패 → 대시보드에서 수동 승인 필요: {why}"
    return f"후보 {len(candidates)}건 서빙 완료"


def step_poll_rsvp() -> str:
    """사용자의 RSVP [예] 응답 감지 → 승인 + 골든타임 큐 등록."""
    serving = _today_serving()
    if not serving or serving.get("status") != "awaiting_rsvp":
        return "RSVP 대기 중인 오늘자 서빙 없음"
    res = CalendarManager().check_rsvp_approvals()
    return res.get("message", "응답 없음")


def step_remind() -> str:
    """15:00 미승인 시 상태 점검(캘린더 리마인더 이벤트는 서빙 시 이미 등록됨)."""
    serving = _today_serving()
    if not serving:
        return "오늘자 서빙 없음"
    return f"서빙 상태={serving.get('status')} (16:00 에 미승인이면 1위 자동 승인)"


def step_timeout_approve() -> str:
    """16:00 이후 미승인이면 적중률 1위 자동 승인(날짜 검증은 module2 에서 수행)."""
    res = CalendarManager().check_and_trigger_timeout_auto_approval()
    return res.get("message") or res.get("reason", "-")


def _render_one(item: Dict[str, Any]) -> str:
    ep_id = item["episode_id"]
    out = RENDERED_DIR / f"{ep_id}.mp4"
    approved_json = APPROVED_DIR / f"{ep_id}.json"
    if out.exists() and out.stat().st_size > 0:  # 승인된 대본은 고정이므로 기존 렌더 재사용
        update_golden_queue_item(ep_id, status="ready_to_publish", video_path=str(out))
        return f"{ep_id}: 기존 렌더 재사용"

    update_golden_queue_item(ep_id, status="rendering", render_started_at=datetime.now().isoformat())
    _write_status(rendering_episode=ep_id)
    try:
        from module3_renderer import BGM_DIR
        from renderers import render_episode_auto
        bgm_name = os.getenv("DAEMON_BGM", "").strip()
        bgm = (BGM_DIR / bgm_name) if bgm_name else None
        path, info = render_episode_auto(load_script(approved_json), preferred=item.get("renderer"), bgm_path=bgm, log=_log)
    except Exception as e:
        attempts = int(item.get("render_attempts", 0)) + 1
        update_golden_queue_item(ep_id, status="render_failed", render_attempts=attempts,
                                 last_error=f"{type(e).__name__}: {e}")
        raise
    finally:
        _write_status(rendering_episode=None)
    update_golden_queue_item(ep_id, status="ready_to_publish", video_path=str(path), last_error=None,
                             rendered_by=info.get("renderer"), render_info=info)
    extra = f" [{info.get('renderer')}" + (f", VLM {info['vlm_pass']}" if info.get("vlm_pass") else "") + \
            (f", 검수 필요 컷 {info['needs_review']}" if info.get("needs_review") else "") + \
            (", 폴백 사용" if info.get("fallback_from") else "") + "]"
    return f"{ep_id}: 렌더 완료 → {Path(path).name}{extra}"


def step_render() -> str:
    """승인 직후 백그라운드 렌더링: 대기열에서 렌더 대상(미렌더/실패 재시도)을 순서대로 처리."""
    done = []
    for item in get_golden_time_queue():
        status = item.get("status")
        retry = status == "render_failed" and int(item.get("render_attempts", 0)) < MAX_RENDER_ATTEMPTS
        # rendering 상태로 남은 항목 = 데몬이 렌더 도중 종료된 경우 → 재시도 대상
        if status in ("ready_for_golden_time_upload", "rendering") or retry:
            try:
                done.append(_render_one(item))
            except Exception as e:
                done.append(f"{item['episode_id']}: 렌더 실패 ({e})")
    return "; ".join(done) or "렌더 대상 없음"


def _publish_one(item: Dict[str, Any]) -> str:
    from module4_uploader import run_pipeline_step4
    ep_id = item["episode_id"]
    video = Path(item.get("video_path") or RENDERED_DIR / f"{ep_id}.mp4")
    attempts = int(item.get("attempts", 0)) + 1
    update_golden_queue_item(ep_id, status="uploading", attempts=attempts)
    try:
        res = run_pipeline_step4(load_script(APPROVED_DIR / f"{ep_id}.json"), video_path=video,
                                 privacy_status="private")
    except Exception as e:  # 파일 없음 등 사전 오류
        res = {"status": "failed", "message": str(e), "error": f"{type(e).__name__}: {e}"}

    if res["status"] == "success":
        update_golden_queue_item(ep_id, status="published", video_id=res["video_id"],
                                 video_url=res["video_url"], published_at=datetime.now().isoformat(),
                                 last_error=None)
        return f"{ep_id}: 업로드 성공 {res['video_url']}"
    if res["status"] == "simulated":  # 인증 미설정 Dry-Run: 실제 발행이 아니므로 별도 상태로 구분
        update_golden_queue_item(ep_id, status="simulated_dry_run", video_id=res.get("video_id"),
                                 last_error="YouTube 인증 미설정(Dry-Run) — 실제 업로드되지 않음")
        return f"{ep_id}: Dry-Run (실제 업로드 아님)"

    final = attempts >= MAX_UPLOAD_ATTEMPTS
    update_golden_queue_item(
        ep_id, status="upload_failed_final" if final else "upload_failed",
        last_error=res.get("error") or res.get("message"),
        next_retry_at=None if final else (datetime.now() + UPLOAD_RETRY_DELAY).isoformat())
    return f"{ep_id}: 업로드 실패({attempts}/{MAX_UPLOAD_ATTEMPTS}) {res.get('error')}"


def step_publish() -> str:
    """17:30 이후 업로드 큐 소모. 당일 예약분만 발행하고, 지난 날짜 항목은 missed_window 로 표시."""
    now = datetime.now()
    if now.time() < PUBLISH_TIME:
        return f"아직 발행 시각({PUBLISH_TIME:%H:%M}) 전"
    done = []
    for item in get_golden_time_queue():
        status = item.get("status")
        due = status == "ready_to_publish"
        if status == "upload_failed" and int(item.get("attempts", 0)) < MAX_UPLOAD_ATTEMPTS:
            retry_at = item.get("next_retry_at")
            due = not retry_at or datetime.fromisoformat(retry_at) <= now
        if not due:
            continue
        sched_day = str(item.get("scheduled_publish_time", ""))[:10]
        if sched_day and sched_day < str(now.date()):
            update_golden_queue_item(item["episode_id"], status="missed_window",
                                     last_error=f"예약일({sched_day}) 경과 — 자동 발행하지 않음")
            done.append(f"{item['episode_id']}: 예약일 경과(missed_window)")
            continue
        done.append(_publish_one(item))
    return "; ".join(done) or "발행 대상 없음"


def step_analytics() -> str:
    """6시간 주기 YouTube 실지표 수집 → analytics_cache.json (읽기 전용이라 Autopilot 과 무관)."""
    from module5_analytics_feedback import collect_analytics
    c = collect_analytics()
    n = len(c.get("videos") or {})
    pending = sum(1 for v in (c.get("videos") or {}).values() if not v.get("analytics"))
    if c["status"] == "auth_required":
        return f"WARN 인증 필요: {c['errors'][0]}"
    suffix = f" (Analytics 집계 대기 {pending}건)" if pending else ""
    err = f" / 오류 {len(c['errors'])}건: {c['errors'][0]}" if c["errors"] else ""
    return f"수집 {c['status']}: 영상 {n}건{suffix}{err}"


def step_tick() -> str:
    """주기 점검(재시작 캐치업 포함): 오전 서빙 누락 보충 → RSVP → 타임아웃 → 렌더 → 발행."""
    if not is_autopilot_enabled():
        return "Autopilot 비활성 → 일시 중지"
    parts = []
    now = datetime.now().time()
    if SERVE_TIME <= now < REMIND_TIME:
        parts.append(step_serve())
    parts.append(step_poll_rsvp())
    if now >= TIMEOUT_TIME:
        parts.append(step_timeout_approve())
    parts.append(step_render())
    parts.append(step_publish())
    return " | ".join(parts)


def _gated(fn: Callable[[], str]) -> Callable[[], str]:
    """Autopilot 이 꺼져 있으면 개별 크론 작업도 실행하지 않는다."""
    def wrapper() -> str:
        return fn() if is_autopilot_enabled() else "Autopilot 비활성 → 건너뜀"
    return wrapper


JOBS: Dict[str, Callable[[], str]] = {
    "serve": _gated(step_serve),
    "remind": _gated(step_remind),
    "timeout": _gated(lambda: step_timeout_approve() + " | " + step_render()),
    "publish": _gated(step_publish),
    "tick": step_tick,
    "analytics": step_analytics,
}


# ---------------------------------------------------------------- 실행
def _heartbeat_loop() -> None:
    while True:
        _write_status()
        _time.sleep(HEARTBEAT_SEC)


def _already_running() -> bool:
    st = read_daemon_status()
    return bool(st.get("alive") and st.get("pid") and st["pid"] != os.getpid())


def _redirect_output_to_logfile() -> None:
    """pythonw/작업 스케줄러로 콘솔 없이 돌 때 stdout·stderr(트레이스백 포함)가 사라지지 않도록 daemon.log 로 보낸다."""
    log_path = OUT_DIR / "daemon.log"
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            os.replace(log_path, OUT_DIR / "daemon.log.1")
        f = open(log_path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = f


def main() -> int:
    ap = argparse.ArgumentParser(description="스틱맨 플라이휠 무인 스케줄러 데몬")
    ap.add_argument("--once", action="store_true", help="tick 1회 실행 후 종료")
    ap.add_argument("--run", choices=sorted(JOBS), help="지정 작업 1회 실행 후 종료")
    ap.add_argument("--renderer", choices=("webtoon", "stage", "legacy"),
                    help="이 데몬 프로세스의 렌더러(기본 webtoon, 환경변수 DAEMON_RENDERER 로도 지정 가능)")
    args = ap.parse_args()
    if args.renderer:
        os.environ["DAEMON_RENDERER"] = args.renderer

    if args.once or args.run:
        name = args.run or "tick"
        _guarded(name, JOBS[name])()
        return 0

    if _already_running():
        print("이미 다른 스케줄러 데몬이 실행 중입니다 (daemon_status.json 하트비트 확인).")
        return 1

    _redirect_output_to_logfile()
    from apscheduler.schedulers.blocking import BlockingScheduler

    sched = BlockingScheduler()
    opts = dict(max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.add_job(_guarded("serve", JOBS["serve"]), "cron", hour=SERVE_TIME.hour, minute=SERVE_TIME.minute, id="serve", **opts)
    sched.add_job(_guarded("remind", JOBS["remind"]), "cron", hour=REMIND_TIME.hour, minute=REMIND_TIME.minute, id="remind", **opts)
    sched.add_job(_guarded("timeout", JOBS["timeout"]), "cron", hour=TIMEOUT_TIME.hour, minute=TIMEOUT_TIME.minute, id="timeout", **opts)
    sched.add_job(_guarded("publish", JOBS["publish"]), "cron", hour=PUBLISH_TIME.hour, minute=PUBLISH_TIME.minute, id="publish", **opts)
    sched.add_job(_guarded("tick", JOBS["tick"]), "interval", minutes=TICK_MINUTES, id="tick",
                  next_run_time=datetime.now() + timedelta(seconds=5), **opts)
    sched.add_job(_guarded("analytics", JOBS["analytics"]), "interval", hours=ANALYTICS_HOURS, id="analytics",
                  next_run_time=datetime.now() + timedelta(seconds=30), **opts)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _log(f"데몬 시작 (pid={os.getpid()}) — serve {SERVE_TIME:%H:%M} / remind {REMIND_TIME:%H:%M} / "
         f"timeout {TIMEOUT_TIME:%H:%M} / publish {PUBLISH_TIME:%H:%M} / tick {TICK_MINUTES}분")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        with _status_lock:
            _status.update(stopped_at=datetime.now().isoformat(), heartbeat="1970-01-01T00:00:00")
            atomic_write_json(DAEMON_STATUS_FILE, _status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
