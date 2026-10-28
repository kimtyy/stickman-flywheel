"""파이프라인 전 모듈이 공유하는 Pydantic 데이터 모델 (단일 진실 공급원)."""
from typing import List, Optional

from pydantic import BaseModel, Field


SHOT_TYPES = ["Wide", "Medium", "Close-up", "Extreme Close-up", "Low Angle", "High Angle", "Over-the-shoulder"]


def normalize_shot_type(raw: Optional[str]) -> str:
    """LLM/구버전이 쓰는 'Wide Shot', 'Medium Shot', 'extreme close up' 등 표기를 SHOT_TYPES 의 정식 이름으로 통일."""
    low = (raw or "").strip().lower().replace("-", " ").replace("_", " ")
    if "extreme" in low:
        return "Extreme Close-up"
    if "close" in low:
        return "Close-up"
    if "low" in low:
        return "Low Angle"
    if "high" in low:
        return "High Angle"
    if "over" in low and "shoulder" in low:
        return "Over-the-shoulder"
    if "wide" in low:
        return "Wide"
    return "Medium"


class SceneBeat(BaseModel):
    scene_number: int = Field(description="씬 번호 (1부터 시작, 1~6컷 콘티)")
    duration_sec: int = Field(default=4, ge=1, le=8, description="씬 길이 (초 단위, 무음 리액션은 1~3초, 일반 씬 3~6초)")
    stickman_action: str = Field(description="스틱맨의 관절 움직임, 물리적 타격감, 표정 연출 지시문")
    camera_work: str = Field(default="Medium Shot", description="카메라 워킹 및 앵글 지시문 (예: Zoom-in, Screen Shake, Close-up)")
    sfx_cue: str = Field(default="", description="타임스탬프별 효과음 큐 (예: 0.5s punch_hit, 2.0s cricket_chirp)")
    tts_narration: str = Field(default="", description="찰진 구어체 내레이션/대사 (무음 리액션 컷은 빈 문자열 가능)")
    subtitle_text: str = Field(default="", description="화면 중앙/하단에 찍힐 짧은 자막")
    visual_props: List[str] = Field(default_factory=list, description="씬별 필수 시각 오브젝트 목록 (상황과 1:1로 매칭되는 핵심 시각물)")
    
    # --- 6컷 카메라 감독 콘티 연출 필드 ---
    shot_type: str = Field(default="Medium Shot", description="샷 종류 (Wide, Close-up, Extreme Close-up, Low Angle, Over-the-shoulder 등 교차 배치)")
    camera_effect: str = Field(default="none", description="카메라 특수효과 (zoom_in_fast, camera_shake, spotlight_darken, none)")
    ui_overlay: str = Field(default="none", description="만화/쇼츠 UI 오버레이 (anger_vein, question_mark, soul_leaving, none)")
    impact_keyword: str = Field(default="", description="결정타 컷 큼직한 타격 자막 2~4글자 (예: 참교육, 팩폭, 멘붕, 금융치료)")
    sfx: str = Field(default="", description="핵심 효과음 종류 (dramatic_thud, car_horn, comic_punch, cricket_chirp, record_scratch, glass_break, whoosh 등)")
    pause_after: float = Field(default=0.0, description="대사 후 정적/침묵 시간(초), 무음 리액션 컷은 0.8s 등")
    character_prompt: str = Field(
        default="3D stylized stickman with a smooth glossy white spherical head, thick bold black outlines, half-closed cynical eyes, crooked sarcastic smirk, wearing a dark charcoal hoodie, crossed arms, high-end Pixar-like 3D toon shading",
        description="주인공(빼딱맨) 고정 외형 프롬프트"
    )



class ReverseEngineeringAnalysis(BaseModel):
    discarded_surface_topic: str = Field(description="원본 트렌드의 표면적 소재 (버릴 껍데기)")
    extracted_core_desire: str = Field(description="대중이 열광한 핵심 심리 기제/결핍/분노 포인트")
    transplanted_setting: str = Field(description="감정선을 이식해 새로 창조한 일상 배경")
    twist_point: str = Field(description="마지막 3초 블랙코미디/사이다 반전")


class StickmanScriptOutput(BaseModel):
    episode_id: str = Field(description="고유 ID (타임스탬프 기반)")
    episode_title: str = Field(description="쇼츠용 클릭 유도 제목")
    target_emotion: str = Field(description="타겟 감정선")
    analysis: ReverseEngineeringAnalysis
    total_duration_sec: int
    scenes: List[SceneBeat]
    hashtags: List[str]
    calendar_render_event_id: Optional[str] = None
    calendar_upload_event_id: Optional[str] = None
    rendered_video_path: Optional[str] = None
