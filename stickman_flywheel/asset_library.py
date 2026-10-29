"""자산 해결층(열린 명사): '조회 → LLM 벡터 스펙 생성 → 검증 → 영구 캐시 → 실패 시 라벨 폴백'.

자산 = 도형 조합 스펙(SVG 유사 벡터) + 앵커 메타데이터(grip / surface / hit_zone / hinge / floor).
도형과 앵커가 같은 좌표계(무대 px, 좌상단 원점)에서 한 번에 생성되므로 어긋날 수 없고,
저장 전에 래스터화해 앵커가 실제 그려진 픽셀 위에 있는지 검증한다.
"""
from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from director_ir import AssetRef, StagePlan, slug
from fs_utils import atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
ASSETS_CACHE = BASE_DIR / "assets_cache"
STYLE_VERSION = "v1"
STAGE_W, STAGE_H, STAGE_FLOOR_Y = 1080, 1920, 1330
ACTOR_HEIGHT_PX = 590
ANCHOR_NAMES = ("grip", "surface", "hit_zone", "hinge", "floor")
SHAPE_TYPES = ("rect", "rrect", "ellipse", "polygon", "line", "text")
OUTLINE_DEFAULT = "#0f0f19"


# --------------------------------------------------------------------------- 스키마
class Shape(BaseModel):
    type: str = Field(description="rect | rrect | ellipse | polygon | line | text")
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    r: float = Field(default=0.0, description="rrect 모서리 반경")
    pts: List[float] = Field(default_factory=list, description="polygon/line 꼭짓점 [x1,y1,x2,y2,...]")
    fill: str = Field(default="", description="#RRGGBB. 빈 문자열=채우지 않음")
    fill2: str = Field(default="", description="지정 시 fill→fill2 세로 그라데이션(툰 셰이딩)")
    stroke: str = OUTLINE_DEFAULT
    stroke_w: float = Field(default=4.0, description="0=외곽선 없음. line 은 선 두께")
    text: str = ""
    size: float = Field(default=0.0, description="text 글자 크기(px). text 의 x,y 는 글자 중심")


class Anchor(BaseModel):
    name: str = Field(description="grip | surface | hit_zone | hinge | floor")
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0


class AssetSpec(BaseModel):
    name: str
    kind: str = "prop"
    w: int
    h: int
    shapes: List[Shape] = Field(default_factory=list)
    anchors: List[Anchor] = Field(default_factory=list)

    def anchor(self, name: str) -> Optional[Anchor]:
        for a in self.anchors:
            if a.name == name:
                return a
        return None


# --------------------------------------------------------------------------- 래스터라이저
def _hex(c: str) -> Optional[Tuple[int, int, int]]:
    c = (c or "").strip().lstrip("#")
    try:
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) != 6:
            return None
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def _bbox(sh: Shape) -> Tuple[float, float, float, float]:
    if sh.type in ("polygon", "line") and len(sh.pts) >= 2:
        xs, ys = sh.pts[0::2], sh.pts[1::2]
        return min(xs), min(ys), max(xs), max(ys)
    if sh.type == "text":
        s = max(sh.size, 8) * max(len(sh.text), 1) * 0.7
        return sh.x - s / 2, sh.y - sh.size, sh.x + s / 2, sh.y + sh.size
    return sh.x, sh.y, sh.x + sh.w, sh.y + sh.h


