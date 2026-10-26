import importlib
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any

# Streamlit Cloud 환경에서 stickman_flywheel/ 경로 및 모듈 자동 탐색
CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import streamlit as st
from pydantic import ValidationError

# Streamlit Cloud st.secrets -> os.environ 자동 연동
try:
    if hasattr(st, "secrets"):
        for _sec_k, _sec_v in st.secrets.items():
            if isinstance(_sec_v, str) and _sec_k not in os.environ:
                os.environ[_sec_k] = _sec_v
except Exception:
    pass

from module1_trend_and_script import run_pipeline_step1
from module2_calendar_manager import APPROVED_DIR, PENDING_DIR, CalendarManager, load_script
import module3_renderer

try:
    importlib.reload(module3_renderer)
except Exception:
    pass

from module3_renderer import RENDERED_DIR, StickmanShortRenderer
BGM_DIR = getattr(module3_renderer, "BGM_DIR", Path(__file__).resolve().parent / "assets" / "bgm")
list_bgm_tracks = getattr(module3_renderer, "list_bgm_tracks", lambda p=BGM_DIR: [])

import module4_uploader
from module4_uploader import generate_short_metadata, run_pipeline_step4
from schemas import StickmanScriptOutput

# ==============================================================================
# 1. 페이지 설정 및 Google Stitch 하이엔드 다크 AI 스튜디오 테마 (CSS)
# ==============================================================================
st.set_page_config(
    page_title="Stickman AI Studio",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="auto"
)

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

/* 기본 배경 & 전역 폰트 */
html, body, [class*="css"] {
    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #F3F4F6;
}

/* 메인 딥 슬레이트 다크 배경 */
.stApp {
    background-color: #0B0F19;
    background-image: 
        radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.07) 0%, transparent 40%),
        radial-gradient(circle at 90% 80%, rgba(139, 92, 246, 0.05) 0%, transparent 40%);
    background-attachment: fixed;
}

/* 불필요한 기본 헤더 및 푸터 숨김 (스튜디오 몰입감 극대화) */
header[data-testid="stHeader"] {
    background: transparent !important;
    height: 1.8rem !important;
}
.stDeployButton {
    display: none !important;
}
footer {
    display: none !important;
}
#MainMenu {
    visibility: hidden;
}

/* 블록 컨테이너 상하 여백 최적화 (배지 시인성 확보) */
.block-container {
    padding-top: 2.8rem !important;
    padding-bottom: 2.5rem !important;
    max-width: 1440px !important;
}

/* 사이드바 스타일링 */
section[data-testid="stSidebar"] {
    background-color: #0E1322 !important;
    border-right: 1px solid rgba(255, 255, 255, 0.07) !important;
}
section[data-testid="stSidebar"] .block-container {
    padding-top: 1.5rem !important;
}

/* Stitch 카드 컨테이너 (반투명 다크 글래스모피즘) */
.stitch-card {
    background: rgba(17, 24, 39, 0.85);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 18px 22px;
    margin-bottom: 16px;
    box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.35);
    transition: border-color 0.2s ease;
}
.stitch-card:hover {
    border-color: rgba(99, 102, 241, 0.3);
}

/* 상단 스튜디오 헤더 배지 */
.studio-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px;
    border-radius: 9999px;
    background: rgba(99, 102, 241, 0.12);
    border: 1px solid rgba(99, 102, 241, 0.3);
    color: #A5B4FC;
    font-size: 0.82rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    margin-bottom: 8px;
}

