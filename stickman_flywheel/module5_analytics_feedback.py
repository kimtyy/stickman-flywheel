"""YouTube 실제 성과 지표 수집(Data API + Analytics API) 및 피드백 룰 관리.

수집 결과는 pipeline_outputs/analytics_cache.json 에 저장되며 대시보드는 이 캐시만 읽는다.
수집이 안 된 값은 절대 추정/목업으로 채우지 않고 None 으로 남겨 '집계 대기'로 표시한다.
"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fs_utils import atomic_write_json

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
OUTPUTS_DIR = BASE_DIR / "pipeline_outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR = BASE_DIR / "config"

FEEDBACK_RULES_FILE = OUTPUTS_DIR / "feedback_rules.json"
ANALYTICS_CACHE_FILE = OUTPUTS_DIR / "analytics_cache.json"
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"

SCOPE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"

# 프롬프트에 주입 가능한 규칙은 실데이터 학습 결과(source 마커)뿐이다.
REAL_RULES_SOURCE = "real_analytics"

# Analytics API 는 신규 영상 반영에 보통 2~3일이 걸린다.
ANALYTICS_METRICS = "views,likes,comments,shares,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
ANALYTICS_KEYS = ANALYTICS_METRICS.split(",")


class AnalyticsAuthError(RuntimeError):
    """토큰 없음/스코프 부족 — `python auth_youtube.py` 재인증이 필요."""


def _load_credentials():
    """저장된 토큰의 스코프를 그대로 사용(좁히지 않음). 필요한 스코프가 없으면 AnalyticsAuthError."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        raise AnalyticsAuthError(f"토큰 파일 없음: {TOKEN_FILE.name}")
    granted = set(json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("scopes") or [])
    missing = [s.rsplit("/", 1)[-1] for s in (SCOPE_READONLY, SCOPE_ANALYTICS) if s not in granted]
    if missing:
        raise AnalyticsAuthError(f"토큰에 스코프 부족({', '.join(missing)}): `python auth_youtube.py` 로 재인증 필요")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE))
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise AnalyticsAuthError("토큰 갱신 불가, 재인증 필요")
        creds.refresh(Request())
        atomic_write_json(TOKEN_FILE, json.loads(creds.to_json()))
    return creds


def _published_videos() -> List[Dict[str, Any]]:
    """골든타임 큐에서 실제 발행된(video_id 보유) 영상만."""
    from module2_calendar_manager import get_golden_time_queue
    return [i for i in get_golden_time_queue() if i.get("status") == "published" and i.get("video_id")]


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _query_analytics(yta, start: date, end: date, filters: Optional[str] = None,
                     dimensions: Optional[str] = None) -> List[List[Any]]:
    kw: Dict[str, Any] = dict(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                              metrics=ANALYTICS_METRICS)
    if filters:
        kw["filters"] = filters
    if dimensions:
        kw["dimensions"] = dimensions
    return yta.reports().query(**kw).execute().get("rows") or []


def collect_analytics() -> Dict[str, Any]:
    """발행된 영상 전체의 실지표를 수집해 analytics_cache.json 에 저장하고 캐시를 반환.

    실패 시 직전 성공 데이터는 보존하고 status/errors 만 갱신한다."""
    from googleapiclient.discovery import build

    now = datetime.now()
    prev = read_analytics_cache() or {}
    cache: Dict[str, Any] = {k: prev.get(k) for k in ("channel", "videos", "windows", "collected_at")}
    cache.update(last_attempt_at=now.isoformat(timespec="seconds"), errors=[])

    try:
        creds = _load_credentials()
    except AnalyticsAuthError as e:
        cache.update(status="auth_required", errors=[str(e)])
        atomic_write_json(ANALYTICS_CACHE_FILE, cache)
        return cache

    queue = _published_videos()
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    yta = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    errors: List[str] = []
    videos: Dict[str, Any] = {}
    for item in queue:
        videos[item["video_id"]] = {
            "episode_id": item["episode_id"], "title": item.get("episode_title"),
            "published_at": item.get("published_at"), "privacy": None, "data": None, "analytics": None,
        }

    # 1) Data API: 조회수/좋아요/댓글 (거의 실시간) + 공개 상태
    ids = list(videos)
    for i in range(0, len(ids), 50):
        try:
            res = yt.videos().list(part="snippet,statistics,status", id=",".join(ids[i:i + 50])).execute()
            for v in res.get("items", []):
                s = v.get("statistics", {})
                videos[v["id"]].update(
                    title=v["snippet"]["title"], privacy=v["status"]["privacyStatus"],
                    data={"views": _num(s.get("viewCount")), "likes": _num(s.get("likeCount")),
                          "comments": _num(s.get("commentCount"))})
        except Exception as e:
            errors.append(f"videos.list 실패: {type(e).__name__}: {e}")

    # 2) Analytics API: 영상별 지표(시청 지속률 등). 반영 전이면 rows 가 비어 analytics=None 유지.
    if ids:
        start = min(datetime.fromisoformat(v["published_at"]).date() for v in videos.values() if v["published_at"])
        try:
            rows = _query_analytics(yta, start, date.today(), filters="video==" + ",".join(ids), dimensions="video")
            for row in rows:
                videos[row[0]]["analytics"] = dict(zip(ANALYTICS_KEYS, map(_num, row[1:])))
        except Exception as e:
            errors.append(f"Analytics 영상별 조회 실패: {type(e).__name__}: {e}")

    # 3) 채널 전체: 구독자/영상 수 + 7일·30일 합계
    channel = prev.get("channel")
    try:
        ch = yt.channels().list(part="snippet,statistics", mine=True).execute()["items"][0]
        cs = ch["statistics"]
        channel = {"id": ch["id"], "title": ch["snippet"]["title"],
                   "subscribers": None if cs.get("hiddenSubscriberCount") else _num(cs.get("subscriberCount")),
                   "video_count": _num(cs.get("videoCount")), "total_views": _num(cs.get("viewCount"))}
    except Exception as e:
        errors.append(f"channels.list 실패: {type(e).__name__}: {e}")

    windows = dict(prev.get("windows") or {})
    for name, days in (("last_7d", 7), ("last_30d", 30)):
        try:
            rows = _query_analytics(yta, date.today() - timedelta(days=days), date.today())
            windows[name] = dict(zip(ANALYTICS_KEYS, map(_num, rows[0]))) if rows else None
        except Exception as e:
            errors.append(f"Analytics {name} 조회 실패: {type(e).__name__}: {e}")

    ok = not errors
    cache.update(status="ok" if ok else "partial", errors=errors, channel=channel, videos=videos, windows=windows,
                 collected_at=now.isoformat(timespec="seconds") if (ok or videos) else cache.get("collected_at"))
    atomic_write_json(ANALYTICS_CACHE_FILE, cache)
    return cache


