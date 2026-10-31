"""후킹 썸네일 자동 합성기 (규칙 기반, API 비용 0).

배경 = 컷 5(결정타) 원본 일러스트, 상단 = 상황 요약 1열 + 도파민 키워드, 하단 = 사이다 펀치라인, 그 위에 만화 효과음 오버레이.
산출물(에피소드당 4파일): <ep>_thumbnail.png/.jpg (1080x1920 세로), <ep>_thumbnail_16x9.png/.jpg (1280x720 검색/홈용).
JPEG 는 유튜브 썸네일 업로드 한도(2MB) 안으로 품질을 낮춰가며 저장한다.

사용: make_thumbnails(script_or_dict, out_dir)  /  python generate_past_thumbnails.py [--all | 에피소드ID ...]
"""
import math
import random
import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFilter

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "pipeline_outputs"
THUMB_DIR = OUT_DIR / "thumbnails"
PAST_THUMB_DIR = OUT_DIR / "past_thumbnails"
WEBTOON_DIR = OUT_DIR / "webtoon"

JPEG_LIMIT = 1_900_000     # 유튜브 한도 2MB 아래로 여유를 둔다

YELLOW = (255, 238, 0)     # 형광 노란색 펀치라인
WHITE = (255, 255, 255)
RED = (235, 30, 50)
BLACK = (0, 0, 0)

# 키워드 풀: 대본의 impact_keyword 가 있으면 "<키워드> 설치완료" 를 후보에 넣고, 없거나 다른 후보와 섞어 에피소드별로 고정 선택한다.
THEME_KEYWORDS = {
    "convenience": ["진상 퇴치 완료", "빌런 영업종료", "인과응보 즉시배송"],
    "noise": ["발망치 응징 완료", "층간소음 참교육", "윗집 사이다 투하"],
    "work": ["얌체 참교육 완료", "갑질 컷!", "직장 사이다 투하"],
}
GENERIC_KEYWORDS = ["참교육 설치완료", "인과응보", "사이다 풀충전"]
THEME_HINTS = (
    ("noise", ("층간", "윗집", "아랫집", "발망치", "소음", "이웃")),
    ("convenience", ("편의점", "계산대", "카운터", "손님", "알바", "매장", "카페", "식당")),
    ("work", ("직장", "회사", "상사", "팀장", "회식", "야근", "갑질", "사무실", "얌체")),
)
SFX_TEXT = {"dramatic_thud": "쾅!", "shock_beep": "헉!", "ding": "땡!", "whoosh": "슝!", "plastic_clatter": "와장창!", "impact": "퍽!"}