def render_spec(spec: AssetSpec, scale: float = 1.0, ss: int = 2) -> Image.Image:
    """AssetSpec → RGBA 스프라이트 (slim: 도형별 bbox 크롭 마스크 + 슈퍼샘플링 안티앨리어싱)."""
    k = scale * ss
    Wp, Hp = max(2, int(math.ceil(spec.w * k))), max(2, int(math.ceil(spec.h * k)))
    layer = Image.new("RGBA", (Wp, Hp), (0, 0, 0, 0))
    for sh in spec.shapes:
        if sh.type not in SHAPE_TYPES:
            continue
        x0, y0, x1, y1 = _bbox(sh)
        pad = (sh.stroke_w + 2) * 0.5
        bx0, by0 = int(max(0, math.floor((x0 - pad) * k))), int(max(0, math.floor((y0 - pad) * k)))
        bx1, by1 = int(min(Wp, math.ceil((x1 + pad) * k))), int(min(Hp, math.ceil((y1 + pad) * k)))
        if bx1 - bx0 < 1 or by1 - by0 < 1:
            continue
        bw, bh = bx1 - bx0, by1 - by0
        fill = _hex(sh.fill)
        stroke = _hex(sh.stroke) or _hex(OUTLINE_DEFAULT)

        def loc(v: float, off: int) -> float:
            return v * k - off

        if sh.type == "text":
            if not sh.text or not fill:
                continue
            from module3_renderer import get_font
            d = ImageDraw.Draw(layer)
            d.text((sh.x * k, sh.y * k), sh.text, font=get_font(max(8, int(sh.size * k))), fill=fill + (255,), anchor="mm")
            continue

        fm = Image.new("L", (bw, bh), 0)   # 채움 마스크
        om = Image.new("L", (bw, bh), 0)   # 외곽선 마스크
        fd, od = ImageDraw.Draw(fm), ImageDraw.Draw(om)
        lw = max(0, int(round(sh.stroke_w * k)))
        if sh.type == "line":
            pts = [(loc(sh.pts[i], bx0), loc(sh.pts[i + 1], by0)) for i in range(0, len(sh.pts) - 1, 2)]
            if len(pts) < 2:
                continue
            w_ = max(1, int(round(max(sh.stroke_w, 1) * k)))
            od.line(pts, fill=255, width=w_, joint="curve")
            for q in (pts[0], pts[-1]):
                od.ellipse((q[0] - w_ / 2, q[1] - w_ / 2, q[0] + w_ / 2, q[1] + w_ / 2), fill=255)
            col = fill or stroke
            layer.paste(Image.new("RGBA", (bw, bh), col + (255,)), (bx0, by0), om)
            continue
        if sh.type == "polygon":
            pts = [(loc(sh.pts[i], bx0), loc(sh.pts[i + 1], by0)) for i in range(0, len(sh.pts) - 1, 2)]
            if len(pts) < 3:
                continue
            fd.polygon(pts, fill=255)
            if lw:
                od.line(pts + [pts[0]], fill=255, width=lw, joint="curve")
        else:
            box = (loc(sh.x, bx0), loc(sh.y, by0), loc(sh.x + sh.w, bx0), loc(sh.y + sh.h, by0))
            if sh.type == "rect":
                fd.rectangle(box, fill=255)
                if lw:
                    od.rectangle(box, outline=255, width=lw)
            elif sh.type == "rrect":
                rad = max(0.0, sh.r * k)
                fd.rounded_rectangle(box, radius=rad, fill=255)
                if lw:
                    od.rounded_rectangle(box, radius=rad, outline=255, width=lw)
            else:  # ellipse
                fd.ellipse(box, fill=255)
                if lw:
                    od.ellipse(box, outline=255, width=lw)
        if fill:
            fill2 = _hex(sh.fill2)
            if fill2:
                t = np.linspace(0.0, 1.0, bh, dtype=np.float32)[:, None, None]
                arr = (np.array(fill, np.float32) * (1 - t) + np.array(fill2, np.float32) * t)
                arr = np.broadcast_to(arr, (bh, bw, 3)).astype(np.uint8)
                src = Image.fromarray(arr, "RGB").convert("RGBA")
            else:
                src = Image.new("RGBA", (bw, bh), fill + (255,))
            layer.paste(src, (bx0, by0), fm)
        if lw:
            layer.paste(Image.new("RGBA", (bw, bh), stroke + (255,)), (bx0, by0), om)
    if ss > 1:
        layer = layer.resize((max(1, int(math.ceil(spec.w * scale))), max(1, int(math.ceil(spec.h * scale)))), Image.LANCZOS)
    return layer


# --------------------------------------------------------------------------- 검증
def _near_opaque(alpha: np.ndarray, x: float, y: float, r: int) -> bool:
    h, w = alpha.shape
    xi, yi = int(round(x)), int(round(y))
    sub = alpha[max(0, yi - r):min(h, yi + r + 1), max(0, xi - r):min(w, xi + r + 1)]
    return bool(sub.size and (sub > 128).any())


