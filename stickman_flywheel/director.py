"""연출가(LLM): 6컷 대본 → StagePlan(장소·세트·소품·컷별 행동 비트). 코드는 장소/소재에 무관하다."""
from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

from dotenv import load_dotenv

from director_ir import STAGE_MARKS, VERB_DOC, StagePlan, validate_plan
from schemas import StickmanScriptOutput

load_dotenv()

VERB_TABLE = "\n".join(f"  - {v}: {doc}" for v, (doc, _) in VERB_DOC.items())
MARKS = ", ".join(f"{k}={v}" for k, v in STAGE_MARKS.items())

DIRECTOR_PROMPT = f"""너는 스틱맨 숏폼의 '무대 연출 감독'이다. 6컷 대본을 받아, 공간감과 구체적 행동이 살아있는 무대 계획(StagePlan)을 설계한다.
화면은 1080x1920 세로, 가로 위치는 0~1(0=왼쪽 끝, 1=오른쪽 끝). 인물 키는 약 590px로 화면 높이의 3분의 1이다.
배우는 둘뿐: A=주인공 '빼딱맨'(기본 왼쪽 0.24, 오른쪽을 봄), B=빌런/진상(기본 오른쪽, 왼쪽을 봄).

[핵심 목표] 마네킹처럼 서서 말풍선만 띄우지 않는다. 컷마다 '움직임(입장/이동)'과 '사물 상호작용(집기·던지기·내려치기·건네기)'이 보여야 한다.

[장소/자산 규칙]
- location.id 는 가능한 한 일반적이고 재사용 가능한 영어 snake_case (예: convenience_store, restaurant, airplane_cabin, hair_salon, secondhand_meetup_street).
  backdrop 은 그 장소의 벽/실내 풍경(인물이 서는 가운데 영역은 비워 둔다). description 에는 장소의 정체성이 드러나는 구체 물건을 적는다.
- set_pieces(가구/구조물)는 최대 4개: 문은 반드시 hinge 가 있는 door 류, 카운터/테이블은 surface 가 있는 것. 인물 앞을 가리는 테이블/카운터는 layer="front".
  (앉는 장면은 의자는 mid, 테이블은 front 로 두면 하체가 자연스럽게 가려진다.)
- props(상호작용 물건)는 최대 3개. 재사용 가능한 영어 id(credit_card, spoon, plate, phone, bag...). start 는 surface:<piece_id> | floor | hand:A | hand:B.
- 모든 asset.description 은 색·형태가 드러나게 구체적으로. height_px 는 실제 무대 높이(문 760, 카운터/테이블 260~300, 카드·숟가락 110~150, 접시 160).

[행동 비트 규칙]
- 사용 가능한 동사(이 목록 밖은 금지):
{VERB_TABLE}
- 위치 마크: {MARKS} (또는 0~1 숫자, prop:<id>, piece:<id>, A, B). 컷 사이 배우 위치/들고 있는 물건/문 열림은 이어진다.
- 컷마다 비트 2~5개. when 으로 시각 동기화: cut_start | after:<id> | with:<id> | seg_start:<k> | seg_end:<k>.
  k 는 그 컷 tts_narration 에서 '해설:/A:/B:' 줄을 위에서부터 센 대사 번호(0부터). 행동은 해당 대사가 시작/끝날 때 터지도록 맞춘다.
- 컷 1(훅): 공간 이동이 있어야 한다. 예) B 가 문을 열고 입장(open → enter), 또는 손님이 다가옴. 첫 갈등 사물을 등장시킨다.
- 컷 2: B 의 무례한 '사물 행동'(던지기/내려치기/밀어 건네기). 컷 3은 대사 없는 A 의 무음 리액션(shock/smirk 정도, 큰 이동 없음).
- 컷 4: A 의 결심 — 물건을 집어 든다(pick_up) 또는 자세를 잡는다. 컷 5: 미러링 응징 — B 가 한 행동을 mirror 로 똑같이 되돌려 준다(of=그 비트 id) + 그에 따른 knockback/fall/shock.
- 컷 6: B 의 최후(fall/faint/cower) + A 의 여유(smirk/cross_arms 후 exit 또는 turn_to).
- 같은 컷 안에서 한 배우가 동시에 여러 팔동작을 하지 않게 시간을 분리한다(after 사용).
- 존재하지 않는 id 를 참조하지 않는다. mirror 는 throw/slam/punch/kick/push/slide/hand_over 등 앞선 비트의 id 를 of 에 쓴다.
- 장소가 달라져도 같은 동사/구조를 쓴다: 식당이면 숟가락·접시·물컵, 비행기면 좌석·짐, 미용실이면 가위·거울 등 그 장소의 사물을 소품/세트로 올린다.
"""


def _scene_digest(script: StickmanScriptOutput) -> str:
    lines: List[str] = [f"제목: {script.episode_title}",
                        f"상황 이식: {script.analysis.transplanted_setting} / 반전: {script.analysis.twist_point}"]
    for s in script.scenes:
        lines.append(f"\n[컷 {s.scene_number}] shot={s.shot_type} 길이={s.duration_sec}s")
        lines.append(f"  연출지문: {s.stickman_action}")
        lines.append(f"  대사:\n" + "\n".join("    " + ln for ln in (s.tts_narration or "(무음)").splitlines() if ln.strip()))
        lines.append(f"  자막: {s.subtitle_text}")
        if s.visual_props:
            lines.append(f"  시각오브젝트 힌트: {', '.join(s.visual_props)}")
    return "\n".join(lines)


def plan_stage(script: StickmanScriptOutput, log=print) -> Tuple[StagePlan, List[str]]:
    from google import genai
    from google.genai import types

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY 가 없습니다.")
    client = genai.Client(api_key=key)
    n = len(script.scenes)
    models = [os.getenv("DIRECTOR_MODEL", "gemini-3.1-pro-preview"), os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-2.5-pro"]
    prompt = _scene_digest(script)
    last: Optional[Exception] = None
    for attempt, model in enumerate([models[0], models[0], models[1], models[2]]):
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(system_instruction=DIRECTOR_PROMPT, response_mime_type="application/json",
                                                   response_schema=StagePlan, temperature=0.8))
            plan = resp.parsed if isinstance(resp.parsed, StagePlan) else StagePlan.model_validate(json.loads(resp.text))
            plan, warns = validate_plan(plan, n)
            total_beats = sum(len(c.beats) for c in plan.cuts)
            if total_beats < n * 2:
                raise ValueError(f"비트가 너무 적음({total_beats}) — 컷마다 2개 이상 필요")
            return plan, warns
        except Exception as e:
            last = e
            log(f"[Director] {model} 시도 {attempt + 1} 실패: {type(e).__name__}: {str(e)[:200]}")
            prompt = _scene_digest(script) + f"\n\n[직전 시도 오류: {str(e)[:200]} — 스키마와 규칙을 지켜 다시 작성]"
    raise RuntimeError(f"무대 계획 생성 실패: {last}")