def read_analytics_cache() -> Optional[Dict[str, Any]]:
    """대시보드/리포트용. 캐시가 없거나 깨졌으면 None(= 집계 대기)."""
    try:
        return json.loads(ANALYTICS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class AnalyticsEngine:
    """실지표 캐시 조회 + 피드백 룰 관리. 목업/추정값은 생성하지 않는다."""

    def get_feedback_rules(self) -> Dict[str, Any]:
        try:
            return json.loads(FEEDBACK_RULES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"active_rules": [], "category_weights": {}}

    def rules_are_trusted(self) -> bool:
        return self.get_feedback_rules().get("source") == REAL_RULES_SOURCE

    def get_metrics(self) -> Optional[Dict[str, Any]]:
        return read_analytics_cache()

    def trigger_closed_loop_learning(self) -> Dict[str, Any]:
        """차단됨: 실데이터 기반 규칙 합성 로직이 아직 없다. 목업으로 feedback_rules.json 을 덮어쓰지 않는다."""
        return {"success": False, "blocked": True,
                "reason": "실데이터 수집 정상화 전까지 자율 재학습이 비활성화되어 있습니다(가짜 데이터 학습 방지)."}

    def generate_executive_report(self) -> str:
        """캐시의 실제 수치만으로 만든 성과 리포트(마크다운). 값이 없으면 '집계 대기'."""
        c = read_analytics_cache()
        wait = "집계 대기"

        def f(v: Any, suffix: str = "", nd: int = 0) -> str:
            return wait if v is None else f"{v:,.{nd}f}{suffix}"

        lines = [f"# 스틱맨 숏폼 플라이휠 성과 리포트",
                 f"**생성**: {datetime.now():%Y-%m-%d %H:%M} | **마지막 수집**: {(c or {}).get('collected_at') or '없음'}", ""]
        if not c or not c.get("videos"):
            lines.append("> 수집된 지표가 없습니다(집계 대기).")
            return "\n".join(lines)
        ch = c.get("channel") or {}
        lines += [f"## 채널: {ch.get('title', wait)}",
                  f"- 구독자 {f(ch.get('subscribers'), '명')} · 영상 {f(ch.get('video_count'), '개')}", ""]
        for name, label in (("last_7d", "최근 7일"), ("last_30d", "최근 30일")):
            w = (c.get("windows") or {}).get(name) or {}
            lines.append(f"- {label}: 조회수 {f(w.get('views'))} · 구독자 순증 {f(w.get('subscribersGained'))} · "
                         f"평균 시청 지속률 {f(w.get('averageViewPercentage'), '%', 1)}")
        lines += ["", "## 영상별", "| 영상 | 조회수 | 좋아요 | 평균 시청 지속률 |", "|:--|--:|--:|--:|"]
        for vid, v in c["videos"].items():
            d, a = v.get("data") or {}, v.get("analytics") or {}
            lines.append(f"| {v.get('title') or vid} (`{vid}`) | {f(d.get('views'))} | {f(d.get('likes'))} | "
                         f"{f(a.get('averageViewPercentage'), '%', 1)} |")
        return "\n".join(lines)


def get_active_feedback_prompt() -> str:
    """Step 1 대본 생성에 주입할 피드백 룰. 실데이터 학습 결과(source 마커)가 있을 때만 반환한다."""
    try:
        data = json.loads(FEEDBACK_RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    rules = data.get("active_rules", [])
    if data.get("source") != REAL_RULES_SOURCE or not rules:
        return ""
    return "\n## [성과 데이터 기반 피드백 지침]\n" + "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules)) + "\n"
