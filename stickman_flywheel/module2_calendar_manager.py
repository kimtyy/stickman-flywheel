"""Google Calendar 연동 및 스케줄 오케스트레이터 (인증 미설정 시 Dry-Run)."""
import json
import os
import shutil
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from schemas import StickmanScriptOutput
from fs_utils import atomic_write_json, atomic_write_text

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PENDING_DIR = BASE_DIR / "pipeline_outputs" / "pending"
APPROVED_DIR = BASE_DIR / "pipeline_outputs" / "approved"
CANDIDATE_SERVING_FILE = BASE_DIR / "pipeline_outputs" / "candidate_serving.json"
AUTOPILOT_CONFIG_FILE = BASE_DIR / "pipeline_outputs" / "autopilot_config.json"
GOLDEN_TIME_QUEUE_FILE = BASE_DIR / "pipeline_outputs" / "golden_time_queue.json"
PENDING_DIR.mkdir(parents=True, exist_ok=True)
APPROVED_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
# 모바일 구글 캘린더는 참석자가 주최자 1명뿐이면 하단 [예/아니오] 바를 숨긴다 → 더미 참석자 1명을 함께 넣는다.
# (sendUpdates="none" 이라 초대 메일은 발송되지 않으며, RSVP 판정에서는 이 봇을 무시한다.)
RSVP_BOT_ATTENDEE = {
    "email": "autopilot-bot@stickman.local",
    "displayName": "스틱맨 자율주행 봇",
    "responseStatus": "needsAction",
}
TIMEZONE = "Asia/Seoul"


def get_autopilot_config() -> Dict[str, Any]:
    """무인 자율 주행 모드(Autopilot) 설정 로드."""
    if not AUTOPILOT_CONFIG_FILE.exists():
        default_cfg = {
            "enabled": True,
            "timeout_time": "16:00",
            "golden_time_publish": "17:30",
            "remind_time": "15:00",
            "morning_alert_time": "09:00",
            "last_checked": datetime.now().isoformat(),
            "last_auto_approved_episode": None,
        }
        atomic_write_json(AUTOPILOT_CONFIG_FILE, default_cfg)
        return default_cfg
    try:
        return json.loads(AUTOPILOT_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"enabled": True, "timeout_time": "16:00", "golden_time_publish": "17:30"}


def set_autopilot_enabled(enabled: bool) -> None:
    """무인 자율 주행 모드 On/Off 토글 저장."""
    cfg = get_autopilot_config()
    cfg["enabled"] = enabled
    cfg["last_updated"] = datetime.now().isoformat()
    atomic_write_json(AUTOPILOT_CONFIG_FILE, cfg)


def is_autopilot_enabled() -> bool:
    """무인 자율 주행 모드 활성화 여부 확인."""
    return get_autopilot_config().get("enabled", True)


