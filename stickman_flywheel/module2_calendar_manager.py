"""Google Calendar 연동 및 스케줄 오케스트레이터 (인증 미설정 시 Dry-Run)."""
import os
import shutil
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from schemas import StickmanScriptOutput

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PENDING_DIR = BASE_DIR / "pipeline_outputs" / "pending"
APPROVED_DIR = BASE_DIR / "pipeline_outputs" / "approved"
PENDING_DIR.mkdir(parents=True, exist_ok=True)
APPROVED_DIR.mkdir(parents=True, exist_ok=True)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TIMEZONE = "Asia/Seoul"


class CalendarManager:
    def __init__(self) -> None:
        self.calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        self.service = None
        self.dry_run = True
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

    def _create_event(self, summary: str, description: str, start: datetime,
                      end: datetime, color_id: str) -> str:
        body: Dict[str, Any] = {
            "summary": summary,
            "description": description,
            "colorId": color_id,
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
        }
        if self.dry_run or self.service is None:
            return f"dryrun-{uuid.uuid4().hex[:12]}"
        try:
            created = self.service.events().insert(
                calendarId=self.calendar_id, body=body
            ).execute()
            return created["id"]
        except Exception as e:
            print(f"[Calendar] 이벤트 생성 실패 → 가상 ID 반환: {e}")
            return f"dryrun-{uuid.uuid4().hex[:12]}"

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

        json_path.write_text(script_data.model_dump_json(indent=2), encoding="utf-8")
        pending_path = PENDING_DIR / f"{script_data.episode_id}.json"
        if pending_path.exists():
            pending_path.unlink()
        mode = "Dry-Run" if self.dry_run else "Live"
        print(f"[Calendar:{mode}] 등록 완료 → {json_path}")
        return script_data


def load_script(path: Path) -> StickmanScriptOutput:
    return StickmanScriptOutput.model_validate_json(path.read_text(encoding="utf-8"))