def _get(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _as_dict(script: Any) -> Dict[str, Any]:
    if isinstance(script, dict):
        return script
    if hasattr(script, "model_dump"):
        return script.model_dump()
    raise TypeError("script 는 dict 또는 pydantic 모델이어야 합니다")


def _font(size: int):
    from module3_renderer import get_font
    return get_font(size)


def _fit(d: ImageDraw.ImageDraw, text: str, sizes, max_w: int, max_lines: int):
    from render_webtoon import _fit_lines
    return _fit_lines(d, text, _font, sizes, max_w, max_lines)


# ------------------------------------------------------------------ 문구 선택(규칙 기반)
def detect_theme(script: Dict[str, Any]) -> Optional[str]:
    blob = " ".join([str(script.get("episode_title", ""))] + [
        f"{s.get('stickman_action', '')} {s.get('tts_narration', '')}" for s in script.get("scenes", [])])
    scored = [(sum(blob.count(w) for w in words), name) for name, words in THEME_HINTS]
    best = max(scored)
    return best[1] if best[0] > 0 else None


def pick_keyword(script: Dict[str, Any]) -> str:
    scenes = script.get("scenes", [])
    kw = next((str(s.get("impact_keyword") or "").strip() for s in scenes if s.get("scene_number") == 5), "")
    pool = ([f"{kw} 설치완료"] if kw else []) + THEME_KEYWORDS.get(detect_theme(script) or "", []) + GENERIC_KEYWORDS
    return pool[zlib.crc32(str(script.get("episode_id", "")).encode()) % len(pool)]


def pick_headline(script: Dict[str, Any]) -> str:
    """제목에서 상황 요약 1절을 뽑는다: 첫 쉼표/문장부호 앞 구절(없으면 전체)."""
    title = str(script.get("episode_title", "")).strip()
    first = re.split(r"[,，?!…]", title)[0].strip()
    return first if len(first) >= 6 else title.rstrip("?!")


def pick_punchline(script: Dict[str, Any]) -> str:
    subs = {s.get("scene_number"): str(s.get("subtitle_text") or "").strip() for s in script.get("scenes", [])}
    return subs.get(6) or subs.get(5) or ""


def pick_action_text(script: Dict[str, Any], shot5_word: str = "") -> str:
    word = (shot5_word or "").strip()
    if word:
        return word if word.endswith(("!", "?")) else word + "!"
    sfx = next((s.get("sfx") for s in script.get("scenes", []) if s.get("scene_number") == 5), "")
    return SFX_TEXT.get(sfx or "", "퍽!")


def _subject_boxes(ep_dir: Path) -> List[tuple]:
    """컷 5 의 인물 경계 상자를 (x0, y0, x1, y1) 비율(0~1)로 돌려준다. 검수 결과가 없으면 빈 목록."""
    try:
        import json
        bx = json.loads((ep_dir / "checks.json").read_text(encoding="utf-8"))["checks"]["5"]["subject_boxes"]
        return [(b["xmin"] / 1000, b["ymin"] / 1000, b["xmax"] / 1000, b["ymax"] / 1000) for b in bx]
    except Exception:
        return []


def _overlap(rect, boxes) -> float:
    x0, y0, x1, y1 = rect
    return sum(max(0.0, min(x1, b[2]) - max(x0, b[0])) * max(0.0, min(y1, b[3]) - max(y0, b[1])) for b in boxes)


def _free_spot(candidates, half_w: float, half_h: float, boxes):
    """효과음이 인물(특히 얼굴)을 덮지 않도록 후보 중 인물 상자와 겹침이 가장 적은 자리를 고른다(동률이면 앞선 후보)."""
    return min(candidates, key=lambda c: _overlap((c[0] - half_w, c[1] - half_h, c[0] + half_w, c[1] + half_h), boxes))


def _shot5_word(ep_dir: Path) -> str:
    try:
        import json
        shots = json.loads((ep_dir / "shots.json").read_text(encoding="utf-8"))["shots"]
        return str(next(s for s in shots if s.get("scene_number") == 5).get("impact_word") or "")
    except Exception:
        return ""


# ------------------------------------------------------------------ 그리기 도구
def _cover(im: Image.Image, size) -> Image.Image:
    w, h = size
    s = max(w / im.width, h / im.height)
    im = im.resize((math.ceil(im.width * s), math.ceil(im.height * s)), Image.LANCZOS)
    x, y = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((x, y, x + w, y + h))


def _gradient(size, y0: int, y1: int, a0: int, a1: int) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    px = layer.load()
    for y in range(max(0, y0), min(size[1], y1)):
        a = int(a0 + (a1 - a0) * (y - y0) / max(1, y1 - y0))
        for x in range(size[0]):
            px[x, y] = (0, 0, 0, a)
    return layer


def _text(d, xy, text, font, fill, sw, shadow=None):
    if shadow:
        d.text((xy[0] + shadow[0], xy[1] + shadow[1]), text, font=font, fill=shadow[2],
               stroke_width=sw, stroke_fill=shadow[2], anchor="mm")
    d.text(xy, text, font=font, fill=fill, stroke_width=sw, stroke_fill=BLACK, anchor="mm")


def _text_block(d, cx: int, cy: int, text: str, sizes, max_w: int, max_lines: int, fill, sw: int, shadow=None, gap: float = 1.12, single_min: int = 0) -> int:
    """text 를 크기 자동 축소로 맞춰 cy 를 중심으로 쌓아 그린다. 블록 높이를 돌려준다."""
    from render_webtoon import _fit_lines
    one = _fit_lines(d, text, _font, [s for s in sizes if s >= single_min], max_w, 1, truncate=False) if single_min else None
    size, font, lines = one or _fit(d, text, sizes, max_w, max_lines)
    if len(lines) == 2 and " " in text.strip():       # 2줄이면 공백 기준으로 균형 있게 나눠 한 글자 고아줄을 막는다
        words = text.strip().split(" ")
        best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
        a, b = " ".join(words[:best]), " ".join(words[best:])
        if max(d.textlength(a, font=font), d.textlength(b, font=font)) <= max_w:
            lines = [a, b]
    lh = int(size * gap)
    top = cy - lh * (len(lines) - 1) / 2
    for i, ln in enumerate(lines):
        _text(d, (cx, int(top + i * lh)), ln, font, fill, sw, shadow)
    return lh * len(lines)


def _burst(d: ImageDraw.ImageDraw, c, r: int, seed: str, fill=YELLOW, outline=BLACK) -> None:
    rnd = random.Random(zlib.crc32(seed.encode()))
    n = 14
    pts = []
    for i in range(n * 2):
        ang = math.pi * i / n + rnd.uniform(-0.05, 0.05)
        rad = r * (1.0 if i % 2 == 0 else 0.62) * rnd.uniform(0.92, 1.08)
        pts.append((c[0] + rad * math.cos(ang), c[1] + rad * math.sin(ang)))
    d.polygon([(x + 8, y + 10) for x, y in pts], fill=(0, 0, 0, 150))
    d.polygon(pts, fill=fill, outline=outline, width=8)


def _action_overlay(base: Image.Image, center, r: int, text: str, seed: str, font_size: int) -> Image.Image:
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    _burst(ImageDraw.Draw(layer), center, r, seed)
    t = Image.new("RGBA", (r * 3, r * 2), (0, 0, 0, 0))
    td = ImageDraw.Draw(t)
    td.text((t.width // 2 + 5, t.height // 2 + 6), text, font=_font(font_size), fill=(0, 0, 0, 255), anchor="mm", stroke_width=10, stroke_fill=(0, 0, 0, 255))
    td.text((t.width // 2, t.height // 2), text, font=_font(font_size), fill=RED, anchor="mm", stroke_width=10, stroke_fill=WHITE)
    t = t.rotate(10, resample=Image.BICUBIC)
    layer.alpha_composite(t, (int(center[0] - t.width / 2), int(center[1] - t.height / 2)))
    return Image.alpha_composite(base, layer)


# ------------------------------------------------------------------ 레이아웃
def compose_vertical(cut5: Image.Image, script: Dict[str, Any], action: str, boxes=()) -> Image.Image:
    W, H = 1080, 1920
    im = _cover(cut5.convert("RGB"), (W, H)).convert("RGBA")
    im.alpha_composite(_gradient((W, H), 0, 820, 175, 0))
    im.alpha_composite(_gradient((W, H), 1180, H, 0, 185))
    cands = [(fx, fy) for fy in (0.345, 0.43, 0.52, 0.61) for fx in (0.78, 0.2, 0.5)]    # 헤드라인 아래~펀치라인 위
    fx, fy = _free_spot(cands, 150 / W, 150 / H, boxes)
    im = _action_overlay(im, (int(fx * W), int(fy * H)), 150, action, str(script.get("episode_id")), 92)
    d = ImageDraw.Draw(im)
    # 상단 12% 이하 안전 영역에서 시작: 상황 요약(1열) → 도파민 키워드(2열)
    _text_block(d, W // 2, 300, pick_headline(script), list(range(68, 47, -4)), 960, 2, WHITE, 8)
    _text_block(d, W // 2, 470, pick_keyword(script), list(range(150, 83, -6)), 980, 1, WHITE, 14, shadow=(10, 12, RED))
    # 하단: 펀치라인(렌더러 자막과 같은 x 100~880, 하단 28% 높이)
    _text_block(d, 490, 1380, pick_punchline(script), list(range(92, 55, -4)), 780, 2, YELLOW, 11)
    return im.convert("RGB")


def compose_landscape(cut5: Image.Image, script: Dict[str, Any], action: str, boxes=()) -> Image.Image:
    W, H = 1280, 720
    cut = cut5.convert("RGB")
    back = _cover(cut, (W, H)).filter(ImageFilter.GaussianBlur(16)).convert("RGBA")
    back.alpha_composite(Image.new("RGBA", (W, H), (0, 0, 0, 120)))
    # 우측 패널: 원본 컷의 중앙부(캐릭터 대치 구간)를 640x720 으로 잘라 얹는다
    ch = round(cut.width * 720 / 640)
    mid = (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2 if boxes else 0.5     # 인물 묶음의 세로 중심
    top = max(0, min(cut.height - ch, int(cut.height * mid - ch / 2)))
    panel = cut.crop((0, top, cut.width, top + ch)).resize((640, 720), Image.LANCZOS).convert("RGBA")
    back.alpha_composite(panel, (W - 640, 0))
    ImageDraw.Draw(back).line([(W - 640, 0), (W - 640, H)], fill=BLACK + (255,), width=8)
    pb = [(b[0], (b[1] * cut.height - top) / ch, b[2], (b[3] * cut.height - top) / ch) for b in boxes]   # 패널 기준 비율
    fx, fy = _free_spot([(0.8, 0.2), (0.2, 0.2), (0.8, 0.8), (0.2, 0.8), (0.5, 0.12)], 120 / 640, 120 / 720, pb)
    back = _action_overlay(back, (W - 640 + int(fx * 640), int(fy * 720)), 120, action, str(script.get("episode_id")), 64)
    d = ImageDraw.Draw(back)
    _text_block(d, 330, 140, pick_headline(script), list(range(54, 35, -3)), 560, 2, WHITE, 6)
    _text_block(d, 330, 330, pick_keyword(script), list(range(110, 59, -5)), 580, 2, WHITE, 10, shadow=(7, 8, RED), single_min=64)
    _text_block(d, 330, 590, pick_punchline(script), list(range(62, 39, -3)), 560, 2, YELLOW, 8)
    return back.convert("RGB")


# ------------------------------------------------------------------ 저장
def _save(im: Image.Image, stem: Path) -> List[Path]:
    png = stem.with_suffix(".png")
    jpg = stem.with_suffix(".jpg")
    im.save(png, optimize=True)
    for q in (94, 90, 86, 80, 74, 68):
        im.save(jpg, quality=q, optimize=True, subsampling=0 if q >= 90 else 2)
        if jpg.stat().st_size <= JPEG_LIMIT:
            break
    return [png, jpg]


def make_thumbnails(script: Any, out_dir: Path = THUMB_DIR, cut5_path: Optional[Path] = None) -> Dict[str, Any]:
    """컷 5 + 대본으로 세로(1080x1920)·가로(1280x720) 썸네일을 PNG/JPG 로 저장하고 경로를 돌려준다."""
    s = _as_dict(script)
    ep = str(s["episode_id"])
    ep_dir = WEBTOON_DIR / ep
    cut5_path = Path(cut5_path) if cut5_path else ep_dir / "cut_5.png"
    if not cut5_path.exists():
        raise FileNotFoundError(f"컷 5 원본이 없습니다: {cut5_path}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cut5 = Image.open(cut5_path)
    action = pick_action_text(s, _shot5_word(ep_dir))
    boxes = _subject_boxes(ep_dir)
    vertical = _save(compose_vertical(cut5, s, action, boxes), out_dir / f"{ep}_thumbnail")
    landscape = _save(compose_landscape(cut5, s, action, boxes), out_dir / f"{ep}_thumbnail_16x9")
    return {"episode_id": ep, "vertical_png": vertical[0], "vertical_jpg": vertical[1],
            "landscape_png": landscape[0], "landscape_jpg": landscape[1],
            "headline": pick_headline(s), "keyword": pick_keyword(s), "punchline": pick_punchline(s), "action": action}
