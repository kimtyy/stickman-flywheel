"""Module 4: 유튜브 쇼츠 메타데이터 자동 패키징 & YouTube Data API v3 업로더.

승인된 대본과 렌더링된 MP4 영상을 바탕으로 제목, 설명, 해시태그를 유튜브 쇼츠 알고리즘에
최적화하여 패키징하고, 채널에 자동 업로드한다 (인증 미설정 시 Dry-Run 시뮬레이션).
"""
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from schemas import StickmanScriptOutput
from fs_utils import atomic_write_json, atomic_write_text

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
RENDERED_DIR = BASE_DIR / "pipeline_outputs" / "rendered"
APPROVED_DIR = BASE_DIR / "pipeline_outputs" / "approved"
CONFIG_DIR = BASE_DIR / "config"

# videos.insert 에는 youtube.upload 하나면 충분하다. 토큰(auth_youtube.py)이 승인받은 범위의 부분집합만 요청해야
# 갱신 시 invalid_scope 가 나지 않는다 (예전에는 승인된 적 없는 'youtube' 범위를 요청해 갱신이 거부됐다).
YOUTUBE_UPLOAD_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
]

# 초기 영상은 사람이 직접 확인한 뒤 수동으로 공개 전환한다: private/unlisted 외의 값은 모두 private 로 강제한다.
DEFAULT_PRIVACY = "private"
ALLOWED_PRIVACY = ("private", "unlisted")