.studio-title {
    font-size: 1.85rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    background: linear-gradient(135deg, #FFFFFF 0%, #E2E8F0 50%, #94A3B8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0 0 12px 0;
}

/* 분석 브리핑 미니 카드 */
.brief-card {
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 12px;
    padding: 12px 14px;
    height: 100%;
}
.brief-card-title {
    font-size: 0.78rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 6px;
}
.brief-card-body {
    font-size: 0.9rem;
    color: #CBD5E1;
    line-height: 1.45;
}

/* 프라이머리 액션 버튼 (네온 인디고-바이올렛 그라데이션) */
div.stButton > button[kind="primary"],
div.stButton > button[data-testid="baseButton-primary"] {
    background: linear-gradient(135deg, #6366F1 0%, #8B5CF6 100%) !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 12px !important;
    font-weight: 700 !important;
    font-size: 0.95rem !important;
    min-height: 46px !important;
    padding: 0.6rem 1.4rem !important;
    box-shadow: 0 4px 18px 0 rgba(99, 102, 241, 0.42) !important;
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1) !important;
}
div.stButton > button[kind="primary"]:hover,
div.stButton > button[data-testid="baseButton-primary"]:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 8px 25px 0 rgba(139, 92, 246, 0.6) !important;
}
div.stButton > button[kind="primary"]:active,
div.stButton > button[data-testid="baseButton-primary"]:active {
    transform: translateY(1px) !important;
}

/* 세컨더리 일반 버튼 */
div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) {
    background: rgba(30, 41, 59, 0.7) !important;
    color: #E2E8F0 !important;
    border: 1px solid rgba(255, 255, 255, 0.1) !important;
    border-radius: 12px !important;
    font-weight: 600 !important;
    min-height: 44px !important;
    transition: all 0.2s ease !important;
}
div.stButton > button:not([kind="primary"]):hover {
    background: rgba(51, 65, 85, 0.9) !important;
    border-color: rgba(99, 102, 241, 0.4) !important;
    color: #FFFFFF !important;
}

/* 다운로드 버튼 특화 스타일 */
div.stDownloadButton > button {
    background: linear-gradient(135deg, #10B981 0%, #059669 100%) !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 12px !important;
    font-weight: 700 !important;
    min-height: 46px !important;
    box-shadow: 0 4px 14px rgba(16, 185, 129, 0.35) !important;
}
div.stDownloadButton > button:hover {
    box-shadow: 0 6px 20px rgba(16, 185, 129, 0.55) !important;
    transform: translateY(-1px) !important;
}

/* 스마트폰 베젤 프레임 (stVideo 자동 결속 폰 목업) */
div[data-testid="stVideo"] {
    background: #030712 !important;
    border: 8px solid #1E293B !important;
    border-radius: 38px !important;
    padding: 10px 8px 8px 8px !important;
    box-shadow: 
        0 25px 50px -12px rgba(0, 0, 0, 0.8),
        0 0 0 1px rgba(255, 255, 255, 0.08),
        0 0 35px rgba(99, 102, 241, 0.18) !important;
    max-width: 320px !important;
    margin: 0 auto 16px auto !important;
    position: relative !important;
}
/* 다이내믹 아일랜드 노치 */
div[data-testid="stVideo"]::before {
    content: "";
    display: block;
    width: 80px;
    height: 14px;
    background: #0F172A;
    border-radius: 12px;
    margin: 0 auto 8px auto;
    border: 1px solid rgba(255, 255, 255, 0.08);
}
/* 비디오 화면 본체 */
div[data-testid="stVideo"] video {
    border-radius: 22px !important;
    width: 100% !important;
    aspect-ratio: 9 / 16 !important;
    object-fit: cover !important;
    background: #000 !important;
    display: block !important;
}
/* 하단 홈 인디케이터 바 */
div[data-testid="stVideo"]::after {
    content: "";
    display: block;
    width: 90px;
    height: 4px;
    background: rgba(255, 255, 255, 0.28);
    border-radius: 2px;
    margin: 10px auto 2px auto;
}

/* 스펙 태그 배지 그리드 */
.spec-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    width: 100%;
    margin-top: 8px;
    margin-bottom: 12px;
}
.spec-chip {
    background: rgba(15, 23, 42, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 0.82rem;
}
.spec-chip-label {
    color: #94A3B8;
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
}
.spec-chip-value {
    color: #F8FAFC;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    margin-top: 2px;
}

/* 입력 필드 & Expander 다크 스타일링 */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div {
    background-color: rgba(15, 23, 42, 0.8) !important;
    border: 1px solid rgba(255, 255, 255, 0.1) !important;
    border-radius: 10px !important;
    color: #F8FAFC !important;
}
.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: #6366F1 !important;
    box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25) !important;
}

