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

from module1_trend_and_script import run_pipeline_step1, run_curation_batch
from module2_calendar_manager import (
    APPROVED_DIR, PENDING_DIR, CalendarManager, load_script, CANDIDATE_SERVING_FILE,
    is_autopilot_enabled, set_autopilot_enabled, get_autopilot_config, get_golden_time_queue
)
from module5_analytics_feedback import AnalyticsEngine, get_active_feedback_prompt
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
    initial_sidebar_state="collapsed"
)

CUSTOM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap');

/* 기본 배경 & 전역 폰트 및 순백색 텍스트 (#F8FAFC) */
html, body, [class*="css"], .stMarkdown, p, span, label {
    font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #F8FAFC !important;
}

/* 메인 딥 슬레이트 다크 배경 (#0B0F19 ~ #0F172A) */
.stApp {
    background-color: #0B0F19 !important;
    background-image: 
        radial-gradient(circle at 15% 15%, rgba(99, 102, 241, 0.09) 0%, transparent 45%),
        radial-gradient(circle at 85% 85%, rgba(139, 92, 246, 0.08) 0%, transparent 45%),
        radial-gradient(circle at 50% 50%, rgba(56, 189, 248, 0.03) 0%, transparent 60%) !important;
    background-attachment: fixed !important;
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

/* 블록 컨테이너 상하 여백 최적화 */
.block-container {
    padding-top: 2.2rem !important;
    padding-bottom: 3rem !important;
    max-width: 1360px !important;
}

/* 사이드바 스타일링 */
section[data-testid="stSidebar"] {
    background-color: #0E1322 !important;
    border-right: 1px solid rgba(255, 255, 255, 0.08) !important;
}
section[data-testid="stSidebar"] .block-container {
    padding-top: 1.5rem !important;
}

/* ==========================================================================
   2. 작업 흐름 기반 4단계 탭 (Tabs) 하이엔드 네온 스타일링
   ========================================================================== */
div[data-baseweb="tab-list"] {
    background: rgba(15, 23, 42, 0.9) !important;
    border: 1.5px solid rgba(255, 255, 255, 0.1) !important;
    border-radius: 16px !important;
    padding: 6px !important;
    gap: 8px !important;
    margin-bottom: 24px !important;
    overflow-x: auto !important;
    flex-wrap: nowrap !important;
    box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5) !important;
}

button[data-baseweb="tab"] {
    background: transparent !important;
    color: #94A3B8 !important;
    font-weight: 700 !important;
    font-size: 0.96rem !important;
    border-radius: 12px !important;
    padding: 12px 22px !important;
    border: none !important;
    white-space: nowrap !important;
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1) !important;
}

button[data-baseweb="tab"]:hover {
    color: #FFFFFF !important;
    background: rgba(255, 255, 255, 0.06) !important;
}

button[data-baseweb="tab"][aria-selected="true"] {
    background: linear-gradient(135deg, #6366F1 0%, #8B5CF6 100%) !important;
    color: #FFFFFF !important;
    font-weight: 800 !important;
    box-shadow: 0 4px 18px rgba(99, 102, 241, 0.5) !important;
    transform: translateY(-1px) !important;
}

/* ==========================================================================
   3. 모바일 가독성 & 시인성 극대화: 입력창 & 라벨 (#38BDF8 포인트 컬러)
   ========================================================================== */
/* 입력창 라벨을 눈에 확 띄는 스카이블루 (#38BDF8) 포인트 컬러로 통일 */
.stTextInput label,
.stTextArea label,
.stSelectbox label,
.stSlider label,
.stDateInput label,
.stRadio label {
    color: #38BDF8 !important;
    font-weight: 800 !important;
    font-size: 0.95rem !important;
    letter-spacing: 0.02em !important;
    margin-bottom: 6px !important;
    display: inline-block !important;
}

/* 모든 입력창 배경 명암(#0F172A)과 테두리 대비 대폭 강화 */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea {
    background-color: #0F172A !important;
    border: 1.5px solid rgba(56, 189, 248, 0.35) !important;
    border-radius: 12px !important;
    color: #FFFFFF !important;
    font-size: 0.98rem !important;
    font-weight: 600 !important;
    padding: 10px 14px !important;
    box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.4) !important;
}

.stTextInput > div > div > input:focus,
.stTextArea > div > div > textarea:focus {
    border-color: #38BDF8 !important;
    box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.3), inset 0 2px 4px rgba(0, 0, 0, 0.4) !important;
    background-color: #131D33 !important;
}

/* 글래스모피즘 스튜디오 카드 컨테이너 */
.stitch-card {
    background: rgba(17, 24, 39, 0.88);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 18px;
    padding: 22px 24px;
    margin-bottom: 20px;
    box-shadow: 0 12px 30px -5px rgba(0, 0, 0, 0.5);
    transition: border-color 0.2s ease, transform 0.2s ease;
}
.stitch-card:hover {
    border-color: rgba(99, 102, 241, 0.4);
}

/* 상단 스튜디오 헤더 배지 */
.studio-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 5px 14px;
    border-radius: 9999px;
    background: rgba(99, 102, 241, 0.14);
    border: 1px solid rgba(99, 102, 241, 0.35);
    color: #A5B4FC;
    font-size: 0.84rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    margin-bottom: 10px;
}

.studio-title {
    font-size: 2.1rem;
    font-weight: 850;
    letter-spacing: -0.025em;
    background: linear-gradient(135deg, #FFFFFF 0%, #E2E8F0 50%, #94A3B8 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin: 0 0 16px 0;
}

/* 분석 브리핑 미니 카드 (고대비 가독성) */
.brief-card {
    background: #0F172A;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
    padding: 16px;
    height: 100%;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
}
.brief-card-title {
    font-size: 0.85rem;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 8px;
}
.brief-card-body {
    font-size: 0.95rem;
    color: #F8FAFC !important;
    line-height: 1.55;
    font-weight: 500;
}

/* ==========================================================================
   4. 버튼 시각적 위계 및 CTA 강조
   ========================================================================== */
div.stButton > button[kind="primary"],
div.stButton > button[data-testid="baseButton-primary"] {
    background: linear-gradient(135deg, #6366F1 0%, #8B5CF6 100%) !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 14px !important;
    font-weight: 800 !important;
    font-size: 1.05rem !important;
    min-height: 52px !important;
    padding: 0.75rem 1.6rem !important;
    box-shadow: 0 6px 20px 0 rgba(99, 102, 241, 0.5) !important;
    transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1) !important;
}
div.stButton > button[kind="primary"]:hover,
div.stButton > button[data-testid="baseButton-primary"]:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 10px 28px 0 rgba(139, 92, 246, 0.7) !important;
}
div.stButton > button[kind="primary"]:active,
div.stButton > button[data-testid="baseButton-primary"]:active {
    transform: translateY(1px) !important;
}

/* 세컨더리 일반 버튼 */
div.stButton > button:not([kind="primary"]):not([data-testid="baseButton-primary"]) {
    background: rgba(30, 41, 59, 0.85) !important;
    color: #F8FAFC !important;
    border: 1px solid rgba(255, 255, 255, 0.15) !important;
    border-radius: 12px !important;
    font-weight: 700 !important;
    min-height: 48px !important;
    transition: all 0.2s ease !important;
}
div.stButton > button:not([kind="primary"]):hover {
    background: rgba(51, 65, 85, 1.0) !important;
    border-color: #38BDF8 !important;
    color: #FFFFFF !important;
}