def safe_privacy(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    if v in ALLOWED_PRIVACY:
        return v
    print(f"[YouTube Uploader] 공개 상태 '{value}' 는 허용되지 않아 '{DEFAULT_PRIVACY}' 로 강제합니다.")
    return DEFAULT_PRIVACY


THEME_EMOJIS = [
    (["당근", "중고", "네고", "매너온도", "거래"], "🥕"),
    (["헬스", "스쿼트", "바벨", "헬창", "원판", "쇠질", "운동"], "🏋️"),
    (["주차", "가로주차", "차량", "운전", "새치기", "문콕"], "🚗"),
    (["엘리베이터", "엘베"], "🛗"),
    (["편의점", "알바", "진상손님"], "🏪"),
    (["회사", "팀장", "부장", "회식", "상사", "사무실"], "💼"),
    (["음식", "배달", "딸배", "치킨", "피자"], "🛵"),
    (["층간소음", "윗집", "소음"], "📢"),
    (["지하철", "지옥철", "출근길"], "🚇"),
    (["참교육", "사이다", "빌런", "진상"], "💥"),
]


def detect_theme_emoji(text: str) -> str:
    """에피소드 제목 및 내용에서 주제에 맞는 대표 이모지 자동 매칭."""
    text_lower = (text or "").lower()
    for keywords, emoji in THEME_EMOJIS:
        if any(k in text_lower for k in keywords):
            return emoji
    return "🎬"


def generate_short_metadata(script: StickmanScriptOutput) -> Dict[str, Any]:
    """승인된 대본을 분석하여 조회수를 극대화하는 유튜브 쇼츠 최적화 메타데이터를 자동 패키징."""
    full_text = f"{script.episode_title} {script.analysis.extracted_core_desire} {script.analysis.transplanted_setting} {script.analysis.twist_point}"
    emoji = detect_theme_emoji(full_text)

    # 1. 시선을 사로잡는 클릭 유도 제목 (100자 이내, #shorts 및 핵심 해시태그 필수)
    clean_title = script.episode_title.strip()
    # 이모지 삽입
    if emoji not in clean_title:
        base_title = f"{clean_title} {emoji}"
    else:
        base_title = clean_title

    # #shorts 및 #참교육 태그 부착
    shorts_tags = "#shorts #참교육"
    if "#shorts" not in base_title.lower():
        title_cand = f"{base_title} {shorts_tags}".strip()
    else:
        title_cand = base_title

    if len(title_cand) > 95:
        title = title_cand[:92] + "..."
    else:
        title = title_cand

    # 2. 설명란 (1줄 핵심 요약 + 감정선 + 유입 유도 CTA + 해시태그)
    tags_pool = ["#shorts", "#쇼츠", "#스틱맨", "#참교육", "#사이다", "#슬랩스틱", "#애니메이션", "#공감", "#개그"]
    for ht in script.hashtags:
        h = ht.strip()
        if not h.startswith("#"):
            h = f"#{h}"
        if h not in tags_pool:
            tags_pool.append(h)

    description_parts = [
        f"⚡ {script.target_emotion}",
        f"📍 상황: {script.analysis.transplanted_setting}",
        f"🔥 결말: {script.analysis.twist_point}",
        "",
        "매일 통쾌한 스틱맨 슬랩스틱 사이다 숏폼이 업로드됩니다!",
        "구독과 좋아요, 알림 설정은 영상 제작에 큰 힘이 됩니다! 🔔",
        "",
        " ".join(tags_pool),
    ]
    description = "\n".join(description_parts).strip()

    # 3. 태그 목록 (유튜브 태그는 '#' 없이 콤마로 구분되는 키워드)
    clean_tags: List[str] = ["shorts", "쇼츠", "스틱맨", "참교육", "사이다", "슬랩스틱", "애니메이션", "공감", "개그", "사이다툰"]
    for ht in script.hashtags:
        cleaned = re.sub(r"[#\s]", "", ht).strip()
        if cleaned and cleaned not in clean_tags:
            clean_tags.append(cleaned)

    return {
        "title": title,
        "description": description,
        "tags": clean_tags,
        "categoryId": "23",  # 23: Comedy
        "privacyStatus": DEFAULT_PRIVACY,
        "emoji": emoji,
    }


class YouTubeUploader:
    """YouTube Data API v3 업로더 (인증 정보 없을 시 무중단 시뮬레이션/Dry-Run 모드 지원)."""

    def __init__(self, credentials_path: Optional[str] = None) -> None:
        self.service = None
        self.dry_run = True
        self._init_service(credentials_path)

    def _resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else BASE_DIR / path

    def _init_service(self, custom_creds_path: Optional[str] = None) -> None:
        """OAuth 2.0 또는 서비스 계정으로 YouTube 서비스 초기화. 미설정 시 Dry-Run 모드."""
        try:
            from googleapiclient.discovery import build

            creds = None
            token_candidates = [
                CONFIG_DIR / "youtube_token.json",
                CONFIG_DIR / "token.json",
                BASE_DIR / "token.json",
            ]
            oauth_candidates = [
                CONFIG_DIR / "oauth_client.json",
                CONFIG_DIR / "client_secrets.json",
                BASE_DIR / "client_secrets.json",
            ]
            if custom_creds_path:
                oauth_candidates.insert(0, self._resolve(custom_creds_path))

            # 1. 기존 저장된 토큰 확인
            token_path = next((p for p in token_candidates if p.exists()), None)
            if token_path:
                from google.oauth2.credentials import Credentials

                creds = Credentials.from_authorized_user_file(str(token_path), YOUTUBE_UPLOAD_SCOPES)

            # 2. 토큰이 없거나 만료된 경우 OAuth Client Secrets로 갱신 시도
            oauth_path = next((p for p in oauth_candidates if p.exists()), None)
            if creds and creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
                if token_path:
                    atomic_write_text(token_path, creds.to_json())
            elif not creds and oauth_path:
                from google_auth_oauthlib.flow import InstalledAppFlow

                flow = InstalledAppFlow.from_client_secrets_file(str(oauth_path), YOUTUBE_UPLOAD_SCOPES)
                creds = flow.run_local_server(port=0)
                # 토큰 저장
                save_token = CONFIG_DIR / "youtube_token.json"
                save_token.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(save_token, creds.to_json())

            if creds:
                self.service = build("youtube", "v3", credentials=creds, cache_discovery=False)
                self.dry_run = False
                print("[YouTube Uploader] YouTube Data API v3 OAuth 인증 성공 (실제 업로드 가능)")
            else:
                self.dry_run = True
                print("[YouTube Uploader] 인증 파일(oauth_client.json/token.json) 미설정 -> 시뮬레이션(Dry-Run) 모드로 실행")
        except Exception as e:
            self.dry_run = True
            print(f"[YouTube Uploader] 인증 초기화 예외 ({e}) -> 시뮬레이션(Dry-Run) 모드로 자동 전환")

    def upload_video(
        self,
        video_path: Path,
        metadata: Dict[str, Any],
        privacy_status: str = DEFAULT_PRIVACY,
    ) -> Dict[str, Any]:
        """비디오 파일과 메타데이터를 유튜브에 업로드 (Dry-Run 시 검증 페이로드 반환)."""
        if not video_path.exists():
            raise FileNotFoundError(f"업로드할 비디오 파일을 찾을 수 없습니다: {video_path}")

        file_size_mb = round(video_path.stat().st_size / (1024 * 1024), 2)
        privacy_status = safe_privacy(privacy_status)
        metadata = {k: v for k, v in metadata.items() if k not in ("publishAt", "publish_at")}  # 예약 자동 공개 차단

        # 1. Dry-Run / 시뮬레이션 모드
        if self.dry_run or not self.service:
            sim_id = f"sim_{uuid.uuid4().hex[:11]}"
            sim_url = f"https://youtube.com/shorts/{sim_id}"
            return {
                "status": "simulated",
                "message": "시뮬레이션 성공: 메타데이터 및 업로드 페이로드 검증 완료 (YouTube Data API v3 Dry-Run)",
                "video_id": sim_id,
                "video_url": sim_url,
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "category_id": metadata.get("categoryId", "23"),
                "privacy_status": privacy_status,
                "file_name": video_path.name,
                "file_size_mb": file_size_mb,
                "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": True,
            }

        # 2. 실제 YouTube Data API v3 업로드
        try:
            from googleapiclient.http import MediaFileUpload

            body = {
                "snippet": {
                    "title": metadata["title"],
                    "description": metadata["description"],
                    "tags": metadata.get("tags", []),
                    "categoryId": metadata.get("categoryId", "23"),
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False,
                },
            }

            media = MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=1024 * 1024 * 4,  # 4MB chunks
            )

            request = self.service.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    print(f"[YouTube Upload] 진행률: {int(status.progress() * 100)}%")

            real_id = response.get("id", "")
            real_url = f"https://youtube.com/shorts/{real_id}"
            print(f"[YouTube Upload] 업로드 성공! 영상 ID: {real_id}")

            return {
                "status": "success",
                "message": "유튜브 쇼츠 업로드 성공!",
                "video_id": real_id,
                "video_url": real_url,
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "category_id": metadata.get("categoryId", "23"),
                "privacy_status": privacy_status,
                "file_name": video_path.name,
                "file_size_mb": file_size_mb,
                "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": False,
            }
        except Exception as e:
            # 실제 업로드 실패는 시뮬레이션 성공으로 위장하지 않고 failed 로 명확히 보고한다.
            print(f"[YouTube Upload] API 업로드 실패: {e}")
            return {
                "status": "failed",
                "message": f"유튜브 업로드 실패: {e}",
                "error": f"{type(e).__name__}: {e}",
                "video_id": None,
                "video_url": None,
                "title": metadata["title"],
                "privacy_status": privacy_status,
                "file_name": video_path.name,
                "file_size_mb": file_size_mb,
                "failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dry_run": False,
            }