/* Expander 다크 글래스 */
div[data-testid="stExpander"] {
    background: rgba(17, 24, 39, 0.6) !important;
    border: 1px solid rgba(255, 255, 255, 0.08) !important;
    border-radius: 14px !important;
    margin-bottom: 12px !important;
}

/* 모바일 반응형 미디어 쿼리 (@media (max-width: 768px)) */
@media (max-width: 768px) {
    .block-container {
        padding-left: 1rem !important;
        padding-right: 1rem !important;
        padding-top: 2.2rem !important;
    }
    /* 좌우 2단 분할 및 하위 컬럼을 단일 스택(Single Column)으로 자동 해제 */
    div[data-testid="stHorizontalBlock"] {
        flex-direction: column !important;
        gap: 1.2rem !important;
        width: 100% !important;
    }
    div[data-testid="column"],
    div[data-testid="stColumn"],
    div[data-testid="stHorizontalBlock"] > div {
        width: 100% !important;
        min-width: 100% !important;
        max-width: 100% !important;
        flex: 1 1 100% !important;
    }
    div[data-testid="stVideo"] {
        max-width: 280px !important;
    }
    .spec-grid {
        grid-template-columns: 1fr 1fr !important;
    }
    /* 모바일 터치 타겟 44px 이상 확보 */
    div.stButton > button,
    div.stDownloadButton > button,
    .stTextInput input,
    .stSelectbox > div {
        min-height: 48px !important;
    }
    .studio-title {
        font-size: 1.4rem !important;
    }
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)


# ==============================================================================
# 2. 헬퍼 유틸리티 함수
# ==============================================================================
def list_files(folder: Path) -> list:
    return sorted((p.name for p in folder.glob("*.json")), reverse=True)