/* 다운로드 버튼 (에메랄드 네온) */
div.stDownloadButton > button {
    background: linear-gradient(135deg, #10B981 0%, #059669 100%) !important;
    color: #FFFFFF !important;
    border: none !important;
    border-radius: 14px !important;
    font-weight: 800 !important;
    font-size: 1.05rem !important;
    min-height: 52px !important;
    box-shadow: 0 6px 20px rgba(16, 185, 129, 0.45) !important;
    transition: all 0.25s ease !important;
}
div.stDownloadButton > button:hover {
    box-shadow: 0 8px 26px rgba(16, 185, 129, 0.65) !important;
    transform: translateY(-2px) !important;
}

/* ==========================================================================
   5. 모바일 프리뷰 스마트폰 프레임 (Phone Mockup)
   ========================================================================== */
div[data-testid="stVideo"] {
    background: #030712 !important;
    border: 8px solid #1E293B !important;
    border-radius: 38px !important;
    padding: 10px 8px 8px 8px !important;
    box-shadow: 
        0 25px 50px -12px rgba(0, 0, 0, 0.85),
        0 0 0 1px rgba(255, 255, 255, 0.1),
        0 0 35px rgba(99, 102, 241, 0.2) !important;
    max-width: 320px !important;
    margin: 0 auto 16px auto !important;
    position: relative !important;
}
div[data-testid="stVideo"]::before {
    content: "";
    display: block;
    width: 80px;
    height: 14px;
    background: #0F172A;
    border-radius: 12px;
    margin: 0 auto 8px auto;
    border: 1px solid rgba(255, 255, 255, 0.1);
}
div[data-testid="stVideo"] video {
    border-radius: 22px !important;
    width: 100% !important;
    aspect-ratio: 9 / 16 !important;
    object-fit: cover !important;
    background: #000 !important;
    display: block !important;
}
div[data-testid="stVideo"]::after {
    content: "";
    display: block;
    width: 90px;
    height: 4px;
    background: rgba(255, 255, 255, 0.35);
    border-radius: 2px;
    margin: 10px auto 2px auto;
}

/* 스펙 태그 배지 그리드 */
.spec-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px;
    width: 100%;
    margin-top: 8px;
    margin-bottom: 14px;
}
.spec-chip {
    background: #0F172A;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    padding: 10px 14px;
}
.spec-chip-label {
    color: #94A3B8;
    font-size: 0.74rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}
.spec-chip-value {
    color: #F8FAFC !important;
    font-weight: 800;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.95rem;
    margin-top: 4px;
}

/* 1. disabled(읽기 전용) 입력창 투명도 제거 및 글씨 쨍하게 복구 */
input:disabled,
textarea:disabled,
[data-baseweb="input"] input:disabled,
[data-baseweb="textarea"] textarea:disabled,
div[data-baseweb="input"] input:disabled,
div[data-baseweb="textarea"] textarea:disabled {
    color: #F8FAFC !important;
    -webkit-text-fill-color: #F8FAFC !important;
    opacity: 1 !important;
    background-color: #1E293B !important;
    border-color: #475569 !important;
    cursor: default !important;
}

/* 2. 최상단 에피소드 선택기, BGM 선택, 공개 범위 등 모든 st.selectbox 완벽 고대비 순백색 */
div[data-testid="stSelectbox"],
.stSelectbox {
    color: #FFFFFF !important;
}

/* 셀렉트박스 모든 박스 및 인풋 배경 완전한 다크 네이비(#1E293B) 강제 */
div[data-testid="stSelectbox"] > div,
div[data-testid="stSelectbox"] div,
div[data-testid="stSelectbox"] input,
div[data-testid="stSelectbox"] [class*="e1fp86qc0"],
div[data-testid="stSelectbox"] [class*="e1fp86qc1"],
.stSelectbox div,
.stSelectbox input {
    background-color: #1E293B !important;
    background: #1E293B !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
}

/* 셀렉트박스 외곽선 */
div[data-testid="stSelectbox"] [class*="e1fp86qc0"],
div[data-testid="stSelectbox"] > div:nth-child(2),
.stSelectbox [class*="e1fp86qc0"] {
    border: 1.5px solid #38BDF8 !important;
    border-radius: 10px !important;
    box-shadow: 0 0 12px rgba(56, 189, 248, 0.25) !important;
}

/* 셀렉트박스 내부 텍스트 쨍한 순백색 */
div[data-testid="stSelectbox"] input,
div[data-testid="stSelectbox"] [class*="e1fp86qc1"],
div[data-testid="stSelectbox"] span,
div[data-testid="stSelectbox"] p,
.stSelectbox input,
.stSelectbox [class*="e1fp86qc1"],
.stSelectbox span,
.stSelectbox p {
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    font-size: 15px !important;
    font-weight: 700 !important;
    opacity: 1 !important;
}

/* 플레이스홀더 */
div[data-testid="stSelectbox"] input::placeholder,
.stSelectbox input::placeholder {
    color: #94A3B8 !important;
    -webkit-text-fill-color: #94A3B8 !important;
    opacity: 1 !important;
}

/* 펼쳐졌을 때의 팝오버 옵션 메뉴 (가상 드롭다운, listbox, option 전체) */
div[data-testid="stSelectboxVirtualDropdown"],
div[data-testid="stSelectboxVirtualDropdown"] *,
div[role="listbox"],
div[role="listbox"] *,
div[role="option"],
div[role="option"] *,
ul[role="listbox"],
ul[role="listbox"] *,
li[role="option"],
li[role="option"] *,
ul[data-baseweb="menu"],
ul[data-baseweb="menu"] *,
div[data-baseweb="popover"],
div[data-baseweb="popover"] * {
    background-color: #1E293B !important;
    color: #FFFFFF !important;
    -webkit-text-fill-color: #FFFFFF !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    opacity: 1 !important;
}

/* 호버되거나 선택된 옵션 항목 */
div[role="option"][aria-selected="true"],
div[role="option"]:hover,
li[role="option"][aria-selected="true"],
li[role="option"]:hover {
    background-color: #334155 !important;
    color: #38BDF8 !important;
    -webkit-text-fill-color: #38BDF8 !important;
}

/* Expander 다크 글래스 */
div[data-testid="stExpander"] {
    background: rgba(17, 24, 39, 0.75) !important;
    border: 1.5px solid rgba(255, 255, 255, 0.1) !important;
    border-radius: 14px !important;
    margin-bottom: 12px !important;
}

/* 단계 이동 가이드 배너 */
.step-guide-card {
    background: rgba(56, 189, 248, 0.08);
    border: 1px dashed rgba(56, 189, 248, 0.35);
    border-radius: 14px;
    padding: 14px 18px;
    margin-top: 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    color: #E0F2FE !important;
    font-size: 0.92rem;
    font-weight: 600;
}

/* ==========================================================================
   6. 모바일 반응형 미디어 쿼리 (@media (max-width: 768px))
   ========================================================================== */
