"""연출 IR(중간 표현): LLM 연출가가 만들고 스테이지 엔진이 해석하는 '무대 계획'.

원칙: 동사(행동)는 이 파일의 닫힌 열거형(VERBS)으로 고정하고, 명사(장소·소품)는 AssetRef 로 열어 둔다.
새 소재가 와도 이 파일/엔진을 고치지 않고, 자산 해결층(asset_library)이 명사를 만든다.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- 동사 (닫힌 열거형)
# 동사 → (설명, 기본 지속시간 초; 0 이면 이동 거리로 계산)
VERB_DOC: Dict[str, Tuple[str, float]] = {
    # 이동
    "enter": ("배우가 무대 밖/문에서 걸어 들어와 to 위치에 선다. to=마크, target=문 piece(선택)", 1.6),
    "exit": ("배우가 무대 밖으로 걸어 나간다. to=offstage_left|offstage_right", 1.4),
    "walk_to": ("to 위치로 걷는다. to=마크|0~1 숫자|prop:<id>|piece:<id>|A|B", 0.0),
    "run_to": ("to 위치로 달린다(같은 인자)", 0.0),
    "turn_to": ("target 쪽으로 몸을 돌린다. target=A|B|prop:<id>|piece:<id>", 0.05),
    # 자세
    "idle": ("기본 서 있는 자세로 복귀", 0.4),
    "cross_arms": ("팔짱 (주인공 시그니처 삐딱한 자세)", 0.4),
    "sit": ("target(의자/좌석 piece) 위치에 앉는다. 앞쪽 front 레이어 테이블이 하체를 가린다", 0.7),
    "stand": ("앉은/쓰러진 상태에서 일어선다", 0.6),
    "lean_on": ("target(카운터/테이블 piece)에 기대거나 손을 짚는다", 0.6),
    "point_at": ("target 을 손가락질한다", 0.9),
    # 사물
    "pick_up": ("prop 을 집어 든다(prop 이 놓인 곳으로 손을 뻗는다)", 1.0),
    "put_down": ("들고 있는 prop 을 to(piece 의 표면 또는 바닥)에 내려놓는다", 0.9),
    "hold_up": ("들고 있는 prop 을 번쩍 들어 보여준다", 1.0),
    "throw": ("들고 있는 prop 을 target 에게 던진다 (포물선 비행, 적중 시 피격 반응 자동)", 1.1),
    "slide": ("prop 을 표면 위에서 to 쪽으로 미끄러뜨려 밀어준다", 0.6),
    "slam": ("target(표면 piece) 을 손바닥/물건으로 쾅 내려친다", 0.7),
    "catch": ("날아오는 prop 을 받는다 (상대의 throw 와 같은 시각에 배치)", 0.6),
    "open": ("target(문 piece)을 연다", 0.8),
    "hand_over": ("들고 있는 prop 을 target 배우에게 건넨다", 1.0),
    # 접촉/타격
    "grab": ("target 배우의 멱살을 잡는다", 0.9),
    "push": ("target 배우를 밀친다 (밀려난다)", 0.8),
    "punch": ("target 배우를 주먹으로 가격한다", 0.9),
    "kick": ("target 배우를 걷어찬다", 0.9),
    # 피격
    "knockback": ("충격으로 뒤로 날아간다", 1.0),
    "fall": ("미끄러져/넘어져 바닥에 쓰러진다", 1.1),
    "dodge": ("옆으로 빠르게 피한다", 0.5),
    "faint": ("기절해 쓰러진다 (눈 X, 별)", 1.2),
    # 리액션
    "shock": ("경악", 1.0),
    "cower": ("움찔/위축", 1.0),
    "smirk": ("비웃는 썩소", 1.0),
    "rage": ("분노 폭발", 1.0),
    "shout": ("대사를 외치며 제스처", 1.2),
    # 매크로
    "mirror": ("of=앞선 비트 id. 그 비트의 배우와 대상을 뒤집어 똑같이 되돌려준다(미러링 응징)", 0.0),
}
VERBS: List[str] = list(VERB_DOC.keys())

STAGE_MARKS: Dict[str, float] = {
    "offstage_left": -0.22, "left": 0.24, "center_left": 0.38, "center": 0.5,
    "center_right": 0.62, "right": 0.76, "offstage_right": 1.22,
}

ACTORS = ("A", "B")
LAYERS = ("back", "mid", "front")
ASSET_KINDS = ("prop", "set_piece", "backdrop")


# --------------------------------------------------------------------------- 스키마
class AssetRef(BaseModel):
    id: str = Field(description="영어 snake_case 식별자 (캐시 키). 예: credit_card, pos_counter, restaurant_interior")
    label: str = Field(description="한국어 이름 (폴백 아이콘에 표기)")
    description: str = Field(description="시각적 외형 묘사. 색, 형태, 대표 디테일")
    kind: str = Field(default="prop", description="prop | set_piece | backdrop")
    height_px: int = Field(default=120, description="무대에서의 높이(px). 인물 키=590px, 문=760, 카운터/테이블=260~300, 카드/숟가락=110~140, 접시=150")


class SetPiece(BaseModel):
    id: str
    asset: AssetRef
    x: float = Field(default=0.5, description="무대 가로 위치 0~1 (바닥 중앙 기준)")
    layer: str = Field(default="mid", description="back(인물 뒤) | mid | front(인물 앞, 하체를 가림)")


class StageProp(BaseModel):
    id: str
    asset: AssetRef
    start: str = Field(default="floor", description="surface:<piece_id> | floor | hand:A | hand:B")
    x: float = Field(default=0.5, description="시작 위치 가로 0~1")


class Beat(BaseModel):
    id: str = Field(description="컷 내 고유 id (예: b1)")
    actor: str = Field(description="A(주인공) | B(빌런)")
    verb: str = Field(description="VERBS 중 하나")
    target: str = Field(default="", description="A | B | prop:<id> | piece:<id>")
    prop: str = Field(default="", description="사용 prop id")
    to: str = Field(default="", description="마크 이름 | 0~1 숫자 | prop:<id> | piece:<id> | A | B")
    when: str = Field(default="cut_start", description="cut_start | after:<beat_id> | with:<beat_id> | seg_start:<k> | seg_end:<k> (k=대사 세그먼트 번호, 0부터)")
    offset: float = Field(default=0.0, description="when 기준 초 단위 보정")
    dur: float = Field(default=0.0, description="초. 0이면 동사 기본값")
    of: str = Field(default="", description="mirror 전용: 되돌려줄 비트 id")


class CutPlan(BaseModel):
    scene_number: int
    beats: List[Beat] = Field(default_factory=list)


class Location(BaseModel):
    id: str = Field(description="영어 snake_case 장소 id (배경 캐시 키). 예: convenience_store, restaurant")
    label: str
    backdrop: AssetRef


class StagePlan(BaseModel):
    location: Location
    set_pieces: List[SetPiece] = Field(default_factory=list)
    props: List[StageProp] = Field(default_factory=list)
    a_start_x: float = Field(default=0.24, description="주인공 A 시작 x (0~1, 무대 밖이면 <0 또는 >1)")
    b_start_x: float = Field(default=0.76, description="빌런 B 시작 x")
    cuts: List[CutPlan] = Field(default_factory=list)


# --------------------------------------------------------------------------- 검증/정규화
_ID_RE = re.compile(r"[^a-z0-9_]+")


def slug(text: str) -> str:
    s = _ID_RE.sub("_", (text or "").strip().lower()).strip("_")
    return s or "unnamed"


def _split_ref(ref: str) -> Tuple[str, str]:
    if ":" in ref:
        a, b = ref.split(":", 1)
        return a.strip().lower(), b.strip()
    return "", ref.strip()


def validate_plan(plan: StagePlan, n_cuts: int) -> Tuple[StagePlan, List[str]]:
    """참조 무결성 검사 + 자동 보정. 반환: (보정된 plan, 경고 목록). 치명적 문제는 ValueError."""
    warns: List[str] = []
    plan.location.id = slug(plan.location.id)
    plan.location.backdrop.id = slug(plan.location.backdrop.id or plan.location.id)
    plan.location.backdrop.kind = "backdrop"

    piece_ids, prop_ids = set(), set()
    for sp in plan.set_pieces:
        sp.id = slug(sp.id)
        sp.asset.id = slug(sp.asset.id)
        sp.asset.kind = "set_piece"
        if sp.layer not in LAYERS:
            warns.append(f"piece {sp.id}: 알 수 없는 layer '{sp.layer}' → mid")
            sp.layer = "mid"
        sp.x = min(max(sp.x, 0.02), 0.98)
        piece_ids.add(sp.id)
    for pr in plan.props:
        pr.id = slug(pr.id)
        pr.asset.id = slug(pr.asset.id)
        pr.asset.kind = "prop"
        prop_ids.add(pr.id)
        kind, ref = _split_ref(pr.start)
        if kind == "surface" and slug(ref) not in piece_ids:
            warns.append(f"prop {pr.id}: surface 대상 piece '{ref}' 없음 → floor")
            pr.start = "floor"
        elif kind == "surface":
            pr.start = f"surface:{slug(ref)}"
        elif kind == "hand" and ref.upper() not in ACTORS:
            pr.start = "floor"

    def check_ref(beat: Beat, field: str, value: str) -> str:
        if not value:
            return value
        kind, ref = _split_ref(value)
        if kind == "prop":
            return f"prop:{slug(ref)}" if slug(ref) in prop_ids else (warns.append(f"{beat.id}.{field}: prop '{ref}' 없음 → 제거") or "")
        if kind == "piece":
            return f"piece:{slug(ref)}" if slug(ref) in piece_ids else (warns.append(f"{beat.id}.{field}: piece '{ref}' 없음 → 제거") or "")
        if ref.upper() in ACTORS:
            return ref.upper()
        if field == "to" and (ref in STAGE_MARKS or _is_float(ref)):
            return ref
        # 접두사 없이 id 만 쓴 경우 자동 해석
        if slug(ref) in prop_ids:
            return f"prop:{slug(ref)}"
        if slug(ref) in piece_ids:
            return f"piece:{slug(ref)}"
        warns.append(f"{beat.id}.{field}: 해석 불가 '{value}' → 제거")
        return ""

    fixed_cuts: List[CutPlan] = []
    history: Dict[str, Beat] = {}   # mirror 가 이전 컷의 비트도 참조할 수 있도록 누적
    for cp in plan.cuts:
        ids = set()
        out: List[Beat] = []
        for b in cp.beats:
            b.id = slug(b.id) or f"b{len(out) + 1}"
            if b.id in ids:
                b.id = f"{b.id}_{len(out)}"
            ids.add(b.id)
            b.actor = b.actor.strip().upper()
            if b.actor not in ACTORS:
                warns.append(f"cut{cp.scene_number}/{b.id}: 알 수 없는 배우 '{b.actor}' → 비트 제거")
                continue
            if b.of and b.verb != "mirror" and (b.of in history or any(x.id == b.of for x in cp.beats)):
                b.verb = "mirror"   # of 를 채웠다면 미러링 의도로 해석
            if b.verb not in VERB_DOC:
                warns.append(f"cut{cp.scene_number}/{b.id}: 알 수 없는 동사 '{b.verb}' → idle 로 대체 (동사 라이브러리 확장 후보)")
                b.verb = "idle"
            b.target = check_ref(b, "target", b.target)
            b.to = check_ref(b, "to", b.to)
            if b.prop:
                b.prop = slug(_split_ref(b.prop)[1])
                if b.prop not in prop_ids:
                    warns.append(f"cut{cp.scene_number}/{b.id}: prop '{b.prop}' 없음 → 제거")
                    b.prop = ""
            b.offset = max(-3.0, min(6.0, b.offset))
            b.dur = max(0.0, min(8.0, b.dur))
            out.append(b)
        cp.beats = _expand_mirrors(out, warns, history)
        for b in out:
            history[b.id] = b
        fixed_cuts.append(cp)
    plan.cuts = fixed_cuts

    # 컷 번호 정렬/보충
    have = {c.scene_number for c in plan.cuts}
    for n in range(1, n_cuts + 1):
        if n not in have:
            warns.append(f"cut{n}: 비트 없음 → 빈 컷(idle)")
            plan.cuts.append(CutPlan(scene_number=n, beats=[]))
    plan.cuts.sort(key=lambda c: c.scene_number)
    plan.cuts = plan.cuts[:n_cuts]
    return plan, warns


def _is_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


_MIRRORABLE = {"throw", "slam", "punch", "kick", "push", "grab", "point_at", "slide", "hand_over", "shout"}


def _expand_mirrors(beats: List[Beat], warns: List[str], history: Optional[Dict[str, Beat]] = None) -> List[Beat]:
    """mirror 매크로 전개: 원 비트의 배우/대상을 뒤집은 복제 비트(+필요 시 pick_up)로 치환."""
    by_id = dict(history or {})
    by_id.update({b.id: b for b in beats})
    out: List[Beat] = []
    for b in beats:
        if b.verb != "mirror":
            out.append(b)
            continue
        src = by_id.get(b.of)
        if src is None or src.verb not in _MIRRORABLE:
            warns.append(f"{b.id}: mirror 대상 '{b.of}' 없음/미지원 → shout 로 대체")
            b.verb = "shout"
            out.append(b)
            continue
        victim = src.target if src.target in ACTORS else ("B" if src.actor == "A" else "A")
        new_actor = victim if victim in ACTORS else b.actor
        new_target = src.actor
        prop = src.prop or b.prop
        if prop and src.verb in {"throw", "slide", "hand_over"}:
            out.append(Beat(id=f"{b.id}_pick", actor=new_actor, verb="pick_up", prop=prop,
                            when=b.when, offset=b.offset, dur=0.0))
            out.append(Beat(id=b.id, actor=new_actor, verb=src.verb, target=new_target, prop=prop,
                            to=src.to, when=f"after:{b.id}_pick", offset=0.0, dur=src.dur))
        else:
            out.append(Beat(id=b.id, actor=new_actor, verb=src.verb, target=new_target, prop=prop,
                            to=src.to, when=b.when, offset=b.offset, dur=src.dur))
    return out
