"""파이프라인 전 모듈이 공유하는 Pydantic 데이터 모델 (단일 진실 공급원)."""
from typing import List, Optional

from pydantic import BaseModel, Field


class SceneBeat(BaseModel):
    scene_number: int = Field(description="씬 번호 (1부터 시작)")
    duration_sec: int = Field(ge=3, le=6, description="씬 길이 (3~6초)")
    stickman_action: str = Field(description="스틱맨의 관절 움직임, 물리적 타격감, 표정 연출 지시문")
    camera_work: str = Field(description="카메라 워킹 (예: Zoom-in, Screen Shake, Freeze Frame)")
    sfx_cue: str = Field(description="타임스탬프별 효과음 큐 (예: 0.5s punch_hit, 2.0s cartoon_slip)")
    tts_narration: str = Field(description="찰진 구어체 내레이션/대사")
    subtitle_text: str = Field(description="화면 중앙/하단에 찍힐 짧은 자막")


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