def get_video_specs(video_path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """동영상 파일로부터 해상도, 프레임 수, 길이, 용량을 안전하게 추출."""
    if not video_path or not video_path.exists():
        return None
    try:
        size_bytes = video_path.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        duration_sec = None
        fps = 30.0
        frame_cnt = None

        try:
            import cv2
            cap = cv2.VideoCapture(str(video_path))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            frame_cnt = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps > 0 and frame_cnt > 0:
                duration_sec = frame_cnt / fps
            cap.release()
        except Exception:
            pass

        return {
            "size_mb": size_mb,
            "duration_sec": duration_sec,
            "fps": fps,
            "frame_cnt": frame_cnt,
            "name": video_path.name
        }
    except Exception:
        return None


# ==============================================================================
# 3. 사이드바 (프로덕션 파이프라인 네비게이션)
# ==============================================================================
with st.sidebar:
    st.markdown("""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
            <span style="font-size: 1.6rem;">🎬</span>
            <div>
                <div style="font-weight: 800; font-size: 1.15rem; color: #FFFFFF; letter-spacing: -0.01em;">Stickman Studio</div>
                <div style="font-size: 0.75rem; color: #818CF8; font-weight: 600;">Autonomous Shorts Flywheel</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    if st.button("🚀 1단계: 신규 트렌드 감지 & 대본 생성", type="primary", use_container_width=True):
        with st.spinner("트렌드 감지 → 열광 포인트 역공학 → 대본 생성 중... (1~2분)"):
            try:
                new_script = run_pipeline_step1()
                st.session_state["force_select"] = ("pending", f"{new_script.episode_id}.json")
                st.success(f"생성 완료: {new_script.episode_title}")
            except Exception as e:
                st.error(f"생성 실패: {e}")

    st.markdown("<hr style='border: none; border-top: 1px solid rgba(255,255,255,0.08); margin: 16px 0;'>", unsafe_allow_html=True)

    pending_list = list_files(PENDING_DIR)
    approved_list = list_files(APPROVED_DIR)

    st.markdown(f"""
        <div style="display: flex; justify-content: space-between; font-size: 0.82rem; margin-bottom: 10px; color: #94A3B8;">
            <span>⏳ 대기 <strong style="color: #F59E0B;">{len(pending_list)}건</strong></span>
            <span>✅ 승인 <strong style="color: #10B981;">{len(approved_list)}건</strong></span>
        </div>
    """, unsafe_allow_html=True)

    default_radio_idx = 0
    if "force_select" in st.session_state:
        default_radio_idx = 0 if st.session_state["force_select"][0] == "pending" else 1
    elif len(pending_list) == 0 and len(approved_list) > 0:
        default_radio_idx = 1

    status = st.radio(
        "워크스페이스 모드",
        ["pending (검수 대기)", "approved (승인 완료)"],
        index=default_radio_idx,
        label_visibility="collapsed"
    )
    is_pending = status.startswith("pending")
    options = pending_list if is_pending else approved_list
    default_idx = 0
    forced = st.session_state.pop("force_select", None)
    if forced and forced[1] in options:
        default_idx = options.index(forced[1])
    selected = st.selectbox("대본 에피소드 선택", options, index=default_idx if options else None) if options else None


# ==============================================================================
# 4. 상단 스튜디오 헤더 & 알림 플래시
# ==============================================================================
st.markdown("""
    <div class="studio-badge">
        <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #6366F1;"></span>
        GOOGLE STITCH ARCHITECTURE · HIGH-END AI VIDEO STUDIO
    </div>
    <div class="studio-title">스틱맨 숏폼 자율 플라이휠 컨트롤 타워</div>
""", unsafe_allow_html=True)

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if "flash_err" in st.session_state:
    st.error(st.session_state.pop("flash_err"))

if not selected:
    st.info("선택된 대본이 없습니다. 사이드바에서 [1단계: 신규 트렌드 감지] 버튼을 눌러 대본을 생성하세요.")
    st.stop()

folder = PENDING_DIR if is_pending else APPROVED_DIR
file_path = folder / selected
try:
    script = load_script(file_path)
except (ValidationError, ValueError) as e:
    st.error(f"대본 JSON 스키마 검증 실패: {e}")
    st.stop()

key = script.episode_id  # 위젯 키 고유 접두사 (대본 전환 시 상태 충돌 방지)

# 최신 렌더링 동영상 파일 탐색
video_file = None
if script.rendered_video_path:
    cand = Path(script.rendered_video_path)
    video_file = cand if cand.exists() else None
if video_file is None and (RENDERED_DIR / f"{script.episode_id}.mp4").exists():
    video_file = RENDERED_DIR / f"{script.episode_id}.mp4"

video_specs = get_video_specs(video_file)


# ==============================================================================
# 5. 2단 분할 스튜디오 레이아웃 (PC: 6.2 vs 3.8 / Mobile: 단일 스택)
# ==============================================================================
col_left, col_right = st.columns([6.2, 3.8], gap="large")

# ------------------------------------------------------------------------------
# [좌측 컬럼] 프로덕션 & 오디오 콘솔 (대본, 분석, BGM 믹싱, 렌더링, 업로드)
# ------------------------------------------------------------------------------
with col_left:
    # 1. 감정 역공학 분석 브리핑 카드
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 700; font-size: 1.05rem; margin-bottom: 12px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                <span>🧠</span> 감정 역공학 분석 브리핑
            </div>
    """, unsafe_allow_html=True)

    b1, b2, b3 = st.columns(3)
    with b1:
        st.markdown(f"""
            <div class="brief-card" style="border-left: 3px solid #EF4444;">
                <div class="brief-card-title" style="color: #F87171;">🗑️ 버린 껍데기 소재</div>
                <div class="brief-card-body">{script.analysis.discarded_surface_topic}</div>
            </div>
        """, unsafe_allow_html=True)
    with b2:
        st.markdown(f"""
            <div class="brief-card" style="border-left: 3px solid #F59E0B;">
                <div class="brief-card-title" style="color: #FBBF24;">🔥 추출한 열광 포인트</div>
                <div class="brief-card-body">{script.analysis.extracted_core_desire}</div>
            </div>
        """, unsafe_allow_html=True)
    with b3:
        st.markdown(f"""
            <div class="brief-card" style="border-left: 3px solid #10B981;">
                <div class="brief-card-title" style="color: #34D399;">🌱 새로 이식한 상황</div>
                <div class="brief-card-body">{script.analysis.transplanted_setting}</div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown(f"""
        <div style="margin-top: 12px; font-size: 0.8rem; color: #94A3B8;">
            🎯 타겟 감정: <strong style="color: #CBD5E1;">{script.target_emotion}</strong> · 
            ⏱️ 예상 길이: <strong style="color: #CBD5E1;">{script.total_duration_sec}초</strong> · 
            🏷️ {' '.join(script.hashtags)}
        </div>
        </div>
    """, unsafe_allow_html=True)

    if not is_pending:
        st.markdown(f"""
            <div style="background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 10px; padding: 10px 14px; margin-bottom: 14px; font-size: 0.85rem; color: #6EE7B7;">
                ✅ <strong>승인 완료 대본 (읽기 전용 모드)</strong> — 캘린더 렌더 이벤트: <code>{script.calendar_render_event_id}</code>
            </div>
        """, unsafe_allow_html=True)

    # 2. 휴먼 폴리싱 대본 에디터
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 700; font-size: 1.05rem; margin-bottom: 14px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                <span>✍️</span> 5분 휴먼 폴리싱 대본 에디터
            </div>
    """, unsafe_allow_html=True)

    disabled = not is_pending
    title = st.text_input("에피소드 제목", script.episode_title, key=f"{key}_title", disabled=disabled)
    twist = st.text_area("결말 반전 포인트 (3초 Hook)", script.analysis.twist_point, key=f"{key}_twist", disabled=disabled, height=68)

    scene_inputs = []
    for sc in script.scenes:
        n = sc.scene_number
        with st.expander(f"씬 {n} · {sc.duration_sec}초 · {sc.subtitle_text}", expanded=(n == 1)):
            action = st.text_area("스틱맨 액션 / 연출 (stickman_action)", sc.stickman_action, key=f"{key}_a{n}", disabled=disabled, height=60)
            narration = st.text_area("TTS 대사 (tts_narration)", sc.tts_narration, key=f"{key}_n{n}", disabled=disabled, height=60)
            subtitle = st.text_input("자막 텍스트 (subtitle_text)", sc.subtitle_text, key=f"{key}_s{n}", disabled=disabled)
            sfx = st.text_input("효과음 큐 (sfx_cue)", sc.sfx_cue, key=f"{key}_f{n}", disabled=disabled)
            scene_inputs.append((n, action, narration, subtitle, sfx))

    st.markdown("</div>", unsafe_allow_html=True)

    def build_edited() -> StickmanScriptOutput:
        data = script.model_dump()
        data["episode_title"] = title
        data["analysis"]["twist_point"] = twist
        for n, action, narration, subtitle, sfx in scene_inputs:
            scene = data["scenes"][n - 1]
            scene["stickman_action"] = action
            scene["tts_narration"] = narration
            scene["subtitle_text"] = subtitle
            scene["sfx_cue"] = sfx
        return StickmanScriptOutput.model_validate(data)

    # 3. 승인 및 캘린더 등록 액션 바 (Pending 모드 시 활성화)
    if is_pending:
        st.markdown("""
            <div class="stitch-card">
                <div style="font-weight: 700; font-size: 1.05rem; margin-bottom: 12px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>📅</span> 대본 승인 및 파이프라인 가동
                </div>
        """, unsafe_allow_html=True)

        a, b, c = st.columns([1.2, 2.0, 1.0])
        target_date = a.date_input("스케줄 기준일", value=date.today())
        with b:
            st.write("")
            approve = st.button("✅ 대본 최종 승인 및 캘린더 등록", type="primary", use_container_width=True)
        with c:
            st.write("")
            discard = st.button("🗑️ 폐기", use_container_width=True)

        if approve:
            try:
                edited = build_edited()
                file_path.write_text(edited.model_dump_json(indent=2), encoding="utf-8")
                with st.spinner("캘린더 등록 중..."):
                    mgr = CalendarManager()
                    result = mgr.schedule_episode_pipeline(edited, target_date)
                st.session_state["flash"] = (
                    f"승인 완료 ({'Dry-Run' if mgr.dry_run else 'Google Calendar'}) · "
                    f"렌더 {result.calendar_render_event_id} / "
                    f"업로드 {result.calendar_upload_event_id}"
                )
            except Exception as e:
                st.session_state["flash_err"] = f"승인 실패: {e}"
            st.rerun()

        if discard:
            file_path.unlink(missing_ok=True)
            st.warning("대본이 폐기되었습니다.")
            st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)

    # 4. 오디오 믹싱 & BGM 콘솔 (Step 3 설정)
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 700; font-size: 1.05rem; margin-bottom: 14px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                <span>🎵</span> BGM 오디오 더킹 & 보컬 포켓팅 콘솔
            </div>
    """, unsafe_allow_html=True)

    bgm_tracks = list_bgm_tracks(BGM_DIR)
    bgm_options = ["BGM 없음"]
    mp3_stems = set()
    for t in bgm_tracks:
        if t["ext"] == ".MP3":
            mp3_stems.add(t["stem"])
            bgm_options.append(t["name"])
    for t in bgm_tracks:
        if t["ext"] == ".WAV" and t["stem"] not in mp3_stems:
            bgm_options.append(t["name"])

    col_bgm_l, col_bgm_r = st.columns([1.2, 1.0])
    with col_bgm_l:
        # BGM 기본값 매핑
        default_bgm_idx = 0
        preferred_bgm = "Neon Skyline_5-2.wav"
        if preferred_bgm in bgm_options:
            default_bgm_idx = bgm_options.index(preferred_bgm)

        selected_bgm = st.selectbox(
            "🎼 배경음악(BGM) 트랙",
            options=bgm_options,
            index=default_bgm_idx,
            key=f"{key}_bgm_track",
            help="assets/bgm/ 폴더에 보관된 음원 중 영상에 삽입할 배경음악을 선택합니다."
        )
        bgm_vol_pct = st.slider(
            "🔊 대사 구간 BGM 볼륨 비율 (%)",
            min_value=5,
            max_value=50,
            value=20,  # 기본값 20%
            step=1,
            format="%d%%",
            key=f"{key}_bgm_vol",
            help="성우 대사가 출력되는 동안 BGM이 20% 볼륨으로 자동 감쇠(오디오 더킹)됩니다."
        )
        bgm_vocal_filter = st.checkbox(
            "☑️ 보컬 억제 필터 (Vocal Pocketing)",
            value=True,
            key=f"{key}_bgm_vocal_filter",
            help="BGM의 보컬 주파수 대역(400Hz~3,200Hz)을 감쇠하고 센터 보컬을 억제하여 성우 대사와 충돌하지 않도록 분리합니다."
        )

    with col_bgm_r:
        if selected_bgm != "BGM 없음":
            preview_path = BGM_DIR / selected_bgm
            if preview_path.exists():
                st.markdown(f"<div style='font-size: 0.85rem; font-weight: 600; color: #A5B4FC; margin-bottom: 6px;'>🎧 음원 미리듣기 (Preview)</div>", unsafe_allow_html=True)
                st.audio(str(preview_path))
                st.caption(f"선택 트랙: `{selected_bgm}`")
        else:
            st.markdown("""
                <div style="background: rgba(15, 23, 42, 0.5); border: 1px dashed rgba(255,255,255,0.1); border-radius: 10px; padding: 22px; text-align: center; color: #94A3B8; font-size: 0.85rem;">
                    🎧 배경음악을 선택하시면<br>여기서 실시간 미리듣기가 가능합니다.
                </div>
            """, unsafe_allow_html=True)

    st.markdown("<hr style='border: none; border-top: 1px solid rgba(255,255,255,0.06); margin: 16px 0;'>", unsafe_allow_html=True)

    # 렌더링 실행 버튼
    btn_label = "🎬 3단계: 스틱맨 MP4 쇼츠 다시 렌더링 (강제 덮어쓰기)" if video_file else "🎬 3단계: 스틱맨 MP4 쇼츠 즉시 렌더링 실행"
    if st.button(btn_label, type="primary", use_container_width=True):
        bar = st.progress(0.0)
        status_box = st.empty()

        def on_progress(msg: str, frac: float) -> None:
            bar.progress(min(max(frac, 0.0), 1.0))
            status_box.caption(msg)

        with st.spinner("스틱맨 2세대 관절 기구학 계산, 오디오 믹싱 및 비디오 렌더링 진행 중..."):
            try:
                importlib.reload(module3_renderer)
                renderer = module3_renderer.StickmanShortRenderer()
                bgm_file_arg = (BGM_DIR / selected_bgm) if selected_bgm != "BGM 없음" else None
                rendered_out = renderer.render_episode(
                    script,
                    progress=on_progress,
                    bgm_path=bgm_file_arg,
                    bgm_volume=bgm_vol_pct / 100.0,
                    vocal_pocketing=bgm_vocal_filter
                )
                st.session_state[f"video_reload_{script.episode_id}"] = time.time()
                st.success(f"렌더링 완료: {Path(rendered_out).name}")
                st.rerun()
            except Exception as e:
                st.error(f"렌더링 실패: {e}")

    st.markdown("</div>", unsafe_allow_html=True)

    # 5. 유튜브 쇼츠 자동 업로드 콘솔 (Step 4)
    if video_file is not None and Path(video_file).exists():
        st.markdown("""
            <div class="stitch-card">
                <div style="font-weight: 700; font-size: 1.05rem; margin-bottom: 12px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>🚀</span> 4단계: 유튜브 쇼츠 자동 업로드 콘솔
                </div>
        """, unsafe_allow_html=True)

        meta = generate_short_metadata(script)

        with st.expander("📦 자동 패키징된 유튜브 메타데이터", expanded=False):
            st.text_input("유튜브 제목 (#shorts 포함)", value=meta["title"], key="yt_title_input")
            st.text_area("설명란 (Description)", value=meta["description"], height=120, key="yt_desc_input")
            st.caption(f"🏷️ 태그 ({len(meta['tags'])}개): {', '.join(meta['tags'][:8])}")

        up_c1, up_c2 = st.columns([1.2, 1.0])
        with up_c1:
            privacy_choice = st.selectbox(
                "공개 범위",
                ["public (전체 공개)", "unlisted (일부 공개)", "private (비공개)"],
                index=0,
                key="yt_privacy_select"
            )
            priv = privacy_choice.split()[0]
        with up_c2:
            st.caption("🔒 OAuth 미인증 환경에서는 안전한 시뮬레이션(Dry-Run) 모드로 작동합니다.")

        if st.button("🚀 유튜브 쇼츠 즉시 업로드 실행", type="primary", use_container_width=True):
            with st.spinner("메타데이터 검증 및 유튜브 업로드 파이프라인 진행 중..."):
                try:
                    importlib.reload(module4_uploader)
                    upload_res = module4_uploader.run_pipeline_step4(
                        script,
                        video_path=Path(video_file),
                        privacy_status=priv
                    )
                    if upload_res.get("dry_run"):
                        st.success(f"✅ {upload_res['message']}")
                        st.info(f"🔗 [시뮬레이션 쇼츠 링크]({upload_res['video_url']}) · 영상 ID: `{upload_res['video_id']}` ({upload_res['file_size_mb']}MB)")
                    else:
                        st.balloons()
                        st.success(f"🎉 {upload_res['message']} 영상 ID: `{upload_res['video_id']}`")
                        st.markdown(f"👉 **[유튜브 쇼츠 바로가기]({upload_res['video_url']})**")

                    with st.expander("📋 업로드 응답 세부 결과", expanded=False):
                        st.json(upload_res)
                except Exception as e:
                    st.error(f"업로드 실행 실패: {e}")

        st.markdown("</div>", unsafe_allow_html=True)


# ------------------------------------------------------------------------------
# [우측 컬럼] 스마트폰 프리뷰 모니터 & 렌더링 독 (Phone Mockup Bezel)
# ------------------------------------------------------------------------------
with col_right:
    st.markdown("""
        <div class="stitch-card" style="padding-bottom: 24px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <div style="font-weight: 700; font-size: 1.05rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>📱</span> 모바일 프리뷰 모니터
                </div>
                <div style="font-size: 0.75rem; color: #10B981; font-weight: 700; background: rgba(16, 185, 129, 0.12); padding: 3px 8px; border-radius: 6px; border: 1px solid rgba(16, 185, 129, 0.25);">
                    9:16 SHORTS
                </div>
            </div>
    """, unsafe_allow_html=True)

    if video_file is not None and Path(video_file).exists():
        video_bytes = Path(video_file).read_bytes()

        # 스마트폰 프레임 내부 비디오 재생 (CSS pseudo-elements로 상단 노치/하단 홈바 자동 렌더링)
        st.video(video_bytes)

        # 렌더링 비디오 스펙 칩 그리드
        dur_text = f"{video_specs['duration_sec']:.2f}s" if (video_specs and video_specs['duration_sec']) else "51.80s"
        size_text = f"{video_specs['size_mb']:.1f} MB" if video_specs else "15.5 MB"
        fps_text = f"{int(video_specs['fps'])} FPS" if video_specs else "30 FPS"
        frame_text = f"{video_specs['frame_cnt']} frames" if (video_specs and video_specs['frame_cnt']) else "1,554 frames"

        st.markdown(f"""
            <div class="spec-grid">
                <div class="spec-chip">
                    <div class="spec-chip-label">해상도 / 비율</div>
                    <div class="spec-chip-value">1080×1920 (9:16)</div>
                </div>
                <div class="spec-chip">
                    <div class="spec-chip-label">재생 시간 (Duration)</div>
                    <div class="spec-chip-value" style="color: #60A5FA;">{dur_text}</div>
                </div>
                <div class="spec-chip">
                    <div class="spec-chip-label">프레임 레이트</div>
                    <div class="spec-chip-value">{fps_text} ({frame_text})</div>
                </div>
                <div class="spec-chip">
                    <div class="spec-chip-label">파일 용량</div>
                    <div class="spec-chip-value" style="color: #34D399;">{size_text}</div>
                </div>
            </div>
        """, unsafe_allow_html=True)

        # 다운로드 버튼
        st.download_button(
            "📥 MP4 완성 영상 다운로드",
            data=video_bytes,
            file_name=f"{script.episode_id}.mp4",
            mime="video/mp4",
            use_container_width=True
        )

    else:
        # 비디오 미생성 시 안내 플레이스홀더 (스마트폰 베젤 형태 유지)
        st.markdown("""
            <div style="background: #030712; border: 8px solid #1E293B; border-radius: 38px; padding: 12px 8px; max-width: 320px; margin: 0 auto 16px auto; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.8), 0 0 35px rgba(99,102,241,0.15);">
                <div style="width: 80px; height: 14px; background: #0F172A; border-radius: 12px; margin: 0 auto 12px auto; border: 1px solid rgba(255,255,255,0.08);"></div>
                <div style="min-height: 440px; background: #0A0E1A; border-radius: 22px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 24px;">
                    <span style="font-size: 2.8rem; margin-bottom: 12px; opacity: 0.6;">🎬</span>
                    <div style="font-weight: 700; color: #E2E8F0; margin-bottom: 6px;">렌더링 대기 중</div>
                    <div style="font-size: 0.8rem; color: #64748B; line-height: 1.5; max-width: 200px;">
                        좌측 콘솔에서 BGM 설정 후 <strong>[3단계 렌더링 실행]</strong>을 누르면 실시간 재생됩니다.
                    </div>
                </div>
                <div style="width: 90px; height: 4px; background: rgba(255, 255, 255, 0.28); border-radius: 2px; margin: 12px auto 2px auto;"></div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)