def run_pipeline_step4(
    script: StickmanScriptOutput,
    video_path: Optional[Path] = None,
    privacy_status: str = DEFAULT_PRIVACY,
) -> Dict[str, Any]:
    """4단계 전체 실행: 승인 대본 메타데이터 패키징 → 영상 업로드(또는 시뮬레이션)."""
    # 1. 비디오 파일 경로 확인
    target_video: Optional[Path] = None
    if video_path and video_path.exists():
        target_video = video_path
    elif script.rendered_video_path and Path(script.rendered_video_path).exists():
        target_video = Path(script.rendered_video_path)
    elif (RENDERED_DIR / f"{script.episode_id}.mp4").exists():
        target_video = RENDERED_DIR / f"{script.episode_id}.mp4"

    if not target_video or not target_video.exists():
        raise FileNotFoundError(f"에피소드 {script.episode_id}의 렌더링된 MP4 영상을 찾을 수 없습니다. 3단계 렌더링을 먼저 수행하세요.")

    # 2. 메타데이터 패키징
    metadata = generate_short_metadata(script)

    # 3. 유튜브 업로드 실행
    uploader = YouTubeUploader()
    result = uploader.upload_video(target_video, metadata, privacy_status=privacy_status)

    # 4. 실패 시 업로드 식별자를 기록하지 않고 호출자(스케줄러/대시보드)에게 그대로 반환
    if result.get("status") == "failed":
        return result

    # 5. 승인 JSON에 업로드 결과 기록
    json_path = APPROVED_DIR / f"{script.episode_id}.json"
    if json_path.exists():
        try:
            # schemas.py 수정 없이 안전하게 calendar_upload_event_id 등에 업로드 식별자 보존
            if not script.calendar_upload_event_id or script.calendar_upload_event_id.startswith("yt-"):
                script.calendar_upload_event_id = f"yt-{result['video_id']}"
            atomic_write_text(json_path, script.model_dump_json(indent=2))
        except Exception as e:
            print(f"[Step 4] JSON 상태 기록 실패: {e}")

    return result


if __name__ == "__main__":
    files = sorted(APPROVED_DIR.glob("*.json"), reverse=True)
    if not files:
        print("approved/ 폴더에 승인된 대본이 없습니다.")
    else:
        import json

        data = StickmanScriptOutput.model_validate(json.loads(files[0].read_text(encoding="utf-8")))
        print(f"Testing Step 4 on: {data.episode_title} ({data.episode_id})")
        res = run_pipeline_step4(data)
        print("Result:", res)
