"""오디오 타임라인 독립 단계: 대본 → (씬별 wav, 길이, 타격 시점, 화자 타임라인).

렌더러 트랙(스틱맨/스테이지/웹툰)은 모두 이 결과를 주입받아 같은 음성·자막 타이밍을 쓴다.

  plan = build_audio_plan(script)        # TTS·효과음 합성 (기존 씬 오디오 캐시 재사용)
  plan = AudioPlan.load(episode_id)      # 저장된 타임라인 재사용
  renderer.render_episode(script, audio_plan=plan)

저장 위치: pipeline_outputs/audio/<episode_id>/timeline/{scene_XX.wav, timeline.json, master.wav}
"""
from __future__ import annotations

import json
import shutil
import tempfile
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from fs_utils import atomic_write_json
from module3_renderer import AUDIO_DIR, SR, StickmanShortRenderer, select_pose
from schemas import StickmanScriptOutput

TIMELINE_DIRNAME = "timeline"


@dataclass
class SceneAudio:
    wav: str                     # timeline 디렉터리 기준 파일명
    dur: float                   # 씬 길이(초, 프레임 경계에 맞춤)
    hit_t: float                 # 타격 효과음 시점(초)
    timeline: List[Dict] = field(default_factory=list)   # [{speaker,text,start,end}]


@dataclass
class AudioPlan:
    episode_id: str
    root: Path
    scenes: List[SceneAudio]

    @property
    def total_sec(self) -> float:
        return sum(s.dur for s in self.scenes)

    def scene_path(self, i: int) -> Path:
        return self.root / self.scenes[i].wav

    def scene_into(self, i: int, tmp: Path) -> Tuple[Path, float, float, List[Dict]]:
        """렌더러용: 씬 wav 를 작업 폴더로 복사하고 (wav, dur, hit_t, timeline) 을 돌려준다."""
        sc = self.scenes[i]
        dst = tmp / f"audio_{i:02d}.wav"
        shutil.copy2(self.scene_path(i), dst)
        return dst, sc.dur, sc.hit_t, [dict(t) for t in sc.timeline]

    def master_wav(self) -> Path:
        return self.root / "master.wav"

    def save(self) -> None:
        atomic_write_json(self.root / "timeline.json", {
            "episode_id": self.episode_id, "sr": SR,
            "scenes": [{"wav": s.wav, "dur": s.dur, "hit_t": s.hit_t, "timeline": s.timeline} for s in self.scenes]})

    @classmethod
    def load(cls, episode_id: str) -> "AudioPlan":
        root = AUDIO_DIR / episode_id / TIMELINE_DIRNAME
        data = json.loads((root / "timeline.json").read_text(encoding="utf-8"))
        return cls(episode_id, root, [SceneAudio(s["wav"], s["dur"], s["hit_t"], s["timeline"]) for s in data["scenes"]])

    @classmethod
    def exists(cls, episode_id: str) -> bool:
        return (AUDIO_DIR / episode_id / TIMELINE_DIRNAME / "timeline.json").exists()


def build_audio_plan(script: StickmanScriptOutput, renderer: StickmanShortRenderer | None = None) -> AudioPlan:
    """대본의 모든 씬에 대해 TTS·효과음을 합성(또는 캐시 복원)하고 타임라인을 확정해 저장한다."""
    r = renderer or StickmanShortRenderer()
    r.prepare_episode(script)
    root = AUDIO_DIR / script.episode_id / TIMELINE_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="audioplan_"))
    scenes: List[SceneAudio] = []
    master = array("h")
    try:
        total = len(script.scenes)
        for i, scene in enumerate(script.scenes):
            pose = select_pose(scene.stickman_action, i, total)
            print(f"[AudioPlan] 씬 {i + 1}/{total} 음성·효과음 합성", flush=True)
            wav, dur, hit_t, timeline = r._build_scene_audio(scene, i, pose, tmp)
            name = f"scene_{i:02d}.wav"
            shutil.copy2(wav, root / name)
            master.extend(r._decode_pcm(wav))
            scenes.append(SceneAudio(name, float(dur), float(hit_t), [dict(t) for t in timeline]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with wave.open(str(root / "master.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(master.tobytes())
    plan = AudioPlan(script.episode_id, root, scenes)
    plan.save()
    print(f"[AudioPlan] 확정: {total}씬 {plan.total_sec:.1f}s → {root}", flush=True)
    return plan