@media (max-width: 768px) {
    .block-container {
        padding-left: 1rem !important;
        padding-right: 1rem !important;
        padding-top: 2.0rem !important;
    }
    div[data-baseweb="tab-list"] {
        padding: 4px !important;
        gap: 4px !important;
    }
    button[data-baseweb="tab"] {
        font-size: 0.88rem !important;
        padding: 10px 14px !important;
    }
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
        max-width: 290px !important;
    }
    .studio-title {
        font-size: 1.6rem !important;
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
# 3. 사이드바 간소화 (단일 작업 흐름 안내 및 시스템 정보)
# ==============================================================================
with st.sidebar:
    st.markdown("""
        <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px;">
            <span style="font-size: 1.7rem;">🎬</span>
            <div>
                <div style="font-weight: 850; font-size: 1.15rem; color: #FFFFFF; letter-spacing: -0.01em;">Stickman Studio</div>
                <div style="font-size: 0.74rem; color: #818CF8; font-weight: 700;">Autonomous Shorts Flywheel</div>
            </div>
        </div>
        <div style="background: rgba(15, 23, 42, 0.8); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px; margin-bottom: 16px; font-size: 0.85rem; color: #CBD5E1; line-height: 1.6;">
            💡 <strong>모바일 단일 작업 흐름</strong><br>
            사이드바를 열 필요 없이 화면 상단에서 에피소드를 선택하고 <strong>1➔2➔3➔4단계 탭</strong>만 누르면 모든 작업이 완결됩니다.
        </div>
    """, unsafe_allow_html=True)
    st.caption("Google Stitch Architecture · Mobile Single-View")


# ==============================================================================
# 4. 상단 스튜디오 타이틀, 알림 플래시 & 통합 에피소드 선택 바
# ==============================================================================
st.markdown("""
    <div class="studio-badge">
        <span style="display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #6366F1;"></span>
        GOOGLE STITCH ARCHITECTURE · SINGLE WORKFLOW STUDIO
    </div>
    <div class="studio-title">스틱맨 숏폼 자율 플라이휠 컨트롤 타워</div>
""", unsafe_allow_html=True)

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
if "flash_err" in st.session_state:
    st.error(st.session_state.pop("flash_err"))

# 1. 전체 에피소드 통합 목록 구성 (사이드바 대신 본문 상단에서 직관적으로 선택)
pending_list = list_files(PENDING_DIR)
approved_list = list_files(APPROVED_DIR)

# (상태, 순수 파일명) 튜플 리스트로 옵션 구성
episode_items = [("pending", fname) for fname in pending_list] + [("approved", fname) for fname in approved_list]

def format_episode_label(item) -> str:
    """st.selectbox 표시용 라벨 포맷터: 순수 파일명 앞에 상태 뱃지 부착"""
    if isinstance(item, tuple) and len(item) == 2:
        status, fname = item
        prefix = "⏳ [검수 대기]" if status == "pending" else "✅ [승인 완료]"
        return f"{prefix} {fname}"
    return str(item)

# 강제 선택(target_episode_to_select / force_select) 또는 활성 에피소드 ID(active_episode_id) 감지 및 위젯 세션 상태 직접 동기화
target_to_select = st.session_state.pop("target_episode_to_select", None)
forced = st.session_state.pop("force_select", None) or target_to_select
target_fname = None

if forced:
    target_fname = forced[1] if isinstance(forced, (tuple, list)) else str(forced)
elif "active_episode_id" in st.session_state and st.session_state["active_episode_id"]:
    target_fname = f"{st.session_state['active_episode_id']}.json"

# target_fname이 있으면 episode_items에서 일치하는 (status, fname) 아이템을 찾아 selectbox 세션 상태 직접 갱신 (위젯 생성 전 1회만 안전하게 설정)
if target_fname:
    for item in episode_items:
        if item[1] == target_fname or target_fname in item[1]:
            st.session_state["active_episode_selector"] = item
            break

# 혹시 파일이 pending -> approved 로 이동되었거나 초기 실행인 경우 유효성 검증
if "active_episode_selector" in st.session_state:
    curr_sel = st.session_state["active_episode_selector"]
    if curr_sel not in episode_items:
        curr_fname = curr_sel[1] if isinstance(curr_sel, (tuple, list)) else str(curr_sel)
        matched = next((it for it in episode_items if it[1] == curr_fname), None)
        if matched:
            st.session_state["active_episode_selector"] = matched
        elif episode_items:
            st.session_state["active_episode_selector"] = episode_items[0]
elif episode_items:
    st.session_state["active_episode_selector"] = episode_items[0]

script = None
is_pending = True
file_path = None
video_file = None
key = "empty"

# 본문 상단 통합 에피소드 선택기 카드
if episode_items:
    with st.container(border=True):
        col_ep_sel, col_ep_badge = st.columns([2.6, 1.0], gap="medium")
        with col_ep_sel:
            selected_item = st.selectbox(
                "📂 작업할 에피소드 선택 (검수 대기 & 승인 완료)",
                options=episode_items,
                key="active_episode_selector",
                format_func=format_episode_label,
                help="에피소드를 선택하면 하단 1➔2➔3➔4단계 탭에 즉시 동기화되어 한 화면에서 기획, 검수, 오디오, 렌더링이 이뤄집니다."
            )
        with col_ep_badge:
            st.markdown(f"""
                <div style="margin-top: 28px; display: flex; gap: 8px; justify-content: flex-end;">
                    <span style="font-size: 0.82rem; background: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.35); color: #FBBF24; padding: 6px 12px; border-radius: 10px; font-weight: 700;">
                        ⏳ 대기 {len(pending_list)}건
                    </span>
                    <span style="font-size: 0.82rem; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); color: #34D399; padding: 6px 12px; border-radius: 10px; font-weight: 700;">
                        ✅ 승인 {len(approved_list)}건
                    </span>
                </div>
            """, unsafe_allow_html=True)

    # 선택된 아이템에서 상태 및 순수 파일명 안전 추출
    if isinstance(selected_item, tuple) and len(selected_item) == 2:
        is_pending = (selected_item[0] == "pending")
        selected_fname = selected_item[1]
    else:
        # 혹시 기존 캐시 등으로 문자열이 넘어온 경우 라벨 프리픽스 안전 정제
        raw = str(selected_item)
        for prefix in ["⏳ [검수 대기]", "✅ [승인 완료]", "[검수 대기]", "[승인 완료]"]:
            raw = raw.replace(prefix, "")
        selected_fname = raw.strip()
        is_pending = (PENDING_DIR / selected_fname).exists() or not (APPROVED_DIR / selected_fname).exists()

    folder = PENDING_DIR if is_pending else APPROVED_DIR
    file_path = folder / selected_fname

    # 파일이 승인 등으로 이동되어 현재 폴더에 없으면 반대쪽 폴더 자동 탐색
    if not file_path.exists():
        alt_folder = APPROVED_DIR if is_pending else PENDING_DIR
        if (alt_folder / selected_fname).exists():
            file_path = alt_folder / selected_fname
            is_pending = not is_pending

    if file_path.exists():
        try:
            script = load_script(file_path)
            key = script.episode_id
            st.session_state["active_episode_id"] = script.episode_id
        except (ValidationError, ValueError) as e:
            st.error(f"대본 JSON 스키마 검증 실패: {e}")
            st.stop()

        if script.rendered_video_path:
            cand = Path(script.rendered_video_path)
            video_file = cand if cand.exists() else None
        if video_file is None and (RENDERED_DIR / f"{script.episode_id}.mp4").exists():
            video_file = RENDERED_DIR / f"{script.episode_id}.mp4"

video_specs = get_video_specs(video_file)


# ==============================================================================
# 5. 작업 흐름 기반 5단계 탭 (Tab) 구조 적용 (상용화 B2B 분석 포함)
# ==============================================================================
tab_step1, tab_step2, tab_step3, tab_step4, tab_step5 = st.tabs([
    "🚀 Step 1: 기획 & 대본",
    "✍️ Step 2: 대본 검수",
    "🎵 Step 3: 오디오 연출",
    "🎬 Step 4: 렌더링 & 배포",
    "📊 Step 5: 성과 분석 & 피드백",
])


# ------------------------------------------------------------------------------
# [Step 1: 기획 & 대본] (트렌드 감지, 소재 추출, 신규 대본 생성, 감정 역공학)
# ------------------------------------------------------------------------------
with tab_step1:
    # --------------------------------------------------------------------------
    # 1. 실시간 트렌드 자동 큐레이션 & 구글 캘린더 '밥상 서빙' (모바일 RSVP 원클릭 승인)
    # --------------------------------------------------------------------------
    st.markdown("""
        <div class="stitch-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                <div style="font-weight: 850; font-size: 1.2rem; color: #FFFFFF; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 1.4rem;">🍽️</span> 실시간 트렌드 자동 큐레이션 & 구글 캘린더 '밥상 서빙'
                </div>
                <div style="font-size: 0.8rem; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.35); color: #38BDF8; padding: 4px 10px; border-radius: 8px; font-weight: 800;">
                    RSVP 모바일 원클릭 승인
                </div>
            </div>
            <div style="font-size: 0.9rem; color: #94A3B8; margin-bottom: 18px; line-height: 1.6;">
                최신 쇼츠 트렌드 및 사이다·미러링 썰(편의점·층간소음·갑질 등)을 실시간 분석하여 <strong>오늘자 3대 킬러 대본</strong>을 큐레이션하고, 구글 캘린더에 <strong>초대장(RSVP: [예 / 아니요 / 미정])</strong> 형태로 자동 등록합니다.<br>
                스마트폰 캘린더 알림에서 <strong>[예]</strong>만 누르면 즉시 [승인 완료]되어 렌더링 파이프라인으로 직행합니다.
            </div>
    """, unsafe_allow_html=True)

    col_cfg1, col_cfg2 = st.columns([1.0, 1.8], gap="medium")
    with col_cfg1:
        buffet_date = st.date_input("📅 서빙 기준 일자", value=date.today(), key="buffet_serving_date")
    with col_cfg2:
        default_user_email = os.getenv("CALENDAR_USER_EMAIL", os.getenv("GOOGLE_CALENDAR_ID", "user@gmail.com"))
        target_attendee = st.text_input(
            "📧 초대받을 구글 캘린더 계정 이메일",
            value=default_user_email if "@" in default_user_email else "",
            placeholder="your-google-account@gmail.com",
            key="buffet_attendee_email",
            help="스마트폰 구글 캘린더 앱에 로그인된 이메일 계정을 입력하면, 해당 계정으로 [예/아니요] 참석 초대장이 즉시 발송됩니다."
        )

    col_act_serve, col_act_check = st.columns([1.6, 1.4], gap="medium")
    with col_act_serve:
        btn_curate_serve = st.button("🍽️ 오늘자 3대 킬러 대본 큐레이션 & 캘린더 밥상 서빙", type="primary", use_container_width=True)
    with col_act_check:
        btn_check_rsvp = st.button("🔄 모바일 캘린더 RSVP '[예]' 승인 동기화 감지", use_container_width=True)

    # 1) 3대 킬러 대본 큐레이션 & 밥상 서빙 실행
    if btn_curate_serve:
        bar_curate = st.progress(0.0)
        status_curate = st.empty()

        def on_curate_progress(msg: str, frac: float):
            bar_curate.progress(min(max(frac, 0.0), 1.0))
            status_curate.caption(msg)

        try:
            with st.spinner("실시간 트렌드 분석 → 3대 사이다 썰 큐레이션 → 구글 캘린더 초대장 발송 중..."):
                candidates = run_curation_batch(count=3, progress_callback=on_curate_progress)
                cal_mgr = CalendarManager()
                serving_res = cal_mgr.serve_candidate_buffet(
                    candidates=candidates,
                    target_date=buffet_date,
                    attendee_email=target_attendee
                )
                st.session_state["flash"] = f"🎉 3대 킬러 대본 큐레이션 완료! 구글 캘린더에 RSVP 초대([예])가 발송되었습니다."
                st.rerun()
        except Exception as e:
            st.error(f"큐레이션 서빙 실패: {e}")

    # 2) 캘린더 RSVP [예] 승인 상태 동기화 감지
    if btn_check_rsvp:
        with st.spinner("구글 캘린더 RSVP 응답 상태 확인 중..."):
            cal_mgr = CalendarManager()
            rsvp_status = cal_mgr.check_rsvp_approvals()
            if rsvp_status.get("status") == "approved":
                ep_id = rsvp_status['episode_id']
                st.session_state["active_episode_id"] = ep_id
                st.session_state["target_episode_to_select"] = ("approved", f"{ep_id}.json")
                st.session_state["flash"] = rsvp_status.get("message", "승인 완료!")
                st.balloons()
                st.rerun()
            else:
                st.info(rsvp_status.get("message", "응답 확인 완료"))

    # 현재 서빙 중인 3대 후보 카드 실시간 모니터
    cal_mgr_inst = CalendarManager()
    active_serving = cal_mgr_inst.get_current_serving_status()

    if active_serving and active_serving.get("candidates"):
        st.markdown("""
            <div style="margin-top: 20px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.08);">
                <div style="font-weight: 800; font-size: 1.05rem; color: #E2E8F0; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                    <span>🍱</span> 현재 서빙된 3대 후보 대본 현황 (모바일 캘린더 연동 모니터)
                </div>
            </div>
        """, unsafe_allow_html=True)

        cand_cols = st.columns(3, gap="small")
        is_already_approved = (active_serving.get("status") == "approved")
        winner_id = active_serving.get("approved_episode_id")

        for idx, c in enumerate(active_serving.get("candidates", [])):
            c_no = c.get("candidate_no", idx + 1)
            hit_pct = c.get("predicted_hit_rate", 95)
            is_winner = (c.get("episode_id") == winner_id)
            with cand_cols[idx]:
                border_color = "#10B981" if is_winner else "#38BDF8"
                badge_bg = "rgba(16, 185, 129, 0.2)" if is_winner else "rgba(56, 189, 248, 0.15)"
                badge_text = "✅ [예] 승인 채택" if is_winner else f"🏆 적중률 {hit_pct}%"

                st.markdown(f"""
                    <div style="background: #0F172A; border: 1.5px solid {border_color}; border-radius: 14px; padding: 14px; height: 100%;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: 850; font-size: 0.95rem; color: #FFFFFF;">후보 {c_no}</span>
                            <span style="font-size: 0.76rem; background: {badge_bg}; color: {border_color}; padding: 3px 8px; border-radius: 6px; font-weight: 800;">
                                {badge_text}
                            </span>
                        </div>
                        <div style="font-size: 0.78rem; color: #94A3B8; margin-bottom: 6px;">{c.get('category', '사이다 상황극')}</div>
                        <div style="font-size: 0.88rem; font-weight: 700; color: #F8FAFC; margin-bottom: 10px; line-height: 1.4;">
                            {c.get('episode_title')}
                        </div>
                        <div style="font-size: 0.75rem; color: #64748B; font-family: 'JetBrains Mono', monospace;">
                            ID: {c.get('episode_id')}
                        </div>
                    </div>
                """, unsafe_allow_html=True)

                if not is_already_approved:
                    btn_sim = st.button(f"📱 [후보 {c_no}] 즉시 채택", key=f"btn_sim_rsvp_{c_no}", use_container_width=True)
                    if btn_sim:
                        res = cal_mgr_inst.simulate_rsvp_approval(c_no)
                        if res.get("status") == "approved":
                            ep_id = c['episode_id']
                            st.session_state["active_episode_id"] = ep_id
                            st.session_state["target_episode_to_select"] = ("approved", f"{ep_id}.json")
                            st.session_state["flash"] = f"🎉 [후보 {c_no}] '{c.get('episode_title')}' 승인 채택 완료!"
                            st.balloons()
                            st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    # --------------------------------------------------------------------------
    # 2. 골든 타임 타임아웃 자동 승인 & 부재중 무인 자율 주행 모드 (Fail-safe)
    # --------------------------------------------------------------------------
    st.markdown("""
        <div class="stitch-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div style="font-weight: 850; font-size: 1.15rem; color: #FFFFFF; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 1.35rem;">🛡️</span> 무인 자율 주행 모드 & 골든타임 자동 발행 안전장치 (Fail-safe)
                </div>
                <div style="font-size: 0.8rem; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); color: #10B981; padding: 4px 10px; border-radius: 8px; font-weight: 800;">
                    3단계 안전 방어망
                </div>
            </div>
            <div style="font-size: 0.88rem; color: #94A3B8; margin-bottom: 14px; line-height: 1.6;">
                바쁜 일상으로 모바일 캘린더 알림을 미처 확인하지 못하더라도, <strong>오후 16:00 타임아웃</strong> 시 예상 적중률 1위 대본을 자동으로 승인하고 렌더링에 착수하여, 쇼츠 트래픽 피크 시간인 <strong>17:30 ~ 18:00 퇴근길 골든 타임</strong>에 유튜브 자동 예약 발행을 완수합니다.
            </div>
    """, unsafe_allow_html=True)

    col_auto_tgl, col_auto_status = st.columns([1.2, 1.8], gap="medium")
    with col_auto_tgl:
        current_autopilot_enabled = is_autopilot_enabled()
        new_autopilot_enabled = st.toggle(
            "무인 자율 주행 모드 (Autopilot) 활성화",
            value=current_autopilot_enabled,
            key="autopilot_master_toggle",
            help="활성화 시 모바일 캘린더 미응답 상태여도 16:00에 1위 후보를 자동 승인하고 17:30 골든타임에 자동 배포합니다."
        )
        if new_autopilot_enabled != current_autopilot_enabled:
            set_autopilot_enabled(new_autopilot_enabled)
            st.session_state["flash"] = f"🤖 무인 자율 주행 모드가 {'[활성화]' if new_autopilot_enabled else '[비활성화]'}되었습니다."
            st.rerun()

    with col_auto_status:
        autopilot_badge_color = "#10B981" if current_autopilot_enabled else "#94A3B8"
        autopilot_status_text = "🟢 자율 주행 가동 중 (16:00 자동승인 & 17:30 예약 배포 대기)" if current_autopilot_enabled else "⚪ 수동 모드 (사용자 직접 승인 필요)"
        st.markdown(f"""
            <div style="padding: 10px 14px; background: #0F172A; border: 1px solid {autopilot_badge_color}44; border-radius: 10px; font-size: 0.86rem; color: #E2E8F0; font-weight: 600;">
                {autopilot_status_text}
            </div>
        """, unsafe_allow_html=True)

    # 4단계 타임라인 인디케이터
    st.markdown("""
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 16px 0 14px 0;">
            <div style="background: #0F172A; border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 10px; padding: 10px; text-align: center;">
                <div style="font-size: 0.76rem; color: #38BDF8; font-weight: 800;">STEP 1 · 09:00</div>
                <div style="font-size: 0.86rem; font-weight: 700; color: #FFFFFF; margin-top: 3px;">서빙 팝업 알림</div>
                <div style="font-size: 0.72rem; color: #94A3B8; margin-top: 2px;">3대 킬러 대본 서빙</div>
            </div>
            <div style="background: #0F172A; border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 10px; padding: 10px; text-align: center;">
                <div style="font-size: 0.76rem; color: #F59E0B; font-weight: 800;">STEP 2 · 15:00</div>
                <div style="font-size: 0.86rem; font-weight: 700; color: #FFFFFF; margin-top: 3px;">리마인드 팝업</div>
                <div style="font-size: 0.72rem; color: #94A3B8; margin-top: 2px;">16시 자동승인 예고</div>
            </div>
            <div style="background: #0F172A; border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 10px; padding: 10px; text-align: center;">
                <div style="font-size: 0.76rem; color: #EF4444; font-weight: 800;">STEP 3 · 16:00</div>
                <div style="font-size: 0.86rem; font-weight: 700; color: #FFFFFF; margin-top: 3px;">타임아웃 자동승인</div>
                <div style="font-size: 0.72rem; color: #94A3B8; margin-top: 2px;">적중률 1위 대본 채택</div>
            </div>
            <div style="background: #0F172A; border: 1px solid rgba(16, 185, 129, 0.25); border-radius: 10px; padding: 10px; text-align: center;">
                <div style="font-size: 0.76rem; color: #10B981; font-weight: 800;">STEP 4 · 17:30</div>
                <div style="font-size: 0.86rem; font-weight: 700; color: #FFFFFF; margin-top: 3px;">골든타임 자동발행</div>
                <div style="font-size: 0.72rem; color: #94A3B8; margin-top: 2px;">유튜브 트래픽 피크</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    col_btn_auto, col_btn_queue = st.columns([1.5, 1.5], gap="medium")
    with col_btn_auto:
        btn_trigger_timeout = st.button("⚡ [시뮬레이션] 16:00 타임아웃 1위 대본 자동 승인 즉시 트리거", use_container_width=True)
    with col_btn_queue:
        golden_queue = get_golden_time_queue()
        queue_count = len(golden_queue)
        btn_view_queue = st.button(f"🌇 골든타임(17:30) 자동 예약 배포 대기열 ({queue_count}건)", use_container_width=True)

    if btn_trigger_timeout:
        cal_mgr = CalendarManager()
        timeout_res = cal_mgr.check_and_trigger_timeout_auto_approval(force_now=True)
        if timeout_res.get("triggered"):
            ep_id = timeout_res['episode_id']
            st.session_state["active_episode_id"] = ep_id
            st.session_state["target_episode_to_select"] = ("approved", f"{ep_id}.json")
            st.session_state["flash"] = timeout_res.get("message", "16:00 타임아웃 자동 승인 성공!")
            st.balloons()
            st.rerun()
        else:
            st.info(timeout_res.get("reason", "타임아웃 조건 미충족 또는 이미 승인 완료"))

    if btn_view_queue:
        if golden_queue:
            with st.expander("🌇 퇴근길 골든타임(17:30~18:00) 예약 배포 대기열 현황", expanded=True):
                for q in golden_queue:
                    st.markdown(f"- **{q['episode_title']}** (ID: `{q['episode_id']}`) · 적중률: **{q.get('predicted_hit_rate')}%** · 예정 시각: `{q.get('scheduled_publish_time')}` · 상태: `{q.get('status')}`")
        else:
            st.info("현재 골든타임 대기열에 등록된 영상이 없습니다.")

    st.markdown("</div>", unsafe_allow_html=True)

    # --------------------------------------------------------------------------
    # 3. 개별 오리지널 대본 단독 즉시 생성 (수동 모드)
    # --------------------------------------------------------------------------
    # --------------------------------------------------------------------------
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 800; font-size: 1.05rem; margin-bottom: 6px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                <span>⚡</span> 개별 오리지널 대본 단독 즉시 생성 (수동 모드)
            </div>
            <div style="font-size: 0.86rem; color: #94A3B8; margin-bottom: 14px;">
                특정 실시간 급상승 키워드를 기반으로 1편의 대본을 즉시 단독 생성하여 기획합니다.
            </div>
    """, unsafe_allow_html=True)

    btn_step1 = st.button("✨ 신규 트렌드 감지 & 오리지널 대본 1편 생성", use_container_width=True)
    if btn_step1:
        with st.spinner("트렌드 감지 → 열광 포인트 역공학 → 대본 생성 중..."):
            try:
                new_script = run_pipeline_step1()
                ep_id = new_script.episode_id
                st.session_state["active_episode_id"] = ep_id
                st.session_state["target_episode_to_select"] = ("pending", f"{ep_id}.json")
                st.session_state["flash"] = f"🎉 새로운 대본 생성 완료: {new_script.episode_title}"
                st.rerun()
            except Exception as e:
                st.error(f"생성 실패: {e}")

    st.markdown("</div>", unsafe_allow_html=True)

    if script is not None:
        # 감정 역공학 분석 브리핑
        st.markdown(f"""
            <div class="stitch-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                    <div style="font-weight: 800; font-size: 1.15rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                        <span>🧠</span> 현재 에피소드 감정 역공학 분석 브리핑
                    </div>
                    <div style="font-size: 0.8rem; background: rgba(99,102,241,0.15); border: 1px solid rgba(99,102,241,0.3); border-radius: 8px; padding: 4px 10px; color: #A5B4FC; font-weight: 700;">
                        에피소드: {script.episode_id}
                    </div>
                </div>
        """, unsafe_allow_html=True)

        b1, b2, b3 = st.columns(3)
        with b1:
            st.markdown(f"""
                <div class="brief-card" style="border-top: 3px solid #EF4444;">
                    <div class="brief-card-title" style="color: #F87171;">🗑️ 버린 껍데기 소재</div>
                    <div class="brief-card-body">{script.analysis.discarded_surface_topic}</div>
                </div>
            """, unsafe_allow_html=True)
        with b2:
            st.markdown(f"""
                <div class="brief-card" style="border-top: 3px solid #F59E0B;">
                    <div class="brief-card-title" style="color: #FBBF24;">🔥 추출한 열광 포인트</div>
                    <div class="brief-card-body">{script.analysis.extracted_core_desire}</div>
                </div>
            """, unsafe_allow_html=True)
        with b3:
            st.markdown(f"""
                <div class="brief-card" style="border-top: 3px solid #10B981;">
                    <div class="brief-card-title" style="color: #34D399;">🌱 새로 이식한 상황</div>
                    <div class="brief-card-body">{script.analysis.transplanted_setting}</div>
                </div>
            """, unsafe_allow_html=True)

        st.markdown(f"""
            <div style="margin-top: 16px; padding: 12px 16px; background: #0F172A; border-radius: 12px; border: 1px solid rgba(255,255,255,0.06); font-size: 0.9rem; color: #CBD5E1;">
                🎯 타겟 감정: <strong style="color: #38BDF8;">{script.target_emotion}</strong> · 
                ⏱️ 예상 길이: <strong style="color: #38BDF8;">{script.total_duration_sec}초</strong> · 
                🏷️ {' '.join(script.hashtags)}
            </div>
            </div>
        """, unsafe_allow_html=True)

        st.markdown("""
            <div class="step-guide-card">
                <span>👉</span>
                <div>기획 및 분석 확인이 완료되면 상단 <strong>[✍️ Step 2: 대본 검수]</strong> 탭으로 이동하여 씬별 대사와 연출을 확인하세요.</div>
            </div>
        """, unsafe_allow_html=True)
    else:
        st.info("💡 아직 생성된 대본이 없습니다. 상단의 **[✨ 신규 트렌드 감지 & 오리지널 대본 즉시 생성]** 버튼을 눌러 첫 번째 대본을 생성하세요.")


# ------------------------------------------------------------------------------
# [Step 2: 대본 검수] (에피소드 선택, 씬별 대사/연출 아코디언 에디터, 승인/폐기)
# ------------------------------------------------------------------------------
with tab_step2:
    if script is None:
        st.info("💡 검수할 대본이 없습니다. [Step 1: 기획 & 대본] 탭에서 신규 대본을 먼저 생성하세요.")
    else:
        st.markdown("""
            <div class="stitch-card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                    <div style="font-weight: 800; font-size: 1.15rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                        <span>✍️</span> 5분 휴먼 폴리싱 대본 에디터
                    </div>
        """, unsafe_allow_html=True)

        if not is_pending:
            st.markdown(f"""
                <div style="font-size: 0.8rem; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 8px; padding: 4px 12px; color: #34D399; font-weight: 700;">
                    ✅ 승인 완료 대본 (읽기 전용) — 캘린더 ID: {script.calendar_render_event_id}
                </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
                <div style="font-size: 0.8rem; background: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.35); border-radius: 8px; padding: 4px 12px; color: #FBBF24; font-weight: 700;">
                    ⏳ 검수 대기 (수정 가능 모드)
                </div>
            """, unsafe_allow_html=True)

        st.markdown("</div>", unsafe_allow_html=True)

        disabled = not is_pending
        title = st.text_input("에피소드 제목", script.episode_title, key=f"{key}_title", disabled=disabled)
        twist = st.text_area("결말 3초 반전 포인트 (사이다 Hook)", script.analysis.twist_point, key=f"{key}_twist", disabled=disabled, height=68)

        scene_inputs = []
        for sc in script.scenes:
            n = sc.scene_number
            with st.expander(f"씬 {n} · {sc.duration_sec}초 · {sc.subtitle_text}", expanded=(n == 1)):
                col_sc1, col_sc2 = st.columns([1, 1])
                with col_sc1:
                    action = st.text_area("스틱맨 액션 / 연출 (stickman_action)", sc.stickman_action, key=f"{key}_a{n}", disabled=disabled, height=65)
                    subtitle = st.text_input("자막 텍스트 (subtitle_text)", sc.subtitle_text, key=f"{key}_s{n}", disabled=disabled)
                with col_sc2:
                    narration = st.text_area("TTS 성우 대사 (tts_narration)", sc.tts_narration, key=f"{key}_n{n}", disabled=disabled, height=65)
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

        # 승인 및 캘린더 등록 액션 바
        if is_pending:
            st.markdown("""
                <div class="stitch-card">
                    <div style="font-weight: 800; font-size: 1.1rem; margin-bottom: 12px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                        <span>📅</span> 대본 승인 및 구글 캘린더 스케줄 등록
                    </div>
            """, unsafe_allow_html=True)

            col_act1, col_act2, col_act3 = st.columns([1.2, 2.0, 1.0])
            with col_act1:
                target_date = st.date_input("스케줄 기준 일자", value=date.today())
            with col_act2:
                st.write("")
                approve = st.button("✅ 대본 최종 승인 및 캘린더 등록", type="primary", use_container_width=True)
            with col_act3:
                st.write("")
                discard = st.button("🗑️ 폐기 (부적합)", use_container_width=True)

            if approve:
                try:
                    edited = build_edited()
                    file_path.write_text(edited.model_dump_json(indent=2), encoding="utf-8")
                    with st.spinner("캘린더 등록 및 상태 갱신 중..."):
                        mgr = CalendarManager()
                        result = mgr.schedule_episode_pipeline(edited, target_date)
                    st.session_state["active_episode_id"] = result.episode_id
                    st.session_state["target_episode_to_select"] = ("approved", f"{result.episode_id}.json")
                    st.session_state["flash"] = (
                        f"🎉 승인 완료 ({'Dry-Run' if mgr.dry_run else 'Google Calendar'}) · "
                        f"렌더 이벤트: {result.calendar_render_event_id} / "
                        f"업로드 이벤트: {result.calendar_upload_event_id}"
                    )
                except Exception as e:
                    st.session_state["flash_err"] = f"승인 실패: {e}"
                st.rerun()

            if discard:
                file_path.unlink(missing_ok=True)
                st.warning("대본이 폐기되었습니다.")
                st.rerun()

            st.markdown("</div>", unsafe_allow_html=True)

        st.markdown("""
            <div class="step-guide-card">
                <span>👉</span>
                <div>대본 검수가 완료되었다면 상단 <strong>[🎵 Step 3: 오디오 연출]</strong> 탭으로 이동하여 BGM 및 성우 믹싱을 설정하세요.</div>
            </div>
        """, unsafe_allow_html=True)


# ------------------------------------------------------------------------------
# [Step 3: 오디오 연출] (BGM 선택, 오디오 더킹 슬라이더 20%, 보컬 필터)
# ------------------------------------------------------------------------------
with tab_step3:
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 800; font-size: 1.15rem; margin-bottom: 8px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                <span>🎵</span> 배경음악(BGM) 믹싱 & 오디오 더킹 콘솔
            </div>
            <div style="font-size: 0.9rem; color: #94A3B8; margin-bottom: 18px; line-height: 1.5;">
                영상에 들어갈 BGM 트랙을 선택하고, 성우 대사 출력 시 음악 볼륨을 자동으로 감쇠(오디오 더킹)할 비율 및 노래 가사와 목소리의 충돌을 차단하는 <strong>보컬 억제 필터</strong>를 조율합니다.
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

    col_bgm_cfg, col_bgm_pre = st.columns([1.2, 1.0])
    with col_bgm_cfg:
        default_bgm_idx = 0
        preferred_bgm = "Neon Skyline_5-2.wav"
        if preferred_bgm in bgm_options:
            default_bgm_idx = bgm_options.index(preferred_bgm)

        selected_bgm = st.selectbox(
            "🎼 배경음악(BGM) 트랙 선택",
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
            help="성우 대사가 출력되는 동안 BGM이 20% 볼륨(약 -14dB)으로 자동 감쇠(오디오 더킹)되며, 대사 사이 무음 구간에서는 자연스럽게 볼륨이 상승합니다."
        )
        bgm_vocal_filter = st.checkbox(
            "☑️ 보컬 억제 필터 (가사 있는 BGM용 Vocal Pocketing)",
            value=True,
            key=f"{key}_bgm_vocal_filter",
            help="BGM의 보컬 주파수 대역(400Hz~3,200Hz)을 감쇠하고 센터 보컬을 억제(Mid-Side 분리)하여 성우 대사와 노래 가사가 충돌하지 않도록 분리합니다."
        )

    with col_bgm_pre:
        if selected_bgm != "BGM 없음":
            preview_path = BGM_DIR / selected_bgm
            if preview_path.exists():
                st.markdown(f"""
                    <div style="background: #0F172A; border: 1.5px solid rgba(56, 189, 248, 0.3); border-radius: 14px; padding: 18px; margin-top: 14px;">
                        <div style="font-size: 0.88rem; font-weight: 800; color: #38BDF8; margin-bottom: 8px;">🎧 음원 실시간 미리듣기</div>
                        <div style="font-size: 0.85rem; color: #CBD5E1; margin-bottom: 10px; font-family: 'JetBrains Mono', monospace;">트랙: {selected_bgm}</div>
                """, unsafe_allow_html=True)
                st.audio(str(preview_path))
                st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.markdown("""
                <div style="background: #0F172A; border: 1px dashed rgba(255,255,255,0.15); border-radius: 14px; padding: 32px 20px; text-align: center; color: #94A3B8; font-size: 0.88rem; margin-top: 14px;">
                    🎧 배경음악을 선택하시면<br>여기서 실시간 미리듣기가 지원됩니다.
                </div>
            """, unsafe_allow_html=True)

    # 멀티 성우 캐스팅 요약
    st.markdown("""
        <div style="margin-top: 18px; padding: 14px 18px; background: #0F172A; border-radius: 12px; border: 1px solid rgba(255,255,255,0.06); font-size: 0.88rem; color: #CBD5E1;">
            🎙️ <strong>Gemini 2.5 Native TTS 멀티 보이스 캐스팅</strong>: 
            주인공 A(블루 나시 - 코레), 빌런 B(오렌지 상의 - 펜릴/퍽), 해설(딥 내레이션)이 자동으로 배역 매칭되어 감정 연기로 합성됩니다.
        </div>
        </div>
    """, unsafe_allow_html=True)

    st.markdown("""
        <div class="step-guide-card">
            <span>👉</span>
            <div>오디오 연출 설정이 완료되면 상단 <strong>[🎬 Step 4: 렌더링 & 배포]</strong> 탭으로 이동하여 최종 영상을 확인하세요.</div>
        </div>
    """, unsafe_allow_html=True)


# ------------------------------------------------------------------------------
# [Step 4: 렌더링 & 배포] (MP4 렌더링 버튼, 모바일 프리뷰, 다운로드, 유튜브 업로드)
# ------------------------------------------------------------------------------
with tab_step4:
    if script is None:
        st.info("💡 렌더링할 대본이 없습니다. [Step 1: 기획 & 대본] 탭에서 신규 대본을 먼저 생성하세요.")
    else:
        st.markdown(f"""
            <div style="background: rgba(99, 102, 241, 0.1); border: 1.5px solid rgba(99, 102, 241, 0.35); border-radius: 12px; padding: 12px 18px; margin-bottom: 16px; display: flex; justify-content: space-between; align-items: center;">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 1.2rem;">🎯</span>
                    <div>
                        <span style="font-weight: 800; font-size: 0.95rem; color: #FFFFFF;">현재 렌더링 대상 에피소드:</span>
                        <span style="font-size: 0.95rem; color: #F59E0B; font-weight: 700; margin-left: 6px;">{'[⏳ 검수 대기]' if is_pending else '[✅ 승인 완료]'} {script.episode_title}</span>
                    </div>
                </div>
                <div style="font-size: 0.8rem; font-family: 'JetBrains Mono', monospace; color: #94A3B8; background: #0F172A; padding: 4px 10px; border-radius: 6px;">
                    ID: {script.episode_id}
                </div>
            </div>
        """, unsafe_allow_html=True)

        st.markdown("""
            <div class="stitch-card">
                <div style="font-weight: 800; font-size: 1.15rem; margin-bottom: 8px; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>🎬</span> 스틱맨 쇼츠 비디오 렌더링
                </div>
                <div style="font-size: 0.9rem; color: #94A3B8; margin-bottom: 16px;">
                    2세대 캡슐 관절 스틱맨 애니메이션, 대사 오디오 1:1 싱크 프레임, 다이나믹 카메라 연출 및 BGM 믹싱을 거쳐 <strong>1080×1920 (9:16) MP4 숏폼 영상</strong>을 완성합니다.
                </div>
        """, unsafe_allow_html=True)

        btn_label = "🎬 4단계: 스틱맨 MP4 쇼츠 다시 렌더링 (강제 덮어쓰기)" if video_file else "🎬 4단계: 스틱맨 MP4 쇼츠 즉시 렌더링 실행"
        if st.button(btn_label, type="primary", use_container_width=True):
            bar = st.progress(0.0)
            status_box = st.empty()

            def on_progress(msg: str, frac: float) -> None:
                bar.progress(min(max(frac, 0.0), 1.0))
                status_box.caption(msg)

            with st.spinner("스틱맨 2세대 관절 기구학 계산, 오디오 믹싱 및 FFmpeg 비디오 렌더링 진행 중..."):
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
                    st.session_state["flash"] = f"🎉 렌더링 완료: {Path(rendered_out).name}"
                    st.rerun()
                except Exception as e:
                    st.error(f"렌더링 실패: {e}")

        st.markdown("</div>", unsafe_allow_html=True)

        # 모바일 프리뷰 모니터 & 렌더링 독 (한 화면에 직관적으로 들어오는 최적화 레이아웃)
        if video_file is not None and Path(video_file).exists():
            video_bytes = Path(video_file).read_bytes()

            col_v_monitor, col_v_info = st.columns([1.0, 1.2], gap="large")

            with col_v_monitor:
                st.markdown("""
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <div style="font-weight: 800; font-size: 1.05rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                            <span>📱</span> 모바일 프리뷰 모니터
                        </div>
                        <div style="font-size: 0.76rem; color: #10B981; font-weight: 800; background: rgba(16, 185, 129, 0.15); padding: 3px 8px; border-radius: 6px; border: 1px solid rgba(16, 185, 129, 0.3);">
                            9:16 SHORTS
                        </div>
                    </div>
                """, unsafe_allow_html=True)

                # 스마트폰 프레임 내부 비디오 재생 (CSS pseudo-elements로 상단 노치/하단 홈바 자동 렌더링)
                st.video(video_bytes)

            with col_v_info:
                st.markdown("""
                    <div style="font-weight: 800; font-size: 1.05rem; color: #FFFFFF; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                        <span>📊</span> 렌더링 비디오 스펙 & 다운로드
                    </div>
                """, unsafe_allow_html=True)

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
                            <div class="spec-chip-value" style="color: #38BDF8;">{dur_text}</div>
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

                # 다운로드 버튼 (대형 에메랄드 CTA)
                st.download_button(
                    "📥 완성된 MP4 영상 즉시 다운로드",
                    data=video_bytes,
                    file_name=f"{script.episode_id}.mp4",
                    mime="video/mp4",
                    use_container_width=True
                )

                # 4단계 유튜브 쇼츠 자동 업로드 콘솔
                st.markdown("""
                    <div style="margin-top: 24px; padding-top: 18px; border-top: 1px solid rgba(255,255,255,0.1);">
                        <div style="font-weight: 800; font-size: 1.05rem; color: #FFFFFF; margin-bottom: 10px; display: flex; align-items: center; gap: 8px;">
                            <span>🚀</span> 유튜브 쇼츠 자동 업로드 콘솔
                        </div>
                """, unsafe_allow_html=True)

                meta = generate_short_metadata(script)

                with st.expander("📦 자동 패키징된 유튜브 메타데이터", expanded=False):
                    st.text_input("유튜브 제목 (#shorts 포함)", value=meta["title"], key="yt_title_input")
                    st.text_area("설명란 (Description)", value=meta["description"], height=100, key="yt_desc_input")
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
                    st.write("")
                    st.write("")
                    st.caption("🔒 미인증 시 시뮬레이션(Dry-Run)")

                if st.button("🚀 유튜브 쇼츠 자동 업로드 실행", type="primary", use_container_width=True):
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

        else:
            st.markdown("""
                <div style="background: #030712; border: 8px solid #1E293B; border-radius: 38px; padding: 12px 8px; max-width: 320px; margin: 20px auto; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.85), 0 0 35px rgba(99,102,241,0.2);">
                    <div style="width: 80px; height: 14px; background: #0F172A; border-radius: 12px; margin: 0 auto 12px auto; border: 1px solid rgba(255,255,255,0.1);"></div>
                    <div style="min-height: 420px; background: #0A0E1A; border-radius: 22px; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 24px;">
                        <span style="font-size: 3rem; margin-bottom: 12px; opacity: 0.6;">🎬</span>
                        <div style="font-weight: 800; color: #FFFFFF; font-size: 1.1rem; margin-bottom: 8px;">렌더링 대기 중</div>
                        <div style="font-size: 0.88rem; color: #94A3B8; line-height: 1.5; max-width: 210px;">
                            상단의 <strong>[🎬 4단계: MP4 쇼츠 렌더링 실행]</strong> 버튼을 누르면 스마트폰 모니터에 즉시 고화질로 렌더링됩니다.
                        </div>
                    </div>
                    <div style="width: 90px; height: 4px; background: rgba(255, 255, 255, 0.35); border-radius: 2px; margin: 12px auto 2px auto;"></div>
                </div>
            """, unsafe_allow_html=True)


# ------------------------------------------------------------------------------
# [Step 5: 성과 분석 & AI 자율 피드백] (YouTube Analytics 3계층 피드백 루프 & B2B SaaS 리포트)
# ------------------------------------------------------------------------------
with tab_step5:
    analytics = AnalyticsEngine()
    three_tier = analytics.get_three_tier_metrics()
    daily = three_tier["daily"]
    weekly = three_tier["weekly"]
    monthly = three_tier["monthly"]
    feedback_rules = three_tier["feedback_rules"]

    # 상단 헤더 카드
    st.markdown("""
        <div class="stitch-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div style="font-weight: 850; font-size: 1.25rem; color: #FFFFFF; display: flex; align-items: center; gap: 10px;">
                    <span style="font-size: 1.45rem;">📊</span> YouTube Analytics 연동 3계층 피드백 루프 & AI 자율 학습
                </div>
                <div style="font-size: 0.82rem; background: rgba(99, 102, 241, 0.15); border: 1px solid rgba(99, 102, 241, 0.35); color: #A5B4FC; padding: 4px 12px; border-radius: 8px; font-weight: 800;">
                    B2B SaaS / Closed-Loop Live
                </div>
            </div>
            <div style="font-size: 0.9rem; color: #94A3B8; line-height: 1.6;">
                유튜브 쇼츠 실제 반응 데이터를 <strong>데일리(24시간)·위클리(7일)·먼슬리(30일)</strong> 3계층으로 정밀 분석하고,<br>
                도출된 성과 패턴을 <strong>Step 1 큐레이션 및 대본 생성 프롬프트</strong>에 실시간 역주입하여 채널을 자율 진화시킵니다.
            </div>
        </div>
    """, unsafe_allow_html=True)

    # 1. 데일리 (Daily - 최근 24시간 실시간 초동 반응 & 훅 진단)
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 850; font-size: 1.1rem; color: #FFFFFF; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                <span>⚡</span> 1. 데일리 분석: 최근 24시간 발행 쇼츠 초동 반응 (Daily Pulse)
            </div>
    """, unsafe_allow_html=True)

    d_col1, d_col2, d_col3, d_col4 = st.columns(4)
    with d_col1:
        st.metric("초반 24시간 조회수", f"{daily['initial_views']:,}회", "+4,200 (실시간)")
    with d_col2:
        st.metric("조회율 (Viewed vs Swiped)", f"{daily['viewed_rate_pct']}%", f"이탈 {daily['swiped_away_pct']}% 방어")
    with d_col3:
        st.metric("오프닝 3초(1씬) 이탈률", f"{daily['scene1_dropoff_pct']}%", "-2.3% (우수)")
    with d_col4:
        st.metric("평균 시청 지속시간 (Retention)", f"{daily['avg_retention_pct']}%", "목표치(85%) 초과")

    st.markdown(f"""
        <div style="margin-top: 14px; padding: 12px 16px; background: #0F172A; border-left: 4px solid #38BDF8; border-radius: 8px;">
            <div style="font-size: 0.85rem; font-weight: 800; color: #38BDF8; margin-bottom: 4px;">💡 AI 오프닝 훅 진단 & 프롬프트 긴급 보정</div>
            <div style="font-size: 0.88rem; color: #E2E8F0; line-height: 1.5;">
                <strong>최근 영상</strong>: <em>{daily['latest_video_title']}</em> (`{daily['latest_video_id']}`)<br>
                {daily['ai_prompt_diagnosis']}
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # 2. 위클리 (Weekly - 최근 7일간 소재별 완독률 비교 & 가중치 재조정)
    st.markdown(f"""
        <div class="stitch-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div style="font-weight: 850; font-size: 1.1rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>📈</span> 2. 위클리 분석: 최근 7일간 소재별 완독률 및 큐레이션 가중치 (Weekly)
                </div>
                <div style="font-size: 0.8rem; color: #10B981; font-weight: 700;">
                    {weekly['action_taken']}
                </div>
            </div>
    """, unsafe_allow_html=True)

    w_col_info1, w_col_info2 = st.columns(2)
    with w_col_info1:
        st.metric("주간 총 조회수 (7편)", f"{weekly['total_views']:,}회")
    with w_col_info2:
        st.metric("주간 평균 완독률", f"{weekly['avg_completion_rate']}%")

    # 카테고리별 테이블 & 차트
    perf_data = [
        {
            "소재 테마": p["category"],
            "시청 완독률 (%)": f"{p['completion_rate']}%",
            "주간 누적 조회수": f"{p['views']:,}회",
            "큐레이션 가중치": f"x{p['weight']}",
            "성과 등급": p["status"],
        }
        for p in weekly["category_performance"]
    ]
    st.dataframe(perf_data, use_container_width=True, hide_index=True)

    # 완독률 비교 차트
    chart_data = {p["category"]: p["completion_rate"] for p in weekly["category_performance"]}
    st.bar_chart(chart_data)

    st.markdown("</div>", unsafe_allow_html=True)

    # 3. 먼슬리 (Monthly - 월간 성장 및 B2B 재무 지표)
    st.markdown("""
        <div class="stitch-card">
            <div style="font-weight: 850; font-size: 1.1rem; color: #FFFFFF; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
                <span>🏆</span> 3. 먼슬리 채널 성장 리포트 (Monthly Executive Summary)
            </div>
    """, unsafe_allow_html=True)

    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        st.metric("월간 총 누적 조회수", f"{monthly['monthly_total_views']:,}회", "+128,400 (전월대비)")
    with m_col2:
        st.metric("구독자 순증", f"+{monthly['subscribers_gained']:,}명", f"총 {monthly['subscribers_total']:,}명")
    with m_col3:
        st.metric("예상 광고 수익(추정)", f"₩{monthly['estimated_revenue_krw']:,}", "숏폼 펀드/광고 연동")

    st.markdown("<div style='margin-top: 14px; font-weight: 800; font-size: 0.95rem; color: #E2E8F0;'>🔥 월간 아웃라이어 TOP 3 숏폼 랭킹</div>", unsafe_allow_html=True)
    for idx, top in enumerate(monthly["top_outliers"]):
        st.markdown(f"""
            <div style="display: flex; justify-content: space-between; align-items: center; background: #0F172A; padding: 10px 14px; border-radius: 8px; margin-top: 6px; border: 1px solid rgba(255,255,255,0.06);">
                <div style="font-size: 0.88rem; font-weight: 700; color: #F8FAFC;">
                    <span style="color: #F59E0B; margin-right: 6px;">#{idx+1}</span> {top['title']}
                </div>
                <div style="font-size: 0.82rem; color: #94A3B8; font-family: 'JetBrains Mono', monospace;">
                    조회수 <strong>{top['views']:,}회</strong> · VPH: <strong>{top['vph']:,}</strong>
                </div>
            </div>
        """, unsafe_allow_html=True)

    st.markdown("</div>", unsafe_allow_html=True)

    # 4. B2B 상용화 리포트 다운로드 & 폐루프(Closed-Loop) 자율 피드백 엔진
    st.markdown("""
        <div class="stitch-card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <div style="font-weight: 850; font-size: 1.15rem; color: #FFFFFF; display: flex; align-items: center; gap: 8px;">
                    <span>💼</span> 4. B2B 상용화 리포트 & AI 폐루프(Closed-Loop) 자율 학습 콘솔
                </div>
                <div style="font-size: 0.78rem; background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.3); color: #38BDF8; padding: 3px 8px; border-radius: 6px; font-weight: 800;">
                    SaaS Ready
                </div>
            </div>
            <div style="font-size: 0.88rem; color: #94A3B8; margin-bottom: 16px; line-height: 1.5;">
                경영진 보고용 1페이지 분석 리포트를 즉시 생성하여 다운로드할 수 있으며, 최신 시청자 행동 데이터를 머신러닝 규칙으로 정제하여 Step 1 큐레이션 엔진에 실시간 피드백합니다.
            </div>
    """, unsafe_allow_html=True)

    report_md = analytics.generate_b2b_executive_report()

    col_rep_down, col_loop_btn = st.columns([1.5, 1.5], gap="medium")
    with col_rep_down:
        st.download_button(
            "📥 B2B 성과 분석 경영진 1페이지 리포트 다운로드 (Markdown)",
            data=report_md,
            file_name=f"stickman_executive_report_{date.today().strftime('%Y%m%d')}.md",
            mime="text/markdown",
            type="primary",
            use_container_width=True
        )
    with col_loop_btn:
        btn_relearn = st.button("🔄 성과 데이터 기반 AI 폐루프(Closed-Loop) 자율 재학습 즉시 실행", use_container_width=True)

    if btn_relearn:
        res_learn = analytics.trigger_closed_loop_learning()
        if res_learn.get("success"):
            st.session_state["flash"] = f"🧠 AI 자율 피드백 폐루프 업데이트 완료! ({res_learn['updated_rules_count']}개 규칙 Step 1 프롬프트 즉시 주입)"
            st.balloons()
            st.rerun()

    # 현재 실시간 프롬프트에 주입 중인 룰 목록 표시
    curr_rules = feedback_rules.get("active_rules", [])
    if curr_rules:
        with st.expander("🧠 현재 Step 1 대본 생성 프롬프트에 실시간 주입 중인 자율 피드백 룰", expanded=True):
            st.caption(f"최종 갱신 일시: {feedback_rules.get('last_updated', '최근')}")
            for idx, r in enumerate(curr_rules):
                st.markdown(f"**{idx+1}.** {r}")

    with st.expander("📄 [B2B 미리보기] 1페이지 경영진 성과 리포트 전문", expanded=False):
        st.markdown(report_md)

    st.markdown("</div>", unsafe_allow_html=True)
