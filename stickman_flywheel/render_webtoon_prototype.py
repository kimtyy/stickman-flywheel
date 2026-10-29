"""웹툰형(인스타툰) 쇼츠 프로토타입: 이미지 생성 모델 6컷 + 켄 번스 모션 + 자막/말풍선 합성.

기존 렌더러/스테이지 코드는 건드리지 않는 독립 스크립트다. 오디오는 audio_timeline 의 확정 타임라인을 그대로 쓴다.

  python render_webtoon_prototype.py <episode_id> [--redo-sheets] [--redo-cuts] [--suffix _webtoon]

산출물: pipeline_outputs/webtoon/<episode_id>/{sheet_A.png, sheet_B.png, shots.json, cut_1..6.png, consistency.json}
        pipeline_outputs/rendered/<episode_id>_webtoon.mp4
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from PIL import Image, ImageDraw  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from fs_utils import atomic_write_json  # noqa: E402

OUT_ROOT = BASE_DIR / "pipeline_outputs" / "webtoon"
W, H, FPS = 1080, 1920, 30

IMAGE_MODELS = [os.getenv("WEBTOON_IMAGE_MODEL", "gemini-3.1-flash-image"), "gemini-2.5-flash-image"]
TEXT_MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.8-flash"), "gemini-2.5-pro"]

# --------------------------------------------------------------------------- 고정 캐릭터/화풍 (모든 호출에 동일하게 삽입)
STYLE = ("Korean webtoon / Instagram-toon style illustration, thick clean black ink outlines, flat cel shading with soft gradient "
         "highlights, saturated but tasteful palette, expressive simplified faces, vertical 9:16 composition, "
         "comedic cartoon tone, no gore, no blood, no realistic violence. "
         "STRICT NEGATIVE RULES: NO text of any kind, NO letters, NO words, NO onomatopoeia / sound-effect lettering (no 'CRASH', 'CLICK', "
         "'BEEP', 'POW', 'CRISP', 'SLAP' etc.), NO Korean or English captions, NO speech bubbles, NO watermarks, NO logos. "
         "Show impact ONLY with motion lines, star bursts and expressions, never with written words.")
ANCHOR_PATH = BASE_DIR / "assets" / "brand" / "character_a_anchor.png"   # 빼딱맨 원본 로고에서 캐릭터만 추출한 브랜드 원화
CHAR_A = ("CHARACTER A ('Ppaedakman', the channel mascot and protagonist): draw him EXACTLY as in the attached MASCOT ORIGINAL image "
          "(the first reference): a large perfectly round flat WHITE head with a bold black outline, heavy-lidded smug eyes, one angled "
          "sharp eyebrow, a long sly one-sided smirk, a plain flat WHITE slim body and thin limbs with bold black outlines (no clothing "
          "details, no glossy shading). Same face and same line style in every panel; only the expression and pose change.")
CHAR_B = ("CHARACTER B (the villain customer): a stocky stick-figure-like man with a large round PINK-WHITE head, a few spiky ORANGE "
          "hair tufts on top, thick angry eyebrows, wide smug mouth, wearing a bright RED jacket/outfit. Always the same face, same red outfit.")
SHEET_PROMPT = ("Character reference sheet on a plain light-grey background: the SAME character shown three times side by side "
                "(full-body front view, full-body 3/4 view, and a close-up of the face with a smug expression). {desc} {style}")
ANCHOR_SHEET_NOTE = ("The attached image is the ORIGINAL MASCOT artwork. Reproduce this exact character (same head shape, same face, "
                     "same eyes/eyebrow/smirk, same flat white body and line weight). ")

# --------------------------------------------------------------------------- 컷 계획 (LLM)
class Shot(BaseModel):
    scene_number: int
    image_prompt: str = Field(description="영어. 이 컷의 구도·행동·표정·소품을 구체적으로. 인물 외형은 쓰지 않는다(참조 시트가 고정).")
    characters: List[str] = Field(description="이 컷에 등장하는 인물: 'A', 'B' 중")
    motion: str = Field(description="zoom_in | zoom_out | pan_left | pan_right | push_in_shake")
    impact_word: str = Field(default="", description="타격/반전 순간에 크게 띄울 한글 한두 단어(없으면 빈 문자열)")


class ShotList(BaseModel):
    setting_bible: str = Field(description="영어. 장소의 고정 묘사(벽/조명/주요 가구 색·배치). 모든 컷이 같은 장소로 보이게 하는 문장")
    shots: List[Shot]


SHOT_SYSTEM = f"""너는 웹툰형 숏폼의 컷 연출가다. 6컷 대본을 받아 컷별 '이미지 생성 프롬프트'를 만든다.
인물 A(주인공), B(빌런)의 외형은 별도 참조 시트로 고정되어 있으니 외형·옷을 다시 묘사하지 말고, 구도/행동/표정/소품/카메라 각도만 쓴다.
같은 장소가 유지되도록 setting_bible 을 한 번 정하고 각 컷 프롬프트에서 그 장소 요소(색, 가구)를 일관되게 언급한다.
A 는 옷 없이 하얀 몸을 가진 브랜드 마스코트(빼딱맨 원본)다. 지문에 후드/주머니 같은 옷 언급이 있어도 옷을 그리게 하지 말고 손동작만 쓴다.
폭력은 코미디 만화 톤(별, 충격 효과, 과장)으로만 묘사하고 피/상처는 쓰지 않는다.
대본의 샷 타입(Wide/Over-the-shoulder/Extreme Close-up/Low Angle/Medium/High Angle)을 반영하되, 말풍선·글자는 그리지 않는다.
motion 은 컷 감정에 맞게: 긴장/결심=zoom_in, 전체 상황=zoom_out, 대치=pan, 타격 순간=push_in_shake.
캐릭터 스타일: {STYLE}"""


def _digest(script) -> str:
    lines = [f"제목: {script.episode_title}", f"장소/상황: {script.analysis.transplanted_setting}"]
    for s in script.scenes:
        lines.append(f"[컷 {s.scene_number}] shot={s.shot_type} | 지문: {s.stickman_action} | 대사: {(s.tts_narration or '(무음)').replace(chr(10), ' / ')} | 자막: {s.subtitle_text}")
    return "\n".join(lines)


def plan_shots(client, script) -> ShotList:
    from google.genai import types
    last: Optional[Exception] = None
    for m in [TEXT_MODELS[0], TEXT_MODELS[0], TEXT_MODELS[1]]:
        try:
            r = client.models.generate_content(
                model=m, contents=_digest(script),
                config=types.GenerateContentConfig(system_instruction=SHOT_SYSTEM, response_mime_type="application/json",
                                                   response_schema=ShotList, temperature=0.7))
            sl = r.parsed if isinstance(r.parsed, ShotList) else ShotList.model_validate(json.loads(r.text))
            if len(sl.shots) >= len(script.scenes):
                return sl
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[Webtoon] 컷 계획 {m} 실패: {type(e).__name__}: {str(e)[:160]}", flush=True)
    raise RuntimeError(f"컷 계획 실패: {last}")


# --------------------------------------------------------------------------- 이미지 생성
def gen_image(client, prompt: str, refs: List[Image.Image], tries: int = 4) -> Image.Image:
    from google.genai import types
    last = ""
    for attempt in range(tries):
        model = IMAGE_MODELS[0] if attempt < 3 else IMAGE_MODELS[1]
        try:
            cfg = types.GenerateContentConfig(response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="9:16"))
            r = client.models.generate_content(model=model, contents=[prompt, *refs], config=cfg)
            for cand in (r.candidates or []):
                for part in (cand.content.parts or []):
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
            last = f"이미지 없음 (finish={getattr(r.candidates[0], 'finish_reason', None) if r.candidates else 'no-candidates'})"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[Webtoon] 이미지 생성 {model} 시도 {attempt + 1}/{tries} 실패: {last}", flush=True)
        time.sleep(2 + attempt * 2)
    raise RuntimeError(f"이미지 생성 실패: {last}")


def make_sheets(client, out: Path, redo: bool) -> Dict[str, Image.Image]:
    """A 는 빼딱맨 원본(앵커)을 1순위 참조로 시트를 만든다. 반환 dict 의 'ANCHOR' 가 원본 이미지."""
    if not ANCHOR_PATH.exists():
        raise FileNotFoundError(f"빼딱맨 앵커 이미지가 없습니다: {ANCHOR_PATH}")
    anchor = Image.open(ANCHOR_PATH).convert("RGB")
    sheets: Dict[str, Image.Image] = {"ANCHOR": anchor}
    for cid, desc in (("A", CHAR_A), ("B", CHAR_B)):
        p = out / f"sheet_{cid}.png"
        if p.exists() and not redo:
            sheets[cid] = Image.open(p).convert("RGB")
            continue
        t = time.time()
        note = ANCHOR_SHEET_NOTE if cid == "A" else ""
        sheets[cid] = gen_image(client, note + SHEET_PROMPT.format(desc=desc, style=STYLE), [anchor] if cid == "A" else [])
        sheets[cid].save(p)
        print(f"[Webtoon] 캐릭터 시트 {cid} 생성 ({time.time() - t:.0f}s)", flush=True)
    return sheets


def make_cut(client, shot: Shot, bible: str, sheets: Dict[str, Image.Image], first: Optional[Image.Image]) -> Image.Image:
    refs: List[Image.Image] = []
    desc: List[str] = []
    notes: List[str] = []
    if "A" in shot.characters:        # 빼딱맨 원본 = 1순위 참조
        refs.append(sheets["ANCHOR"])
        notes.append("image %d = ORIGINAL MASCOT artwork of character A (highest priority: A's face, head and body must match it exactly)" % len(refs))
    for cid in shot.characters:
        if cid in sheets:
            refs.append(sheets[cid])
            notes.append(f"image {len(refs)} = reference sheet of character {cid}")
            desc.append(CHAR_A if cid == "A" else CHAR_B)
    ref_note = ("Attached references in order: " + "; ".join(notes) + ". Reproduce the characters' faces, heads, hair and outfits EXACTLY "
                "(same character identity in every panel). ") if refs else ""
    if first is not None:
        refs.append(first)
        ref_note += f"Image {len(refs)} (the last) is panel 1 of the same story: keep the same location, color palette and drawing style. "
    prompt = (f"{ref_note}{' '.join(desc)}\nLOCATION (keep consistent): {bible}\nPANEL: {shot.image_prompt}\n{STYLE}")
    return gen_image(client, prompt, refs)


# --------------------------------------------------------------------------- 컷 점검: 글자 아티팩트 / 정체성 / 피사체 박스
class Box(BaseModel):
    ymin: int = Field(description="0~1000 정규화 좌표")
    xmin: int
    ymax: int
    xmax: int


class CutCheck(BaseModel):
    has_text: bool = Field(description="배경 간판/상품 포장 글자를 제외하고, 화면 위에 그려진 효과음·영문 단어·자막·워터마크 글자가 있는가")
    text_boxes: List[Box] = Field(default_factory=list, description="위 아티팩트 글자 각각의 경계 상자")
    a_ok: Optional[bool] = Field(default=None, description="A 가 보이면 원본 마스코트와 같은 캐릭터인가(둥근 흰 머리, 무거운 눈꺼풀, 각진 눈썹, 비스듬한 스머크, 하얀 몸). 미등장이면 null")
    b_ok: Optional[bool] = Field(default=None, description="B 가 보이면 같은 캐릭터인가(분홍 머리, 빨간 옷). 미등장이면 null")
    subject_boxes: List[Box] = Field(default_factory=list, description="화면에 보이는 주요 인물(A/B) 각각의 머리~몸통 경계 상자")
    note: str = ""


CHECK_PROMPT = ("첫 세 이미지는 A 의 원본 마스코트(최우선 기준), A 참조 시트, B 참조 시트이고 마지막 이미지는 검사할 컷이다. 엄격하게 검사하라. "
                "A 는 원본 마스코트와 같은 얼굴(둥근 흰 머리, 무거운 눈꺼풀, 각진 눈썹, 길게 비스듬한 스머크)과 같은 하얀 몸이어야 한다. "
                "글자 아티팩트는 효과음(CRISP, CLICK, beep 등), 영문 단어, 캡션, 워터마크처럼 그림 위에 그려진 글자를 말한다. "
                "B 의 주황 머리카락은 이야기상 가발이 벗겨져 없을 수 있으므로 머리카락 유무는 무시하고 얼굴형·분홍 머리색·빨간 옷으로 판단한다. "
                "편의점 간판·상품 포장의 작은 배경 글자는 무시한다. 경계 상자는 [ymin,xmin,ymax,xmax] 를 0~1000 으로 정규화한다.")


def check_cut(client, sheets: Dict[str, Image.Image], im: Image.Image) -> Optional[CutCheck]:
    from google.genai import types
    for m in TEXT_MODELS:
        try:
            r = client.models.generate_content(model=m, contents=[CHECK_PROMPT, sheets["ANCHOR"], sheets["A"], sheets["B"], im],
                                               config=types.GenerateContentConfig(response_mime_type="application/json",
                                                                                  response_schema=CutCheck, temperature=0.0))
            return r.parsed if isinstance(r.parsed, CutCheck) else CutCheck.model_validate(json.loads(r.text))
        except Exception as e:  # noqa: BLE001
            print(f"[Webtoon] 컷 점검 {m} 실패: {type(e).__name__}: {str(e)[:120]}", flush=True)
    return None


def cover_text(im: Image.Image, boxes: List[Box]) -> Image.Image:
    """재생성 후에도 남은 글자 영역을 주변색으로 인페인팅한다(최후 보완책)."""
    import cv2
    import numpy as np
    arr = np.array(im.convert("RGB"))
    h, w = arr.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for b in boxes:
        pad = 12
        x0, x1 = int(b.xmin / 1000 * w) - pad, int(b.xmax / 1000 * w) + pad
        y0, y1 = int(b.ymin / 1000 * h) - pad, int(b.ymax / 1000 * h) + pad
        mask[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 255
    return Image.fromarray(cv2.inpaint(arr, mask, 9, cv2.INPAINT_TELEA))


def focus_from(check: Optional[CutCheck]):
    """피사체 경계 상자들의 합집합 → (x0,y0,x1,y1) 0~1. 없으면 None."""
    if not check or not check.subject_boxes:
        return None
    return (min(b.xmin for b in check.subject_boxes) / 1000, min(b.ymin for b in check.subject_boxes) / 1000,
            max(b.xmax for b in check.subject_boxes) / 1000, max(b.ymax for b in check.subject_boxes) / 1000)


# --------------------------------------------------------------------------- 합성 (켄 번스 + 오버레이)
def _ease(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> List[str]:
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch.lstrip()
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _outlined(d: ImageDraw.ImageDraw, xy, text, font, fill, stroke=(0, 0, 0), sw=6, anchor="mm") -> None:
    d.text(xy, text, font=font, fill=fill, stroke_width=sw, stroke_fill=stroke, anchor=anchor)


SAFE_MARGIN = 70   # 인물 경계 상자 바깥 여백(px). 줌/팬 창은 항상 [상자+여백]을 포함한다


class CutPainter:
    def __init__(self, img: Image.Image, shot: Shot, scene, tl: List[Dict], dur: float, hit_t: float, title: str,
                 focus=None) -> None:
        from module3_renderer import get_font
        self.get_font = get_font
        s = max(W / img.width, H / img.height)             # 화면을 정확히 덮는 크기(줌 1.0 = 전체 구도가 그대로 보임)
        self.base = img.resize((int(math.ceil(img.width * s)), int(math.ceil(img.height * s))), Image.LANCZOS)
        Wb, Hb = self.base.size
        self.focus = None
        self.z_cap = 1.3
        if focus:
            self.focus = (focus[0] * Wb, focus[1] * Hb, focus[2] * Wb, focus[3] * Hb)
            fw = self.focus[2] - self.focus[0] + 2 * SAFE_MARGIN
            fh = self.focus[3] - self.focus[1] + 2 * SAFE_MARGIN
            self.z_cap = max(1.0, min(W / fw, H / fh))      # 인물+여백이 창에 들어가는 최대 줌
        self.shot, self.scene, self.tl, self.dur, self.hit_t, self.title = shot, scene, tl, dur, hit_t, title
        self.n = max(1, int(round(dur * FPS)))
        rnd = random.Random(scene.scene_number)
        self.shake_phase = [rnd.random() * 6.28 for _ in range(4)]
        self.sub0 = max(0.0, min((x["start"] for x in tl), default=0.0) - 0.05)
        self.sub1 = min(dur, max((x["end"] for x in tl), default=dur) + 0.15)

    def _camera(self, t: float):
        """(x0, y0, vw, vh): 베이스 이미지 위의 잘라낼 창. 피사체 상자+여백이 항상 창 안에 있다."""
        Wb, Hb = self.base.size
        u = _ease(t / max(self.dur, 0.1))
        mv, cap = self.shot.motion, self.z_cap
        z0, z1, wx = 1.0, 1.0, 0.5       # wx: 창 중심이 피사체 중심 쪽으로 끌리는 정도(0~1)
        pan = 0.0
        if mv == "zoom_in":
            z1 = min(1.22, cap)
            wx = u
        elif mv == "zoom_out":
            z0, z1 = min(1.22, cap), 1.0
            wx = 1 - u
        elif mv in ("pan_left", "pan_right"):
            z0 = z1 = min(1.12, cap)
            pan = (-1 if mv == "pan_right" else 1) * (1 - 2 * u) * 0.8
        else:
            z1 = min(1.3, cap)
            wx = 1.0
        z = z0 + (z1 - z0) * u
        vw, vh = W / z, H / z
        if self.focus:
            fcx, fcy = (self.focus[0] + self.focus[2]) / 2, (self.focus[1] + self.focus[3]) / 2
        else:
            fcx, fcy = Wb / 2, Hb / 2
        cx = Wb / 2 + (fcx - Wb / 2) * wx + pan * (Wb - vw) / 2
        cy = Hb / 2 + (fcy - Hb / 2) * wx
        if self.focus:
            fx0, fy0, fx1, fy1 = self.focus
            lo_x, hi_x = fx1 + SAFE_MARGIN - vw / 2, fx0 - SAFE_MARGIN + vw / 2
            lo_y, hi_y = fy1 + SAFE_MARGIN - vh / 2, fy0 - SAFE_MARGIN + vh / 2
            cx = (lo_x + hi_x) / 2 if lo_x > hi_x else max(lo_x, min(hi_x, cx))
            cy = (lo_y + hi_y) / 2 if lo_y > hi_y else max(lo_y, min(hi_y, cy))
        sx = sy = 0.0
        if mv == "push_in_shake" or (0 <= t - self.hit_t < 0.45):
            k = max(0.0, 1.0 - (t - self.hit_t) / 0.45) if t >= self.hit_t else (0.15 if mv == "push_in_shake" else 0.0)
            amp = min(26.0, SAFE_MARGIN * 0.35) * k
            sx = amp * math.sin(t * 61 + self.shake_phase[0])
            sy = amp * math.cos(t * 53 + self.shake_phase[1])
        x0 = max(0.0, min(Wb - vw, cx + sx - vw / 2))
        y0 = max(0.0, min(Hb - vh, cy + sy - vh / 2))
        return x0, y0, vw, vh

    def frame(self, i: int) -> Image.Image:
        t = i / FPS
        x0, y0, vw, vh = self._camera(t)
        fr = self.base.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + vw, y0 + vh)).convert("RGBA")
        d = ImageDraw.Draw(fr)
        self._overlays(fr, d, t)
        return fr.convert("RGB")

    def _overlays(self, fr: Image.Image, d: ImageDraw.ImageDraw, t: float) -> None:
        gf = self.get_font
        # 상단 제목 바
        d.rounded_rectangle((40, 40, W - 40, 150), 22, fill=(14, 14, 22, 215), outline=(255, 214, 10), width=3)
        tf = gf(40)
        _outlined(d, (W // 2, 95), self.title[:22], tf, (255, 214, 10), sw=0)
        # 현재 화자 말풍선 / 해설 박스
        cur = next((x for x in self.tl if x["start"] - 0.05 <= t <= x["end"] + 0.08), None)
        if cur:
            spk = (cur.get("speaker") or "").strip().upper()
            font = gf(52)
            lines = _wrap(d, cur["text"], font, 760)[:4]
            lh = 66
            bw = max(d.textlength(ln, font=font) for ln in lines) + 70
            bh = lh * len(lines) + 46
            if spk in ("A", "B"):
                cx = 300 if spk == "A" else 780
                bx0 = max(30, min(W - 30 - bw, cx - bw / 2)); by0 = 230
                d.rounded_rectangle((bx0, by0, bx0 + bw, by0 + bh), 38, fill=(255, 255, 255, 245),
                                    outline=(15, 15, 25), width=6)
                tx = cx + (-40 if spk == "A" else 40)
                d.polygon([(tx - 26, by0 + bh - 4), (tx + 26, by0 + bh - 4), (tx + (-20 if spk == "A" else 20), by0 + bh + 62)],
                          fill=(255, 255, 255, 245), outline=(15, 15, 25))
                col = (15, 15, 25)
            else:
                bx0 = (W - bw) / 2; by0 = 230
                d.rounded_rectangle((bx0, by0, bx0 + bw, by0 + bh), 24, fill=(255, 241, 190, 245), outline=(130, 90, 10), width=5)
                col = (70, 40, 0)
            for k, ln in enumerate(lines):
                d.text((bx0 + bw / 2, by0 + 30 + lh * k + lh / 2), ln, font=font, fill=col, anchor="mm")
        # 임팩트 단어
        word = (self.shot.impact_word or "").strip()
        if word and self.hit_t <= t <= self.hit_t + 0.9:
            k = _ease((t - self.hit_t) / 0.18) if t - self.hit_t < 0.18 else 1.0
            fade = 1.0 if t - self.hit_t < 0.6 else max(0.0, 1 - (t - self.hit_t - 0.6) / 0.3)
            if fade > 0:
                size = int(150 * (0.6 + 0.5 * k))
                _outlined(d, (W // 2, 880), word, gf(size), (255, 235, 40) + (int(255 * fade),), stroke=(30, 0, 0), sw=12)
        # 하단 자막
        if self.sub0 <= t <= self.sub1 and (self.scene.subtitle_text or "").strip():
            sf = gf(76)
            lines = _wrap(d, self.scene.subtitle_text.strip(), sf, 940)[:2]
            for k, ln in enumerate(lines):
                _outlined(d, (W // 2, 1640 + 96 * k), ln, sf, (255, 255, 255), sw=9)


# --------------------------------------------------------------------------- 일관성 측정 (VLM 채점)
class CutScore(BaseModel):
    scene_number: int
    a_same_identity: Optional[bool] = Field(default=None, description="A 가 등장하면: 원본 마스코트와 같은 캐릭터인가. 미등장이면 null")
    b_same_identity: Optional[bool] = Field(default=None, description="B 가 등장하면: 참조 시트와 같은 캐릭터(분홍 머리, 주황 머리카락, 빨간 옷)인가. 미등장이면 null")
    style_match: bool = Field(description="컷 1과 같은 화풍(선 굵기, 채색, 톤)인가")
    location_match: bool = Field(description="컷 1과 같은 장소로 보이는가(편의점 색/구조)")
    text_artifacts: bool = Field(description="이미지에 깨진 글자/말풍선/로고가 그려져 있는가")
    note: str = ""


class ScoreList(BaseModel):
    scores: List[CutScore]


def score_consistency(client, sheets: Dict[str, Image.Image], cuts: List[Image.Image]) -> ScoreList:
    from google.genai import types
    parts: List = ["첫 세 이미지는 A 원본 마스코트, 캐릭터 A, B 참조 시트다. 이어지는 이미지는 컷 1~%d 순서다. 각 컷을 엄격하게 채점하라." % len(cuts), sheets["ANCHOR"], sheets["A"], sheets["B"]]
    for i, im in enumerate(cuts, 1):
        parts += [f"컷 {i}:", im]
    last = None
    for m in TEXT_MODELS:
        try:
            r = client.models.generate_content(model=m, contents=parts, config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=ScoreList, temperature=0.0))
            return r.parsed if isinstance(r.parsed, ScoreList) else ScoreList.model_validate(json.loads(r.text))
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"채점 실패: {last}")


# --------------------------------------------------------------------------- 메인
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_id")
    ap.add_argument("--redo-sheets", action="store_true")
    ap.add_argument("--redo-cuts", action="store_true")
    ap.add_argument("--suffix", default="_webtoon")
    args = ap.parse_args()

    from google import genai
    from audio_timeline import AudioPlan, build_audio_plan
    from module2_calendar_manager import APPROVED_DIR, PENDING_DIR, load_script
    from module3_renderer import RENDERED_DIR, find_ffmpeg

    sp = next(p for d in (APPROVED_DIR, PENDING_DIR) if (p := d / f"{args.episode_id}.json").exists())
    script = load_script(sp)
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "").strip())
    out = OUT_ROOT / script.episode_id
    out.mkdir(parents=True, exist_ok=True)

    audio = AudioPlan.load(script.episode_id) if AudioPlan.exists(script.episode_id) else build_audio_plan(script)
    t0 = time.time()
    sheets = make_sheets(client, out, args.redo_sheets)

    shots_p = out / "shots.json"
    if shots_p.exists() and not args.redo_cuts:
        sl = ShotList.model_validate_json(shots_p.read_text(encoding="utf-8"))
    else:
        sl = plan_shots(client, script)
        atomic_write_json(shots_p, sl.model_dump())
    shots = {s.scene_number: s for s in sl.shots}

    cuts: List[Image.Image] = []
    checks: Dict[int, Optional[CutCheck]] = {}
    report: Dict[int, Dict] = {}
    MAX_RETRY = 2

    def bad(c: Optional[CutCheck]) -> str:
        if c is None:
            return ""
        why = []
        if c.has_text:
            why.append("글자 아티팩트")
        if c.a_ok is False:
            why.append("A 정체성")
        if c.b_ok is False:
            why.append("B 정체성")
        return ",".join(why)

    for i, sc in enumerate(script.scenes):
        p = out / f"cut_{i + 1}.png"
        shot = shots.get(sc.scene_number) or sl.shots[i]
        if p.exists() and not args.redo_cuts:
            im = Image.open(p).convert("RGB")
        else:
            t = time.time()
            im = make_cut(client, shot, sl.setting_bible, sheets, cuts[0] if cuts else None)
            im.save(p)
            print(f"[Webtoon] 컷 {i + 1}/{len(script.scenes)} 생성 {im.size} ({time.time() - t:.0f}s) motion={shot.motion}", flush=True)
        chk = check_cut(client, sheets, im)
        rep = {"retries": 0, "covered": False, "first_issue": bad(chk)}
        retry = 0
        while bad(chk) and retry < MAX_RETRY:
            retry += 1
            print(f"[Webtoon] 컷 {i + 1} 문제({bad(chk)}) -> 재생성 {retry}/{MAX_RETRY}", flush=True)
            shot2 = shot.model_copy(update={"image_prompt": shot.image_prompt + (
                " (IMPORTANT: the previous attempt wrongly contained written text/onomatopoeia. Draw ZERO letters or words; "
                "express impact only with motion lines and star bursts. Keep the characters EXACTLY as in the reference sheets.)")})
            im.save(out / f"cut_{i + 1}_rejected{retry}.png")
            im = make_cut(client, shot2, sl.setting_bible, sheets, cuts[0] if cuts else None)
            chk = check_cut(client, sheets, im)
        rep["retries"] = retry
        if chk and chk.has_text and chk.text_boxes:       # 재생성으로도 남은 글자는 인페인팅으로 가린다
            im = cover_text(im, chk.text_boxes)
            rep["covered"] = True
            print(f"[Webtoon] 컷 {i + 1} 잔여 글자 {len(chk.text_boxes)}곳 인페인팅으로 가림", flush=True)
        rep["final_issue"] = "covered" if rep["covered"] else bad(chk)
        im.save(p)
        cuts.append(im)
        checks[i] = chk
        report[i + 1] = rep
        print(f"[Webtoon] 컷 {i + 1} 점검: {rep} subject={focus_from(chk)}", flush=True)
    atomic_write_json(out / "checks.json", {"report": report, "checks": {k + 1: (v.model_dump() if v else None) for k, v in checks.items()}})
    t_img = time.time() - t0

    # 합성 → mp4 (확정 마스터 오디오와 함께 한 번에 인코딩)
    ffmpeg = find_ffmpeg()
    dst = RENDERED_DIR / f"{script.episode_id}{args.suffix}.mp4"
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(audio.master_wav()), "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(dst)]
    t1 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for i, sc in enumerate(script.scenes):
        a = audio.scenes[i]
        cp = CutPainter(cuts[i], shots.get(sc.scene_number) or sl.shots[i], sc, a.timeline, a.dur, a.hit_t, script.episode_title,
                        focus=focus_from(checks.get(i)))
        for f in range(cp.n):
            proc.stdin.write(cp.frame(f).tobytes())
        print(f"[Webtoon] 컷 {i + 1} 합성 {cp.n}프레임", flush=True)
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg 인코딩 실패")
    print(f"[Webtoon] 인코딩 {time.time() - t1:.0f}s", flush=True)

    # 일관성 채점
    try:
        sc_list = score_consistency(client, sheets, cuts)
        atomic_write_json(out / "consistency.json", sc_list.model_dump())
        ok = 0
        for s in sc_list.scores:
            passed = (s.a_same_identity is not False and s.b_same_identity is not False and s.style_match
                      and s.location_match and not s.text_artifacts)
            ok += int(passed)
            print(f"[Score] 컷 {s.scene_number}: A={s.a_same_identity} B={s.b_same_identity} style={s.style_match} "
                  f"loc={s.location_match} text_artifact={s.text_artifacts} → {'PASS' if passed else 'FAIL'} | {s.note}", flush=True)
        print(f"[Score] VLM 자동 채점 합격 {ok}/{len(sc_list.scores)}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[Score] 채점 생략: {e}", flush=True)
    print(f"DONE {dst} (이미지 {t_img:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