def get_golden_time_queue() -> List[Dict[str, Any]]:
    """퇴근길 골든 타임(17:30~18:00) 예약 배포 대기열 로드."""
    if not GOLDEN_TIME_QUEUE_FILE.exists():
        return []
    try:
        return json.loads(GOLDEN_TIME_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


_QUEUE_LOCK = threading.Lock()


def enqueue_golden_time(item: Dict[str, Any]) -> None:
    """대기열에 에피소드를 등록(동일 episode_id 는 교체)."""
    with _QUEUE_LOCK:
        queue = [q for q in get_golden_time_queue() if q.get("episode_id") != item.get("episode_id")]
        queue.append(item)
        atomic_write_json(GOLDEN_TIME_QUEUE_FILE, queue)


def update_golden_queue_item(episode_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """대기열 항목 상태/필드 갱신. 항목이 없으면 None."""
    with _QUEUE_LOCK:
        queue = get_golden_time_queue()
        target = next((q for q in queue if q.get("episode_id") == episode_id), None)
        if target is None:
            return None
        target.update(fields)
        target["updated_at"] = datetime.now().isoformat()
        atomic_write_json(GOLDEN_TIME_QUEUE_FILE, queue)
        return target


def parse_serving_date(data: Dict[str, Any]) -> Optional[date]:
    """서빙 파일의 serving_date 를 date 로 파싱. 없거나 형식 오류면 None."""
    try:
        return datetime.strptime(str(data.get("serving_date", "")), "%Y-%m-%d").date()
    except ValueError:
        return None


class CalendarManager:
    def __init__(self) -> None:
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        self.service = None
        self.dry_run = True
        self.event_errors: List[str] = []  # Live 모드에서 이벤트 생성이 실패한 사유(가상 ID 대체 발생)
        self._init_service()

    def _resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else BASE_DIR / path

    def _init_service(self) -> None:
        """서비스 계정 → OAuth2(config/oauth_client.json) 순으로 시도, 실패 시 Dry-Run."""
        try:
            from googleapiclient.discovery import build

            sa_path = self._resolve(
                os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "config/service_account.json")
            )
            creds = None
            if sa_path.exists():
                from google.oauth2 import service_account

                creds = service_account.Credentials.from_service_account_file(
                    str(sa_path), scopes=SCOPES
                )
            else:
                oauth_client = BASE_DIR / "config" / "oauth_client.json"
                token_path = BASE_DIR / "config" / "token.json"
                if oauth_client.exists():
                    from google.auth.transport.requests import Request
                    from google.oauth2.credentials import Credentials
                    from google_auth_oauthlib.flow import InstalledAppFlow

                    if token_path.exists():
                        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
                    if not creds or not creds.valid:
                        if creds and creds.expired and creds.refresh_token:
                            creds.refresh(Request())
                        else:
                            flow = InstalledAppFlow.from_client_secrets_file(
                                str(oauth_client), SCOPES
                            )
                            creds = flow.run_local_server(port=0)
                        token_path.write_text(creds.to_json(), encoding="utf-8")
            if creds is None:
                print("[Calendar] 인증 파일 없음 → Dry-Run 모드")
                return
            self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            self.dry_run = False
        except Exception as e:
            print(f"[Calendar] 인증 실패 → Dry-Run 모드: {e}")
            self.service = None
            self.dry_run = True

    def get_my_email(self) -> Optional[str]:
        """인증된 계정의 이메일(calendarList 의 primary ID). 조회 실패 시 환경변수로 대체."""
        cached = getattr(self, "_my_email", None)
        if cached:
            return cached
        email = None
        if not self.dry_run and self.service is not None:
            try:
                email = self.service.calendarList().get(calendarId="primary").execute().get("id")
            except Exception as e:
                print(f"[Calendar] 내 계정 이메일 조회 실패: {e}")
        if not email:
            cand = (os.getenv("CALENDAR_USER_EMAIL") or os.getenv("GOOGLE_CALENDAR_USER_EMAIL")
                    or os.getenv("GOOGLE_CALENDAR_ID") or "")
            email = cand.strip() if "@" in cand else None
        self._my_email = email
        return email

    def _rsvp_attendees(self, my_email: Optional[str]) -> List[Dict[str, Any]]:
        """[예/아니오] 바가 뜨도록 본인(needsAction, 명시하지 않으면 주최자는 기본 accepted) + 더미 봇."""
        if not my_email:
            return []
        return [
            {"email": my_email.strip(), "responseStatus": "needsAction", "displayName": "스틱맨 숏폼 총괄 디렉터"},
            dict(RSVP_BOT_ATTENDEE),
        ]

    def patch_candidate_attendees(self, serving: Dict[str, Any]) -> List[str]:
        """이미 등록된 후보 이벤트(+리마인더)에 참석자를 소급 적용. 성공한 이벤트 ID 목록 반환."""
        if self.dry_run or self.service is None:
            return []
        my_email = self.get_my_email()
        attendees = self._rsvp_attendees(my_email)
        if not attendees:
            return []
        ids = [c.get("calendar_event_id") for c in serving.get("candidates", [])]
        ids.append(serving.get("reminder_event_id"))
        done = []
        for ev_id in ids:
            if not ev_id or ev_id.startswith("dryrun-"):
                continue
            try:
                self.service.events().patch(
                    calendarId=self.calendar_id, eventId=ev_id,
                    body={"attendees": attendees}, sendUpdates="none",
                ).execute()
                done.append(ev_id)
            except Exception as e:
                print(f"[Calendar] 참석자 패치 실패 ({ev_id}): {e}")
        serving["attendee_email"] = my_email
        return done

    def _create_event(self, summary: str, description: str, start: datetime,
                      end: datetime, color_id: str,
                      attendees: Optional[List[Dict[str, Any]]] = None,
                      reminders: Optional[Dict[str, Any]] = None) -> str:
        body: Dict[str, Any] = {
            "summary": summary,
            "description": description,
            "colorId": color_id,
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
        }
        if attendees:
            body["attendees"] = attendees
        if reminders:
            body["reminders"] = reminders
        if self.dry_run or self.service is None:
            return f"dryrun-{uuid.uuid4().hex[:12]}"
        try:
            created = self.service.events().insert(
                calendarId=self.calendar_id, body=body, sendUpdates="none"
            ).execute()
            return created["id"]
        except Exception as e:
            print(f"[Calendar] 이벤트 생성 실패 → 가상 ID 반환: {e}")
            self.event_errors.append(str(e))
            return f"dryrun-{uuid.uuid4().hex[:12]}"

    def serve_candidate_buffet(self, candidates: List[Dict[str, Any]],
                               target_date: date,
                               attendee_email: Optional[str] = None) -> Dict[str, Any]:
        """오늘자 3대 킬러 후보 대본을 구글 캘린더에 RSVP 초대로 서빙 등록 (오전 09:00 알림 및 15:00 리마인더 연동)."""
        email_to_invite = attendee_email or self.get_my_email()
        attendees_list = self._rsvp_attendees(email_to_invite)

        color_palette = ["9", "5", "11"]  # Blueberry, Banana, Flamingo
        served_candidates = []

        # 09:00 팝업 알림 설정 (후보 서빙 시 스마트폰 팝업 즉시 푸시)
        morning_popup_reminders = {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 10}]
        }

        for idx, cand in enumerate(candidates):
            c_no = cand.get("candidate_no", idx + 1)
            script: StickmanScriptOutput = cand["script"]
            hit_rate = cand.get("predicted_hit_rate", 95)
            cat = cand.get("category", "사이다 참교육")

            summary = f"[후보 {c_no}] {script.episode_title} (적중률 {hit_rate}%)"

            scene_brief = "\n".join(
                f"S{s.scene_number} ({s.duration_sec}s): {s.subtitle_text} / {s.tts_narration[:55]}"
                for s in script.scenes
            )

            description = (
                f"🏆 [후보 {c_no}] 테마: {cat}\n"
                f"📊 예상 쇼츠 적중률: {hit_rate}%\n\n"
                f"📌 감정 역공학 브리핑:\n"
                f"- 버린 껍데기: {script.analysis.discarded_surface_topic}\n"
                f"- 코어 욕망: {script.analysis.extracted_core_desire}\n"
                f"- 새로운 상황: {script.analysis.transplanted_setting}\n"
                f"- 3초 반전: {script.analysis.twist_point}\n\n"
                f"🎬 5~7씬 대사/연출 요약 (총 {script.total_duration_sec}초):\n{scene_brief}\n\n"
                f"--------------------------------------------------\n"
                f"📱 [모바일 원클릭 승인 안내]\n"
                f"스마트폰 캘린더 상단의 '[예(Yes)]'를 터치하시면,\n"
                f"이 대본이 [승인 완료]되어 스틱맨 렌더링 파이프라인으로 즉시 자동 연결됩니다!\n"
                f"⚠️ [부재중 무인 자율주행]: 오후 16:00까지 미응답 시 1위 대본이 자동 승인되어 17:30 골든타임에 자동 배포됩니다.\n"
                f"--------------------------------------------------\n"
                f"<!-- EPISODE_ID: {script.episode_id} -->\n"
                f"<!-- CANDIDATE_NO: {c_no} -->\n"
            )

            # 오전 09:00부터 30분 간격으로 서빙
            start_dt = datetime.combine(target_date, time(9 + (idx * 1), 0))
            end_dt = start_dt + timedelta(minutes=45)
            cid = color_palette[idx % len(color_palette)]

            event_id = self._create_event(
                summary=summary,
                description=description,
                start=start_dt,
                end=end_dt,
                color_id=cid,
                attendees=attendees_list if attendees_list else None,
                reminders=morning_popup_reminders
            )

            # candidate의 script 객체를 반드시 PENDING_DIR에 파일로 영구 저장 보장
            cand_p_path = PENDING_DIR / f"{script.episode_id}.json"
            if not cand_p_path.exists() and not (APPROVED_DIR / f"{script.episode_id}.json").exists():
                atomic_write_text(cand_p_path, script.model_dump_json(indent=2))

            served_candidates.append({
                "candidate_no": c_no,
                "episode_id": script.episode_id,
                "episode_title": script.episode_title,
                "predicted_hit_rate": hit_rate,
                "category": cat,
                "calendar_event_id": event_id,
                "rsvp_status": "needsAction",
                "start_time": start_dt.strftime("%H:%M"),
            })

        # 오후 15:00 미승인 시 "16시에 1위 후보로 자동 승인됩니다" 리마인드 팝업 알림 일정 등록
        remind_dt = datetime.combine(target_date, time(15, 0))
        remind_end = remind_dt + timedelta(minutes=20)
        reminder_event_id = self._create_event(
            summary="🔔 [리마인드] 16:00 적중률 1위 대본 자동 승인 예고 (골든타임 17:30 배포)",
            description=(
                "⚡ [부재중 무인 자율 주행 모드 가동 중]\n\n"
                "오늘 16:00까지 캘린더 RSVP([예]) 응답이 없을 경우,\n"
                "후보 중 '예상 적중률 1위' 대본이 자동으로 [승인 완료] 처리되며,\n"
                "백그라운드 렌더링 착수 후 퇴근길 골든 타임(17:30~18:00) 유튜브 예약 배포 대기열에 등록됩니다.\n\n"
                "원하시는 특정 대본이 있다면 16:00 전에 [예]를 눌러주세요!"
            ),
            start=remind_dt,
            end=remind_end,
            color_id="11",  # Flamingo
            attendees=attendees_list if attendees_list else None,
            reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]}
        )

        serving_data = {
            "serving_date": str(target_date),
            "served_at": datetime.now().isoformat(),
            "status": "awaiting_rsvp",
            "attendee_email": email_to_invite,
            "approved_episode_id": None,
            "approved_candidate_no": None,
            "approved_by": None,
            "golden_time_scheduled": None,
            "reminder_event_id": reminder_event_id,
            "candidates": served_candidates,
            "calendar_synced": not (self.dry_run or self.service is None or self.event_errors),
        }
        if self.event_errors:
            serving_data["calendar_error"] = self.event_errors[0][:300]

        atomic_write_json(CANDIDATE_SERVING_FILE, serving_data)
        mode = "Live" if (not self.dry_run and self.service) else "Dry-Run"
        print(f"[CalendarBuffet:{mode}] 3대 후보 대본(09:00 알림) 및 15:00 자동승인 예고 리마인더 구글 캘린더 등록 완료")
        return serving_data

    def check_rsvp_approvals(self) -> Dict[str, Any]:
        """모바일 캘린더에서 사용자가 '[예]'를 눌렀는지 실시간 감지하여 자동 승인."""
        if not CANDIDATE_SERVING_FILE.exists():
            return {"has_serving": False, "message": "등록된 밥상 서빙 내역이 없습니다."}

        try:
            data = json.loads(CANDIDATE_SERVING_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            return {"has_serving": False, "message": f"서빙 데이터 파싱 실패: {e}"}

        # 이미 승인 완료된 경우
        if data.get("status") == "approved":
            ep_id = data.get("approved_episode_id")
            c_no = data.get("approved_candidate_no")
            approved_path = APPROVED_DIR / f"{ep_id}.json"
            script = load_script(approved_path) if approved_path.exists() else None
            return {
                "has_serving": True,
                "status": "approved",
                "winner_candidate_no": c_no,
                "episode_id": ep_id,
                "script": script,
                "message": f"🎉 [후보 {c_no}] {script.episode_title if script else ep_id} 승인 완료 상태"
            }

        # 실시간 캘린더 API 조회 (Live 모드)
        winner_candidate = None

        if not self.dry_run and self.service is not None:
            for cand in data.get("candidates", []):
                ev_id = cand.get("calendar_event_id")
                if not ev_id or ev_id.startswith("dryrun-"):
                    continue
                try:
                    ev = self.service.events().get(calendarId=self.calendar_id, eventId=ev_id).execute()
                    attendees = ev.get("attendees", [])
                    invited = (data.get("attendee_email") or self.get_my_email() or "").strip().lower()
                    bot = RSVP_BOT_ATTENDEE["email"]
                    for att in attendees:
                        addr = (att.get("email") or "").strip().lower()
                        if addr == bot:
                            continue
                        # 본인 계정(이메일 일치 또는 self 플래그)의 응답만 인정
                        if not (att.get("self") or (invited and addr == invited)):
                            continue
                        if att.get("responseStatus") == "accepted":
                            cand["rsvp_status"] = "accepted"
                            winner_candidate = cand
                            break
                    if winner_candidate:
                        break
                except Exception as e:
                    print(f"[CalendarRSVP] 이벤트 조회 실패 ({ev_id}): {e}")

        # Dry-run이나 로컬에서 accepted로 마킹된 항목 확인
        if not winner_candidate:
            for cand in data.get("candidates", []):
                if cand.get("rsvp_status") == "accepted":
                    winner_candidate = cand
                    break

        if winner_candidate:
            ep_id = winner_candidate["episode_id"]
            c_no = winner_candidate["candidate_no"]
            pending_path = PENDING_DIR / f"{ep_id}.json"
            approved_path = APPROVED_DIR / f"{ep_id}.json"

            script = None
            if pending_path.exists():
                script = load_script(pending_path)
                atomic_write_text(approved_path, script.model_dump_json(indent=2))
                pending_path.unlink(missing_ok=True)
            elif approved_path.exists():
                script = load_script(approved_path)

            # 구글 캘린더 이벤트 제목 업데이트 (승인 채택 강조 및 미채택 표기)
            if not self.dry_run and self.service is not None:
                for cand in data.get("candidates", []):
                    ev_id = cand.get("calendar_event_id")
                    if not ev_id or ev_id.startswith("dryrun-"):
                        continue
                    try:
                        if cand["candidate_no"] == c_no:
                            self.service.events().patch(
                                calendarId=self.calendar_id,
                                eventId=ev_id,
                                body={
                                    "summary": f"[✅ 승인 채택] {cand['episode_title']} (적중률 {cand['predicted_hit_rate']}%)",
                                    "colorId": "10"  # Basil (Green)
                                },
                                sendUpdates="none",
                            ).execute()
                        else:
                            self.service.events().patch(
                                calendarId=self.calendar_id,
                                eventId=ev_id,
                                body={
                                    "summary": f"[미채택 후보] {cand['episode_title']}",
                                    "colorId": "8"  # Graphite
                                },
                                sendUpdates="none",
                            ).execute()
                    except Exception as e:
                        print(f"[Calendar] 이벤트 상태 업데이트 실패: {e}")

            data["status"] = "approved"
            data["approved_episode_id"] = ep_id
            data["approved_candidate_no"] = c_no
            data["approved_by"] = "rsvp"
            atomic_write_json(CANDIDATE_SERVING_FILE, data)

            serving_day = parse_serving_date(data) or date.today()
            enqueue_golden_time({
                "episode_id": ep_id,
                "episode_title": winner_candidate.get("episode_title", ep_id),
                "candidate_no": c_no,
                "predicted_hit_rate": winner_candidate.get("predicted_hit_rate"),
                "approved_at": datetime.now().isoformat(),
                "approved_by": "rsvp",
                "scheduled_publish_time": f"{serving_day}T17:30:00+09:00",
                "status": "ready_for_golden_time_upload",
                "attempts": 0,
            })

            return {
                "has_serving": True,
                "status": "approved",
                "just_approved": True,
                "winner_candidate_no": c_no,
                "episode_id": ep_id,
                "script": script,
                "message": f"🎉 모바일 캘린더에서 [후보 {c_no}] '{script.episode_title if script else ep_id}' 승인 감지!"
            }

        return {
            "has_serving": True,
            "status": "awaiting_rsvp",
            "candidates": data.get("candidates", []),
            "message": "⏳ 구글 캘린더 앱에서 원하시는 후보의 [예(Yes)]를 터치해 주세요."
        }

    def simulate_rsvp_approval(self, candidate_no: int) -> Dict[str, Any]:
        """모바일 대신 대시보드에서 즉시 [예] 응답을 시뮬레이션 승인."""
        if not CANDIDATE_SERVING_FILE.exists():
            return {"success": False, "message": "서빙 데이터가 없습니다."}
        data = json.loads(CANDIDATE_SERVING_FILE.read_text(encoding="utf-8"))
        for cand in data.get("candidates", []):
            if cand.get("candidate_no") == candidate_no:
                cand["rsvp_status"] = "accepted"
        atomic_write_json(CANDIDATE_SERVING_FILE, data)
        return self.check_rsvp_approvals()

    def check_and_trigger_timeout_auto_approval(self, force_now: bool = False) -> Dict[str, Any]:
        """오후 16:00 타임아웃 자동 승인 & 퇴근길 골든 타임(17:30~18:00) 자동 배포 대기열 안전장치."""
        if not is_autopilot_enabled() and not force_now:
            return {"triggered": False, "reason": "무인 자율 주행 모드(Autopilot)가 비활성화되어 있습니다."}

        if not CANDIDATE_SERVING_FILE.exists():
            return {"triggered": False, "reason": "서빙된 밥상 후보 내역이 없습니다."}

        try:
            data = json.loads(CANDIDATE_SERVING_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            return {"triggered": False, "reason": f"서빙 데이터 파싱 오류: {e}"}

        if data.get("status") == "approved":
            return {
                "triggered": False,
                "already_approved": True,
                "episode_id": data.get("approved_episode_id"),
                "reason": "이미 승인 완료된 에피소드가 존재합니다."
            }

        now = datetime.now()
        # 서빙 날짜 검증: 오늘자 서빙만 자동 승인 대상 (어제 이전 잔존 파일로 오승인 방지)
        serving_day = parse_serving_date(data)
        if not force_now:
            if serving_day is None:
                return {"triggered": False, "reason": "서빙 파일의 serving_date 가 없거나 형식이 올바르지 않아 자동 승인을 건너뜁니다."}
            if serving_day != now.date():
                return {
                    "triggered": False,
                    "stale_serving": True,
                    "reason": f"서빙 날짜({serving_day})가 오늘({now.date()})이 아니어서 자동 승인하지 않습니다."
                }
        # 16:00 타임아웃 도달 검사 (force_now=True 시 즉시 강제 테스트 가능)
        if not force_now and now.time() < time(16, 0):
            return {
                "triggered": False,
                "reason": f"아직 자동 승인 타임아웃(16:00) 전입니다. (현재 시각: {now.strftime('%H:%M')})"
            }

        candidates = data.get("candidates", [])
        if not candidates:
            return {"triggered": False, "reason": "후보 대본 목록이 비어있습니다."}

        # 예상 적중률 1위 후보 자동 선정 (predicted_hit_rate 기준 내림차순)
        sorted_candidates = sorted(
            candidates,
            key=lambda c: c.get("predicted_hit_rate", 0),
            reverse=True
        )
        winner = sorted_candidates[0]
        c_no = winner["candidate_no"]
        ep_id = winner["episode_id"]

        pending_path = PENDING_DIR / f"{ep_id}.json"
        approved_path = APPROVED_DIR / f"{ep_id}.json"
        script = None

        if pending_path.exists():
            script = load_script(pending_path)
            atomic_write_text(approved_path, script.model_dump_json(indent=2))
            pending_path.unlink(missing_ok=True)
        elif approved_path.exists():
            script = load_script(approved_path)

        # 캘린더 이벤트 업데이트 (구글 캘린더 상에 자동 승인 표시)
        if not self.dry_run and self.service is not None:
            for cand in candidates:
                ev_id = cand.get("calendar_event_id")
                if not ev_id or ev_id.startswith("dryrun-"):
                    continue
                try:
                    if cand["candidate_no"] == c_no:
                        self.service.events().patch(
                            calendarId=self.calendar_id,
                            eventId=ev_id,
                            body={
                                "summary": f"[🤖 16:00 자율승인] {cand['episode_title']} (적중률 {cand['predicted_hit_rate']}%)",
                                "colorId": "10"  # Green
                            },
                            sendUpdates="none",
                        ).execute()
                    else:
                        self.service.events().patch(
                            calendarId=self.calendar_id,
                            eventId=ev_id,
                            body={
                                "summary": f"[타임아웃 미채택] {cand['episode_title']}",
                                "colorId": "8"  # Graphite
                            },
                            sendUpdates="none",
                        ).execute()
                except Exception as e:
                    print(f"[Calendar] 타임아웃 이벤트 패치 실패: {e}")

        # 퇴근길 골든 타임(17:30 ~ 18:00) 예약 배포 캘린더 등록
        serving_date_str = data.get("serving_date", str(date.today()))
        try:
            target_date_obj = datetime.strptime(serving_date_str, "%Y-%m-%d").date()
        except Exception:
            target_date_obj = date.today()

        golden_start = datetime.combine(target_date_obj, time(17, 30))
        golden_end = datetime.combine(target_date_obj, time(18, 0))

        golden_event_id = self._create_event(
            summary=f"🌇 [골든타임 예약발행] {winner['episode_title']}",
            description=(
                f"🚀 [트래픽 피크 타임 무인 자율 발행 대기열]\n\n"
                f"- 에피소드: {winner['episode_title']} (ID: {ep_id})\n"
                f"- 예상 적중률: {winner.get('predicted_hit_rate', 95)}% (1위 자동승인)\n"
                f"- 배포 예정 시간: {golden_start.strftime('%H:%M')} ~ {golden_end.strftime('%H:%M')}\n"
                f"- 상태: 렌더링 후 유튜브 예약 업로드 대기열 등록 완료"
            ),
            start=golden_start,
            end=golden_end,
            color_id="9",  # Blueberry
            reminders={"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]}
        )

        # 서빙 파일 상태 갱신
        data["status"] = "approved"
        data["approved_episode_id"] = ep_id
        data["approved_candidate_no"] = c_no
        data["approved_by"] = "autopilot_timeout_1600"
        data["golden_time_scheduled"] = "17:30"
        data["golden_event_id"] = golden_event_id
        data["auto_approved_at"] = datetime.now().isoformat()
        atomic_write_json(CANDIDATE_SERVING_FILE, data)

        # 퇴근길 골든타임 큐에 등록
        enqueue_golden_time({
            "episode_id": ep_id,
            "episode_title": winner["episode_title"],
            "candidate_no": c_no,
            "predicted_hit_rate": winner.get("predicted_hit_rate", 95),
            "approved_at": datetime.now().isoformat(),
            "approved_by": "autopilot_timeout_1600",
            "scheduled_publish_time": f"{target_date_obj}T17:30:00+09:00",
            "status": "ready_for_golden_time_upload",
            "attempts": 0,
            "traffic_target": "퇴근길 쇼츠 트래픽 피크 (17:30 ~ 18:00)"
        })

        # 오토파일럿 설정에 기록
        cfg = get_autopilot_config()
        cfg["last_checked"] = datetime.now().isoformat()
        cfg["last_auto_approved_episode"] = ep_id
        atomic_write_json(AUTOPILOT_CONFIG_FILE, cfg)

        return {
            "triggered": True,
            "winner_candidate_no": c_no,
            "episode_id": ep_id,
            "script": script,
            "golden_time": "17:30 ~ 18:00",
            "message": f"🤖 [16:00 타임아웃 무인 자율 승인 발동] 예상 적중률 1위(후보 {c_no}, {winner.get('predicted_hit_rate')}%) '{winner['episode_title']}' 승인 완료 → 17:30 골든타임 발행 대기열 등록!"
        }

    def get_current_serving_status(self) -> Optional[Dict[str, Any]]:
        """현재 서빙 중인 3대 후보 대본 정보 반환."""
        if not CANDIDATE_SERVING_FILE.exists():
            return None
        try:
            return json.loads(CANDIDATE_SERVING_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None

    def schedule_episode_pipeline(self, script_data: StickmanScriptOutput,
                                  target_date: date) -> StickmanScriptOutput:
        """3개 일정 등록 후 ID를 반영하고 pending → approved 로 이동 저장."""
        title = script_data.episode_title
        json_path = APPROVED_DIR / f"{script_data.episode_id}.json"

        scene_summary = "\n".join(
            f"S{s.scene_number} ({s.duration_sec}s) {s.subtitle_text} | {s.stickman_action[:60]}"
            for s in script_data.scenes
        )
        render_desc = (
            f"[씬 요약] 총 {script_data.total_duration_sec}초\n{scene_summary}\n\n"
            f"JSON: {json_path}"
        )
        upload_desc = (
            f"해시태그: {' '.join(script_data.hashtags)}\n"
            f"타겟 감정: {script_data.target_emotion}"
        )
        feedback_desc = (
            f"YouTube Analytics 수집 트리거\nepisode_id: {script_data.episode_id}\n"
            f"JSON: {json_path}"
        )

        render_start = datetime.combine(target_date, time(14, 0))
        upload_start = datetime.combine(target_date, time(19, 0))
        feedback_start = upload_start + timedelta(hours=48)

        script_data.calendar_render_event_id = self._create_event(
            f"[렌더링 대기] {title}", render_desc, render_start,
            render_start + timedelta(minutes=30), "5",
        )
        script_data.calendar_upload_event_id = self._create_event(
            f"[쇼츠 업로드 예약] {title}", upload_desc, upload_start,
            upload_start + timedelta(minutes=15), "9",
        )
        self._create_event(
            f"[성과 피드백 수확] {title}", feedback_desc, feedback_start,
            feedback_start + timedelta(minutes=15), "3",
        )

        atomic_write_text(json_path, script_data.model_dump_json(indent=2))
        pending_path = PENDING_DIR / f"{script_data.episode_id}.json"
        if pending_path.exists():
            pending_path.unlink()
        mode = "Dry-Run" if self.dry_run else "Live"
        print(f"[Calendar:{mode}] 등록 완료 → {json_path}")
        return script_data


def load_script(path: Path) -> StickmanScriptOutput:
    return StickmanScriptOutput.model_validate_json(path.read_text(encoding="utf-8"))
