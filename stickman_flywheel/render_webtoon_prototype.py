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
         "Absolutely NO text, letters, captions, speech bubbles or logos anywhere in the image.")
CHAR_A = ("CHARACTER A ('Ppaedakman', the protagonist): a slim stick-figure-like man with a large glossy perfectly round WHITE head, "
          "half-closed cynical sleepy eyes with heavy eyelids, a small smirk, thin black limbs, wearing a dark charcoal-grey zip hoodie "
          "with the hood down. Always the same face, same hoodie.")
CHAR_B = ("CHARACTER B (the villain customer): a stocky stick-figure-like man with a large round PINK-WHITE head, a few spiky ORANGE "
          "hair tufts on top, thick angry eyebrows, wide smug mouth, wearing a bright RED jacket/outfit. Always the same face, same red outfit.")
SHEET_PROMPT = ("Character reference sheet on a plain light-grey background: the SAME character shown three times side by side "
                "(front view, 3/4 view, and a close-up of the face with a smug expression). {desc} {style}")

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
    sheets = {}
    for cid, desc in (("A", CHAR_A), ("B", CHAR_B)):
        p = out / f"sheet_{cid}.png"
        if p.exists() and not redo:
            sheets[cid] = Image.open(p).convert("RGB")
            continue
        t = time.time()
        sheets[cid] = gen_image(client, SHEET_PROMPT.format(desc=desc, style=STYLE), [])
        sheets[cid].save(p)
        print(f"[Webtoon] 캐릭터 시트 {cid} 생성 ({time.time() - t:.0f}s)", flush=True)
    return sheets


def make_cut(client, shot: Shot, bible: str, sheets: Dict[str, Image.Image], first: Optional[Image.Image]) -> Image.Image:
    refs: List[Image.Image] = []
    desc: List[str] = []
    for cid in shot.characters:
        if cid in sheets:
            refs.append(sheets[cid])
            desc.append(CHAR_A if cid == "A" else CHAR_B)
    ref_note = ("The attached reference sheet(s) define the characters: reproduce their faces, heads, hair and outfits EXACTLY "
                "(same character identity in every panel). ") if refs else ""
    if first is not None:
        refs.append(first)
        ref_note += "The last attached image is panel 1 of the same story: keep the same location, color palette and drawing style. "
    prompt = (f"{ref_note}{' '.join(desc)}\nLOCATION (keep consistent): {bible}\nPANEL: {shot.image_prompt}\n{STYLE}")
    return gen_image(client, prompt, refs)


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


class CutPainter:
    def __init__(self, img: Image.Image, shot: Shot, scene, tl: List[Dict], dur: float, hit_t: float, title: str) -> None:
        from module3_renderer import get_font
        self.get_font = get_font
        s = max(W / img.width, H / img.height) * 1.32     # 줌/팬 여유를 두고 화면을 덮도록 확대
        self.base = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        self.shot, self.scene, self.tl, self.dur, self.hit_t, self.title = shot, scene, tl, dur, hit_t, title
        self.n = max(1, int(round(dur * FPS)))
        rnd = random.Random(scene.scene_number)
        self.shake_phase = [rnd.random() * 6.28 for _ in range(4)]
        self.sub0 = max(0.0, min((x["start"] for x in tl), default=0.0) - 0.05)
        self.sub1 = min(dur, max((x["end"] for x in tl), default=dur) + 0.15)

    def _camera(self, t: float):
        u = _ease(t / max(self.dur, 0.1))
        mv = self.shot.motion
        z0, z1, px0, px1 = 1.0, 1.0, 0.0, 0.0
        if mv == "zoom_in":
            z0, z1 = 1.0, 1.22
        elif mv == "zoom_out":
            z0, z1 = 1.22, 1.0
        elif mv == "pan_left":
            z0 = z1 = 1.12; px0, px1 = 0.5, -0.5
        elif mv == "pan_right":
            z0 = z1 = 1.12; px0, px1 = -0.5, 0.5
        else:  # push_in_shake
            z0, z1 = 1.0, 1.3
        z = z0 + (z1 - z0) * u
        px = px0 + (px1 - px0) * u
        sx = sy = 0.0
        if mv == "push_in_shake" or (0 <= t - self.hit_t < 0.45):
            k = max(0.0, 1.0 - (t - self.hit_t) / 0.45) if t >= self.hit_t else (0.15 if mv == "push_in_shake" else 0.0)
            amp = 26.0 * k
            sx = amp * math.sin(t * 61 + self.shake_phase[0])
            sy = amp * math.cos(t * 53 + self.shake_phase[1])
        return z, px, sx, sy

    def frame(self, i: int) -> Image.Image:
        t = i / FPS
        z, px, sx, sy = self._camera(t)
        vw, vh = W / z, H / z                                    # 베이스 이미지에서 잘라낼 창
        cx = self.base.width / 2 + px * (self.base.width - vw) / 2 + sx
        cy = self.base.height / 2 + sy
        x0 = max(0, min(self.base.width - vw, cx - vw / 2))
        y0 = max(0, min(self.base.height - vh, cy - vh / 2))
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
    a_same_identity: Optional[bool] = Field(default=None, description="A 가 등장하면: 참조 시트와 같은 캐릭터(머리 색/형태, 후디)인가. 미등장이면 null")
    b_same_identity: Optional[bool] = Field(default=None, description="B 가 등장하면: 참조 시트와 같은 캐릭터(분홍 머리, 주황 머리카락, 빨간 옷)인가. 미등장이면 null")
    style_match: bool = Field(description="컷 1과 같은 화풍(선 굵기, 채색, 톤)인가")
    location_match: bool = Field(description="컷 1과 같은 장소로 보이는가(편의점 색/구조)")
    text_artifacts: bool = Field(description="이미지에 깨진 글자/말풍선/로고가 그려져 있는가")
    note: str = ""


class ScoreList(BaseModel):
    scores: List[CutScore]


def score_consistency(client, sheets: Dict[str, Image.Image], cuts: List[Image.Image]) -> ScoreList:
    from google.genai import types
    parts: List = ["첫 두 이미지는 캐릭터 A, B 참조 시트다. 이어지는 이미지는 컷 1~%d 순서다. 각 컷을 엄격하게 채점하라." % len(cuts), sheets["A"], sheets["B"]]
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
    for i, sc in enumerate(script.scenes):
        p = out / f"cut_{i + 1}.png"
        if p.exists() and not args.redo_cuts:
            cuts.append(Image.open(p).convert("RGB"))
            continue
        shot = shots.get(sc.scene_number) or sl.shots[i]
        t = time.time()
        im = make_cut(client, shot, sl.setting_bible, sheets, cuts[0] if cuts else None)
        im.save(p)
        cuts.append(im)
        print(f"[Webtoon] 컷 {i + 1}/{len(script.scenes)} 생성 {im.size} ({time.time() - t:.0f}s) motion={shot.motion}", flush=True)
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
        cp = CutPainter(cuts[i], shots.get(sc.scene_number) or sl.shots[i], sc, a.timeline, a.dur, a.hit_t, script.episode_title)
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
