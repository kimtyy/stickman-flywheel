"""스테이지 엔진: 연출 IR(StagePlan)의 '행동 비트'를 시간축에서 시뮬레이션해 프레임으로 그린다.

  - 동사 34개는 여기서 한 번 구현되고, 어떤 장소/소품에서도 앵커(grip/surface/hit_zone/hinge)에 대해 동작한다.
  - 배우 위치·들고 있는 물건·문 열림 등 상태는 컷을 넘어 이어진다(StageRuntime).
  - 카메라/오버레이/말풍선/자막 합성은 기존 SceneVisuals._compose 를 그대로 재사용한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

from asset_library import AssetSpec, Anchor, derive_default_anchors, render_spec
from director_ir import STAGE_MARKS, VERB_DOC, CutPlan, Beat, StagePlan, slug
from module3_renderer import (FLOOR_Y, FPS, H, W, POSES, SceneVisuals, UA, FA, _base, _ease, _lerp,
                              compute_figure, draw_figure, fx_anger, fx_burst, fx_stars, fx_sweat, get_font)

GRAVITY = 2600.0
WALK_STEP_PX = 150.0
CHEST_K = 0.35


def _sgn(v: float) -> int:
    return 1 if v >= 0 else -1


def _clamp(v: float, a: float, b: float) -> float:
    return max(a, min(b, v))


LYING_HEAD_REACH = 410.0   # 누운 몸: 힙에서 머리 쪽(넘어지는 방향)으로 뻗는 길이(머리 반지름 포함)
LYING_LEG_REACH = 260.0    # 힙에서 다리 쪽으로 뻗는 길이


def _lying_x(x: float, facing: int) -> float:
    """쓰러진 뒤 몸 전체가 화면 안(좌우 20px 여백)에 들어오도록 힙 x 를 제한한다."""
    back = -facing                      # 넘어지는 방향
    if back > 0:
        return _clamp(x, 20 + LYING_LEG_REACH, W - 20 - LYING_HEAD_REACH)
    return _clamp(x, 20 + LYING_HEAD_REACH, W - 20 - LYING_LEG_REACH)


# --------------------------------------------------------------------------- 상태
@dataclass
class Actor:
    id: str
    x: float
    facing: int
    visible: bool = True
    sit_k: float = 0.0          # 0 서있음 ~ 1 앉음
    sit_target: float = 0.0
    down_k: float = 0.0         # 0 서있음 ~ 1 쓰러짐
    down_target: float = 0.0
    crossed: bool = False
    holding: Optional[str] = None
    phase: float = 0.0
    flinch_until: float = -1.0
    face: str = ""              # "X" 등 표정 고정(기절)
    last_fig: Optional[Dict] = None
    air: float = 0.0
    lean_extra: float = 0.0


@dataclass
class PropState:
    id: str
    spec: AssetSpec
    anchors: Dict[str, Anchor]
    mode: str = "rest"          # rest | held | flying | falling | gone
    x: float = 0.0
    y: float = 0.0              # rest: 밑변 y / flying·falling: 중심 y
    rot: float = 0.0
    spin: float = 0.0
    vx: float = 0.0
    vy: float = 0.0
    holder: Optional[str] = None
    surface: Optional[str] = None
    ground_y: float = FLOOR_Y
    fly: Optional[Dict[str, Any]] = None


@dataclass
class PieceState:
    id: str
    spec: AssetSpec
    anchors: Dict[str, Anchor]
    x: float                    # 중심 px
    layer: str
    open: float = 0.0

    @property
    def left(self) -> float:
        return self.x - self.spec.w / 2

    @property
    def top(self) -> float:
        return FLOOR_Y - self.spec.h

    def surface_y(self) -> float:
        return self.top + self.anchors["surface"].y

    def surface_range(self) -> Tuple[float, float]:
        a = self.anchors["surface"]
        return self.left + a.x, self.left + a.x + (a.w or self.spec.w)


@dataclass
class ScheduledBeat:
    beat: Beat
    t0: float
    t1: float
    started: bool = False
    ended: bool = False
    d: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FrameState:
    actors: Dict[str, Dict[str, Any]]
    props: List[Dict[str, Any]]
    pieces: Dict[str, float]
    fx: List[Dict[str, Any]]
    shake: float


# --------------------------------------------------------------------------- 런타임 (에피소드 전체에서 상태 유지)
class StageRuntime:
    def __init__(self, plan: StagePlan, assets: Dict[str, AssetSpec], log=print) -> None:
        self.plan, self.assets, self.log = plan, assets, log
        self.warnings: List[str] = []
        bd = assets[f"backdrop:{slug(plan.location.backdrop.id)}"]
        self.backdrop = render_spec(bd, 1.0, 1).convert("RGB")
        self._sprites: Dict[Tuple[int, float], Image.Image] = {}
        self.pieces: Dict[str, PieceState] = {}
        for sp in plan.set_pieces:
            spec = assets[f"set_piece:{slug(sp.asset.id)}"]
            self.pieces[sp.id] = PieceState(sp.id, spec, derive_default_anchors(spec), sp.x * W, sp.layer)
        self.props: Dict[str, PropState] = {}
        self.actors: Dict[str, Actor] = {
            "A": Actor("A", plan.a_start_x * W, 1, visible=0.0 <= plan.a_start_x <= 1.0),
            "B": Actor("B", plan.b_start_x * W, -1, visible=0.0 <= plan.b_start_x <= 1.0),
        }
        self.actors["A"].facing = 1 if plan.a_start_x <= plan.b_start_x else -1
        self.actors["B"].facing = -self.actors["A"].facing
        for pr in plan.props:
            spec = assets[f"prop:{slug(pr.asset.id)}"]
            ps = PropState(pr.id, spec, derive_default_anchors(spec), x=pr.x * W)
            kind, _, ref = pr.start.partition(":")
            if kind == "surface" and slug(ref) in self.pieces:
                pc = self.pieces[slug(ref)]
                lo, hi = pc.surface_range()
                ps.x = _clamp(ps.x, lo + 25, hi - 25)
                ps.y = ps.ground_y = pc.surface_y()
                ps.surface = pc.id
            elif kind == "hand" and ref.upper() in self.actors:
                ps.mode, ps.holder = "held", ref.upper()
                self.actors[ref.upper()].holding = pr.id
            else:
                ps.y = ps.ground_y = FLOOR_Y
            self.props[pr.id] = ps
        # --- 컷 시뮬레이션 중 쓰는 임시 상태
        self.t = 0.0
        self.pose_over: Dict[str, Dict[str, Any]] = {}
        self.pose_full: Dict[str, Dict[str, Any]] = {}
        self.tags: Dict[str, set] = {}
        self.fx: List[Dict[str, Any]] = []
        self.shake = 0.0
        self.moved: Dict[str, float] = {}
        self.active: List[ScheduledBeat] = []
        self.all_beats: List[ScheduledBeat] = []
        self.timeline: List[Dict] = []

    def doorway(self, pc: PieceState, size: Tuple[int, int]) -> Image.Image:
        """문이 열렸을 때 보이는 문 밖(밤거리) — 새까만 사각형 대신 그라데이션 + 가로등 빛."""
        key = (id(pc.spec), "doorway")
        if key not in self._sprites:
            w, h = size
            t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
            arr = np.array([20, 26, 54], np.float32) * (1 - t) + np.array([70, 78, 112], np.float32) * t
            arr = np.broadcast_to(arr, (h, w, 3)).copy()
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            glow = np.clip(1.0 - np.hypot((xx - w * 0.5) / (w * 0.55), (yy - h * 0.25) / (h * 0.3)), 0, 1)[..., None]
            arr = arr + glow * np.array([95, 85, 40], np.float32)
            arr[int(h * 0.88):] = arr[int(h * 0.88):] * 0.7 + np.array([30, 30, 36], np.float32)
            self._sprites[key] = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        return self._sprites[key]

    # ---- 스프라이트
    def sprite(self, spec: AssetSpec) -> Image.Image:
        key = (id(spec), 1.0)
        if key not in self._sprites:
            self._sprites[key] = render_spec(spec, 1.0, 3)
        return self._sprites[key]

    # ---- 좌표 해석
    def resolve_x(self, ref: str, actor: Optional[Actor] = None, default: float = 0.5) -> float:
        if not ref:
            return default * W
        kind, _, val = ref.partition(":")
        if kind == "prop" and slug(val) in self.props:
            return self.props[slug(val)].x
        if kind == "piece" and slug(val) in self.pieces:
            return self.pieces[slug(val)].x
        if ref.upper() in self.actors:
            return self.actors[ref.upper()].x
        if ref in STAGE_MARKS:
            return STAGE_MARKS[ref] * W
        try:
            return float(ref) * W
        except ValueError:
            return default * W

    def point_of(self, ref: str) -> Tuple[float, float]:
        """상호작용 목표 지점(월드 px)."""
        kind, _, val = ref.partition(":")
        if kind == "prop" and slug(val) in self.props:
            p = self.props[slug(val)]
            return self.prop_center(p)
        if kind == "piece" and slug(val) in self.pieces:
            pc = self.pieces[slug(val)]
            lo, hi = pc.surface_range()
            return (lo + hi) / 2, pc.surface_y()
        a = self.actors.get(ref.upper())
        if a is not None:
            fig = a.last_fig
            if fig:
                sx, sy = fig["shoulder"]
                hx, hy = fig["hip"]
                return _lerp(sx, hx, CHEST_K), _lerp(sy, hy, CHEST_K)
            return a.x, FLOOR_Y - 330
        return self.resolve_x(ref), FLOOR_Y - 330

    def prop_center(self, p: PropState) -> Tuple[float, float]:
        if p.mode == "rest":
            return p.x, p.y - p.spec.h / 2
        if p.mode == "held" and p.holder:
            hand = self.hand_of(p.holder)
            if hand:
                g = p.anchors["grip"]
                return hand[0] + (p.spec.w / 2 - g.x), hand[1] + (p.spec.h / 2 - g.y)
        return p.x, p.y

    def hand_of(self, actor_id: str) -> Optional[Tuple[float, float]]:
        fig = self.actors[actor_id].last_fig
        return tuple(fig["arms"][1][2]) if fig else None

    def other(self, actor_id: str) -> Actor:
        return self.actors["B" if actor_id == "A" else "A"]

    def piece_for_surface(self, x: float) -> Optional[PieceState]:
        best = None
        for pc in self.pieces.values():
            lo, hi = pc.surface_range()
            if lo - 10 <= x <= hi + 10 and "surface" in pc.anchors:
                best = pc
        return best

    # ---- 공통 연산
    def set_pose(self, aid: str, **kv: Any) -> None:
        self.pose_over.setdefault(aid, {}).update(kv)

    def tag(self, aid: str, name: str) -> None:
        self.tags.setdefault(aid, set()).add(name)

    def add_fx(self, kind: str, x: float, y: float, text: str = "", dur: float = 0.45) -> None:
        self.fx.append({"kind": kind, "x": x, "y": y, "text": text, "t0": self.t, "dur": dur})

    def impulse_shake(self, amp: float) -> None:
        self.shake = max(self.shake, amp)

    def flinch(self, aid: str, dur: float = 0.55) -> None:
        self.actors[aid].flinch_until = self.t + dur

    def solve_arm(self, aid: str, pose: Dict[str, Any], target: Tuple[float, float], arm: int = 1) -> Tuple[Tuple[float, float], bool]:
        """2관절 IK: pose(lean 등)를 적용한 어깨에서 target 까지의 (상완각, 전완각)과 도달 가능 여부."""
        a = self.actors[aid]
        fig = compute_figure(pose, a.facing, a.x, FLOOR_Y)
        sx, sy = fig["shoulder"]
        dx, dy = (target[0] - sx) * a.facing, target[1] - sy
        d_raw = math.hypot(dx, dy)
        reach_ok = d_raw <= UA + FA - 2
        d = _clamp(d_raw, abs(UA - FA) + 2, UA + FA - 2)
        theta = math.atan2(dx, dy)
        cosA = _clamp((UA * UA + d * d - FA * FA) / (2 * UA * d), -1.0, 1.0)
        A = math.acos(cosA)
        best = None
        for a1 in (theta - A, theta + A):
            mid = (UA * math.sin(a1), UA * math.cos(a1))
            if best is None or mid[1] > best[1][1]:   # 팔꿈치가 더 아래로 처진 해 선택
                best = (a1, mid)
        a1, mid = best
        a2 = math.atan2(dx - mid[0], dy - mid[1])
        return (math.degrees(a1), math.degrees(a2)), reach_ok

    # ---- 컷 스케줄
    def _schedule(self, cut: CutPlan, dur: float, timeline: List[Dict]) -> List[ScheduledBeat]:
        sched: Dict[str, ScheduledBeat] = {}
        pending = list(cut.beats)
        out: List[ScheduledBeat] = []
        for _ in range(len(pending) + 1):
            rest = []
            for b in pending:
                kind, _, ref = b.when.partition(":")
                base: Optional[float] = None
                if kind in ("", "cut_start"):
                    base = 0.0
                elif kind == "after" and ref in sched:
                    base = sched[ref].t1
                elif kind == "with" and ref in sched:
                    base = sched[ref].t0
                elif kind in ("seg_start", "seg_end"):
                    try:
                        seg = timeline[int(ref)]
                        base = seg["start"] if kind == "seg_start" else seg["end"]
                    except (ValueError, IndexError):
                        self.warnings.append(f"cut{cut.scene_number}/{b.id}: 대사 세그먼트 '{ref}' 없음 → cut_start")
                        base = 0.0
                if base is None:
                    rest.append(b)
                    continue
                t0 = max(0.0, base + b.offset)
                d = b.dur or VERB_DOC[b.verb][1]
                if d <= 0:
                    d = 1.4 if b.verb == "walk_to" else 0.8
                if t0 >= dur - 0.12:
                    self.warnings.append(f"cut{cut.scene_number}/{b.id}({b.verb}): 컷 길이({dur:.1f}s)를 넘어 생략")
                    sched[b.id] = ScheduledBeat(b, dur, dur, True, True)
                    continue
                t1 = min(t0 + d, dur - 0.04)
                sb = ScheduledBeat(b, t0, max(t1, t0 + 0.04))
                sched[b.id] = sb
                out.append(sb)
            if len(rest) == len(pending):
                for b in rest:
                    self.warnings.append(f"cut{cut.scene_number}/{b.id}: when '{b.when}' 해석 불가 → cut_start")
                    b.when = "cut_start"
                continue
            pending = rest
            if not pending:
                break
        out.sort(key=lambda s: s.t0)
        return out

    # ---- 컷 시뮬레이션
    def simulate_cut(self, cut: CutPlan, dur: float, timeline: List[Dict], hit_t: float) -> List[FrameState]:
        beats = self._schedule(cut, dur, timeline)
        self.all_beats = beats
        self.timeline = timeline or []
        n = max(1, int(round(dur * FPS)))
        ctx = {"hit_t": hit_t, "land_t": 0.9}
        frames: List[FrameState] = []
        self.active = []
        self.fx = []
        self.shake = 0.0
        for i in range(n):
            t = i / FPS
            dt = 1.0 / FPS
            self.t = t
            self.pose_over, self.pose_full, self.tags, self.moved = {}, {}, {}, {}
            for sb in beats:
                if not sb.started and t >= sb.t0:
                    sb.started = True
                    self.active.append(sb)
                    VERBS[sb.beat.verb].start(self, sb, t)
            for sb in list(self.active):
                u = _clamp((t - sb.t0) / max(sb.t1 - sb.t0, 1e-6), 0.0, 1.0)
                VERBS[sb.beat.verb].step(self, sb, u, t)
                if t >= sb.t1 - 1e-6 or i == n - 1:
                    VERBS[sb.beat.verb].end(self, sb, t)
                    sb.ended = True
                    self.active.remove(sb)
            self._physics(t, dt, ctx)
            frames.append(self._snapshot(t, dt, ctx))
            self.shake = max(0.0, self.shake - 60.0 * dt)
        # 컷 종료 정리: 날아가던 물건은 착지시켜 다음 컷으로 상태를 이어준다
        for p in self.props.values():
            if p.mode in ("flying", "falling"):
                self._land(p)
        return frames

    # ---- 물리
    def _land(self, p: PropState) -> None:
        p.mode, p.vx, p.vy, p.spin, p.fly = "rest", 0.0, 0.0, 0.0, None
        pc = self.piece_for_surface(p.x) if p.ground_y != FLOOR_Y else None
        p.y = p.ground_y
        p.rot = 0.0
        p.surface = pc.id if pc else None

    def _physics(self, t: float, dt: float, ctx: Dict) -> None:
        for p in self.props.values():
            if p.mode == "flying" and p.fly:
                f = p.fly
                tau = t - f["t0"]
                if tau >= f["T"]:
                    p.x, p.y = f["tx"], f["ty"]
                    self._impact(p, f, t)
                else:
                    p.x = f["sx"] + f["vx"] * tau
                    p.y = f["sy"] + f["vy"] * tau + 0.5 * GRAVITY * tau * tau
                    p.rot += p.spin * dt
            elif p.mode == "falling":
                p.vy += GRAVITY * dt
                p.x += p.vx * dt
                p.y += p.vy * dt
                p.rot += p.spin * dt
                bottom = p.y + p.spec.h / 2
                if bottom >= p.ground_y:
                    if abs(p.vy) > 500:
                        p.vy *= -0.32
                        p.vx *= 0.5
                        p.y = p.ground_y - p.spec.h / 2
                    else:
                        p.mode, p.y, p.rot, p.vx, p.vy, p.spin = "rest", p.ground_y, 0.0, 0.0, 0.0, 0.0
                        pc = self.piece_for_surface(p.x) if p.ground_y != FLOOR_Y else None
                        p.surface = pc.id if pc else None

    def _impact(self, p: PropState, f: Dict, t: float) -> None:
        target = f["target"]
        tgt = self.actors.get(target.upper()) if target else None
        if tgt is not None and self._catching(tgt.id, t):
            p.mode, p.holder, tgt.holding, p.fly = "held", tgt.id, p.id, None
            p.rot = 0.0
            return
        if tgt is not None:
            self.flinch(tgt.id)
            self.add_fx("burst", p.x, p.y, "퍽!")
            self.impulse_shake(24.0)
            p.vx, p.vy, p.spin = -f["dir"] * 260.0, -520.0, -f["dir"] * 360.0
        else:
            self.impulse_shake(10.0)
            p.vx, p.vy, p.spin = f["dir"] * 120.0, -260.0, f["dir"] * 200.0
        p.mode, p.fly = "falling", None
        pc = self.piece_for_surface(p.x) if f.get("surface_land") else None
        p.ground_y = pc.surface_y() if pc else FLOOR_Y

    def _catching(self, aid: str, t: float) -> bool:
        return any(sb.beat.verb == "catch" and sb.beat.actor == aid and sb.t0 - 0.5 <= t <= sb.t1 + 0.4
                   for sb in self.all_beats)

    # ---- 프레임 스냅샷 (배우 포즈 합성)
    def _snapshot(self, t: float, dt: float, ctx: Dict) -> FrameState:
        actors: Dict[str, Dict[str, Any]] = {}
        for aid, a in self.actors.items():
            a.sit_k += _clamp(a.sit_target - a.sit_k, -4.0 * dt, 4.0 * dt)
            a.down_k += _clamp(a.down_target - a.down_k, -3.0 * dt, 3.0 * dt)
            pose = self._base_pose(a, t)
            full = self.pose_full.get(aid)
            if full is not None:
                pose.update({k: v for k, v in full.items() if k not in ("xoff", "rot", "air")})
            pose.update(self.pose_over.get(aid, {}))
            if self._speaker_at(t) == aid and "mouth" not in self.pose_over.get(aid, {}) and a.down_k < 0.5:
                pose.update(mouth="talk", mo=abs(math.sin(t * 13)))
            moved = self.moved.get(aid, 0.0)
            if abs(moved) > 0.4 and a.sit_k < 0.5 and a.down_k < 0.5:
                a.phase += abs(moved) / WALK_STEP_PX * math.pi
                running = abs(moved) / max(dt, 1e-6) > 480
                amp = 44.0 if running else 26.0
                s, c = math.sin(a.phase), math.cos(a.phase)
                pose.update(ll=(amp * s, amp * s * 0.6 - 10 * abs(c)), rl=(-amp * s, -amp * s * 0.6 - 10 * abs(c)),
                            lean=(14.0 if running else 6.0))
                if "ra" not in self.pose_over.get(aid, {}) and a.holding is None:
                    pose.update(la=(-amp * s + 8, -amp * s + 20), ra=(amp * s + 4, amp * s + 24))
            if a.sit_k > 0.01:
                k = a.sit_k
                pose["ll"] = (_lerp(pose["ll"][0], 88, k), _lerp(pose["ll"][1], 70, k))
                pose["rl"] = (_lerp(pose["rl"][0], 80, k), _lerp(pose["rl"][1], 66, k))
                pose["air"] = pose.get("air", 0.0) + 100.0 * k
                pose["lean"] = pose.get("lean", 0.0) * (1 - 0.5 * k)
            if a.down_k > 0.01:
                k = a.down_k
                pose["rot"] = -95.0 * k
                pose["ll"] = (_lerp(pose["ll"][0], 14, k), _lerp(pose["ll"][1], 24, k))
                pose["rl"] = (_lerp(pose["rl"][0], -10, k), _lerp(pose["rl"][1], 6, k))
                pose["la"] = (_lerp(pose["la"][0], 70, k), _lerp(pose["la"][1], 30, k))
                pose["ra"] = (_lerp(pose["ra"][0], 110, k), _lerp(pose["ra"][1], 50, k))
                pose["eyes"] = "X" if a.face == "X" else pose["eyes"]
            pose["air"] = pose.get("air", 0.0) + a.air
            if t < a.flinch_until:
                k = (a.flinch_until - t) / 0.55
                pose.update(lean=-14.0 * k, eyes="wide", mouth="O", xoff=pose.get("xoff", 0.0) + 5 * math.sin(t * 60) * k)
                self.tag(aid, "sweat")
            if a.face == "X" and a.down_k > 0.01:
                pose["eyes"] = "X"
            fig = compute_figure(pose, a.facing, a.x, FLOOR_Y)
            a.last_fig = fig
            actors[aid] = {"pose": pose, "fig": fig, "x": a.x, "facing": a.facing, "visible": a.visible,
                           "tags": set(self.tags.get(aid, set()))}
            if a.down_k > 0.9 and a.face == "X":
                actors[aid]["tags"].add("stars")
        props = [{"id": p.id, "mode": p.mode, "x": p.x, "y": p.y, "rot": p.rot, "holder": p.holder,
                  "surface": p.surface, "spec": p.spec, "anchors": p.anchors} for p in self.props.values() if p.mode != "gone"]
        fx = [dict(e, age=t - e["t0"]) for e in self.fx if 0 <= t - e["t0"] <= e["dur"]]
        return FrameState(actors, props, {pid: pc.open for pid, pc in self.pieces.items()}, fx, self.shake)

    def _speaker_at(self, t: float) -> Optional[str]:
        """발화 중인 배우(A/B). 해설/무화자 구간은 None — 기존 SceneVisuals 의 화자 규약과 동일."""
        for seg in self.timeline:
            if seg["start"] - 0.05 <= t <= seg["end"] + 0.08:
                spk = (seg.get("speaker") or "").strip().upper()
                if spk in ("A", "주인공", "남자") or ("A" in spk and "B" not in spk and spk not in ("해설",)):
                    return "A"
                if spk in ("B", "빌런", "상대", "여자", "진상") or ("B" in spk and "A" not in spk):
                    return "B"
                return None
        return None

    def _base_pose(self, a: Actor, t: float) -> Dict[str, Any]:
        p = _base()
        b = math.sin(t * 2.6 + (0.0 if a.id == "A" else 1.7))
        p.update(lean=2.0 + 0.8 * b, tilt=-1.5 + 1.0 * b, la=(8 + 3 * b, 6 + 2 * b), ra=(-6 + 2 * b, 8 + 2 * b),
                 ll=(5.0, -3.0), rl=(-5.0, 3.0))
        if a.id == "A":
            p.update(eyes="dot", mouth="smirk")
        else:
            p.update(eyes="dot", mouth="smirk")
        if a.crossed:
            p.update(la=(24, 112), ra=(30, 106))
        if a.holding:
            p.update(ra=(52, 100))
        return p


# --------------------------------------------------------------------------- 동사 구현
class Verb:
    def start(self, rt: StageRuntime, sb: ScheduledBeat, t: float) -> None: ...
    def step(self, rt: StageRuntime, sb: ScheduledBeat, u: float, t: float) -> None: ...
    def end(self, rt: StageRuntime, sb: ScheduledBeat, t: float) -> None: ...


def _actor(rt: StageRuntime, sb: ScheduledBeat) -> Actor:
    return rt.actors[sb.beat.actor]


def _face_toward(a: Actor, x: float) -> None:
    if abs(x - a.x) > 8:
        a.facing = _sgn(x - a.x)


def _move(rt: StageRuntime, a: Actor, x0: float, x1: float, u: float, ease: bool = True) -> None:
    nx = _lerp(x0, x1, _ease(u) if ease else u)
    rt.moved[a.id] = rt.moved.get(a.id, 0.0) + (nx - a.x)
    a.x = nx


class Enter(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        b = sb.beat
        dest = rt.resolve_x(b.to, a, default=0.24 if a.id == "A" else 0.76)
        if b.target.startswith("piece:") and slug(b.target[6:]) in rt.pieces:
            x0 = rt.pieces[slug(b.target[6:])].x
        else:
            x0 = (-0.22 * W) if dest < W / 2 else (1.22 * W)
            if abs(a.x - dest) < 40 or (0 <= a.x <= W and a.visible):
                x0 = a.x
        a.visible = True
        sb.d.update(x0=x0, x1=dest)
        a.x = x0
        _face_toward(a, dest)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], u, ease=False)

    def end(self, rt, sb, t):
        a = _actor(rt, sb)
        a.x = sb.d["x1"]
        _face_toward(a, rt.other(a.id).x)


class Exit(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        to = sb.beat.to
        dest = rt.resolve_x(to, a) if to in STAGE_MARKS or to else (-0.22 * W if a.x < W / 2 else 1.22 * W)
        if 0 <= dest <= W:
            dest = -0.22 * W if dest < W / 2 else 1.22 * W
        sb.d.update(x0=a.x, x1=dest)
        _face_toward(a, dest)

    def step(self, rt, sb, u, t):
        _move(rt, _actor(rt, sb), sb.d["x0"], sb.d["x1"], u, ease=False)

    def end(self, rt, sb, t):
        a = _actor(rt, sb)
        a.x = sb.d["x1"]
        a.visible = False


class Walk(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        b = sb.beat
        dest = rt.resolve_x(b.to or b.target, a)
        if (b.to or b.target).upper() in rt.actors:
            dest -= _sgn(dest - a.x) * 250
        elif (b.to or b.target).startswith(("prop:", "piece:")):
            dest -= _sgn(dest - a.x) * 120
        sb.d.update(x0=a.x, x1=_clamp(dest, -0.3 * W, 1.3 * W))
        _face_toward(a, sb.d["x1"])

    def step(self, rt, sb, u, t):
        _move(rt, _actor(rt, sb), sb.d["x0"], sb.d["x1"], u)

    def end(self, rt, sb, t):
        _actor(rt, sb).x = sb.d["x1"]


class TurnTo(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        ref = sb.beat.target or sb.beat.to
        _face_toward(a, rt.point_of(ref)[0] if ref else rt.other(a.id).x)


class Idle(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        a.crossed = False


class CrossArms(Verb):
    def start(self, rt, sb, t):
        _actor(rt, sb).crossed = True


class Sit(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        ref = sb.beat.target or sb.beat.to
        sb.d.update(x0=a.x, x1=rt.resolve_x(ref, a) if ref else a.x)
        a.sit_target = 1.0

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], _clamp(u / 0.5, 0, 1))

    def end(self, rt, sb, t):
        a = _actor(rt, sb)
        a.x = sb.d["x1"]
        _face_toward(a, rt.other(a.id).x)


class Stand(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        a.sit_target = a.down_target = 0.0
        a.face = ""


class LeanOn(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        ref = sb.beat.target or sb.beat.to
        px = rt.resolve_x(ref, a) if ref else a.x
        sb.d.update(x0=a.x, x1=px - _sgn(px - a.x) * 170)
        _face_toward(a, px)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], _clamp(u / 0.4, 0, 1))
        rt.set_pose(a.id, lean=14.0, ra=(78, 82), la=(70, 80))


class PointAt(Verb):
    def start(self, rt, sb, t):
        _face_toward(_actor(rt, sb), rt.point_of(sb.beat.target)[0])

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        k = _ease(_clamp(u / 0.3, 0, 1))
        rt.set_pose(a.id, lean=_lerp(2, 10, k), ra=(_lerp(30, 88, k), _lerp(60, 90, k)), mouth="talk", mo=abs(math.sin(t * 13)), brow=True, eyes="angry")


def _approach(rt: StageRuntime, sb: ScheduledBeat, a: Actor, px: float, gap: float, u: float, frac: float = 0.35) -> None:
    if "x0" not in sb.d:
        side = _sgn(a.x - px) if abs(a.x - px) > 5 else -a.facing
        sb.d.update(x0=a.x, x1=px + side * gap if abs(a.x - px) > gap else a.x)
        _face_toward(a, px)
    _move(rt, a, sb.d["x0"], sb.d["x1"], _clamp(u / frac, 0, 1))


class PickUp(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        pid = sb.beat.prop
        if not pid and a.holding is None:
            pid = min(rt.props.values(), key=lambda p: abs(p.x - a.x)).id if rt.props else ""
        sb.d["pid"] = pid
        if a.holding and a.holding != pid:  # 이미 다른 것을 들었다면 바닥에 둔다
            old = rt.props[a.holding]
            old.mode, old.holder, old.y, old.x, old.ground_y = "rest", None, FLOOR_Y, a.x, FLOOR_Y
            a.holding = None

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        p = rt.props.get(sb.d["pid"])
        if p is None:
            return
        if p.mode != "held":
            _approach(rt, sb, a, p.x, 150.0, u)
            lean = _lerp(2, 16, _ease(_clamp(u / 0.5, 0, 1)))
            pose = _base()
            pose.update(lean=lean)
            g = p.anchors["grip"]
            gx, gy = (p.x - p.spec.w / 2 + g.x, p.y - p.spec.h + g.y) if p.mode == "rest" else rt.prop_center(p)
            ang, ok = rt.solve_arm(a.id, pose, (gx, gy))
            k = _ease(_clamp(u / 0.55, 0, 1))
            rt.set_pose(a.id, lean=lean, ra=(_lerp(30, ang[0], k), _lerp(60, ang[1], k)))
            if u >= 0.6:
                p.mode, p.holder, p.surface, a.holding, p.rot = "held", a.id, None, p.id, 0.0
        else:
            k = _ease(_clamp((u - 0.6) / 0.4, 0, 1))
            rt.set_pose(a.id, lean=_lerp(16, 2, k), ra=(_lerp(70, 52, k), _lerp(80, 100, k)))


class PutDown(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        sb.d["pid"] = sb.beat.prop or (a.holding or "")

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        p = rt.props.get(sb.d["pid"])
        if p is None:
            return
        ref = sb.beat.to or sb.beat.target
        if "dest" not in sb.d:
            px = rt.resolve_x(ref, a, default=0.0) if ref else a.x + a.facing * 150
            if not ref:
                px = a.x + a.facing * 150
            pc = rt.pieces.get(slug(ref.split(":", 1)[1])) if ref.startswith("piece:") else rt.piece_for_surface(px)
            if pc is not None:
                lo, hi = pc.surface_range()
                px = _clamp(px, lo + 25, hi - 25)
                sb.d["dest"] = (px, pc.surface_y(), pc.id)
            else:
                sb.d["dest"] = (px, float(FLOOR_Y), None)
            _face_toward(a, px)
        dx, dy, pid = sb.d["dest"]
        _approach(rt, sb, a, dx, 170.0, u)
        pose = _base()
        pose.update(lean=14)
        ang, _ = rt.solve_arm(a.id, pose, (dx, dy - 30))
        k = _ease(_clamp(u / 0.55, 0, 1))
        rt.set_pose(a.id, lean=_lerp(2, 14, k), ra=(_lerp(52, ang[0], k), _lerp(100, ang[1], k)))
        if u >= 0.65 and p.mode == "held":
            p.mode, p.holder, p.x, p.y, p.ground_y, p.surface, p.rot = "rest", None, dx, dy, dy, pid, 0.0
            a.holding = None


class HoldUp(Verb):
    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        k = _ease(_clamp(u / 0.3, 0, 1))
        rt.set_pose(a.id, ra=(_lerp(52, 150, k), _lerp(100, 172, k)), lean=_lerp(2, -4, k), mouth="talk", mo=abs(math.sin(t * 13)))


class Throw(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        pid = sb.beat.prop or (a.holding or "")
        p = rt.props.get(pid)
        if p is not None and p.mode != "held":  # 안 쥐고 있으면 즉시 쥔 것으로 보정(경고)
            rt.warnings.append(f"{sb.beat.id}: throw 시점에 {pid} 를 들고 있지 않아 자동으로 집어 듦")
            p.mode, p.holder, p.surface, a.holding = "held", a.id, None, pid
        sb.d["pid"] = pid
        _face_toward(a, rt.point_of(sb.beat.target)[0] if sb.beat.target else rt.other(a.id).x)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        p = rt.props.get(sb.d["pid"])
        if u < 0.42:
            k = _ease(u / 0.42)
            rt.set_pose(a.id, lean=_lerp(2, -12, k), ra=(_lerp(52, -115, k), _lerp(100, -165, k)), brow=True, eyes="angry", mouth="teeth")
        else:
            k = _ease(_clamp((u - 0.42) / 0.13, 0, 1))
            rt.set_pose(a.id, lean=_lerp(-12, 24, k), ra=(_lerp(-115, 100, k), _lerp(-165, 92, k)), brow=True, eyes="angry", mouth="teeth")
        if u >= 0.5 and p is not None and p.mode == "held" and not sb.d.get("released"):
            sb.d["released"] = True
            hand = rt.hand_of(a.id) or (a.x, FLOOR_Y - 400)
            cx, cy = rt.prop_center(p)
            tgt_ref = sb.beat.target or ("B" if a.id == "A" else "A")
            tx, ty = rt.point_of(tgt_ref) if tgt_ref else (a.x + a.facing * 400, FLOOR_Y - 300)
            dist = math.hypot(tx - cx, ty - cy)
            T = _clamp(dist / 1500.0, 0.28, 0.6)
            p.mode, p.holder, a.holding = "flying", None, None
            p.x, p.y = cx, cy
            p.spin = a.facing * 720.0
            surface_land = tgt_ref.startswith(("piece:",))
            p.fly = {"t0": t, "T": T, "sx": cx, "sy": cy, "tx": tx, "ty": ty, "vx": (tx - cx) / T,
                     "vy": (ty - cy - 0.5 * GRAVITY * T * T) / T, "target": tgt_ref, "dir": a.facing,
                     "surface_land": surface_land}


class Slide(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        pid = sb.beat.prop or (a.holding or "")
        p = rt.props.get(pid)
        sb.d["pid"] = pid
        if p is None:
            return
        if p.mode == "held":
            hand = rt.hand_of(a.id) or (a.x + a.facing * 120, FLOOR_Y)
            ref = sb.beat.target
            pc = rt.pieces.get(slug(ref.split(":", 1)[1])) if ref.startswith("piece:") else rt.piece_for_surface(hand[0] + a.facing * 60)
            p.mode, p.holder, a.holding, p.rot = "rest", None, None, 0.0
            if pc is not None:
                lo, hi = pc.surface_range()
                p.x, p.y, p.ground_y, p.surface = _clamp(hand[0], lo + 25, hi - 25), pc.surface_y(), pc.surface_y(), pc.id
            else:
                p.x, p.y, p.ground_y = hand[0], FLOOR_Y, FLOOR_Y
        dest_ref = sb.beat.to or ("A" if a.id == "B" else "B")
        dx = rt.resolve_x(dest_ref, a)
        if dest_ref.upper() in rt.actors:
            dx -= _sgn(dx - p.x) * 140
        if p.surface:
            lo, hi = rt.pieces[p.surface].surface_range()
            dx = _clamp(dx, lo + 25, hi - 25)
        sb.d.update(x0=p.x, x1=dx)
        _face_toward(a, dx)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        p = rt.props.get(sb.d["pid"])
        if p is None:
            return
        p.x = _lerp(sb.d["x0"], sb.d["x1"], _ease(u))
        rt.set_pose(a.id, lean=12, ra=(80, 88), brow=True)


class Slam(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        ref = sb.beat.target or sb.beat.to
        pc = rt.pieces.get(slug(ref.split(":", 1)[1])) if ref.startswith("piece:") else None
        if pc is None:
            pc = rt.piece_for_surface(a.x + a.facing * 150)
        lo, hi = pc.surface_range() if pc else (a.x, a.x)
        gx = _clamp(a.x + a.facing * 150, lo, hi) if pc else a.x + a.facing * 150
        sb.d.update(piece=pc.id if pc else None, hit=(gx, pc.surface_y() if pc else FLOOR_Y - 250))
        _face_toward(a, gx)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        hx, hy = sb.d["hit"]
        pose = _base()
        pose.update(lean=16)
        ang, _ = rt.solve_arm(a.id, pose, (hx, hy))
        if u < 0.45:
            k = _ease(u / 0.45)
            rt.set_pose(a.id, lean=_lerp(2, -8, k), ra=(_lerp(30, 150, k), _lerp(60, 170, k)), brow=True, eyes="angry", mouth="teeth")
        else:
            k = _ease(_clamp((u - 0.45) / 0.12, 0, 1))
            rt.set_pose(a.id, lean=_lerp(-8, 16, k), ra=(_lerp(150, ang[0], k), _lerp(170, ang[1], k)), brow=True, eyes="angry", mouth="teeth")
            if not sb.d.get("hit_done") and u >= 0.55:
                sb.d["hit_done"] = True
                rt.add_fx("burst", hx, hy - 30, "쾅!")
                rt.impulse_shake(26.0)
                for p in rt.props.values():
                    if p.mode == "rest" and p.surface == sb.d["piece"]:
                        p.mode, p.vy, p.vx, p.spin = "falling", -520.0, 0.0, 120.0
                        p.y = p.y - p.spec.h / 2
                        pc = rt.pieces.get(p.surface)
                        p.ground_y = pc.surface_y() if pc else FLOOR_Y


class Catch(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        src = rt.other(a.id)
        _face_toward(a, src.x)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        rt.set_pose(a.id, la=(96, 100), ra=(96, 100), mouth="O", eyes="wide")


class Open(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        ref = sb.beat.target or sb.beat.to
        sb.d["piece"] = slug(ref.split(":", 1)[1]) if ref.startswith("piece:") else None
        pc = rt.pieces.get(sb.d["piece"] or "")
        if pc is not None:
            sb.d.update(x0=a.x, x1=pc.x - _sgn(pc.x - a.x) * 200 if abs(a.x - pc.x) > 200 else a.x)
            _face_toward(a, pc.x)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        pc = rt.pieces.get(sb.d["piece"] or "")
        if pc is None:
            return
        _move(rt, a, sb.d.get("x0", a.x), sb.d.get("x1", a.x), _clamp(u / 0.35, 0, 1))
        rt.set_pose(a.id, lean=8, ra=(80, 88))
        pc.open = _ease(u)

    def end(self, rt, sb, t):
        pc = rt.pieces.get(sb.d["piece"] or "")
        if pc is not None:
            pc.open = 1.0


class HandOver(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        tgt = rt.actors.get(sb.beat.target.upper()) or rt.other(a.id)
        sb.d["tgt"] = tgt.id
        sb.d["pid"] = sb.beat.prop or (a.holding or "")
        _face_toward(a, tgt.x)
        sb.d["gap"] = abs(tgt.x - a.x)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        tgt = rt.actors[sb.d["tgt"]]
        rt.set_pose(a.id, ra=(_lerp(52, 88, _ease(_clamp(u / 0.4, 0, 1))), _lerp(100, 90, _ease(_clamp(u / 0.4, 0, 1)))))
        rt.set_pose(tgt.id, ra=(88, 92))
        p = rt.props.get(sb.d["pid"])
        if u >= 0.55 and p is not None and p.mode == "held" and p.holder == a.id:
            p.holder, a.holding, tgt.holding = tgt.id, None, p.id


class Grab(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        tgt = rt.actors.get(sb.beat.target.upper()) or rt.other(a.id)
        sb.d["tgt"] = tgt.id

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        tgt = rt.actors[sb.d["tgt"]]
        _approach(rt, sb, a, tgt.x, 200.0, u, 0.3)
        _face_toward(a, tgt.x)
        ctx = {"hit_t": 0.5, "land_t": 0.9}
        rt.pose_full[a.id] = POSES["grab"](t, ctx)
        rt.pose_full[tgt.id] = POSES["grabbed"](t, ctx)
        rt.tag(tgt.id, "sweat")


class Strike(Verb):
    """punch / kick / push 공통: 접근 → 타격 모션 → 피격(밀림+버스트+셰이크)."""
    kind = "punch"
    knock = 130.0
    text = "퍽!"
    shake = 24.0

    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        tgt = rt.actors.get(sb.beat.target.upper()) or rt.other(a.id)
        sb.d["tgt"] = tgt.id
        _face_toward(a, tgt.x)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        tgt = rt.actors[sb.d["tgt"]]
        _approach(rt, sb, a, tgt.x, 230.0, u, 0.3)
        a.facing = _sgn(tgt.x - a.x) or a.facing
        if u < 0.45:
            k = _ease(u / 0.45)
            arm = dict(ra=(_lerp(40, -75, k), _lerp(110, -35, k)))
            leg = dict(rl=(_lerp(-5, -40, k), _lerp(3, -20, k))) if self.kind == "kick" else {}
            rt.set_pose(a.id, lean=_lerp(2, -12, k), brow=True, eyes="angry", mouth="teeth", **({} if self.kind == "kick" else arm), **leg)
        else:
            k = _ease(_clamp((u - 0.45) / 0.14, 0, 1))
            arm = dict(ra=(_lerp(-75, 88, k), _lerp(-35, 92, k)))
            leg = dict(rl=(_lerp(-40, 96, k), _lerp(-20, 92, k))) if self.kind == "kick" else {}
            rt.set_pose(a.id, lean=_lerp(-12, 26, k), brow=True, eyes="angry", mouth="teeth", **({} if self.kind == "kick" else arm), **leg)
            if not sb.d.get("hit") and u >= 0.58:
                sb.d["hit"] = True
                hx, hy = rt.point_of(tgt.id)
                rt.add_fx("burst", hx - tgt.facing * 20, hy - 30, self.text)
                rt.impulse_shake(self.shake)
                rt.flinch(tgt.id, 0.7)
                sb.d.update(kx0=tgt.x, kt0=t)
        if sb.d.get("hit"):
            k = _ease(_clamp((t - sb.d["kt0"]) / 0.35, 0, 1))
            tgt.x = sb.d["kx0"] + _sgn(tgt.x - a.x) * self.knock * k


class Punch(Strike):
    pass


class Kick(Strike):
    kind = "kick"
    knock = 150.0


class Push(Strike):
    kind = "push"
    knock = 190.0
    text = "퍽!"
    shake = 16.0

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        tgt = rt.actors[sb.d["tgt"]]
        _approach(rt, sb, a, tgt.x, 200.0, u, 0.3)
        rt.set_pose(a.id, lean=_lerp(2, 26, _ease(u)), ra=(90, 94), la=(90, 94), brow=True, eyes="angry", mouth="teeth")
        if not sb.d.get("hit") and u >= 0.5:
            sb.d["hit"] = True
            rt.add_fx("burst", tgt.x, FLOOR_Y - 330, self.text)
            rt.impulse_shake(self.shake)
            rt.flinch(tgt.id, 0.7)
            sb.d.update(kx0=tgt.x, kt0=t)
        if sb.d.get("hit"):
            k = _ease(_clamp((t - sb.d["kt0"]) / 0.4, 0, 1))
            tgt.x = sb.d["kx0"] + _sgn(tgt.x - a.x) * self.knock * k


class Knockback(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        sb.d.update(x0=a.x, x1=_lying_x(a.x - a.facing * 300, a.facing))  # 쓰러진 몸이 화면 밖으로 잘리지 않게
        a.down_target = 1.0

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], u)
        a.air = 170.0 * math.sin(math.pi * u) if u < 1 else 0.0
        rt.set_pose(a.id, eyes="X" if u > 0.5 else "wide", mouth="O")
        if u > 0.9 and not sb.d.get("land"):
            sb.d["land"] = True
            rt.impulse_shake(18.0)
            rt.add_fx("burst", a.x, FLOOR_Y - 60, "쿵!")

    def end(self, rt, sb, t):
        a = _actor(rt, sb)
        a.x, a.air, a.face = sb.d["x1"], 0.0, "X"


class Fall(Verb):
    faint = False

    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        sb.d.update(x0=a.x, x1=_lying_x(a.x - a.facing * 90, a.facing))
        a.down_target = 1.0
        if self.faint:
            a.face = "X"

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], u)
        a.air = 110.0 * math.sin(math.pi * u) if u < 1 else 0.0
        rt.set_pose(a.id, eyes="X" if (self.faint or u > 0.6) else "wide", mouth="O")
        if u > 0.85 and not sb.d.get("land"):
            sb.d["land"] = True
            rt.impulse_shake(14.0)

    def end(self, rt, sb, t):
        a = _actor(rt, sb)
        a.x, a.air = sb.d["x1"], 0.0
        if self.faint:
            a.face = "X"


class Faint(Fall):
    faint = True


class Dodge(Verb):
    def start(self, rt, sb, t):
        a = _actor(rt, sb)
        sb.d.update(x0=a.x, x1=a.x + _sgn(a.x - rt.other(a.id).x) * 140)

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        _move(rt, a, sb.d["x0"], sb.d["x1"], _clamp(u / 0.6, 0, 1))
        rt.set_pose(a.id, lean=-16, eyes="wide", mouth="O")


class Express(Verb):
    """shock / cower / smirk / rage / shout: 기존 표정 포즈 라이브러리를 이동과 무관하게 덧입힌다."""
    pose_name = "shock"

    def step(self, rt, sb, u, t):
        a = _actor(rt, sb)
        ctx = {"hit_t": 0.5, "land_t": 0.9}
        pose = POSES[self.pose_name](t, ctx)
        pose = {k: v for k, v in pose.items() if k not in ("xoff", "rot", "air")}
        if a.holding:
            pose.pop("ra", None)
        rt.pose_full[a.id] = pose
        if self.pose_name in ("shock", "cower"):
            rt.tag(a.id, "sweat")
        if self.pose_name == "angry":
            rt.tag(a.id, "anger")


class Shock(Express):
    pose_name = "shock"


class Cower(Express):
    pose_name = "cower"


class Smirk(Express):
    pose_name = "smirk"


class Rage(Express):
    pose_name = "angry"


class Shout(Express):
    pose_name = "talk"


class MirrorFallback(Shout):
    pass


VERBS: Dict[str, Verb] = {
    "enter": Enter(), "exit": Exit(), "walk_to": Walk(), "run_to": Walk(), "turn_to": TurnTo(),
    "idle": Idle(), "cross_arms": CrossArms(), "sit": Sit(), "stand": Stand(), "lean_on": LeanOn(), "point_at": PointAt(),
    "pick_up": PickUp(), "put_down": PutDown(), "hold_up": HoldUp(), "throw": Throw(), "slide": Slide(), "slam": Slam(),
    "catch": Catch(), "open": Open(), "hand_over": HandOver(),
    "grab": Grab(), "push": Push(), "punch": Punch(), "kick": Kick(),
    "knockback": Knockback(), "fall": Fall(), "dodge": Dodge(), "faint": Faint(),
    "shock": Shock(), "cower": Cower(), "smirk": Smirk(), "rage": Rage(), "shout": Shout(), "mirror": MirrorFallback(),
}
assert set(VERBS) == set(VERB_DOC), set(VERB_DOC) ^ set(VERBS)


# --------------------------------------------------------------------------- 그리기
def _paste_anchored(img: Image.Image, sprite: Image.Image, anchor: Tuple[float, float], at: Tuple[float, float], rot: float = 0.0) -> None:
    """스프라이트의 anchor 지점이 월드 at 에 오도록 (rot 도 anchor 중심으로) 붙인다."""
    if abs(rot) > 0.5:
        w, h = sprite.size
        R = int(math.hypot(w, h)) + 4
        canvas = Image.new("RGBA", (2 * R, 2 * R), (0, 0, 0, 0))
        canvas.paste(sprite, (int(R - anchor[0]), int(R - anchor[1])), sprite)
        canvas = canvas.rotate(-rot, resample=Image.BICUBIC)
        img.paste(canvas, (int(at[0] - R), int(at[1] - R)), canvas)
    else:
        img.paste(sprite, (int(at[0] - anchor[0]), int(at[1] - anchor[1])), sprite)


class StageVisuals(SceneVisuals):
    def __init__(self, rt: StageRuntime, scene, idx: int, total: int, title: str, pose: str,
                 dur: float, hit_t: float, timeline: Optional[List[Dict]] = None) -> None:
        super().__init__(scene, idx, total, title, pose, dur, hit_t, timeline=timeline, episode_setting="stage_mode")
        self.rt = rt
        cp = next((c for c in rt.plan.cuts if c.scene_number == scene.scene_number), None) or \
            (rt.plan.cuts[idx] if idx < len(rt.plan.cuts) else CutPlan(scene_number=idx + 1, beats=[]))
        self.frames = rt.simulate_cut(cp, dur, self.timeline, hit_t)
        self.main_pose = self.foe_pose = "stage"

    def _draw_world(self, i: int, t: float) -> Tuple[Image.Image, Dict, Dict]:
        rt = self.rt
        fs = self.frames[min(i, len(self.frames) - 1)]
        img = rt.backdrop.copy()
        d = ImageDraw.Draw(img)

        def draw_piece(pid: str) -> None:
            pc = rt.pieces[pid]
            sp = rt.sprite(pc.spec)
            op = fs.pieces.get(pid, 0.0)
            pos = (int(pc.left), int(pc.top))
            if op > 0.02:
                hg = pc.anchors.get("hinge")
                left_hinge = (hg.x if hg else 0) < pc.spec.w / 2
                sw = max(4, int(sp.width * max(0.08, math.cos(math.radians(op * 82)))))
                img.paste(rt.doorway(pc, sp.size), pos)
                sq = sp.resize((sw, sp.height), Image.BILINEAR)
                img.paste(sq, (pos[0] if left_hinge else pos[0] + sp.width - sw, pos[1]), sq)
            else:
                img.paste(sp, pos, sp)

        def draw_prop(pr: Dict, figs: Dict) -> None:
            sp = rt.sprite(pr["spec"])
            g = pr["anchors"]["grip"]
            if pr["mode"] == "held" and pr["holder"]:
                fig = figs.get(pr["holder"])
                if fig is None:
                    return
                hand = fig["arms"][1][2]
                _paste_anchored(img, sp, (g.x, g.y), hand, pr["rot"])
            elif pr["mode"] == "rest":
                _paste_anchored(img, sp, (pr["spec"].w / 2, pr["spec"].h), (pr["x"], pr["y"]), 0)
            else:
                _paste_anchored(img, sp, (pr["spec"].w / 2, pr["spec"].h / 2), (pr["x"], pr["y"]), pr["rot"])

        figs = {aid: a["fig"] for aid, a in fs.actors.items()}
        for layer in ("back", "mid"):
            for pid, pc in rt.pieces.items():
                if pc.layer == layer:
                    draw_piece(pid)
        front_ids = [pid for pid, pc in rt.pieces.items() if pc.layer == "front"]
        for pr in fs.props:
            if pr["mode"] == "rest" and (pr["surface"] is None or rt.pieces[pr["surface"]].layer != "front"):
                draw_prop(pr, figs)
        # 인물 (빌런 → 주인공 순)
        for aid, ctype, color, hfill in (("B", "B", (170, 30, 40), (255, 235, 235)), ("A", "A", (20, 20, 30), (255, 255, 255))):
            st = fs.actors[aid]
            if st["visible"]:
                draw_figure(d, st["fig"], st["pose"], color, hfill, char_type=ctype, t=t, img=img)
        for pid in front_ids:
            draw_piece(pid)
        for pr in fs.props:
            if pr["mode"] == "rest" and pr["surface"] and rt.pieces[pr["surface"]].layer == "front":
                draw_prop(pr, figs)
        for pr in fs.props:
            if pr["mode"] in ("held", "flying", "falling"):
                draw_prop(pr, figs)
        # 이펙트
        for aid, st in fs.actors.items():
            if not st["visible"]:
                continue
            hx, hy = st["fig"]["head"]
            R = st["fig"]["R"]
            if "sweat" in st["tags"]:
                fx_sweat(d, hx, hy, R, t)
            if "anger" in st["tags"]:
                fx_anger(d, hx + R * 0.95, hy - R * 0.95, t)
            if "stars" in st["tags"]:
                fx_stars(d, hx, hy, R, t)
        for e in fs.fx:
            k = e["age"] / e["dur"]
            if e["kind"] == "burst":
                fx_burst(d, e["x"], e["y"], 120 + 170 * k, e["text"], get_font(110))
        self.stage_shake = fs.shake
        # 카메라/말풍선이 쓰는 위치 갱신 (화면 밖 배우는 가장자리로 클램프)
        A, B = fs.actors["A"], fs.actors["B"]
        self.main_x, self.foe_x = _clamp(A["x"], 140, W - 140), _clamp(B["x"], 140, W - 140)
        self.main_pose = "talk" if A["visible"] else "smirk"
        self.foe_pose = "stage"

        def clamp_fig(fig: Dict) -> Dict:
            c = dict(fig)
            c["head"] = (_clamp(fig["head"][0], 120, W - 120), fig["head"][1])
            return c
        return img, clamp_fig(A["fig"]), clamp_fig(B["fig"])

    def frame(self, i: int) -> Image.Image:
        t = i / FPS
        img, main_fig, foe_fig = self._draw_world(i, t)
        # 발화 중 입 모양(기존 화자 타임라인 규약 재사용)
        return self._compose(img, i, t, main_fig, foe_fig)