def validate_spec(spec: AssetSpec, ref: AssetRef) -> List[str]:
    """LLM 교정 지시에 그대로 쓸 수 있는 오류 문장 목록. 비어 있으면 통과."""
    errs: List[str] = []
    if spec.kind == "backdrop":
        spec.w, spec.h = STAGE_W, STAGE_H
    if not (30 <= spec.w <= 1200 and 30 <= spec.h <= 1950):
        errs.append(f"캔버스 크기 {spec.w}x{spec.h} 가 범위를 벗어남 (props 30~700, 배경 1080x1920)")
        return errs
    if not spec.shapes:
        errs.append("shapes 가 비어 있음")
        return errs
    if len(spec.shapes) > 160:
        spec.shapes = spec.shapes[:160]
    for i, sh in enumerate(spec.shapes):
        if sh.type not in SHAPE_TYPES:
            errs.append(f"shapes[{i}] 알 수 없는 type '{sh.type}'")
        elif sh.type in ("polygon", "line") and (len(sh.pts) % 2 or len(sh.pts) < (6 if sh.type == "polygon" else 4)):
            errs.append(f"shapes[{i}] {sh.type} 의 pts 개수가 부적절({len(sh.pts)})")
        for c in (sh.fill, sh.fill2):
            if c and _hex(c) is None:
                errs.append(f"shapes[{i}] 색상 '{c}' 는 #RRGGBB 형식이어야 함")
    if errs:
        return errs[:6]

    ss = 1
    alpha = np.asarray(render_spec(spec, 1.0, ss))[..., 3]
    cover = float((alpha > 128).mean())
    if spec.kind == "backdrop":
        if cover < 0.9:
            errs.append(f"배경은 캔버스 전체(1080x1920)를 빈틈없이 채워야 함 (현재 {cover:.0%}만 채워짐)")
        fl = spec.anchor("floor")
        if fl is None:
            errs.append("backdrop 에 floor 앵커(바닥선 y=1330)가 필요함")
        elif abs(fl.y - STAGE_FLOOR_Y) > 12:
            errs.append(f"floor 앵커 y={fl.y:.0f} 는 {STAGE_FLOOR_Y} 이어야 함(인물 발 위치)")
        return errs
    if cover < 0.04:
        errs.append("그려진 면적이 거의 없음 (색 채움 누락?)")
    for a in spec.anchors:
        if a.name not in ANCHOR_NAMES:
            errs.append(f"알 수 없는 앵커 '{a.name}'")
        elif not (-5 <= a.x <= spec.w + 5 and -5 <= a.y <= spec.h + 5):
            errs.append(f"앵커 {a.name}({a.x:.0f},{a.y:.0f}) 가 캔버스({spec.w}x{spec.h}) 밖")
    if errs:
        return errs[:6]
    grip = spec.anchor("grip")
    if spec.kind == "prop":
        if grip is None:
            errs.append("prop 에는 grip 앵커(손으로 쥐는 지점)가 필요함")
        elif not _near_opaque(alpha, grip.x, grip.y, 14):
            errs.append(f"grip({grip.x:.0f},{grip.y:.0f}) 가 그려진 도형 위가 아님 — 물체의 실제 손잡이/잡는 가장자리 위로 옮길 것")
    sf = spec.anchor("surface")
    if sf is not None:
        xs = np.linspace(sf.x + 0.1 * sf.w, sf.x + 0.9 * sf.w, 7)
        below = np.mean([_near_opaque(alpha, x, sf.y + 8, 3) for x in xs])
        above = np.mean([_near_opaque(alpha, x, sf.y - 8, 3) for x in xs])
        if below < 0.5:
            errs.append(f"surface(y={sf.y:.0f}) 아래에 물체가 없음 — 물건이 놓일 윗면 y 로 정확히 맞출 것")
        if above > 0.6:
            errs.append(f"surface(y={sf.y:.0f}) 위가 이미 막혀 있음 — 윗면의 가장 높은 y 여야 함")
    hg = spec.anchor("hinge")
    if hg is not None and not _near_opaque(alpha, hg.x, hg.y, 16):
        errs.append(f"hinge({hg.x:.0f},{hg.y:.0f}) 가 문짝 가장자리 위가 아님")
    return errs[:6]


def derive_default_anchors(spec: AssetSpec) -> Dict[str, Anchor]:
    """누락 앵커를 불투명 영역 bbox 로 보충 (LLM 이 생략해도 엔진이 동작하도록)."""
    a = np.asarray(render_spec(spec, 1.0, 1))[..., 3]
    ys, xs = np.where(a > 128)
    if len(xs) == 0:
        x0, y0, x1, y1 = 0, 0, spec.w, spec.h
    else:
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    out = {
        "surface": Anchor(name="surface", x=x0, y=y0, w=x1 - x0),
        "hit_zone": Anchor(name="hit_zone", x=x0, y=y0, w=x1 - x0, h=y1 - y0),
        "grip": Anchor(name="grip", x=(x0 + x1) / 2, y=(y0 + y1) / 2),
        "hinge": Anchor(name="hinge", x=x0, y=(y0 + y1) / 2, h=y1 - y0),
        "floor": Anchor(name="floor", y=STAGE_FLOOR_Y),
    }
    for an in spec.anchors:
        out[an.name] = an
    return out


# --------------------------------------------------------------------------- 폴백 아이콘
def fallback_spec(ref: AssetRef) -> AssetSpec:
    """LLM 생성 실패 시: 라벨이 붙은 범용 아이콘 (엔진이 동일 규약의 앵커로 그대로 다룬다)."""
    label = ref.label or ref.id
    if ref.kind == "backdrop":
        shapes = [
            Shape(type="rect", x=0, y=0, w=STAGE_W, h=STAGE_FLOOR_Y, fill="#cfd8e6", fill2="#aebbd0", stroke_w=0),
            Shape(type="rect", x=0, y=STAGE_FLOOR_Y, w=STAGE_W, h=STAGE_H - STAGE_FLOOR_Y, fill="#e6e9f0", stroke_w=0),
            Shape(type="rect", x=0, y=STAGE_FLOOR_Y, w=STAGE_W, h=14, fill="#586075", stroke_w=0),
            Shape(type="text", x=540, y=420, text=label, size=90, fill="#8794ad"),
        ]
        return AssetSpec(name=ref.id, kind="backdrop", w=STAGE_W, h=STAGE_H, shapes=shapes,
                         anchors=[Anchor(name="floor", y=STAGE_FLOOR_Y)])
    size = max(90, int(ref.height_px or 120))
    if ref.kind == "set_piece":
        w, h = int(size * 1.5), size
        shapes = [Shape(type="rrect", x=4, y=4, w=w - 8, h=h - 8, r=14, fill="#d7c2a0", fill2="#b79b72", stroke_w=5),
                  Shape(type="text", x=w / 2, y=h / 2, text=label, size=max(24, h // 6), fill="#3b2f20")]
        anchors = [Anchor(name="surface", x=4, y=4, w=w - 8), Anchor(name="hit_zone", x=4, y=4, w=w - 8, h=h - 8),
                   Anchor(name="hinge", x=4, y=h / 2, h=h - 8)]
    else:
        w, h = size, size
        shapes = [Shape(type="rrect", x=4, y=4, w=w - 8, h=h - 8, r=18, fill="#ffe08a", fill2="#f2b73c", stroke_w=5),
                  Shape(type="text", x=w / 2, y=h / 2, text=label, size=max(20, h // 4), fill="#4a3410")]
        anchors = [Anchor(name="grip", x=w / 2, y=h / 2), Anchor(name="hit_zone", x=4, y=4, w=w - 8, h=h - 8)]
    return AssetSpec(name=ref.id, kind=ref.kind, w=w, h=h, shapes=shapes, anchors=anchors)


# --------------------------------------------------------------------------- LLM 생성
STYLE_GUIDE = """너는 스틱맨 숏폼용 '벡터 자산 디자이너'다. 요청된 사물/장소를 도형 조합 스펙(JSON)으로 그린다.

[좌표계] 픽셀 단위, 좌상단 원점, x→오른쪽, y→아래. 캔버스는 w×h. 모든 도형과 앵커는 같은 좌표계에 둔다.
[기준 크기] 무대 1080x1920, 바닥선 y=1330. 서 있는 인물 키=590px(손 높이 약 y=1070). 문=높이 760, 카운터/테이블=높이 260~300,
  의자=높이 300, 신용카드=가로 130, 숟가락/포크=길이 150, 접시=지름 170, 컵=높이 130. 작은 소품은 만화처럼 약간 크게 그린다.
[화풍] 깔끔한 플랫 벡터 만화. 물체마다 2~3톤 색(fill, 필요하면 fill2 로 위→아래 그라데이션) + 밝은 하이라이트 도형 1~2개.
  외곽선은 기본 굵은 어두운 선(stroke_w 4~6). 외곽선이 필요 없는 도형만 stroke_w=0. 색은 반드시 #RRGGBB.
[도형] type: rect(x,y,w,h) | rrect(x,y,w,h,r) | ellipse(x,y,w,h = 외접 사각형) | polygon(pts=[x1,y1,...]) |
  line(pts, stroke_w=선 두께, fill=선 색) | text(x,y=글자 중심, text, size, fill). 그리는 순서가 곧 겹침 순서(뒤→앞). 도형은 60개 이하.
[앵커 — 도형과 정확히 일치해야 함]
  grip    : 손으로 쥐는 지점(손잡이/잡는 가장자리). 반드시 그려진 도형 위. (prop 필수)
  hit_zone: 충격을 받는 영역 x,y,w,h (보통 물체 몸통).
  surface : 물건이 놓이는 윗면. x=왼쪽 끝, y=윗면의 가장 높은 y, w=폭. 그 아래는 물체, 위는 비어 있어야 함. (테이블/카운터/선반)
  hinge   : 문의 경첩 쪽 세로 모서리 위의 점 x,y 와 h=문 높이. (문)
  floor   : 배경 전용. y=1330.
[prop]  캔버스는 물체에 꼭 맞게(여백 6~10px). 세워 두는 물체는 밑변이 캔버스 바닥.
[set_piece] 캔버스는 물체에 꼭 맞게, 밑변이 캔버스 바닥(바닥 접지). 테이블/카운터는 반드시 surface 앵커. 문은 hinge 앵커.
[backdrop] w=1080,h=1920 전체를 빈틈없이 채운다: 위쪽(0~1330)은 벽/실내 풍경과 천장·조명·진열대 등 장소의 정체성을 드러내는 디테일,
  1330 아래는 바닥. 중앙(x 250~830, y 700~1330)은 인물이 서는 자리이므로 시각적으로 단순하게 비워 둘 것(큰 가구 금지).
  floor 앵커 y=1330 필수. 색은 인물(흰 머리/검은 후디, 주황/빨강 옷)과 겹쳐도 대비되도록 중채도 파스텔~중간 톤.
"""

EXAMPLE = """[작성 예시 — 머그컵(prop), 캔버스 120x120]
{"name":"mug","kind":"prop","w":120,"h":120,"shapes":[
 {"type":"rrect","x":14,"y":22,"w":78,"h":88,"r":14,"fill":"#ff7a59","fill2":"#e2553a","stroke_w":5},
 {"type":"ellipse","x":16,"y":24,"w":74,"h":20,"fill":"#5b3a29","stroke_w":4},
 {"type":"rrect","x":84,"y":42,"w":28,"h":48,"r":14,"fill":"","stroke_w":9},
 {"type":"rrect","x":24,"y":50,"w":10,"h":44,"r":5,"fill":"#ffc2a8","stroke_w":0}],
 "anchors":[{"name":"grip","x":104,"y":66},{"name":"hit_zone","x":14,"y":22,"w":78,"h":88}]}"""


class AssetGenerationError(RuntimeError):
    pass


class AssetLibrary:
    def __init__(self, root: Path = ASSETS_CACHE, log: Callable[[str], None] = print) -> None:
        self.root = Path(root)
        self.log = log
        self.stats: Dict[str, List[str]] = {"cache_hit": [], "generated": [], "fallback": []}
        self._lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._client = None
        self._mem: Dict[str, AssetSpec] = {}

    # ---- 캐시
    def _path(self, ref: AssetRef) -> Path:
        return self.root / ref.kind / f"{slug(ref.id)}.json"

    _GENERIC = {"the", "a", "of", "and", "interior", "inside"}

    def _tokens(self, name: str) -> set:
        return {t for t in slug(name).split("_") if t and t not in self._GENERIC}

    def _find_similar(self, ref: AssetRef) -> Optional[Path]:
        """같은 kind 의 캐시에서 이름 토큰이 포함 관계인 자산을 재사용 (예: blue_credit_card ↔ credit_card)."""
        d = self.root / ref.kind
        if not d.exists():
            return None
        want = self._tokens(ref.id)
        best, best_score = None, 0.0
        for f in d.glob("*.json"):
            have = self._tokens(f.stem)
            if not have or not want:
                continue
            inter = len(want & have)
            if inter == 0 or not (want <= have or have <= want):
                continue
            score = inter / len(want | have)
            if score > best_score:
                best, best_score = f, score
        return best if best_score >= 0.5 else None

    def _load_cached(self, ref: AssetRef) -> Optional[AssetSpec]:
        p = self._path(ref)
        if not p.exists():
            p = self._find_similar(ref)
            if p is None:
                return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("style_version") != STYLE_VERSION:
                return None
            return AssetSpec.model_validate(data["spec"])
        except Exception:
            return None

    def _store(self, ref: AssetRef, spec: AssetSpec) -> None:
        atomic_write_json(self._path(ref), {"style_version": STYLE_VERSION, "ref": ref.model_dump(), "spec": spec.model_dump()})

    # ---- LLM
    def _gemini(self):
        with self._client_lock:  # 스레드가 동시에 클라이언트를 만들다 한쪽이 GC 로 닫히는 경합 방지
            if self._client is None:
                from google import genai
                key = os.getenv("GEMINI_API_KEY", "").strip()
                if not key:
                    raise AssetGenerationError("GEMINI_API_KEY 없음")
                self._client = genai.Client(api_key=key)
            return self._client

    def _generate_once(self, ref: AssetRef, location_hint: str, errors: List[str], model: str) -> AssetSpec:
        from google.genai import types
        if ref.kind == "backdrop":
            size = "캔버스 w=1080, h=1920 (고정)."
        else:
            size = f"무대에서의 높이 약 {ref.height_px}px (캔버스 높이를 이에 맞추고 가로는 비율에 맞게)."
        prompt = (f"{EXAMPLE}\n\n[요청]\nkind={ref.kind}\nid={ref.id}\n이름={ref.label}\n외형={ref.description}\n"
                  f"{size}\n장소 맥락={location_hint}\n")
        if errors:
            prompt += "\n[직전 시도의 오류 — 반드시 고쳐서 다시 작성]\n- " + "\n- ".join(errors) + "\n"
        resp = self._gemini().models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=STYLE_GUIDE, response_mime_type="application/json",
                                               response_schema=AssetSpec, temperature=0.6))
        spec = resp.parsed if isinstance(resp.parsed, AssetSpec) else AssetSpec.model_validate(json.loads(resp.text))
        spec.name, spec.kind = ref.id, ref.kind
        return spec

    def _generate(self, ref: AssetRef, location_hint: str) -> AssetSpec:
        models = [os.getenv("ASSET_MODEL", "gemini-3.1-pro-preview"), os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-2.5-pro"]
        errors: List[str] = []
        last: Optional[Exception] = None
        for attempt, model in enumerate([models[0], models[0], models[1], models[2]]):
            try:
                spec = self._generate_once(ref, location_hint, errors, model)
                errors = validate_spec(spec, ref)
                if not errors:
                    return spec
                self.log(f"[Asset] {ref.kind}:{ref.id} 검증 실패(시도 {attempt + 1}, {model}): {errors[0]}")
            except Exception as e:
                last = e
                errors = [f"직전 응답 처리 실패: {type(e).__name__}"]
                self.log(f"[Asset] {ref.kind}:{ref.id} 생성 오류(시도 {attempt + 1}, {model}): {type(e).__name__}: {str(e)[:160]}")
        raise AssetGenerationError(f"{ref.id}: {errors} / {last}")

    # ---- 공개 API
    def get(self, ref: AssetRef, location_hint: str = "") -> AssetSpec:
        key = f"{ref.kind}:{slug(ref.id)}"
        with self._lock:
            if key in self._mem:
                return self._mem[key]
        spec = self._load_cached(ref)
        how = "cache_hit"
        if spec is None:
            try:
                spec = self._generate(ref, location_hint)
                self._store(ref, spec)
                how = "generated"
            except Exception as e:
                self.log(f"[Asset] {key} → 폴백 아이콘 사용: {str(e)[:200]}")
                spec, how = fallback_spec(ref), "fallback"
        with self._lock:
            self._mem[key] = spec
            self.stats[how].append(key)
        return spec

    def resolve_plan(self, plan: StagePlan, workers: int = 8) -> Dict[str, AssetSpec]:
        """플랜의 모든 명사(배경/세트/소품)를 병렬 해결. 키: '<kind>:<id>'."""
        hint = f"{plan.location.label} ({plan.location.backdrop.description})"
        refs: Dict[str, AssetRef] = {}
        for r in [plan.location.backdrop] + [s.asset for s in plan.set_pieces] + [p.asset for p in plan.props]:
            refs.setdefault(f"{r.kind}:{slug(r.id)}", r)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {k: ex.submit(self.get, r, hint) for k, r in refs.items()}
            return {k: f.result() for k, f in futs.items()}
