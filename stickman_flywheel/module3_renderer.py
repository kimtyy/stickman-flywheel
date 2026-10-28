"""Module 3: 프로시저럴 스틱맨 드로잉 + TTS/SFX 합성 + FFmpeg 자동 렌더링 엔진.

외부 에셋(이미지/효과음) 없이 승인된 대본 JSON 하나로 1080x1920 / 30fps 쇼츠 MP4를 만든다.
"""
import asyncio
import json
import math
import os
import random
import re
import shutil
import hashlib
import subprocess
import tempfile
import wave
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
from scipy import signal
from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

from schemas import SceneBeat, StickmanScriptOutput
from fs_utils import atomic_write_json, atomic_write_text

load_dotenv()

_GEMINI_CLIENT = None


def get_gemini_client() -> Optional[genai.Client]:
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        load_dotenv(override=True)
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if api_key:
            _GEMINI_CLIENT = genai.Client(api_key=api_key)
    return _GEMINI_CLIENT


BASE_DIR = Path(__file__).resolve().parent
APPROVED_DIR = BASE_DIR / "pipeline_outputs" / "approved"
RENDERED_DIR = BASE_DIR / "pipeline_outputs" / "rendered"
AUDIO_DIR = BASE_DIR / "pipeline_outputs" / "audio"
ASSETS_DIR = BASE_DIR / "assets"
SFX_DIR = ASSETS_DIR / "sfx"
BGM_DIR = ASSETS_DIR / "bgm"
FONTS_DIR = ASSETS_DIR / "fonts"
for _d in (APPROVED_DIR, RENDERED_DIR, AUDIO_DIR, SFX_DIR, BGM_DIR, FONTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

W, H, FPS = 1080, 1920, 30
SR = 44100
SC = 1.6                      # 스틱맨 스케일
FLOOR_Y = 1330                # 바닥선 y
MAIN_X, FOE_X = 360, 720
LINE_W = 22

FONT_CANDIDATES = [
    FONTS_DIR / "NanumGothic-Bold.ttf",
    FONTS_DIR / "NanumGothic.ttf",
    Path("C:/Windows/Fonts/malgunbd.ttf"),
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("C:/Windows/Fonts/NanumGothicBold.ttf"),
    Path("C:/Windows/Fonts/NanumGothic.ttf"),
    Path("C:/Windows/Fonts/gulim.ttc"),
    Path("C:/Windows/Fonts/batang.ttc"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
]

_RESOLVED_KOREAN_FONT: Optional[str] = None
_FONT_CACHE: Dict[int, ImageFont.FreeTypeFont] = {}


def resolve_korean_font_path() -> Optional[str]:
    """한글 렌더링이 100% 보장되는 TTF/OTF 폰트 파일 경로 탐색."""
    global _RESOLVED_KOREAN_FONT
    if _RESOLVED_KOREAN_FONT and Path(_RESOLVED_KOREAN_FONT).exists():
        return _RESOLVED_KOREAN_FONT

    for p in FONT_CANDIDATES:
        path_obj = Path(p)
        if path_obj.exists() and path_obj.is_file():
            try:
                # 한글 글리프 렌더링 테스트
                test_font = ImageFont.truetype(str(path_obj), 24)
                test_img = Image.new("RGBA", (100, 40), (0, 0, 0, 0))
                test_draw = ImageDraw.Draw(test_img)
                test_draw.text((10, 10), "한글", font=test_font, fill=(255, 255, 255))
                _RESOLVED_KOREAN_FONT = str(path_obj)
                print(f"[Renderer:Font] 한글 전용 트루타입 폰트 확정 로드: {_RESOLVED_KOREAN_FONT}")
                return _RESOLVED_KOREAN_FONT
            except Exception as e:
                print(f"[Renderer:Font] 폰트 로드 실패 ({path_obj}): {e}")
                continue

    return None


def get_font(size: int):
    size = int(size)
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]

    font_path = resolve_korean_font_path()
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, size)
        except Exception as e:
            print(f"[Renderer:Font] truetype({font_path}, {size}) 실패: {e}")

    if font is None:
        print("[Renderer:Font] ⚠️ 경고: 한글 폰트를 찾을 수 없어 기본 폰트로 대체됩니다. 한글이 깨질 수 있습니다.")
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()

    _FONT_CACHE[size] = font
    return font


def find_ffmpeg() -> str:
    """PATH의 ffmpeg 우선, 없으면 imageio-ffmpeg 내장 바이너리."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


# =====================================================================
# 1. SFX 합성
# =====================================================================
SFX_ALIASES: List[Tuple[str, List[str]]] = [
    ("punch_hit", ["punch", "hit", "slap", "smack", "impact", "펀치", "타격", "퍽", "때리"]),
    ("whoosh", ["whoosh", "swish", "swoosh", "zoom", "wind", "휙", "줌"]),
    ("shock_beep", ["shock", "beep", "alert", "alarm", "gasp", "충격", "경고", "삑"]),
    ("cartoon_slip", ["slip", "slide", "skid", "미끄", "슬립"]),
    ("boing", ["boing", "bounce", "spring", "jump", "보잉"]),
    ("ding", ["ding", "bell", "chime", "ring", "딩"]),
    ("thud", ["thud", "fall", "crash", "bang", "drop", "쿵", "쾅", "넘어"]),
    ("pop", ["pop", "click", "bubble", "팝"]),
]


def canonical_sfx(name: str) -> str:
    low = name.lower()
    for canon, keys in SFX_ALIASES:
        if canon == low or any(k in low for k in keys):
            return canon
    return "pop"


class SfxSynth:
    """wave/math 만으로 효과음을 즉석 합성해 assets/sfx/*.wav 로 캐시."""

    def __init__(self, sfx_dir: Path = SFX_DIR) -> None:
        self.sfx_dir = sfx_dir
        self.sfx_dir.mkdir(parents=True, exist_ok=True)
        self.makers: Dict[str, Callable[[], List[float]]] = {
            "punch_hit": self._punch_hit, "whoosh": self._whoosh,
            "shock_beep": self._shock_beep, "cartoon_slip": self._cartoon_slip,
            "boing": self._boing, "ding": self._ding, "thud": self._thud, "pop": self._pop,
        }

    def ensure_all(self) -> None:
        for name in self.makers:
            self.path(name)

    def path(self, name: str) -> Path:
        """사용자가 같은 이름의 wav를 넣어 두었으면 그것을, 아니면 합성본을 사용."""
        custom = self.sfx_dir / f"{name.lower()}.wav"
        if custom.exists():
            return custom
        canon = canonical_sfx(name)
        target = self.sfx_dir / f"{canon}.wav"
        if not target.exists():
            self._write_wav(target, self.makers[canon]())
        return target

    @staticmethod
    def _write_wav(path: Path, samples: List[float]) -> None:
        peak = max((abs(s) for s in samples), default=1.0) or 1.0
        gain = 0.85 / peak
        pcm = array("h", (int(max(-1.0, min(1.0, s * gain)) * 32767) for s in samples))
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(pcm.tobytes())

    @staticmethod
    def _punch_hit() -> List[float]:
        rnd = random.Random(1)
        out, ph = [], 0.0
        for i in range(int(0.30 * SR)):
            t = i / SR
            ph += 2 * math.pi * (45 + 170 * math.exp(-t * 10)) / SR
            out.append(math.sin(ph) * math.exp(-t * 12)
                       + (rnd.random() * 2 - 1) * math.exp(-t * 45) * 0.7)
        return out

    @staticmethod
    def _whoosh() -> List[float]:
        rnd = random.Random(2)
        n = int(0.55 * SR)
        out, lp = [], 0.0
        for i in range(n):
            x = i / n
            env = math.sin(math.pi * x) ** 1.6
            a = 0.03 + 0.45 * math.sin(math.pi * x)
            lp += a * ((rnd.random() * 2 - 1) - lp)
            out.append(lp * env)
        return out

    @staticmethod
    def _shock_beep() -> List[float]:
        out: List[float] = []
        for freq, dur, gap in ((1175, 0.09, 0.04), (1175, 0.09, 0.04), (1568, 0.22, 0.0)):
            n = int(dur * SR)
            for i in range(n):
                t = i / SR
                env = min(1.0, t / 0.005) * min(1.0, (dur - t) / 0.02)
                sq = 1.0 if math.sin(2 * math.pi * freq * t) >= 0 else -1.0
                out.append(env * (0.6 * math.sin(2 * math.pi * freq * t) + 0.25 * sq))
            out.extend([0.0] * int(gap * SR))
        return out

    @staticmethod
    def _cartoon_slip() -> List[float]:
        n_sec = 0.65
        out, ph = [], 0.0
        for i in range(int(n_sec * SR)):
            t = i / SR
            freq = 250 + 1300 * math.exp(-t * 3.0) + 35 * math.sin(2 * math.pi * 16 * t)
            ph += 2 * math.pi * freq / SR
            env = min(1.0, t / 0.01) * min(1.0, (n_sec - t) / 0.12)
            out.append(env * (0.7 * math.sin(ph) + 0.2 * math.sin(2 * ph)))
        return out

    @staticmethod
    def _boing() -> List[float]:
        out, ph = [], 0.0
        for i in range(int(0.6 * SR)):
            t = i / SR
            freq = 220 + 110 * math.sin(2 * math.pi * 8 * t) * math.exp(-t * 3)
            ph += 2 * math.pi * freq / SR
            out.append(math.exp(-t * 5) * (math.sin(ph) + 0.3 * math.sin(2 * ph)))
        return out

    @staticmethod
    def _ding() -> List[float]:
        return [
            (math.sin(2 * math.pi * 1320 * (i / SR)) + 0.35 * math.sin(2 * math.pi * 2640 * (i / SR)))
            * math.exp(-(i / SR) * 7)
            for i in range(int(0.7 * SR))
        ]

    @staticmethod
    def _thud() -> List[float]:
        rnd = random.Random(3)
        out, ph = [], 0.0
        for i in range(int(0.4 * SR)):
            t = i / SR
            ph += 2 * math.pi * (35 + 70 * math.exp(-t * 3)) / SR
            out.append(math.sin(ph) * math.exp(-t * 11)
                       + (rnd.random() * 2 - 1) * math.exp(-t * 60) * 0.4)
        return out

    @staticmethod
    def _pop() -> List[float]:
        out, ph = [], 0.0
        for i in range(int(0.14 * SR)):
            t = i / SR
            ph += 2 * math.pi * (200 + 900 * math.exp(-t * 20)) / SR
            out.append(math.sin(ph) * math.exp(-t * 30))
        return out


# =====================================================================
# 2. 포즈 / 카메라 선택 로직
# =====================================================================
POSE_RULES: List[Tuple[str, List[str]]] = [
    ("freeze", ["freeze", "프리즈", "얼어", "굳어", "멈춰"]),
    ("grab", ["멱살", "grab", "collar", "움켜", "잡아채"]),
    ("punch", ["punch", "주먹", "펀치", "때리", "후려", "날리", "가격", "타격", "싸대기", "slap", "kick", "걷어"]),
    ("fall", ["fall", "쓰러", "넘어", "미끄러", "slip", "자빠", "기절", "나자빠", "뻗"]),
    ("run", ["run", "달리", "달려", "도망", "뛰", "질주", "전력", "sprint", "쫓"]),
    ("shock", ["shock", "경악", "충격", "놀라", "놀란", "당황", "멘붕", "헉", "머리를 감"]),
    ("smirk", ["smirk", "비웃", "삐딱", "조롱", "비꼬", "팔짱", "여유", "미소", "썩소"]),
    ("angry", ["angry", "분노", "화가", "빡", "폭발", "격분", "열받", "성질", "노려", "버럭", "짜증"]),
    ("twist", ["twist", "반전", "깜짝"]),
]
FOE_FOR = {"punch": "hit", "grab": "grabbed", "smirk": "cower", "angry": "cower"}
BG_COLORS = [(255, 236, 179), (255, 205, 210), (197, 225, 250),
             (200, 240, 210), (235, 215, 250), (255, 224, 200)]
POSE_BG = {"angry": (255, 190, 170), "freeze": (200, 230, 255), "twist": (255, 245, 140)}


def select_pose(action: str, idx: int, total: int) -> str:
    text = (action or "").lower()
    for name, keys in POSE_RULES:
        if any(k in text for k in keys):
            return name
    return "twist" if (idx == total - 1 and total > 1) else "talk"


def parse_camera(camera_work: str) -> Dict[str, object]:
    c = (camera_work or "").lower()
    zoom_out = any(k in c for k in ["zoom-out", "zoom out", "줌아웃", "줌 아웃", "축소", "pull"])
    zoom_in = any(k in c for k in ["zoom", "줌", "push", "확대", "클로즈"]) and not zoom_out
    return {
        "zoom_out": zoom_out, "zoom_in": zoom_in,
        "shake": any(k in c for k in ["shake", "흔들", "쉐이크", "셰이크", "진동"]),
        "freeze": any(k in c for k in ["freeze", "프리즈", "정지"]),
        "pan": ("pan" in c) or ("패닝" in c),
    }


SFX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s(?:ec)?\s*[:\-]?\s*([A-Za-z_가-힣0-9]+)")


def parse_sfx_cues(cue: str) -> List[Tuple[float, str]]:
    return [(float(m.group(1)), m.group(2)) for m in SFX_RE.finditer(cue or "")]


DEFAULT_SFX: Dict[str, List[Tuple[float, str]]] = {
    "punch": [(0.0, "whoosh"), (0.55, "punch_hit")], "fall": [(0.15, "cartoon_slip"), (0.9, "thud")],
    "shock": [(0.0, "shock_beep")], "twist": [(0.0, "shock_beep")], "freeze": [(0.0, "shock_beep")],
    "run": [(0.0, "whoosh")], "angry": [(0.0, "boing")], "grab": [(0.1, "whoosh")],
    "smirk": [(0.0, "pop")], "talk": [(0.0, "pop")],
}


# =====================================================================
# 3. 스틱맨 포즈 정의 (각도: 아래 수직 기준, 앞쪽(+)이 양수 / 단위: degree)
# =====================================================================
def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def _ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _ease_in_out(x: float) -> float:
    """부드러운 에르미트(Smoothstep) 기반 Ease-in-out 보간 함수."""
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _base() -> Dict:
    return dict(lean=0.0, tilt=0.0, la=(0.0, 0.0), ra=(0.0, 0.0), ll=(0.0, 0.0), rl=(0.0, 0.0),
                rot=0.0, air=0.0, xoff=0.0, eyes="dot", mouth="smile", mo=0.0, brow=False)


def pose_talk(t: float, ctx: Dict) -> Dict:
    p = _base()
    # 자연스러운 유기적 호흡 사이클 (~2.4s 주기)
    breath = math.sin(t * 2.6)
    # 프레임 간 Ease-in-out 보간을 적용한 부드러운 손 제스처 회전
    cycle = (t * 1.7) % 2.0
    k_arm = _ease_in_out(cycle if cycle < 1.0 else (2.0 - cycle))

    # 제스처 오른팔: 부드러운 회전 및 관절 굽힘
    ra_u = _lerp(35.0, 68.0, k_arm) + 2.0 * breath
    ra_f = _lerp(60.0, 108.0, k_arm) + 3.0 * breath
    # 대기 왼팔: 자연스러운 호흡 스윙
    la_u = 14.0 + 3.5 * breath
    la_f = 8.0 + 2.0 * breath

    # 상체 호흡 및 골반 자연스러운 접지 (발이 공중에 뜨지 않음)
    lean = 3.0 + 0.8 * breath
    tilt = -2.0 + 1.2 * math.cos(t * 2.6)
    p.update(
        lean=lean, tilt=tilt,
        la=(la_u, la_f), ra=(ra_u, ra_f),
        ll=(6.0, -4.0), rl=(-6.0, 4.0),
        air=0.0,
        eyes="dot", mouth="talk", mo=abs(math.sin(t * 11))
    )
    return p


def pose_shock(t: float, ctx: Dict, frozen: bool = False) -> Dict:
    p = _base()
    jit = 0.0 if frozen else 3.0 * math.sin(t * 32.0)
    wob = 0.0 if frozen else 2.5 * math.sin(t * 24.0)
    p.update(
        lean=-5.0, tilt=-2.0,
        # 양손으로 머리/뺨 옆을 감싸며 패닉에 빠진 포즈 (양옆으로 펼쳐져 명확히 노출)
        la=(-50.0 + wob, -145.0 - wob),
        ra=(50.0 - wob, 145.0 + wob),
        # 다리: 무릎 꺾임 각도 엄격 제한 (160°~175° 일자 유지, 부러짐 방지)
        ll=(12.0, -4.0),
        rl=(-12.0, 4.0),
        xoff=jit, air=0.0,
        eyes="wide", mouth="O"
    )
    return p


def pose_freeze(t: float, ctx: Dict) -> Dict:
    return pose_shock(0.4, ctx, frozen=True)


def pose_cower(t: float, ctx: Dict) -> Dict:
    p = pose_shock(t, ctx)
    p["lean"] = -10.0
    p["ll"] = (10.0, -2.0)
    p["rl"] = (-10.0, 2.0)
    return p


def pose_punch(t: float, ctx: Dict) -> Dict:
    p = _base()
    hit = ctx["hit_t"]
    ph = t - hit + 0.55
    guard = (50, 125)
    if ph < 0:
        p.update(lean=4, la=guard, ra=(45, 115), ll=(22, -8), rl=(-24, 10))
    else:
        ph %= 1.15
        if ph < 0.4:
            k = _ease(ph / 0.4)
            ra, lean = (_lerp(45, -75, k), _lerp(115, -35, k)), _lerp(4, -14, k)
        elif ph < 0.55:
            k = (ph - 0.4) / 0.15
            ra, lean = (_lerp(-75, 88, k), _lerp(-35, 92, k)), _lerp(-14, 28, k)
        else:
            k = _ease((ph - 0.55) / 0.6)
            ra, lean = (_lerp(88, 45, k), _lerp(92, 115, k)), _lerp(28, 4, k)
        p.update(lean=lean, la=guard, ra=ra, ll=(38, -14), rl=(-44, 12))
    p.update(eyes="angry", mouth="teeth", brow=True)
    return p


def pose_hit(t: float, ctx: Dict) -> Dict:
    hit = ctx["hit_t"]
    if t < hit:
        p = pose_shock(t, ctx)
        p["lean"] = -8
        return p
    p = _base()
    k = _ease((t - hit) / 0.7)
    p.update(lean=-10, la=(130 + 40 * math.sin(t * 10) * (1 - k), 110), ra=(100, 80),
             ll=(30, 40), rl=(10, 60), rot=-88 * k, xoff=230 * k, air=150 * math.sin(math.pi * k),
             eyes="X", mouth="O" if k < 0.6 else "frown")
    return p


def pose_grab(t: float, ctx: Dict) -> Dict:
    p = _base()
    p.update(lean=22, xoff=20 + 5 * math.sin(t * 25), la=(88, 105), ra=(95 + 6 * math.sin(t * 20), 100),
             ll=(30, -10), rl=(-26, 10), eyes="angry", mouth="teeth", brow=True)
    return p


def pose_grabbed(t: float, ctx: Dict) -> Dict:
    p = _base()
    p.update(lean=-12, tilt=-18, la=(40 + 50 * math.sin(t * 14), 20), ra=(60 + 50 * math.sin(t * 14 + 2), 30),
             ll=(20 * math.sin(t * 12), 5), rl=(-20 * math.sin(t * 12), 5), air=45,
             xoff=10 * math.sin(t * 22), eyes="wide", mouth="O")
    return p


def pose_run(t: float, ctx: Dict) -> Dict:
    p, c = _base(), math.sin(t * 17)
    p.update(lean=20, la=(-55 * c, -55 * c + 65), ra=(55 * c, 55 * c + 65),
             ll=(55 * c, 55 * c - 55), rl=(-55 * c, -55 * c - 55), air=18 * abs(c),
             eyes="wide", mouth="O")
    return p


def pose_smirk(t: float, ctx: Dict) -> Dict:
    p = _base()
    breath = math.sin(t * 2.2)
    k_sway = _ease_in_out((math.sin(t * 1.4) + 1.0) / 2.0)

    lean = _lerp(-6.0, -8.5, k_sway)
    tilt = 8.0 + 1.5 * breath

    # 얄미운 팔짱 및 부드러운 손동작 이징
    cycle_ra = (t * 1.2) % 2.0
    k_ra = _ease_in_out(cycle_ra if cycle_ra < 1.0 else (2.0 - cycle_ra))
    ra_u = _lerp(118.0, 126.0, k_ra) + 2.0 * breath
    ra_f = _lerp(144.0, 158.0, k_ra) + 3.0 * breath

    p.update(
        lean=lean, tilt=tilt,
        la=(54.0 + 2.0 * breath, -22.0),
        ra=(ra_u, ra_f),
        ll=(10.0, -5.0), rl=(-10.0 + 4.0 * k_sway, -8.0),
        air=0.0,
        eyes="squint", mouth="smirk"
    )
    return p


def pose_fall(t: float, ctx: Dict) -> Dict:
    landing = ctx["land_t"]
    p = _base()
    if t < landing:
        k = _ease(t / landing)
        fl = math.sin(t * 28)
        p.update(lean=_lerp(15, -5, k), rot=-100 * k, xoff=90 * k, air=110 * math.sin(math.pi * k),
                 la=(110 + 50 * fl, 150), ra=(120 - 50 * fl, 160), ll=(85, 100), rl=(50, 80),
                 eyes="wide", mouth="O")
    else:
        bounce = max(0.0, 1 - (t - landing) * 3.5)
        p.update(lean=0, rot=-100, xoff=90, air=14 * bounce * abs(math.sin((t - landing) * 14)),
                 la=(40, 20), ra=(-30, -10), ll=(8, 6), rl=(-4, 3), eyes="X", mouth="frown")
    return p


def pose_angry(t: float, ctx: Dict) -> Dict:
    p = _base()
    breath = math.sin(t * 3.6)
    shake = 3.0 * math.sin(t * 30.0)
    p.update(
        lean=8.0 + 1.2 * breath, tilt=1.8 * breath,
        xoff=shake,
        la=(34.0 + 3.0 * breath, 142.0 + 2.5 * breath),
        ra=(38.0 + 3.0 * breath, 148.0 + 2.5 * breath),
        ll=(14.0, -6.0), rl=(-14.0, 6.0),
        air=0.0,
        eyes="angry", mouth="teeth", brow=True
    )
    return p


def pose_twist(t: float, ctx: Dict) -> Dict:
    p = _base()
    p.update(lean=-4, la=(160 + 10 * math.sin(t * 12), 172), ra=(165, 176 + 8 * math.sin(t * 12)),
             ll=(26, 8), rl=(-26, -8), air=130 * abs(math.sin(t * 5)), eyes="wide", mouth="O")
    return p


def pose_crushed(t: float, ctx: Dict) -> Dict:
    """바닥에 개구리처럼 납작하게 깔려 짓눌린 슬랩스틱 포즈."""
    p = _base()
    wob = 4 * math.sin(t * 30)
    p.update(
        lean=82, tilt=5,
        rot=-88,
        xoff=15 + wob,
        air=-10,
        la=(115, 140), ra=(75, 50),
        ll=(70, 85), rl=(85, 95),
        eyes="X", mouth="O"
    )
    return p


def pose_radio_call(t: float, ctx: Dict) -> Dict:
    """씬 5 본부 무전 호출 포즈: 오른손은 입가의 무전기를 들고, 왼팔은 허리 옆으로 자연스럽게 분리."""
    p = _base()
    breath = math.sin(t * 2.4)
    p.update(
        lean=4.0 + 0.8 * breath,
        tilt=-2.5 + 0.6 * breath,
        rot=0.0,
        air=0.0,
        xoff=0.0,
        # 오른손(앞손): 입가로 무전기/폰 들기 (어깨 32° + 팔꿈치 166°로 턱/입가에 안착)
        ra=(32.0 + 1.0 * breath, 166.0 + 1.2 * breath),
        # 왼손(뒷손): 허리 옆으로 자연스럽게 내려서 팔이 엉키지 않고 분리됨
        la=(-26.0 - 1.0 * breath, -12.0 - 0.8 * breath),
        # 다리: 곧은 직립 스탠스
        ll=(6.0, 2.0),
        rl=(-6.0, -2.0),
        eyes="angry", brow=True, mouth="talk", mo=abs(math.sin(t * 11))
    )
    return p


def pose_retribution(t: float, ctx: Dict) -> Dict:
    """당당하게 서서 참교육을 관람하는 주인공 포즈 (뒷손은 허리에 얹고, 앞손은 당당한 제스처)."""
    p = _base()
    breath = math.sin(t * 2.0)
    p.update(
        lean=5.5 + 0.6 * breath, tilt=-3.5 + 0.8 * breath,
        rot=0.0,
        air=0.0,  # 확실한 바닥 접지! 공중에 절대 뜨지 않음
        xoff=0.0,
        # 왼팔(뒷손): 허리에 자연스럽게 얹은 실루엣 (Hand-on-hip) -> 외팔이 현상 원천 해소!
        la=(-38.0 - 0.8 * breath, 48.0 + 0.6 * breath),
        # 오른팔(앞손): 당당하게 앞을 가리키거나 주먹 쥐고 참교육 관람
        ra=(52.0 + 1.2 * breath, 95.0 + 1.0 * breath),
        # 다리: 안정적이고 곧은 스탠스 (170°~180° 유지)
        ll=(8.0, 2.0), rl=(-10.0, -4.0),
        eyes="angry", brow=True, mouth="smirk"
    )
    return p


POSES: Dict[str, Callable[[float, Dict], Dict]] = {
    "talk": pose_talk, "punch": pose_punch, "grab": pose_grab, "shock": pose_shock, "run": pose_run,
    "smirk": pose_smirk, "fall": pose_fall, "angry": pose_angry, "freeze": pose_freeze,
    "twist": pose_twist, "hit": pose_hit, "grabbed": pose_grabbed, "cower": pose_cower,
    "crushed": pose_crushed, "retribution": pose_retribution, "radio_call": pose_radio_call,
}


# =====================================================================
# 4. 스틱맨 기하 계산 및 작화
# =====================================================================
T_LEN, UA, FA, TH, SH = 135 * SC, 55 * SC, 50 * SC, 65 * SC, 65 * SC
HEAD_R = 50 * SC


def quadratic_bezier_curve(p0: Tuple[float, float], p1: Tuple[float, float], p2: Tuple[float, float], n: int = 16) -> List[Tuple[float, float]]:
    """어깨-팔꿈치-손목(또는 골반-무릎-발목) 3점을 제어점으로 하는 2차 베지어 곡선 점열 생성."""
    pts = []
    for i in range(n + 1):
        t = i / float(n)
        u = 1.0 - t
        x = u * u * p0[0] + 2.0 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2.0 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _limb(start, a1, a2, l1, l2, clamp_bend: Optional[float] = None):
    if clamp_bend is not None:
        diff = a2 - a1
        if abs(diff) > clamp_bend:
            a2 = a1 + math.copysign(clamp_bend, diff)
    r1, r2 = math.radians(a1), math.radians(a2)
    mid = (start[0] + l1 * math.sin(r1), start[1] + l1 * math.cos(r1))
    end = (mid[0] + l2 * math.sin(r2), mid[1] + l2 * math.cos(r2))
    return mid, end


def compute_figure(p: Dict, f: int, cx: float, floor_y: float) -> Dict:
    lean, tilt = math.radians(p["lean"]), math.radians(p["tilt"])
    sh = (T_LEN * math.sin(lean), -T_LEN * math.cos(lean))
    la_m, la_e = _limb(sh, p["la"][0], p["la"][1], UA, FA)
    ra_m, ra_e = _limb(sh, p["ra"][0], p["ra"][1], UA, FA)
    # 다리: 무릎 꺾임 각도 제한 (최대 28도 굴곡 -> 무릎 내각 152도 이상 일자 유지, 부러짐 방지)
    ll_m, ll_e = _limb((0, 0), p["ll"][0], p["ll"][1], TH, SH, clamp_bend=28.0)
    rl_m, rl_e = _limb((0, 0), p["rl"][0], p["rl"][1], TH, SH, clamp_bend=28.0)
    hc = (sh[0] + (HEAD_R + 10) * math.sin(lean + tilt), sh[1] - (HEAD_R + 10) * math.cos(lean + tilt))
    ll_f = (ll_e[0] + 26 * SC / 1.6 * 1.0, ll_e[1])
    rl_f = (rl_e[0] + 26 * SC / 1.6 * 1.0, rl_e[1])

    r = math.radians(p["rot"]) * f
    cr, sr = math.cos(r), math.sin(r)

    def tf0(pt):
        x, y = pt[0] * f, pt[1]
        return (x * cr - y * sr, x * sr + y * cr)

    lows = [tf0(q)[1] + LINE_W / 2 for q in (ll_m, ll_e, rl_m, rl_e, ll_f, rl_f, (0, 0), sh, la_e, ra_e)]
    lows.append(tf0(hc)[1] + HEAD_R)
    hip_y = floor_y - max(lows) - p["air"]
    hip_x = cx + p["xoff"]

    def tf(pt):
        q = tf0(pt)
        return (hip_x + q[0], hip_y + q[1])

    def face_pt(dx: float, dy: float):
        ox, oy = dx * HEAD_R, dy * HEAD_R
        rx = ox * math.cos(tilt) - oy * math.sin(tilt)
        ry = ox * math.sin(tilt) + oy * math.cos(tilt)
        return tf((hc[0] + rx, hc[1] + ry))

    return {
        "hip": tf((0, 0)), "shoulder": tf(sh), "head": tf(hc), "R": HEAD_R,
        "legs": [[tf((0, 0)), tf(ll_m), tf(ll_e), tf(ll_f)], [tf((0, 0)), tf(rl_m), tf(rl_e), tf(rl_f)]],
        "torso": [tf((0, 0)), tf(sh)],
        "arms": [[tf(sh), tf(la_m), tf(la_e)], [tf(sh), tf(ra_m), tf(ra_e)]],
        "face": face_pt, "floor_y": floor_y,
    }


def draw_capsule(d: ImageDraw.ImageDraw, p1: Tuple[float, float], p2: Tuple[float, float],
                 out_w: float, in_w: float, out_color, in_color) -> None:
    """2단 관절 캡슐 세그먼트 (볼륨감 있는 웹툰 바디 + 굵고 선명한 블랙 펜선 아웃라인)"""
    # 1. 외곽선 (블랙 펜선 느낌)
    d.line([p1, p2], fill=out_color, width=int(out_w))
    ro = out_w / 2.0
    d.ellipse((p1[0] - ro, p1[1] - ro, p1[0] + ro, p1[1] + ro), fill=out_color)
    d.ellipse((p2[0] - ro, p2[1] - ro, p2[0] + ro, p2[1] + ro), fill=out_color)

    # 2. 내부 볼륨 채움 (스킨/의상 바디 톤)
    d.line([p1, p2], fill=in_color, width=int(in_w))
    ri = in_w / 2.0
    d.ellipse((p1[0] - ri, p1[1] - ri, p1[0] + ri, p1[1] + ri), fill=in_color)
    d.ellipse((p2[0] - ri, p2[1] - ri, p2[0] + ri, p2[1] + ri), fill=in_color)


def draw_figure(d: ImageDraw.ImageDraw, fig: Dict, p: Dict, color, head_fill,
                char_type: str = "A", t: float = 0.0) -> None:
    # 1. 바닥 그림자 (웹툰 특유의 부드러운 타원형 접지 그림자)
    hx, hy = fig["hip"][0], fig["floor_y"]
    d.ellipse((hx - 120, hy - 14, hx + 120, hy + 16), fill=(45, 42, 52, 180) if len(color) == 4 else (65, 60, 75))

    limb_color = (20, 20, 25) if char_type == "A" else (185, 32, 42)
    outline_color = (15, 15, 25)
    lw = int(LINE_W)

    # 2. 다리 (골반-무릎-발목 3점 제어점 기반 부드러운 2차 베지어 곡선 + 둥근 캡)
    for leg in fig["legs"]:
        hip_pt, knee_pt, ankle_pt, foot_pt = leg[0], leg[1], leg[2], leg[3]
        leg_curve = quadratic_bezier_curve(hip_pt, knee_pt, ankle_pt, n=16)
        d.line(leg_curve, fill=limb_color, width=lw, joint="curve")
        d.line([ankle_pt, foot_pt], fill=limb_color, width=lw, joint="curve")
        r = lw / 2.0
        d.ellipse((hip_pt[0] - r, hip_pt[1] - r, hip_pt[0] + r, hip_pt[1] + r), fill=limb_color)
        d.ellipse((foot_pt[0] - r, foot_pt[1] - r, foot_pt[0] + r, foot_pt[1] + r), fill=limb_color)

    # 3. 몸통 (토르소 일체형 선 및 캐릭터 의상)
    hip_pt, shoulder_pt = fig["hip"], fig["shoulder"]
    d.line([hip_pt, shoulder_pt], fill=limb_color, width=lw + 4, joint="curve")
    d.ellipse((hip_pt[0] - lw / 2, hip_pt[1] - lw / 2, hip_pt[0] + lw / 2, hip_pt[1] + lw / 2), fill=limb_color)
    d.ellipse((shoulder_pt[0] - lw / 2, shoulder_pt[1] - lw / 2, shoulder_pt[0] + lw / 2, shoulder_pt[1] + lw / 2), fill=limb_color)

    # 상체 의상 / 실루엣
    sx, sy = shoulder_pt
    hip_x, hip_y = hip_pt
    if char_type == "A":
        # 주인공 A: 머슬 짐웨어 나시 조끼 (Royal Blue)
        tank_pts = [
            (sx - 48, sy - 2), (sx + 48, sy - 2),
            (hip_x + 28, hip_y + 4), (hip_x - 28, hip_y + 4)
        ]
        d.polygon(tank_pts, fill=(28, 80, 195), outline=limb_color)
        d.line([(sx - 48, sy - 2), (hip_x - 28, hip_y + 4)], fill=limb_color, width=4)
        d.line([(sx + 48, sy - 2), (hip_x + 28, hip_y + 4)], fill=limb_color, width=4)
        d.ellipse((sx - 24, sy - 14, sx + 24, sy + 18), fill=head_fill, outline=limb_color, width=3)
    else:
        # 빌런 B: 얄미운 네온 오렌지 포인트 상의
        tank_pts = [
            (sx - 42, sy - 2), (sx + 42, sy - 2),
            (hip_x + 36, hip_y + 4), (hip_x - 36, hip_y + 4)
        ]
        d.polygon(tank_pts, fill=(255, 95, 25), outline=limb_color)
        d.line([(sx - 42, sy - 2), (hip_x - 36, hip_y + 4)], fill=limb_color, width=4)
        d.line([(sx + 42, sy - 2), (hip_x + 36, hip_y + 4)], fill=limb_color, width=4)
        d.ellipse((sx - 22, sy - 12, sx + 22, sy + 16), fill=head_fill, outline=limb_color, width=3)

    # 4. 팔 (어깨-팔꿈치-손목 3점 제어점 기반 부드러운 2차 베지어 곡선 + 둥근 캡)
    for arm in fig["arms"]:
        sh_pt, elbow_pt, wrist_pt = arm[0], arm[1], arm[2]
        arm_curve = quadratic_bezier_curve(sh_pt, elbow_pt, wrist_pt, n=16)
        d.line(arm_curve, fill=limb_color, width=lw, joint="curve")
        r = lw / 2.0
        d.ellipse((sh_pt[0] - r, sh_pt[1] - r, sh_pt[0] + r, sh_pt[1] + r), fill=limb_color)
        # 손 디테일: 둥근 손 끝
        wx, wy = wrist_pt
        rf = lw * 0.75
        d.ellipse((wx - rf, wy - rf, wx + rf, wy + rf), fill=limb_color)

    # 5. 머리 기본 원 및 입체 음영 (3D Shading & Specular Jelly Highlight)
    cx, cy = fig["head"]
    R = fig["R"]

    # 머리 기본 원 채움
    d.ellipse((cx - R, cy - R, cx + R, cy + R), fill=head_fill)

    # 우하단 초승달 모양 입체 그림자
    shadow_fill = (222, 226, 238) if char_type == "A" else (245, 214, 214)
    d.chord((cx - R + 4, cy - R + 4, cx + R - 2, cy + R - 2), start=340, end=110, fill=shadow_fill)

    # 웹툰 굵은 블랙 외곽선
    d.ellipse((cx - R, cy - R, cx + R, cy + R), outline=outline_color, width=9)

    # 좌상단 영롱한 젤리광 하이라이트 (Specular Jelly Capsule)
    d.ellipse((cx - R * 0.54, cy - R * 0.54, cx - R * 0.26, cy - R * 0.34), fill=(255, 255, 255))
    d.ellipse((cx - R * 0.22, cy - R * 0.52, cx - R * 0.12, cy - R * 0.42), fill=(255, 255, 255))

    # 생동감 넘치는 핑크 홍조 (Cheek Blush)
    blush_color = (255, 175, 185)
    d.ellipse((cx - R * 0.54, cy + R * 0.14, cx - R * 0.26, cy + R * 0.28), fill=blush_color)
    d.ellipse((cx + R * 0.26, cy + R * 0.14, cx + R * 0.54, cy + R * 0.28), fill=blush_color)

    # 캐릭터별 헤어 / 액세서리 파츠 드로잉
    fp = fig["face"]
    if char_type == "A":
        # 주인공 A: 이마 스포츠 헤어밴드 (Red) + 단정한 짧은 앞머리
        hb_poly = [
            fp(-0.84, -0.44), fp(0.84, -0.44),
            fp(0.84, -0.16), fp(-0.84, -0.16)
        ]
        d.polygon(hb_poly, fill=(225, 45, 45), outline=outline_color)
        # 헤어밴드 화이트 스트라이프 라인
        d.line([fp(-0.80, -0.30), fp(0.80, -0.30)], fill=(255, 255, 255), width=4)
        # 짧은 앞머리 팁
        for bx in (-0.3, 0.05, 0.4):
            d.polygon([fp(bx - 0.1, -0.44), fp(bx, -0.64), fp(bx + 0.1, -0.44)], fill=outline_color)
    else:
        # 빌런 B: 얄미운 노란색/주황색 삐죽머리 파츠 (머리 위로 솟은 스파이크 헤어)
        spikes = [
            [fp(-0.42, -0.82), fp(-0.68, -1.35), fp(-0.12, -0.92)],
            [fp(-0.12, -0.92), fp(0.18, -1.52), fp(0.40, -0.86)],
            [fp(0.35, -0.86), fp(0.72, -1.34), fp(0.68, -0.70)],
            [fp(0.62, -0.70), fp(0.98, -1.02), fp(0.82, -0.46)],
        ]
        for sp in spikes:
            d.polygon(sp, fill=(255, 185, 15), outline=outline_color)

        # 빌런 B의 얄미운 콧수염 (매너온도 99도 진상 아저씨 시그니처)
        d.line([fp(0.16, 0.28), fp(0.36, 0.20), fp(0.56, 0.28)], fill=outline_color, width=5)

    _draw_face(d, fp, R, p, char_type, t)


def _draw_face(d: ImageDraw.ImageDraw, fp, R: float, p: Dict, char_type: str = "A", t: float = 0.0) -> None:
    lw = max(3, int(R * 0.085))
    black = (15, 15, 25)

    def circle(dx, dy, r, fill=None, outline=None, width=1):
        x, y = fp(dx, dy)
        rr = r * R
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill, outline=outline, width=width)

    def line(*pts, width=lw):
        d.line([fp(*q) for q in pts], fill=black, width=width, joint="curve")

    eyes = p["eyes"]

    # 빌런 B이면서 온전한 상태일 때 깐족 선글라스 착용
    if char_type == "B" and eyes not in ("wide", "X"):
        lens_left = [fp(0.04, -0.24), fp(0.34, -0.24), fp(0.30, -0.04), fp(0.08, -0.04)]
        lens_right = [fp(0.44, -0.24), fp(0.74, -0.24), fp(0.70, -0.04), fp(0.48, -0.04)]
        d.polygon(lens_left, fill=(20, 22, 28), outline=black)
        d.polygon(lens_right, fill=(20, 22, 28), outline=black)
        d.line([fp(0.32, -0.16), fp(0.46, -0.16)], fill=black, width=lw)
        # 선글라스 렌즈 반사광
        d.line([fp(0.10, -0.20), fp(0.24, -0.08)], fill=(255, 255, 255), width=2)
        d.line([fp(0.50, -0.20), fp(0.64, -0.08)], fill=(255, 255, 255), width=2)
    else:
        for ex, ey in ((0.12, -0.12), (0.58, -0.12)):
            if eyes == "dot":
                circle(ex, ey, 0.10, fill=black)
            elif eyes == "wide":
                # 눈알 돌출 충격
                circle(ex, ey, 0.24, fill=(255, 255, 255), outline=black, width=lw)
                circle(ex + 0.04, ey, 0.08, fill=black)
                # 눈알 충격 충혈 핏줄 라인
                d.line([fp(ex - 0.14, ey - 0.04), fp(ex - 0.06, ey - 0.02)], fill=(230, 40, 40), width=2)
            elif eyes == "X":
                line((ex - 0.12, ey - 0.12), (ex + 0.12, ey + 0.12))
                line((ex - 0.12, ey + 0.12), (ex + 0.12, ey - 0.12))
            elif eyes == "squint":
                line((ex - 0.13, ey), (ex + 0.13, ey), width=lw + 2)
            else:  # angry
                circle(ex, ey, 0.09, fill=black)

    # 눈썹 각도 연출
    if char_type == "A":
        if p["brow"] or eyes == "angry" or p["mouth"] == "smirk":
            # 굵고 단호한 주인공 각도 눈썹
            line((-0.06, -0.46), (0.32, -0.22), width=lw + 3)
            line((0.36, -0.22), (0.76, -0.46), width=lw + 3)
            # 참교육 번뜩이는 안광 (4각 반짝이 별)
            gx, gy = fp(0.66, -0.16)
            for r1, r2 in ((20, 6), (15, 4)):
                star_pts = [
                    (gx, gy - r1), (gx + r2, gy - r2), (gx + r1, gy), (gx + r2, gy + r2),
                    (gx, gy + r1), (gx - r2, gy + r2), (gx - r1, gy), (gx - r2, gy - r2)
                ]
                d.polygon(star_pts, fill=(255, 235, 40))
        elif eyes == "squint":
            # 황당할 때의 굵은 일자 눈썹
            line((-0.08, -0.28), (0.74, -0.28), width=lw + 2)
    elif char_type == "B" and (p["brow"] or eyes in ("wide", "X")):
        # 깐족대거나 당황할 때의 비대칭 눈썹
        line((-0.08, -0.44), (0.34, -0.22), width=lw + 2)
        line((0.36, -0.22), (0.78, -0.44), width=lw + 2)

    # 입 모양 (비열한 썩소, 턱 빠진 경악, 사이다 미소, 티키타카 개폐)
    mx, my = 0.36, 0.44
    mouth = p["mouth"]
    if mouth == "talk":
        x, y = fp(mx, my)
        w, h = 0.22 * R, (0.05 + 0.17 * p["mo"]) * R
        d.ellipse((x - w, y - h, x + w, y + h), fill=black)
        d.ellipse((x - w * 0.6, y + h * 0.1, x + w * 0.6, y + h * 0.9), fill=(225, 50, 50))  # 혀
    elif mouth == "O":
        # 턱 빠진 경악 (깊은 타원형 오픈 마우스)
        x, y = fp(mx, my + 0.04)
        d.ellipse((x - 0.22 * R, y - 0.30 * R, x + 0.22 * R, y + 0.30 * R), fill=black, outline=black, width=lw)
        d.ellipse((x - 0.14 * R, y + 0.05 * R, x + 0.14 * R, y + 0.25 * R), fill=(225, 50, 50))  # 혀
    elif mouth == "teeth":
        # 악물고 부들거리는 치아
        corners = [fp(mx - 0.3, my - 0.12), fp(mx + 0.3, my - 0.12), fp(mx + 0.3, my + 0.16), fp(mx - 0.3, my + 0.16)]
        d.polygon(corners, fill=(255, 255, 255), outline=black)
        d.line(corners + [corners[0]], fill=black, width=lw)
        d.line([fp(mx, my - 0.12), fp(mx, my + 0.16)], fill=black, width=max(2, lw // 2))
        d.line([fp(mx - 0.3, my + 0.02), fp(mx + 0.3, my + 0.02)], fill=black, width=max(2, lw // 2))
    elif mouth == "smirk":
        if char_type == "B":
            # 빌런 얄미운 '3' (ω) 비열한 썩소
            line((mx - 0.26, my + 0.06), (mx - 0.04, my + 0.10), (mx, my + 0.02), width=lw + 1)
            line((mx, my + 0.02), (mx + 0.04, my + 0.10), (mx + 0.26, my + 0.06), width=lw + 1)
            # 비열한 송곳니 포인트
            d.polygon([fp(mx + 0.08, my + 0.06), fp(mx + 0.16, my + 0.06), fp(mx + 0.12, my + 0.16)], fill=(255, 255, 255), outline=black)
        else:
            # 주인공 자신만만한 사이다 미소 (치아 하이라이트)
            line((mx - 0.26, my + 0.04), (mx + 0.12, my + 0.04), (mx + 0.38, my - 0.12), width=lw + 1)
            d.polygon([fp(mx - 0.05, my + 0.04), fp(mx + 0.18, my + 0.04), fp(mx + 0.25, my - 0.02)], fill=(255, 255, 255))
    elif mouth == "frown":
        line((mx - 0.26, my + 0.12), (mx, my), (mx + 0.26, my + 0.12), width=lw + 1)
    else:  # smile
        line((mx - 0.26, my), (mx, my + 0.12), (mx + 0.26, my), width=lw + 1)

    # 분노 / 참교육 / 패닉 시 이마의 애니메이션 분노 혈관 마크 (💢)
    if (char_type == "A" and (p["brow"] or eyes == "angry")) or (char_type == "B" and eyes in ("wide", "X")):
        ang_x, ang_y = fp(0.55 if char_type == "A" else -0.55, -0.60)
        ag = 14 + 3 * math.sin(t * 16)
        for sx, sy, a0 in ((1, 1, 180), (-1, 1, 270), (-1, -1, 0), (1, -1, 90)):
            cx_ang, cy_ang = ang_x + sx * ag, ang_y + sy * ag
            d.arc((cx_ang - ag, cy_ang - ag, cx_ang + ag, cy_ang + ag), a0, a0 + 90, fill=(220, 30, 30), width=6)


# =====================================================================
# 5. 이펙트 (집중선 / 분노 / 땀 / 번개 / 폭발 / 별)
# =====================================================================
def fx_radial_lines(d, center, rnd, n, r_in, r_out, color):
    for _ in range(n):
        a = rnd.uniform(0, 2 * math.pi)
        r1, r2 = rnd.uniform(r_in, r_in * 1.5), rnd.uniform(r_out * 0.8, r_out * 1.2)
        w = rnd.uniform(0.012, 0.03)
        p0 = (center[0] + r1 * math.cos(a), center[1] + r1 * math.sin(a))
        p1 = (center[0] + r2 * math.cos(a - w), center[1] + r2 * math.sin(a - w))
        p2 = (center[0] + r2 * math.cos(a + w), center[1] + r2 * math.sin(a + w))
        d.polygon([p0, p1, p2], fill=color)


def fx_horizontal_lines(d, rnd, n, color):
    for _ in range(n):
        y = rnd.uniform(200, FLOOR_Y + 80)
        x = rnd.uniform(-100, W)
        d.rounded_rectangle((x, y, x + rnd.uniform(200, 520), y + rnd.uniform(5, 12)), radius=5, fill=color)


def fx_anger(d, x, y, t, scale=1.0):
    g = (24 + 6 * math.sin(t * 18)) * scale
    for sx, sy, a0 in ((1, 1, 180), (-1, 1, 270), (-1, -1, 0), (1, -1, 90)):
        cx, cy = x + sx * g, y + sy * g
        d.arc((cx - g, cy - g, cx + g, cy + g), a0, a0 + 90, fill=(220, 30, 30), width=int(11 * scale))


def fx_sweat(d, x, y, R, t):
    for i, (ox, oy) in enumerate(((-1.25, -0.4), (1.25, -0.2), (-1.0, 0.5))):
        yy = y + oy * R + ((t * 160 + i * 25) % 70)
        xx = x + ox * R
        d.polygon([(xx, yy - 34), (xx - 17, yy), (xx + 17, yy)], fill=(100, 180, 255), outline=(20, 20, 40))
        d.ellipse((xx - 17, yy - 14, xx + 17, yy + 20), fill=(100, 180, 255), outline=(20, 20, 40), width=3)


def fx_lightning(d, x0, rnd):
    pts = [(x0 + rnd.uniform(-60, 60), y) for y in range(0, 760, 95)]
    d.line(pts, fill=(0, 0, 0), width=34, joint="curve")
    d.line(pts, fill=(255, 235, 40), width=18, joint="curve")


def fx_burst(d, x, y, r, text, font):
    for scale, color in ((1.08, (200, 30, 30)), (1.0, (255, 225, 30))):
        pts = []
        for i in range(24):
            rr = r * scale * (1.0 if i % 2 == 0 else 0.58)
            a = math.pi * i / 12
            pts.append((x + rr * math.cos(a), y + rr * math.sin(a)))
        d.polygon(pts, fill=color)
    if text:
        d.text((x, y), text, font=font, fill=(200, 30, 30), anchor="mm", stroke_width=6, stroke_fill=(255, 255, 255))


def fx_stars(d, x, y, R, t):
    for i in range(3):
        a = t * 5 + i * 2 * math.pi / 3
        sx, sy = x + math.cos(a) * R * 1.1, y - R * 1.15 + math.sin(a) * R * 0.25
        pts = []
        for k in range(10):
            rr = 20 if k % 2 == 0 else 8
            ang = math.pi * k / 5 - math.pi / 2
            pts.append((sx + rr * math.cos(ang), sy + rr * math.sin(ang)))
        d.polygon(pts, fill=(255, 220, 20), outline=(20, 20, 30))


def fx_dust(d, x, floor_y, t):
    for i in range(3):
        k = (t * 3 + i * 0.33) % 1.0
        r = 22 + 40 * k
        d.ellipse((x - 120 - 150 * k - r, floor_y - 20 - r, x - 120 - 150 * k + r, floor_y - 20 + r),
                  fill=(225 - int(40 * k), 215 - int(40 * k), 200 - int(40 * k)))


def draw_props(d: ImageDraw.ImageDraw, fig_a: Dict, fig_b: Dict, scene_text: str,
               pose_a: str, pose_b: str, t: float, setting_type: str = "") -> None:
    text = (scene_text or "").lower()

    # 1. 📱 스마트폰 소품 (A가 초반 거래 씬에서 손에 쥐고 확인하거나 통화/채팅하는 연출)
    has_phone_context = any(k in text for k in ["스마트폰", "폰", "연락", "당근", "거래", "채팅", "상자", "쿨거래", "매너온도"])
    is_hero_holding_phone = has_phone_context and not any(k in text for k in ["투하", "본부", "무전"])
    if is_hero_holding_phone:
        # 주인공 A의 오른손(앞손) 좌표
        px, py = fig_a["arms"][1][2]
        if pose_a not in ("crushed", "fall"):
            # 스마트폰 본체 (최신형 메탈릭 딥블루 바디 + 얇은 베젤)
            d.rounded_rectangle((px - 16, py - 28, px + 16, py + 28), radius=6,
                                fill=(24, 38, 64), outline=(180, 205, 235), width=2)
            # 밝은 터치스크린 디스플레이
            d.rectangle((px - 13, py - 23, px + 13, py + 21), fill=(75, 195, 255))
            # 채팅 말풍선 UI 라인
            d.line([(px - 8, py - 14), (px + 8, py - 14)], fill=(255, 255, 255), width=2)
            d.line([(px - 8, py - 6), (px + 4, py - 6)], fill=(255, 255, 255), width=2)
            # 후면 카메라 3구 렌즈 포인트
            d.ellipse((px + 7, py - 24, px + 11, py - 20), fill=(20, 22, 28))

    # 2. ✉️ 얄팍한 돈봉투 소품 (빌런 B가 5만원, 차비, 네고 등을 핑계 대며 뻔뻔하게 흔드는 연출)
    has_envelope = any(k in text for k in ["봉투", "오만 원", "5만원", "오만원", "차비", "네고", "깎아", "현금", "얇은"])
    if has_envelope and pose_b not in ("crushed", "fall"):
        # 빌런 B의 왼손(주인공을 향해 내미는 앞손) 좌표
        ex, ey = fig_b["arms"][0][2]
        # 얄밉게 봉투를 파르르 흔드는 애니메이션
        env_wobble = math.sin(t * 16) * 6
        ey_wob = ey + env_wobble
        ex_wob = ex - 10

        # 노란색/연갈색의 얄팍한 현금 봉투 바디
        ew, eh = 62, 36
        env_box = (ex_wob - ew // 2, ey_wob - eh // 2, ex_wob + ew // 2, ey_wob + eh // 2)
        d.rounded_rectangle(env_box, radius=4, fill=(255, 232, 135), outline=(130, 95, 30), width=2)
        # 봉투 덮개 삼각 플랩 라인
        d.polygon([(env_box[0], env_box[1]), (ex_wob, ey_wob + 4), (env_box[2], env_box[1])],
                  fill=(245, 220, 115), outline=(130, 95, 30))
        # 봉투 중앙에 선명하게 새겨진 원화 기호(₩)
        d.text((ex_wob, ey_wob + 8), "₩", font=get_font(22), fill=(195, 40, 40), anchor="mm")
        # 얄팍함을 강조하는 "50,000" 초미니 텍스트
        d.text((ex_wob + 16, ey_wob + 8), "5만", font=get_font(12), fill=(100, 70, 20), anchor="mm")

    # 3. 📻 전술 무전기 소품 (주인공 A가 씬 5 등에서 본부에 10톤 당근 투하를 요청하는 클라이맥스 연출)
    has_radio = any(k in text for k in ["본부", "투하", "요망", "무전", "좌표", "출동", "진상 발견"])
    if has_radio and pose_a not in ("crushed", "fall"):
        # 주인공 A의 오른손(앞손) 좌표에 무전기 정확히 결합
        rx, ry = fig_a["arms"][1][2]

        # 무전기 본체 (택티컬 매트 블랙 바디)
        d.rounded_rectangle((rx - 15, ry - 32, rx + 15, ry + 32), radius=6,
                            fill=(32, 35, 44), outline=(15, 18, 24), width=3)
        # 스피커 그릴 슬롯 (가로 3줄)
        for gy in (-12, -4, 4):
            d.line([(rx - 9, ry + gy), (rx + 9, ry + gy)], fill=(18, 20, 26), width=2)
        # 볼륨 노브 및 안테나
        d.rectangle((rx + 5, ry - 38, rx + 12, ry - 32), fill=(55, 60, 72))
        ant_top = ry - 105
        d.line([(rx - 7, ry - 32), (rx - 12, ant_top)], fill=(20, 22, 28), width=5)
        # 안테나 끝단 송신 LED (Red)
        d.ellipse((rx - 15, ant_top - 4, rx - 9, ant_top + 2), fill=(255, 50, 50))

        # 안테나 끝에서 뿜어져 나오는 번쩍이는 전파 스파크 (⚡ 이펙트)
        spark_pts1 = [(rx - 12, ant_top), (rx - 32, ant_top - 18), (rx - 20, ant_top - 28)]
        spark_pts2 = [(rx - 12, ant_top), (rx + 16, ant_top - 16), (rx + 6, ant_top - 32)]
        d.line(spark_pts1, fill=(255, 235, 30), width=4, joint="curve")
        d.line(spark_pts2, fill=(255, 235, 30), width=4, joint="curve")
        d.text((rx + 24, ant_top - 20), "⚡", font=get_font(28), fill=(255, 225, 0), anchor="mm")

    # 4. 맞춤형 참교육 소품 (황금 VIP 카드, 10T 강철 당근, 20KG 바벨, 100T 참교육 철퇴/무게추)
    # 실제 빌런 B가 깔려 쓰러지는 포즈일 때만 압사 소품이 등장하도록 엄격히 제한
    is_climax_crush = (pose_b in ("crushed", "fall") or (pose_b == "hit" and any(k in text for k in [
        "짓눌", "깔려", "납작", "개구리", "살려", "사람 살려", "으아악"
    ])))

    has_convenience_theme = (setting_type == "convenience_store") or any(k in text for k in ["편의점", "카드", "적립", "포스기", "알바", "골드"])
    has_barbell_theme = any(k in text for k in [
        "바벨", "원판", "스쿼트", "랙", "덤벨", "벤치", "쇠질", "무반동", "세트", "중량"
    ])
    has_carrot_theme = any(k in text for k in ["당근", "중고", "네고", "매너온도"])
    has_parking_theme = (setting_type == "parking_road") or any(k in text for k in [
        "주차", "견인", "렉카", "차량", "밀착", "두 칸", "외제차", "문틈", "0cm"
    ])

    if is_climax_crush:
        bx = fig_b["hip"][0] + 15
        by = FLOOR_Y - 45
        if has_convenience_theme:
            # [거대 100TON 황금 참교육 VIP 적립 카드 슬랩스틱 그래픽]
            card_w, card_h = 360, 230
            card_x0, card_y0 = bx - card_w // 2, FLOOR_Y - card_h - 20
            card_x1, card_y1 = bx + card_w // 2, FLOOR_Y - 20
            d.rounded_rectangle((card_x0 + 8, card_y0 + 8, card_x1 + 8, card_y1 + 8), radius=16, fill=(15, 18, 25))
            d.rounded_rectangle((card_x0, card_y0, card_x1, card_y1), radius=16, fill=(245, 190, 35), outline=(255, 240, 160), width=5)
            d.rounded_rectangle((card_x0 + 30, card_y0 + 35, card_x0 + 85, card_y0 + 75), radius=6, fill=(215, 160, 20), outline=(130, 95, 10), width=2)
            d.rectangle((card_x0, card_y0 + 95, card_x1, card_y0 + 130), fill=(40, 42, 50))
            d.text((bx + 30, card_y0 + 55), "VIP 참교육", font=get_font(28), fill=(30, 25, 15), anchor="mm")
            d.text((bx, card_y0 + 112), "100 TON CRUSH", font=get_font(24), fill=(255, 215, 0), anchor="mm")
            d.text((bx, card_y0 + 175), "무한 적립 완료", font=get_font(30), fill=(180, 25, 25), anchor="mm")
            fx_dust(d, bx, FLOOR_Y, t)
        elif has_carrot_theme or "당근" in text:
            # [10T 강철 당근 참교육 슬랩스틱 그래픽]
            leaf_base_y = FLOOR_Y - 370
            # 초록색 당근 잎사귀 3개
            d.polygon([(bx - 20, leaf_base_y), (bx, leaf_base_y - 95), (bx + 20, leaf_base_y)], fill=(40, 180, 50), outline=(15, 80, 25))
            d.polygon([(bx - 40, leaf_base_y + 12), (bx - 95, leaf_base_y - 70), (bx - 10, leaf_base_y)], fill=(35, 160, 45), outline=(15, 80, 25))
            d.polygon([(bx + 10, leaf_base_y), (bx + 95, leaf_base_y - 70), (bx + 40, leaf_base_y + 12)], fill=(35, 160, 45), outline=(15, 80, 25))
            # 주황색 거대 원뿔 당근 몸통
            carrot_poly = [
                (bx - 150, leaf_base_y), (bx + 150, leaf_base_y),
                (bx + 42, FLOOR_Y - 20), (bx - 42, FLOOR_Y - 20)
            ]
            d.polygon(carrot_poly, fill=(255, 115, 20), outline=(180, 50, 10))
            d.ellipse((bx - 150, leaf_base_y - 25, bx + 150, leaf_base_y + 25), fill=(255, 125, 25), outline=(180, 50, 10), width=4)
            # 당근 가로 주름 디테일 라인
            d.line([(bx - 80, FLOOR_Y - 290), (bx - 10, FLOOR_Y - 290)], fill=(210, 80, 10), width=5)
            d.line([(bx + 15, FLOOR_Y - 210), (bx + 85, FLOOR_Y - 210)], fill=(210, 80, 10), width=5)
            d.line([(bx - 50, FLOOR_Y - 130), (bx + 30, FLOOR_Y - 130)], fill=(210, 80, 10), width=5)
            # 전면에 굵은 강철 플레이트 및 '10 TON' 텍스트
            d.rounded_rectangle((bx - 110, FLOOR_Y - 275, bx + 110, FLOOR_Y - 215), radius=16, fill=(35, 38, 48), outline=(240, 240, 250), width=4)
            d.text((bx, FLOOR_Y - 245), "10 TON", font=get_font(44), fill=(255, 220, 0), anchor="mm")
            d.text((bx, FLOOR_Y - 70), "강철 당근 압사", font=get_font(24), fill=(255, 255, 255), anchor="mm")
            fx_dust(d, bx, FLOOR_Y, t)
        elif has_barbell_theme:
            # [20KG 헬스장 바벨 참교육]
            bar_w = 480
            d.rectangle((bx - bar_w // 2, by - 9, bx + bar_w // 2, by + 9), fill=(180, 185, 195), outline=(15, 15, 25), width=3)
            font_plate = get_font(22)
            for side in (-1, 1):
                base_x = bx + side * (bar_w // 2 - 70)
                for stack in range(3):
                    px = base_x + side * (stack * 24)
                    d.rounded_rectangle((px - 12, by - 95, px + 12, by + 95), radius=8, fill=(30, 30, 40), outline=(10, 10, 20), width=3)
                    d.text((px, by), "20KG", font=font_plate, fill=(255, 220, 0), anchor="mm")
            fx_dust(d, bx, FLOOR_Y, t)
        elif has_parking_theme:
            # [100TON 강제 견인 특수 휠클램프 & 초대형 렉카 타이어 참교육]
            # 초대형 트럭 타이어
            d.ellipse((bx - 160, FLOOR_Y - 320, bx + 160, FLOOR_Y - 20), fill=(20, 22, 28), outline=(10, 12, 15), width=8)
            d.ellipse((bx - 110, FLOOR_Y - 270, bx + 110, FLOOR_Y - 70), fill=(65, 72, 85), outline=(220, 225, 235), width=6)
            # 옐로우 강철 휠클램프 바디 (바퀴 잠금 장치)
            clamp_poly = [
                (bx - 180, FLOOR_Y - 200), (bx + 180, FLOOR_Y - 200),
                (bx + 140, FLOOR_Y - 130), (bx - 140, FLOOR_Y - 130)
            ]
            d.polygon(clamp_poly, fill=(245, 180, 25), outline=(15, 18, 22), width=4)
            d.rounded_rectangle((bx - 150, FLOOR_Y - 240, bx + 150, FLOOR_Y - 170), radius=12, fill=(25, 28, 35), outline=(245, 200, 30), width=4)
            d.text((bx, FLOOR_Y - 205), "100 TON 견인", font=get_font(42), fill=(255, 220, 0), anchor="mm")
            d.text((bx, FLOOR_Y - 95), "강제 견인 완료", font=get_font(28), fill=(255, 255, 255), anchor="mm")
            fx_dust(d, bx, FLOOR_Y, t)
        else:
            # [100T 참교육 거대 강철 무게추]
            d.ellipse((bx - 55, FLOOR_Y - 370, bx + 55, FLOOR_Y - 260), outline=(180, 185, 195), width=18)
            weight_poly = [
                (bx - 130, FLOOR_Y - 280), (bx + 130, FLOOR_Y - 280),
                (bx + 190, FLOOR_Y - 20), (bx - 190, FLOOR_Y - 20)
            ]
            d.polygon(weight_poly, fill=(40, 42, 52), outline=(15, 15, 25))
            d.rounded_rectangle((bx - 135, FLOOR_Y - 215, bx + 135, FLOOR_Y - 145), radius=14, fill=(25, 27, 35), outline=(220, 220, 230), width=4)
            d.text((bx, FLOOR_Y - 180), "100 TON", font=get_font(48), fill=(255, 215, 0), anchor="mm")
            d.text((bx, FLOOR_Y - 80), "참 교 육", font=get_font(34), fill=(255, 65, 65), anchor="mm")
            fx_dust(d, bx, FLOOR_Y, t)
    elif has_barbell_theme:
        font_plate = get_font(22)
        if pose_a in ("punch", "angry", "grab", "retribution"):
            # 주인공이 분노/참교육 상태에서 어깨나 손에 장착한 고중량 바벨
            bx = fig_a["shoulder"][0] + 40
            by = fig_a["shoulder"][1] - 40
            bar_w = 420
            d.rectangle((bx - bar_w // 2, by - 7, bx + bar_w // 2, by + 7), fill=(185, 190, 200), outline=(15, 15, 25), width=3)
            for side in (-1, 1):
                base_x = bx + side * (bar_w // 2 - 50)
                for stack in range(2):
                    px = base_x + side * (stack * 20)
                    d.rounded_rectangle((px - 10, by - 82, px + 10, by + 82), radius=6, fill=(30, 30, 40), outline=(10, 10, 20), width=3)
                    d.text((px, by), "20KG", font=font_plate, fill=(255, 220, 0), anchor="mm")
        else:
            # 기본 대화 구도: 배경 스쿼트 랙에 묵직하게 거치된 20KG 바벨
            rack_x = 540
            rack_y = FLOOR_Y - 260
            d.line([(rack_x - 140, rack_y - 60), (rack_x - 140, FLOOR_Y)], fill=(70, 75, 85), width=16)
            d.line([(rack_x + 140, rack_y - 60), (rack_x + 140, FLOOR_Y)], fill=(70, 75, 85), width=16)
            bar_w = 460
            d.rectangle((rack_x - bar_w // 2, rack_y - 7, rack_x + bar_w // 2, rack_y + 7), fill=(180, 185, 195), outline=(15, 15, 25), width=3)
            for side in (-1, 1):
                px = rack_x + side * (bar_w // 2 - 45)
                d.rounded_rectangle((px - 11, rack_y - 80, px + 11, rack_y + 80), radius=6, fill=(35, 35, 45), outline=(10, 10, 20), width=3)
                d.text((px, rack_y), "20KG", font=font_plate, fill=(255, 215, 0), anchor="mm")


# =====================================================================
# 6. 텍스트 유틸 / 배경 / 오버레이
# =====================================================================
def wrap_text(text: str, font, maxw: float) -> List[str]:
    lines: List[str] = []
    cur = ""
    for ch in (text or "").replace("\n", " "):
        if font.getlength(cur + ch) <= maxw:
            cur += ch
            continue
        if ch == " ":
            lines.append(cur.strip())
            cur = ""
            continue
        sp = cur.rfind(" ")
        if sp > 0 and len(cur) - sp <= 8:
            lines.append(cur[:sp].strip())
            cur = cur[sp + 1:] + ch
        else:
            lines.append(cur)
            cur = ch
    if cur.strip():
        lines.append(cur.strip())
    return [ln for ln in lines if ln]


# =====================================================================
# 6. 장소 키워드 자동 감지 엔진 & 배경 프리셋 벡터 드로잉
# =====================================================================
def detect_setting(setting_text: str = "", title: str = "", scene_texts: Optional[List[str]] = None) -> str:
    """대본의 배경 설정, 제목, 씬 텍스트를 종합 분석하여 장소 핵심 키워드 자동 감지."""
    combined = f"{setting_text} {title} " + " ".join(scene_texts or [])
    text = combined.lower()

    # 1. 편의점 / 매장 서비스 (카운터, 알바, POS기, 진열대 등)
    convenience_keywords = [
        "편의점", "알바", "카운터", "포스기", "단말기", "계산대", "바코드", "담배", 
        "삼각김밥", "적립", "영수증", "교대", "cu", "gs25", "세븐일레븐", "이마트24", 
        "편의점손님", "편의점알바", "폐기", "라면온수기"
    ]
    convenience_score = sum(1 for k in convenience_keywords if k in text)

    # 2. 헬스장 / 피트니스 (바벨, 덤벨, 랙, 쇠질 등)
    gym_keywords = [
        "헬스장", "헬스", "쇠질", "바벨", "덤벨", "스쿼트", "벤치프레스", "벤치", 
        "원판", "득근", "무반동", "세트독점", "세트 사이", "기구 독점", "pt", 
        "트레이너", "인바디", "헬창", "스트랩"
    ]
    gym_score = sum(1 for k in gym_keywords if k in text)

    # 3. 주차장 / 도로 / 교통 (주차선, 아스팔트, 차단기 등)
    parking_keywords = [
        "주차장", "주차선", "문콕", "칼치기", "아스팔트", "차단기", "골목", "보복운전", 
        "난폭운전", "신호위반", "횡단보도", "차도", "접촉사고", "주차빌런", "2칸주차", "주차",
        "두 칸", "두칸", "문틈", "0cm", "밀착", "차량", "자동차", "외제차", "비싼 차",
        "긁힐까", "견인", "렉카", "크레인", "봉쇄", "주차구역", "카스토퍼", "단속반", "휠클램프"
    ]
    parking_score = sum(1 for k in parking_keywords if k in text)

    # 4. 당근마켓 / 중고 직거래 / 지하철역 앞 (단순 '역대급' 등 부분일치 방지)
    subway_keywords = [
        "당근마켓", "당근", "중고거래", "네고", "직거래", "지하철역", "4번출구", 
        "출구 앞", "매너온도", "쿨거래", "차비"
    ]
    subway_score = sum(1 for k in subway_keywords if k in text)

    # 5. 직장 / 사무실 (파티션, 회의실, 복사기 등)
    office_keywords = [
        "사무실", "직장", "회사", "부장", "팀장", "대리", "사원", "야근", "탕비실", 
        "복사기", "회의실", "파티션", "월급", "출근", "퇴근", "결재", "사내", "꼰대"
    ]
    office_score = sum(1 for k in office_keywords if k in text)

    # 6. 아파트 / 주거 실내 / 층간소음 (거실, 발망치, 우퍼 등)
    apartment_keywords = [
        "층간소음", "윗집", "아랫집", "발망치", "우퍼", "아파트", "거실", "천장", 
        "소파", "슬리퍼", "안방", "복도"
    ]
    apartment_score = sum(1 for k in apartment_keywords if k in text)

    scores = {
        "convenience_store": convenience_score,
        "gym": gym_score,
        "parking_road": parking_score,
        "subway_street": subway_score,
        "office": office_score,
        "apartment": apartment_score,
    }

    best_setting = max(scores, key=scores.get)
    if scores[best_setting] > 0:
        return best_setting
    return "default_modern"


def resolve_visual_props(scene: SceneBeat, idx: int, total: int, setting_type: str, scene_text: str) -> List[str]:
    """씬 데이터의 visual_props 또는 텍스트 문맥을 분석하여 1:1 시각 오브젝트 확정."""
    raw_props = list(getattr(scene, "visual_props", []) or [])
    text = (scene_text or "").lower()

    is_parking = (setting_type == "parking_road") or any(k in text for k in [
        "주차", "두 칸", "두칸", "외제차", "밀착", "견인", "렉카", "차량", "자동차", "문틈", "0cm", "긁힐"
    ])

    if is_parking:
        has_tow = any("tow" in p or "견인" in p or "렉카" in p for p in raw_props) or any(k in text for k in ["견인", "렉카", "크레인", "단속", "끌려", "들려", "실려"])
        has_tight = any("tight" in p or "block" in p or "jam" in p or "밀착" in p for p in raw_props) or any(k in text for k in ["밀착", "0cm", "문틈", "봉쇄", "가로막", "바짝", "참교육 차"])
        has_double = any("double" in p or "luxury" in p or "두 칸" in p for p in raw_props) or any(k in text for k in ["두 칸", "두칸", "삐딱", "외제차", "비싼 차", "긁힐까", "주차선"])

        if has_tow or (idx >= max(4, total - 2) and not has_tight and not has_double):
            if "tow_truck_lifting" not in raw_props:
                raw_props.append("tow_truck_lifting")
        elif has_tight or (idx in (3, 4) and not has_double):
            if "tight_blocking_car" not in raw_props:
                raw_props.append("tight_blocking_car")
            if "door_jammed" not in raw_props:
                raw_props.append("door_jammed")
        else:
            if "luxury_car_double_parked" not in raw_props:
                raw_props.append("luxury_car_double_parked")

    return raw_props


def draw_parking_background_base(d: ImageDraw.ImageDraw) -> None:
    """지하 주차장 공통 인프라 벡터 드로잉 (조명, 덕트, 안내 띠, 안전 기둥, 아스팔트 주차 구획)."""
    # 1. 지하 주차장 벽면
    d.rectangle((0, 0, W, FLOOR_Y), fill=(38, 42, 50))

    # 2. 상단 공조 덕트 및 소방 배관
    d.rectangle((0, 0, W, 175), fill=(25, 28, 35))
    d.rectangle((80, 65, W - 80, 105), fill=(60, 66, 78))
    for vy in range(90, W - 80, 120):
        d.line([(vy, 65), (vy, 105)], fill=(40, 45, 55), width=3)
    # 스프링클러 배관 (Red)
    d.line([(0, 130), (W, 130)], fill=(195, 45, 45), width=6)
    for spx in (200, 540, 880):
        d.line([(spx, 130), (spx, 150)], fill=(195, 45, 45), width=4)
        d.ellipse((spx - 6, 148, spx + 6, 160), fill=(240, 180, 30))

    # 3. 천장 LED 조명 패널 및 투사광
    for lx in (270, 810):
        d.rounded_rectangle((lx - 110, 170, lx + 110, 190), radius=4, fill=(240, 245, 255), outline=(180, 200, 230), width=2)
        d.polygon([(lx - 100, 190), (lx + 100, 190), (lx + 220, 520), (lx - 220, 520)], fill=(48, 54, 66))

    # 4. 벽면 상부 녹색 안내 띠 (차량 높이에 가려지지 않도록 FLOOR_Y-470 ~ FLOOR_Y-380에 배치)
    guide_y0, guide_y1 = FLOOR_Y - 470, FLOOR_Y - 380
    d.rectangle((0, guide_y0, W, guide_y1), fill=(24, 115, 68))
    d.line([(0, guide_y0), (W, guide_y0)], fill=(255, 255, 255), width=3)
    d.line([(0, guide_y1), (W, guide_y1)], fill=(255, 255, 255), width=3)
    d.text((540, (guide_y0 + guide_y1) // 2), "[ 지하 2층 주차구역 B2-01 ~ B2-08 ]", font=get_font(30), fill=(245, 255, 245), anchor="mm")

    # 5. 콘크리트 안전 기둥 (좌측 벽면 끝 x: 10~95)
    d.rectangle((10, 180, 95, FLOOR_Y), fill=(52, 58, 68), outline=(22, 26, 32), width=3)
    for hy in range(FLOOR_Y - 300, FLOOR_Y, 36):
        d.polygon([(10, hy), (35, hy), (35, min(FLOOR_Y, hy + 18)), (10, min(FLOOR_Y, hy + 18))], fill=(245, 200, 25))
    d.rounded_rectangle((20, 360, 85, 425), radius=6, fill=(22, 26, 32), outline=(245, 200, 25), width=2)
    d.text((52, 392), "B2", font=get_font(28), fill=(255, 255, 255), anchor="mm")

    # 6. CCTV 카메라 (우측 상단)
    d.rectangle((960, 215, 1000, 235), fill=(40, 45, 55))
    d.polygon([(940, 230), (970, 220), (970, 250), (940, 240)], fill=(220, 225, 235), outline=(30, 35, 45))
    d.ellipse((935, 232, 943, 238), fill=(255, 50, 50))

    # 7. 아스팔트 바닥면 (FLOOR_Y ~ H)
    d.rectangle((0, FLOOR_Y, W, H), fill=(34, 36, 42))
    d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 14), fill=(60, 64, 75))

    # 8. 노란색 주차 구획선 (왼쪽 B2-01, 오른쪽 B2-02, 문제의 중앙 침범선 x: 540)
    d.line([(180, FLOOR_Y), (60, H)], fill=(245, 205, 30), width=14)
    d.line([(540, FLOOR_Y), (420, H)], fill=(245, 205, 30), width=16)
    d.line([(900, FLOOR_Y), (780, H)], fill=(245, 205, 30), width=14)

    # 9. 바닥 카스토퍼 (주차 멈춤턱)
    for cx in (360, 720):
        d.rounded_rectangle((cx - 85, FLOOR_Y + 100, cx + 85, FLOOR_Y + 128), radius=5, fill=(45, 48, 56), outline=(15, 18, 22), width=2)
        d.rectangle((cx - 55, FLOOR_Y + 106, cx - 25, FLOOR_Y + 122), fill=(245, 210, 35))
        d.rectangle((cx + 25, FLOOR_Y + 106, cx + 55, FLOOR_Y + 122), fill=(245, 210, 35))

    d.text((360, FLOOR_Y + 45), "B2-01", font=get_font(28), fill=(160, 165, 180), anchor="mm")
    d.text((720, FLOOR_Y + 45), "B2-02", font=get_font(28), fill=(160, 165, 180), anchor="mm")


def draw_luxury_car_double_parked(d: ImageDraw.ImageDraw, x_center: int = 540, y_bottom: int = FLOOR_Y + 10) -> None:
    """두 칸을 물고 삐딱하게 주차된 거대 럭셔리 세단/쿠페 빌런 차량."""
    w, h = 760, 270
    x0 = x_center - w // 2
    y0 = y_bottom - h

    # 1. 차체 밑 바닥 접지 그림자
    d.ellipse((x0 + 40, y_bottom - 25, x0 + w - 40, y_bottom + 35), fill=(15, 16, 20))

    # 2. 메인 차체 (메탈릭 다크 건메탈 바디)
    body_poly = [
        (x0 + 20, y0 + 170), (x0 + 10, y0 + 140), (x0 + 100, y0 + 115),
        (x0 + 260, y0 + 105), (x0 + 360, y0 + 25), (x0 + 580, y0 + 25),
        (x0 + 680, y0 + 105), (x0 + 745, y0 + 125), (x0 + 755, y0 + 160),
        (x0 + 735, y0 + 215), (x0 + 630, y0 + 215), (x0 + 530, y0 + 215),
        (x0 + 310, y0 + 215), (x0 + 210, y0 + 215), (x0 + 50, y0 + 215)
    ]
    d.polygon(body_poly, fill=(26, 30, 40), outline=(15, 18, 25))

    # 3. 차체 상단 메탈릭 하이라이트 레이어
    d.polygon([
        (x0 + 100, y0 + 115), (x0 + 260, y0 + 105), (x0 + 360, y0 + 25),
        (x0 + 580, y0 + 25), (x0 + 680, y0 + 105), (x0 + 745, y0 + 125),
        (x0 + 730, y0 + 138), (x0 + 260, y0 + 115), (x0 + 95, y0 + 125)
    ], fill=(42, 48, 62))

    # 4. 틴팅 글래스 윈도우 & 반사광
    win_poly = [
        (x0 + 275, y0 + 102), (x0 + 365, y0 + 36),
        (x0 + 570, y0 + 36), (x0 + 665, y0 + 102)
    ]
    d.polygon(win_poly, fill=(16, 22, 30), outline=(65, 75, 95), width=3)
    d.line([(x0 + 465, y0 + 36), (x0 + 465, y0 + 102)], fill=(26, 30, 40), width=8) # B필러
    d.polygon([(x0 + 340, y0 + 102), (x0 + 410, y0 + 36), (x0 + 440, y0 + 36), (x0 + 370, y0 + 102)], fill=(65, 120, 180))
    d.polygon([(x0 + 490, y0 + 102), (x0 + 530, y0 + 36), (x0 + 550, y0 + 36), (x0 + 510, y0 + 102)], fill=(65, 120, 180))

    # 5. 크롬 캐릭터 라인 & 도어 캐치 핸들
    d.line([(x0 + 110, y0 + 132), (x0 + 720, y0 + 132)], fill=(70, 80, 100), width=3)
    d.line([(x0 + 275, y0 + 102), (x0 + 275, y0 + 205)], fill=(18, 22, 30), width=3)
    d.line([(x0 + 465, y0 + 102), (x0 + 465, y0 + 205)], fill=(18, 22, 30), width=3)
    d.rounded_rectangle((x0 + 340, y0 + 140, x0 + 390, y0 + 150), radius=3, fill=(225, 230, 240), outline=(50, 55, 65))
    d.rounded_rectangle((x0 + 490, y0 + 140, x0 + 540, y0 + 150), radius=3, fill=(225, 230, 240), outline=(50, 55, 65))

    # 6. 사이드 미러 (접지 않고 활짝 펼쳐둠)
    d.polygon([(x0 + 250, y0 + 95), (x0 + 290, y0 + 90), (x0 + 285, y0 + 115), (x0 + 255, y0 + 112)], fill=(26, 30, 40), outline=(80, 90, 110), width=2)

    # 7. 헤드라이트 (아이스블루 LED) & 테일램프 (딥레드 LED)
    d.polygon([(x0 + 15, y0 + 142), (x0 + 90, y0 + 125), (x0 + 75, y0 + 160), (x0 + 22, y0 + 165)], fill=(210, 245, 255), outline=(130, 190, 240), width=2)
    d.polygon([(x0 + 740, y0 + 132), (x0 + 752, y0 + 148), (x0 + 740, y0 + 162), (x0 + 695, y0 + 145)], fill=(240, 35, 45), outline=(140, 15, 20), width=2)

    # 8. 대형 스포츠 휠 & 타이어 (2개)
    for wx in (x0 + 160, x0 + 580):
        wy = y_bottom - 45
        R_tire = 62
        d.ellipse((wx - R_tire, wy - R_tire, wx + R_tire, wy + R_tire), fill=(18, 20, 24), outline=(10, 12, 15), width=4)
        d.arc((wx - R_tire + 14, wy - R_tire + 14, wx + R_tire - 14, wy + R_tire - 14), start=30, end=110, fill=(235, 30, 30), width=10)
        d.ellipse((wx - R_tire + 16, wy - R_tire + 16, wx + R_tire - 16, wy + R_tire - 16), fill=(65, 72, 85), outline=(190, 200, 215), width=3)
        for sp in range(5):
            ang = sp * (2 * math.pi / 5)
            sx = wx + int(math.cos(ang) * (R_tire - 20))
            sy = wy + int(math.sin(ang) * (R_tire - 20))
            d.line([(wx, wy), (sx, sy)], fill=(195, 205, 220), width=5)
        d.ellipse((wx - 12, wy - 12, wx + 12, wy + 12), fill=(30, 35, 45), outline=(235, 240, 250), width=2)

    # 9. 골드 VIP 번호판 [ 77가 7777 ]
    d.rounded_rectangle((x0 + 35, y0 + 175, x0 + 135, y0 + 205), radius=4, fill=(245, 245, 250), outline=(20, 22, 28), width=2)
    d.text((x0 + 85, y0 + 190), "77가 7777", font=get_font(18), fill=(20, 22, 28), anchor="mm")

    # 10. [핵심 연출: 두 칸 주차 침범 시각 태그 및 경고선]
    tag_x, tag_y = x_center, y0 - 30
    d.polygon([(tag_x - 12, tag_y + 36), (tag_x + 12, tag_y + 36), (tag_x, tag_y + 52)], fill=(245, 40, 40))
    d.rounded_rectangle((tag_x - 160, tag_y - 20, tag_x + 160, tag_y + 36), radius=10, fill=(245, 40, 40), outline=(255, 235, 235), width=3)
    d.text((tag_x, tag_y + 8), "[ 2칸 독점 무개념 주차 ]", font=get_font(26), fill=(255, 255, 255), anchor="mm")


def draw_tight_blocking_car(d: ImageDraw.ImageDraw, x_center: int = 540, y_bottom: int = FLOOR_Y + 10) -> None:
    """문틈 0cm 밀착 봉쇄 참교육 씬 (빌런차 운전석 문을 0cm로 막아선 정의의 경차)."""
    # 1. 우측: 갇혀버린 빌런 럭셔리 세단 (x: 535 ~ 1030)
    vx0, vy_bot = 535, y_bottom
    vw, vh = 510, 260
    vy0 = vy_bot - vh
    d.ellipse((vx0 + 30, vy_bot - 20, vx0 + vw - 20, vy_bot + 30), fill=(15, 16, 20))
    v_poly = [
        (vx0, vy0 + 100), (vx0 + 120, vy0 + 25), (vx0 + 360, vy0 + 25),
        (vx0 + 460, vy0 + 105), (vx0 + 510, vy0 + 130), (vx0 + 510, vy0 + 180),
        (vx0 + 490, vy0 + 210), (vx0 + 410, vy0 + 210), (vx0 + 320, vy0 + 210),
        (vx0 + 140, vy0 + 210), (vx0 + 50, vy0 + 210), (vx0, vy0 + 210)
    ]
    d.polygon(v_poly, fill=(28, 32, 42), outline=(15, 18, 25))
    d.polygon([(vx0 + 10, vy0 + 100), (vx0 + 125, vy0 + 35), (vx0 + 350, vy0 + 35), (vx0 + 440, vy0 + 100)], fill=(16, 20, 28), outline=(60, 70, 90), width=2)
    d.rounded_rectangle((vx0 + 70, vy0 + 135, vx0 + 115, vy0 + 145), radius=3, fill=(200, 205, 215))
    for wx in (vx0 + 90, vx0 + 370):
        d.ellipse((wx - 52, vy_bot - 45 - 52, wx + 52, vy_bot - 45 + 52), fill=(18, 20, 24), outline=(8, 10, 12), width=4)
        d.ellipse((wx - 34, vy_bot - 45 - 34, wx + 34, vy_bot - 45 + 34), fill=(60, 68, 80), outline=(180, 190, 205), width=3)
    # 접혀버린 운전석 사이드미러 (강제 폴딩)
    d.polygon([(vx0 + 5, vy0 + 95), (vx0 + 25, vy0 + 85), (vx0 + 20, vy0 + 110), (vx0 + 2, vy0 + 108)], fill=(20, 24, 32), outline=(90, 100, 120))
    d.text((vx0 + 15, vy0 + 70), "접힘", font=get_font(18), fill=(245, 80, 80), anchor="mm")

    # 2. 좌측: 0cm로 밀착 가로막은 참교육 경차 (x: 50 ~ 535) - 박스형 경차 레이/캐스퍼
    rx0, ry_bot = 50, y_bottom
    rw, rh = 485, 275
    ry0 = ry_bot - rh
    d.ellipse((rx0 + 20, ry_bot - 20, rx0 + rw - 10, ry_bot + 30), fill=(15, 16, 20))
    r_poly = [
        (rx0 + 10, ry0 + 180), (rx0 + 20, ry0 + 120), (rx0 + 90, ry0 + 110),
        (rx0 + 140, ry0 + 20), (rx0 + 440, ry0 + 20), (rx0 + rw, ry0 + 50),
        (rx0 + rw, ry0 + 215), (rx0 + 410, ry0 + 215), (rx0 + 320, ry0 + 215),
        (rx0 + 170, ry0 + 215), (rx0 + 80, ry0 + 215), (rx0 + 10, ry0 + 215)
    ]
    d.polygon(r_poly, fill=(245, 248, 252), outline=(30, 35, 45), width=3)
    d.polygon([(rx0 + 140, ry0 + 20), (rx0 + 440, ry0 + 20), (rx0 + rw, ry0 + 50), (rx0 + rw, ry0 + 65), (rx0 + 130, ry0 + 65)], fill=(255, 105, 30))
    d.polygon([(rx0 + 100, ry0 + 110), (rx0 + 155, ry0 + 35), (rx0 + 430, ry0 + 35), (rx0 + 465, ry0 + 110)], fill=(40, 140, 200), outline=(25, 35, 45), width=2)
    d.line([(rx0 + 285, ry0 + 35), (rx0 + 285, ry0 + 110)], fill=(245, 248, 252), width=6)
    for wx in (rx0 + 125, rx0 + 365):
        d.ellipse((wx - 48, ry_bot - 42 - 48, wx + 48, ry_bot - 42 + 48), fill=(22, 24, 28), outline=(10, 12, 14), width=3)
        d.ellipse((wx - 30, ry_bot - 42 - 30, wx + 30, ry_bot - 42 + 30), fill=(240, 245, 250), outline=(255, 105, 30), width=3)
    d.rounded_rectangle((rx0 + 25, ry0 + 155, rx0 + 105, ry0 + 185), radius=3, fill=(255, 255, 255), outline=(20, 22, 28), width=2)
    d.text((rx0 + 65, ry0 + 170), "00참 7777", font=get_font(16), fill=(20, 22, 28), anchor="mm")

    # 3. [핵심 연출: 0cm 문틈 밀착 봉쇄 시각 측정선 & 경고 그래픽]
    seam_x = 535
    for sy in range(vy0 - 20, vy_bot + 10, 16):
        d.line([(seam_x, sy), (seam_x, sy + 10)], fill=(255, 30, 30), width=4)
    d.text((seam_x, vy0 + 90), "💥", font=get_font(38), fill=(255, 220, 0), anchor="mm")
    d.text((seam_x, vy0 + 150), "⚡", font=get_font(34), fill=(255, 230, 50), anchor="mm")

    badge_y = vy0 - 45
    d.rounded_rectangle((seam_x - 170, badge_y - 25, seam_x + 170, badge_y + 35), radius=12, fill=(20, 24, 32), outline=(255, 45, 45), width=4)
    d.text((seam_x, badge_y + 5), "[ 문틈 간격 0.0 cm ]", font=get_font(28), fill=(255, 230, 50), anchor="mm")

    bot_badge_y = vy_bot + 45
    d.rounded_rectangle((seam_x - 170, bot_badge_y - 18, seam_x + 170, bot_badge_y + 22), radius=8, fill=(225, 30, 30), outline=(255, 255, 255), width=2)
    d.text((seam_x, bot_badge_y + 2), "[ 운전석 탑승 원천 봉쇄 ]", font=get_font(22), fill=(255, 255, 255), anchor="mm")


def draw_tow_truck_lifting(d: ImageDraw.ImageDraw, x_center: int = 540, y_bottom: int = FLOOR_Y + 10) -> None:
    """견인차(렉카)가 빌런 차량 앞바퀴를 공중으로 번쩍 들어 올리는 사이다 클라이맥스 씬."""
    # 1. 좌측: 대형 견인차 (Wrecker Tow Truck) - x: 40 ~ 460
    tx0, ty_bot = 40, y_bottom
    tw, th = 420, 310
    ty0 = ty_bot - th
    d.ellipse((tx0 + 20, ty_bot - 20, tx0 + tw + 60, ty_bot + 30), fill=(15, 16, 20))

    # 견인차 캡
    cab_poly = [
        (tx0, ty0 + 230), (tx0 + 10, ty0 + 140), (tx0 + 60, ty0 + 130),
        (tx0 + 100, ty0 + 60), (tx0 + 240, ty0 + 60), (tx0 + 250, ty0 + 150),
        (tx0 + 250, ty0 + 240), (tx0 + 190, ty0 + 240), (tx0 + 100, ty0 + 240), (tx0, ty0 + 240)
    ]
    d.polygon(cab_poly, fill=(245, 175, 25), outline=(20, 24, 30), width=4)
    d.polygon([(tx0 + 70, ty0 + 130), (tx0 + 105, ty0 + 75), (tx0 + 230, ty0 + 75), (tx0 + 235, ty0 + 130)], fill=(30, 45, 60), outline=(20, 24, 30), width=2)
    d.text((tx0 + 150, ty0 + 175), "견인 단속", font=get_font(22), fill=(20, 24, 30), anchor="mm")
    d.text((tx0 + 150, ty0 + 205), "SPECIAL TOW", font=get_font(14), fill=(60, 65, 75), anchor="mm")

    # 견인차 휠
    for wx in (tx0 + 75, tx0 + 270):
        d.ellipse((wx - 55, ty_bot - 45 - 55, wx + 55, ty_bot - 45 + 55), fill=(20, 22, 26), outline=(10, 12, 14), width=4)
        d.ellipse((wx - 35, ty_bot - 45 - 35, wx + 35, ty_bot - 45 + 35), fill=(80, 85, 95), outline=(220, 225, 235), width=4)

    # 견인차 상단 경광등 베이스
    siren_x, siren_y = tx0 + 170, ty0 + 45
    d.rounded_rectangle((siren_x - 30, siren_y - 18, siren_x + 30, siren_y + 12), radius=6, fill=(255, 60, 40), outline=(120, 20, 15), width=2)
    d.ellipse((siren_x - 45, siren_y - 30, siren_x + 45, siren_y + 25), fill=(255, 200, 50))

    # 2. 유압 크레인 붐 암
    boom_base = (tx0 + 250, ty0 + 160)
    boom_tip = (tx0 + 470, ty0 + 15)
    d.line([boom_base, boom_tip], fill=(70, 75, 88), width=24)
    d.line([boom_base, boom_tip], fill=(160, 170, 185), width=12)
    d.line([(boom_base[0] + 30, boom_base[1] + 25), (boom_tip[0] - 80, boom_tip[1] + 65)], fill=(225, 180, 30), width=14)
    d.ellipse((boom_tip[0] - 18, boom_tip[1] - 18, boom_tip[0] + 18, boom_tip[1] + 18), fill=(40, 45, 55), outline=(220, 225, 235), width=3)

    # 3. 강철 체인 & 언더리프트 크래들
    hook_point = (boom_tip[0] + 10, boom_tip[1] + 120)
    d.line([boom_tip, hook_point], fill=(210, 215, 225), width=5)
    for cy in range(int(boom_tip[1] + 20), int(hook_point[1]), 18):
        d.ellipse((boom_tip[0] + 5, cy, boom_tip[0] + 15, cy + 12), outline=(220, 225, 235), width=3)
    cradle_pt = (hook_point[0] + 50, ty_bot - 160)
    d.line([hook_point, cradle_pt], fill=(60, 65, 75), width=16)

    # 4. 공중으로 번쩍 들려 올라간 빌런 럭셔리 세단 (대각선 틸팅)
    cx0, cy_lift = cradle_pt[0] - 30, cradle_pt[1]
    tilted_poly = [
        (cx0, cy_lift), (cx0 + 20, cy_lift - 50), (cx0 + 130, cy_lift - 90),
        (cx0 + 220, cy_lift - 160), (cx0 + 390, cy_lift - 130), (cx0 + 470, cy_lift - 60),
        (cx0 + 560, cy_lift + 20), (cx0 + 550, cy_lift + 85),
        (cx0 + 460, ty_bot - 35), (cx0 + 380, ty_bot - 35),
        (cx0 + 140, cy_lift + 40), (cx0 + 40, cy_lift + 30)
    ]
    d.polygon(tilted_poly, fill=(28, 32, 42), outline=(15, 18, 25), width=3)
    d.polygon([
        (cx0 + 140, cy_lift - 85), (cx0 + 225, cy_lift - 150),
        (cx0 + 385, cy_lift - 122), (cx0 + 455, cy_lift - 65)
    ], fill=(16, 22, 30), outline=(60, 70, 85), width=2)

    # 공중에 뜬 앞바퀴 타이어
    front_wheel = (cx0 + 90, cy_lift + 15)
    d.ellipse((front_wheel[0] - 46, front_wheel[1] - 46, front_wheel[0] + 46, front_wheel[1] + 46), fill=(20, 22, 26), outline=(10, 12, 14), width=3)
    d.ellipse((front_wheel[0] - 28, front_wheel[1] - 28, front_wheel[0] + 28, front_wheel[1] + 28), fill=(70, 75, 85), outline=(190, 200, 215), width=3)

    # 지면에 닿은 뒷바퀴 & 스키드 마크
    rear_wheel = (cx0 + 420, ty_bot - 45)
    d.ellipse((rear_wheel[0] - 52, rear_wheel[1] - 52, rear_wheel[0] + 52, rear_wheel[1] + 52), fill=(18, 20, 24), outline=(8, 10, 12), width=4)
    d.ellipse((rear_wheel[0] - 32, rear_wheel[1] - 32, rear_wheel[0] + 32, rear_wheel[1] + 32), fill=(60, 65, 75), outline=(180, 190, 200), width=3)
    d.line([(rear_wheel[0] - 120, ty_bot), (rear_wheel[0] + 20, ty_bot)], fill=(12, 12, 16), width=10)
    for sm_x, sm_r in ((rear_wheel[0] - 40, 24), (rear_wheel[0] - 80, 32), (rear_wheel[0] - 130, 20)):
        d.ellipse((sm_x - sm_r, ty_bot - 10 - sm_r, sm_x + sm_r, ty_bot - 10 + sm_r), fill=(190, 195, 205))

    # 견인 경고 딱지 스티커
    sticker_box = (cx0 + 200, cy_lift - 120, cx0 + 310, cy_lift - 65)
    d.rectangle(sticker_box, fill=(245, 35, 35), outline=(255, 255, 255), width=2)
    d.text((cx0 + 255, cy_lift - 102), "강제 견인", font=get_font(20), fill=(255, 255, 255), anchor="mm")
    d.text((cx0 + 255, cy_lift - 80), "과태료 부과", font=get_font(14), fill=(255, 230, 70), anchor="mm")

    # 5. 상단 사이다 현수막 배너
    banner_y = ty0 - 30
    d.rounded_rectangle((W // 2 - 250, banner_y - 25, W // 2 + 250, banner_y + 35), radius=12, fill=(215, 25, 25), outline=(255, 220, 0), width=4)
    d.text((W // 2, banner_y + 5), "[ 불법주차 즉시 강제 견인 집행 ]", font=get_font(28), fill=(255, 245, 200), anchor="mm")


def draw_parking_lot_scene(d: ImageDraw.ImageDraw, scene_idx: int = 0, total_scenes: int = 7,
                           visual_props: Optional[List[str]] = None, scene_text: str = "") -> None:
    """주차장 테마 문맥 인식형 작화: 기본 배경 + 씬 데이터(visual_props) 1:1 차량 오브젝트 합성."""
    # 1. 공통 지하 주차장 인프라 드로잉
    draw_parking_background_base(d)

    # 2. 씬 데이터 및 visual_props에 따른 차량 합성
    props = visual_props or []
    text = (scene_text or "").lower()

    if "tow_truck_lifting" in props or (scene_idx >= max(4, total_scenes - 2) and any(k in text for k in ["견인", "렉카", "크레인", "끌려", "단속"])):
        draw_tow_truck_lifting(d, 540, FLOOR_Y + 10)
    elif "tight_blocking_car" in props or any(k in text for k in ["밀착", "0cm", "문틈", "봉쇄", "가로막", "바짝", "참교육 차"]):
        draw_tight_blocking_car(d, 540, FLOOR_Y + 10)
    else:
        # 씬 1~3 기본: 두 칸 물고 삐딱하게 주차된 빌런 차량
        draw_luxury_car_double_parked(d, 540, FLOOR_Y + 10)


def make_background(color, pose: str, scene_text: str = "", setting_type: str = "",
                    scene_idx: int = 0, total_scenes: int = 7, visual_props: Optional[List[str]] = None) -> Image.Image:
    """장소 키워드 프리셋에 따라 고품질 벡터/오브젝트 레이어가 합성된 완성형 무대 배경을 생성."""
    if not setting_type:
        setting_type = detect_setting(scene_text)
    img = Image.new("RGB", (W, H), color)
    d = ImageDraw.Draw(img)

    if setting_type == "convenience_store":
        # [1. 편의점 24시 내부 벡터 배경]
        # 천장 및 LED 라이트 패널
        d.rectangle((0, 0, W, 220), fill=(35, 38, 48))
        for lx in (220, 540, 860):
            d.rounded_rectangle((lx - 120, 80, lx + 120, 110), radius=6, fill=(245, 250, 255), outline=(200, 225, 255), width=2)
            d.polygon([(lx - 150, 110), (lx + 150, 110), (lx + 240, 360), (lx - 240, 360)], fill=(48, 55, 72))

        # 바닥: 유광 편의점 타일
        d.rectangle((0, FLOOR_Y, W, H), fill=(230, 234, 242))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 16), fill=(60, 65, 78))
        tile_w, tile_h = 108, 68
        for r, ty in enumerate(range(FLOOR_Y + 16, H, tile_h)):
            d.line([(0, ty), (W, ty)], fill=(205, 210, 222), width=3)
            shift = 54 if r % 2 == 1 else 0
            for tx in range(shift, W + 108, tile_w):
                d.line([(tx, ty), (tx, min(H, ty + tile_h))], fill=(205, 210, 222), width=3)

        # 좌측: 음료 쇼케이스 대형 냉장고 (x: 40 ~ 440, y: 220 ~ FLOOR_Y)
        d.rounded_rectangle((40, 220, 440, FLOOR_Y), radius=12, fill=(22, 32, 45), outline=(130, 145, 165), width=6)
        d.rectangle((44, 224, 436, 290), fill=(20, 130, 220)) # 브랜드 헤더
        d.rectangle((54, 300, 426, FLOOR_Y - 20), fill=(28, 55, 78)) # 내부 조명
        for sy in range(380, FLOOR_Y - 60, 180):
            d.rectangle((54, sy, 426, sy + 14), fill=(160, 175, 195))
            can_colors = [(235, 45, 45), (45, 180, 75), (255, 210, 30), (30, 130, 240), (255, 255, 255)]
            for cx in range(70, 410, 38):
                cc = can_colors[(cx // 38) % len(can_colors)]
                d.rounded_rectangle((cx, sy - 85, cx + 28, sy), radius=4, fill=cc, outline=(20, 30, 40), width=2)
        # 유리 반사광
        d.polygon([(60, 300), (130, 300), (70, FLOOR_Y - 20), (54, FLOOR_Y - 20)], fill=(50, 85, 115))
        d.polygon([(250, 300), (320, 300), (190, FLOOR_Y - 20), (120, FLOOR_Y - 20)], fill=(50, 85, 115))

        # 우측: 스낵 및 식품 진열대 (x: 520 ~ 1040, y: 240 ~ FLOOR_Y)
        d.rounded_rectangle((520, 240, 1040, FLOOR_Y), radius=10, fill=(35, 40, 52), outline=(100, 110, 130), width=4)
        d.rectangle((524, 244, 1036, 300), fill=(235, 110, 30))
        for sy in range(390, FLOOR_Y - 40, 200):
            d.rectangle((530, sy, 1030, sy + 16), fill=(140, 150, 168))
            snack_colors = [(255, 80, 80), (255, 180, 40), (80, 200, 120), (160, 80, 220), (255, 220, 50)]
            for sx in range(545, 1010, 55):
                sc = snack_colors[(sx // 55) % len(snack_colors)]
                d.rounded_rectangle((sx, sy - 110, sx + 44, sy), radius=8, fill=sc, outline=(30, 35, 45), width=2)
                d.line([(sx + 8, sy - 65), (sx + 36, sy - 65)], fill=(255, 255, 255), width=2)

        # 계산대 카운터 기본 바탕 (x: 180 ~ 560, y: 1040 ~ FLOOR_Y)
        d.rounded_rectangle((180, 1070, 560, FLOOR_Y), radius=8, fill=(32, 40, 55), outline=(90, 105, 130), width=4)
        d.rounded_rectangle((160, 1040, 580, 1075), radius=8, fill=(245, 245, 250), outline=(160, 175, 195), width=3)
        return img

    elif setting_type == "gym":
        # [2. 헬스장 피트니스 배경]
        d.rectangle((0, 0, W, FLOOR_Y), fill=(40, 44, 52))
        # 고무 매트 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(28, 30, 36))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 14), fill=(245, 200, 30)) # 옐로우 안전선
        for gy in range(FLOOR_Y + 14, H, 64):
            d.line([(0, gy), (W, gy)], fill=(18, 20, 24), width=3)
            for gx in range(0, W, 80):
                d.line([(gx, gy), (gx, min(H, gy + 64))], fill=(18, 20, 24), width=3)
        # 대형 전신 거울
        d.rounded_rectangle((70, 160, W - 70, FLOOR_Y - 40), radius=10, fill=(62, 70, 84), outline=(190, 195, 205), width=8)
        for sx in (240, 580, 860):
            d.polygon([(sx, 160), (sx + 80, 160), (sx - 70, FLOOR_Y - 40), (sx - 150, FLOOR_Y - 40)], fill=(78, 88, 106))
        # 바벨 랙 & 20kg 원판 (좌측)
        d.rectangle((90, 480, 110, FLOOR_Y - 40), fill=(40, 44, 52), outline=(120, 130, 145), width=2)
        d.rectangle((240, 480, 260, FLOOR_Y - 40), fill=(40, 44, 52), outline=(120, 130, 145), width=2)
        d.line([(70, 720), (280, 720)], fill=(180, 190, 205), width=10)
        d.rounded_rectangle((95, 660, 115, 780), radius=6, fill=(220, 40, 40))
        d.rounded_rectangle((235, 660, 255, 780), radius=6, fill=(220, 40, 40))
        # 덤벨 거치대 (우측)
        d.rectangle((820, 560, 1000, 576), fill=(90, 95, 105))
        d.rectangle((820, 760, 1000, 776), fill=(90, 95, 105))
        for dbx in (850, 910, 970):
            d.rounded_rectangle((dbx - 14, 545, dbx + 14, 560), radius=4, fill=(25, 27, 32))
            d.rounded_rectangle((dbx - 14, 745, dbx + 14, 760), radius=4, fill=(25, 27, 32))
        return img

    elif setting_type == "parking_road":
        # [3. 주차장 문맥 인식형 작화 엔진: 인프라 + 씬별 자동차 레이어 합성]
        draw_parking_lot_scene(d, scene_idx=scene_idx, total_scenes=total_scenes,
                               visual_props=visual_props, scene_text=scene_text)
        return img

    elif setting_type == "office":
        # [4. 사무실 / 직장 배경]
        d.rectangle((0, 0, W, FLOOR_Y), fill=(50, 55, 66))
        # 화이트보드
        d.rounded_rectangle((100, 220, 460, 680), radius=10, fill=(245, 248, 255), outline=(140, 150, 165), width=4)
        d.line([(140, 620), (420, 620)], fill=(40, 45, 55), width=3)
        d.line([(140, 620), (140, 280)], fill=(40, 45, 55), width=3)
        chart_pts = [(160, 580), (220, 520), (280, 440), (340, 470), (400, 320)]
        d.line(chart_pts, fill=(235, 60, 60), width=5)
        # 파티션 데스크 (우측 x: 520~1020)
        d.rounded_rectangle((520, 380, 1020, FLOOR_Y), radius=8, fill=(65, 80, 102), outline=(130, 145, 170), width=4)
        d.rectangle((540, 780, 1000, 820), fill=(35, 40, 52))
        d.rounded_rectangle((600, 640, 760, 780), radius=6, fill=(20, 22, 28), outline=(100, 110, 130), width=3)
        d.rectangle((610, 650, 750, 755), fill=(30, 110, 200))
        # 회색 카펫 타일 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(65, 68, 76))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 12), fill=(40, 42, 48))
        for cy in range(FLOOR_Y + 12, H, 58):
            d.line([(0, cy), (W, cy)], fill=(48, 50, 58), width=2)
            for cx in range(0, W, 90):
                d.line([(cx, cy), (cx, min(H, cy + 58))], fill=(48, 50, 58), width=2)
        return img

    elif setting_type == "apartment":
        # [5. 아파트 / 주거 실내 배경]
        d.rectangle((0, 0, W, FLOOR_Y), fill=(58, 56, 68))
        # 통창문 야경
        d.rounded_rectangle((100, 180, 500, 880), radius=10, fill=(18, 22, 34), outline=(180, 185, 195), width=6)
        for bx in range(120, 480, 55):
            bh = 150 + ((bx * 7) % 200)
            d.rectangle((bx, 880 - bh, bx + 45, 880), fill=(30, 36, 52))
            for wy in range(880 - bh + 20, 860, 32):
                d.rectangle((bx + 12, wy, bx + 32, wy + 16), fill=(255, 230, 110) if (bx + wy) % 3 == 0 else (120, 190, 240))
        # 액자
        d.rounded_rectangle((620, 240, 940, 560), radius=8, fill=(230, 225, 215), outline=(180, 140, 90), width=8)
        d.ellipse((680, 300, 880, 500), fill=(235, 100, 80))
        # 헤링본 우드 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(135, 95, 60))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 14), fill=(80, 50, 30))
        for hy in range(FLOOR_Y + 14, H, 45):
            d.line([(0, hy), (W, hy)], fill=(95, 65, 40), width=2)
            for hx in range(0, W + 60, 60):
                d.line([(hx, hy), (hx + 30, min(H, hy + 45))], fill=(105, 75, 45), width=2)
        return img

    elif setting_type == "subway_street":
        # [6. 야외 당근마켓 / 지하철역 4번 출구 앞 노을빛 도심 거리 배경]
        # 1. 하늘 그라디언트 (상단 딥 네이비 퍼플 -> 중간 노을빛 마젠타/오렌지 -> 지평선 웜 피치)
        for y in range(0, FLOOR_Y):
            ratio = y / float(FLOOR_Y)
            if ratio < 0.45:
                k = ratio / 0.45
                r = int(24 * (1 - k) + 115 * k)
                g = int(22 * (1 - k) + 48 * k)
                b = int(48 * (1 - k) + 85 * k)
            else:
                k = (ratio - 0.45) / 0.55
                r = int(115 * (1 - k) + 245 * k)
                g = int(48 * (1 - k) + 140 * k)
                b = int(85 * (1 - k) + 80 * k)
            d.line([(0, y), (W, y)], fill=(r, g, b))

        # 2. 먼 도심 스카이라인 실루엣 (Layer 1)
        rnd_bldg = random.Random(42)
        bx = 0
        while bx < W:
            bw = rnd_bldg.randint(70, 130)
            bh = rnd_bldg.randint(220, 420)
            by = FLOOR_Y - bh
            d.rectangle((bx, by, bx + bw, FLOOR_Y), fill=(38, 28, 52))
            if rnd_bldg.random() < 0.35:
                d.line([(bx + bw // 2, by), (bx + bw // 2, by - rnd_bldg.randint(30, 60))], fill=(38, 28, 52), width=3)
            bx += bw + rnd_bldg.randint(8, 24)

        # 3. 중간 도심 아파트 실루엣 & 불 켜진 창문 (Layer 2)
        bx = -20
        rnd_bldg2 = random.Random(107)
        while bx < W + 40:
            bw = rnd_bldg2.randint(90, 160)
            bh = rnd_bldg2.randint(140, 290)
            by = FLOOR_Y - bh
            d.rectangle((bx, by, bx + bw, FLOOR_Y), fill=(52, 38, 68))
            for wy in range(by + 20, FLOOR_Y - 40, 36):
                for wx in range(bx + 14, bx + bw - 14, 28):
                    if rnd_bldg2.random() < 0.42:
                        win_color = (255, 230, 130) if rnd_bldg2.random() < 0.8 else (140, 220, 255)
                        d.rectangle((wx, wy, wx + 12, wy + 16), fill=win_color)
            bx += bw + rnd_bldg2.randint(12, 30)

        # 4. 가로수 / 나무 실루엣
        for tx in (80, 420, 780, 1010):
            d.ellipse((tx - 65, FLOOR_Y - 140, tx + 65, FLOOR_Y - 20), fill=(26, 36, 42))
            d.ellipse((tx - 45, FLOOR_Y - 180, tx + 45, FLOOR_Y - 70), fill=(26, 36, 42))

        # 5. 지하철역 4번 출구 폴대 간판
        pole_x = 940
        pole_top = FLOOR_Y - 380
        d.rectangle((pole_x - 10, pole_top + 100, pole_x + 10, FLOOR_Y), fill=(45, 50, 60), outline=(20, 22, 28), width=2)
        d.rounded_rectangle((pole_x - 70, pole_top, pole_x + 70, pole_top + 100), radius=10, fill=(30, 35, 45), outline=(220, 220, 230), width=3)
        d.ellipse((pole_x - 52, pole_top + 14, pole_x - 14, pole_top + 52), fill=(255, 175, 20), outline=(255, 255, 255), width=2)
        d.text((pole_x - 33, pole_top + 33), "4", font=get_font(26), fill=(255, 255, 255), anchor="mm")
        d.text((pole_x + 24, pole_top + 28), "지하철", font=get_font(18), fill=(240, 240, 250), anchor="mm")
        d.text((pole_x + 24, pole_top + 52), "4번출구", font=get_font(20), fill=(255, 225, 40), anchor="mm")
        d.polygon([(pole_x - 16, pole_top + 78), (pole_x + 16, pole_top + 78), (pole_x, pole_top + 92)], fill=(255, 255, 255))

        # 6. 클래식 도심 가로등 & 조명 콘
        lx = 140
        ly_head = FLOOR_Y - 440
        cone_poly = [
            (lx - 25, ly_head + 30), (lx + 25, ly_head + 30),
            (lx + 260, FLOOR_Y), (lx - 120, FLOOR_Y)
        ]
        d.polygon(cone_poly, fill=(120, 95, 60))
        d.rectangle((lx - 8, ly_head + 30, lx + 8, FLOOR_Y), fill=(35, 38, 44), outline=(15, 18, 22), width=2)
        d.polygon([(lx - 45, ly_head + 30), (lx + 45, ly_head + 30), (lx + 20, ly_head), (lx - 20, ly_head)],
                  fill=(45, 50, 58), outline=(15, 18, 22))
        d.ellipse((lx - 26, ly_head + 22, lx + 26, ly_head + 46), fill=(255, 245, 160), outline=(255, 200, 50), width=3)

        # 7. 보도블록 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(95, 95, 110))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 16), fill=(40, 42, 50), outline=(20, 20, 25))
        tile_h = 52
        for r_idx, ty in enumerate(range(FLOOR_Y + 16, H, tile_h)):
            d.line([(0, ty), (W, ty)], fill=(45, 45, 55), width=3)
            shift = 45 if r_idx % 2 == 1 else 0
            for tx in range(shift, W + 90, 90):
                d.line([(tx, ty), (tx, min(H, ty + tile_h))], fill=(50, 50, 62), width=3)
        return img

    else:
        # [7. 모던 어반 실내/라운지 범용 배경 (단색 도트 배제)]
        d.rectangle((0, 0, W, FLOOR_Y), fill=(42, 45, 54))
        # 상단 앰비언트 백라이트
        d.rectangle((0, 0, W, 180), fill=(32, 35, 44))
        for lx in (180, 540, 900):
            d.rounded_rectangle((lx - 90, 60, lx + 90, 85), radius=4, fill=(240, 245, 255), outline=(190, 210, 240))
        # 모던 벽면 몰딩 패널
        for px in (80, 400, 720):
            d.rounded_rectangle((px, 240, px + 280, FLOOR_Y - 50), radius=10, fill=(48, 52, 62), outline=(65, 70, 84), width=3)
        # 웜 우드 플로어링 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(90, 68, 50))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 14), fill=(55, 40, 28))
        for fy in range(FLOOR_Y + 14, H, 52):
            d.line([(0, fy), (W, fy)], fill=(65, 48, 34), width=3)
            for fx in range(0, W + 120, 120):
                d.line([(fx, fy), (fx, min(H, fy + 52))], fill=(65, 48, 34), width=3)
        return img


# 상단 타이틀 바 경계 및 말풍선 레이아웃 상수 (타이틀 바 하단 + 안전 마진 이하로 말풍선이 내려오지 않도록 SSOT 로 관리)
TITLE_BAR_TOP, TITLE_BAR_BOTTOM = 30, 175
BUBBLE_SAFE_MARGIN = 20
BUBBLE_TOP_MIN = max(TITLE_BAR_BOTTOM + BUBBLE_SAFE_MARGIN, 220)
BUBBLE_GAP = 14
BUBBLE_EDGE_PAD = 40
BUBBLE_CENTER_X = {"A": 280, "B": 720}
BUBBLE_TAIL_HEAD = {"A": (300, 800), "B": (680, 800)}


def make_overlay(title: str, idx: int, total: int, subtitle: str, action: str) -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    # 1. 상단 타이틀 배너 (컴팩트 & 하이엔드: 30 ~ 175px 높이로 최적화하여 하단 말풍선 공간 넉넉히 확보)
    banner_y0, banner_y1 = TITLE_BAR_TOP, TITLE_BAR_BOTTOM
    d.rounded_rectangle((40, banner_y0, W - 40, banner_y1), radius=30, fill=(18, 18, 28, 235),
                        outline=(255, 214, 10, 255), width=5)

    # 제목 텍스트 (박스 중앙에 여유 있게 최대 880px 폭으로 배치)
    size, lines = 50, []
    while size >= 34:
        font = get_font(size)
        lines = wrap_text(title, font, 880)
        if len(lines) <= 2:
            break
        size -= 4
    lines = lines[:2]
    ly = 102 - (len(lines) - 1) * size * 0.58
    for ln in lines:
        d.text((W / 2, ly), ln, font=get_font(size), fill=(255, 214, 10), anchor="mm")
        ly += size * 1.16

    # 2. 하단 핵심 자막 (subtitle_text 만 노란색 굵은 폰트로 타격감 있게 배치, 연출 지문 action 은 절대 노출하지 않음)
    size, sub_lines = 110, []
    while size >= 56:
        font = get_font(size)
        sub_lines = wrap_text(subtitle, font, 940)
        if len(sub_lines) <= 2:
            break
        size -= 6
    sub_lines = sub_lines[:2]
    font = get_font(size)
    ly = 1680 - (len(sub_lines) - 1) * size * 0.58
    for ln in sub_lines:
        d.text((W / 2, ly), ln, font=font, fill=(0, 0, 0), anchor="mm", stroke_width=20, stroke_fill=(0, 0, 0))
        d.text((W / 2, ly), ln, font=font, fill=(255, 255, 255), anchor="mm", stroke_width=10, stroke_fill=(255, 255, 255))
        d.text((W / 2, ly), ln, font=font, fill=(255, 230, 0), anchor="mm")
        ly += size * 1.22
    return ov


# =====================================================================
# 6.5 화자 분리 및 텍스트/음성 전처리 유틸리티
# =====================================================================
SPEAKER_TAG_RE = re.compile(r"(?:^|(?<=[\s\n]))\[?([A-Za-z가-힣][A-Za-z가-힣0-9]*)\]?\s*[:：]\s*")

SLANG_REPLACEMENTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"ㅅㄱ"), " 수고 "),
    (re.compile(r"ㅇㅇ"), " 응 "),
    (re.compile(r"ㄴㄴ"), " 노노 "),
    (re.compile(r"ㄷㄷ+"), " 덜덜 "),
    (re.compile(r"ㄱㅅ"), " 감사 "),
    (re.compile(r"ㅈㅅ"), " 죄송 "),
    (re.compile(r"ㅇㅋ"), " 오케이 "),
    (re.compile(r"ㄹㅇ"), " 레알 "),
    (re.compile(r"ㅂㅂ"), " 바이바이 "),
    (re.compile(r"ㅃㄹ"), " 빨리 "),
]

VOICE_PROFILES: Dict[str, Dict[str, str]] = {
    "해설": {"voice": "ko-KR-HyunsuMultilingualNeural", "rate": "+22%", "pitch": "+0Hz"},
    "A": {"voice": "ko-KR-InJoonNeural", "rate": "+18%", "pitch": "-2Hz"},
    "B": {"voice": "ko-KR-SunHiNeural", "rate": "+20%", "pitch": "+3Hz"},
    "C": {"voice": "ko-KR-InJoonNeural", "rate": "+12%", "pitch": "-8Hz"},
}
DEFAULT_VOICE_PROFILE = {"voice": "ko-KR-HyunsuMultilingualNeural", "rate": "+22%", "pitch": "+0Hz"}

INTERJECTIONS: List[str] = ["저기요", "저기", "아", "와", "야", "어어", "어", "아니", "잠깐만", "잠만"]


def parse_speaker_segments(text: str) -> List[Tuple[str, str]]:
    """문자열에서 A:, B:, C:, 해설:, 관장:, 남자:, 여자: 등의 화자 라벨을 기준으로 대사 분리."""
    if not (text or "").strip():
        return []
    matches = list(SPEAKER_TAG_RE.finditer(text))
    if not matches:
        return [("", text.strip())]

    segments: List[Tuple[str, str]] = []
    if matches[0].start() > 0:
        pre = text[:matches[0].start()].strip()
        if pre:
            segments.append(("", pre))

    for i, m in enumerate(matches):
        speaker = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content:
            segments.append((speaker, content))
    return segments


def get_voice_profile(speaker: str, default_voice: str = "ko-KR-HyunsuMultilingualNeural", context_text: str = "") -> Dict[str, str]:
    """화자별 다중 성우(Multi-Voice) 및 자연스러운 순정 육성 속도/피치 설정 반환."""
    if not speaker or speaker in ("해설", "내레이션", "전체", "상황"):
        return {"voice": "ko-KR-HyunsuMultilingualNeural", "rate": "+20%", "pitch": "+0Hz"}
    raw = speaker.strip()
    u = raw.upper()
    if u == "A" or raw in ("주인공", "참교육자", "남자"):
        return {"voice": "ko-KR-InJoonNeural", "rate": "+18%", "pitch": "-2Hz"}
    if u == "B" or raw in ("빌런", "상대", "여자", "진상") or any(k in raw for k in ["여", "녀", "진상"]):
        ctx_lower = (context_text or "").lower()
        if any(k in ctx_lower for k in ["아저씨", "남자", "형씨", "아재", "할아버지", "남성"]) and not any(k in ctx_lower for k in ["아줌마", "여자", "언니", "여사", "여성", "아가씨"]):
            return {"voice": "ko-KR-InJoonNeural", "rate": "+20%", "pitch": "+2Hz"}
        return {"voice": "ko-KR-SunHiNeural", "rate": "+20%", "pitch": "+3Hz"}
    if u == "C" or any(k in raw for k in ["관장", "3자", "제3자", "사장", "경찰", "아저씨", "노인", "상사", "C"]):
        return {"voice": "ko-KR-InJoonNeural", "rate": "+12%", "pitch": "-8Hz"}
    return {"voice": default_voice, "rate": "+18%", "pitch": "+0Hz"}


def get_gemini_voice_profile(speaker: str, context_text: str = "") -> Tuple[str, str]:
    """Gemini Native TTS 화자별 (voice_name, acting_direction) 반환."""
    if not speaker or speaker in ("해설", "내레이션", "전체", "상황"):
        return (
            "Charon",
            "정확하고 또박또박한 한국어 표준 발음으로, 과장된 억양 없이 깔끔하고 속도감 있는 쇼츠 내레이션 톤으로 다음 문장만 읽으세요.",
        )
    raw = speaker.strip()
    u = raw.upper()
    if u == "A" or raw in ("주인공", "참교육자", "남자"):
        return (
            "Fenrir",
            "처음엔 정중하고 어이없어하다가, 참교육할 때는 단호하고 사이다처럼 통쾌한 톤으로 다음 대사만 자연스럽게 연기해주세요",
        )
    if u == "B" or raw in ("빌런", "상대", "여자", "진상") or any(k in raw for k in ["여", "녀", "진상"]):
        ctx_lower = (context_text or "").lower()
        has_male = any(k in ctx_lower for k in ["아저씨", "남자", "형씨", "아재", "할아버지", "남성"])
        has_female = any(k in ctx_lower for k in ["아줌마", "여자", "언니", "여사", "여성", "아가씨"])
        if has_male and not has_female:
            return (
                "Puck",
                "평소엔 뻔뻔하고 얄밉게 깐족거리는 중년 남성 톤으로, 참교육 당할 때는 진짜 당황해서 다급하게 소리치는 연기로 다음 대사만 읽어주세요",
            )
        else:
            return (
                "Kore",
                "평소엔 뻔뻔하고 얄밉게 깐족거리다가, 참교육 당할 때는 진짜 당황해서 다급하게 소리치는 연기로 다음 대사만 읽어주세요",
            )
    if u == "C" or any(k in raw for k in ["관장", "3자", "제3자", "사장", "경찰", "아저씨", "노인", "상사", "C"]):
        return (
            "Charon",
            "상황을 지켜보는 제3자로서 다음 대사만 자연스럽게 연기해주세요",
        )
    return (
        "Charon",
        "정확하고 또박또박한 한국어 표준 발음으로, 과장된 억양 없이 깔끔하고 속도감 있는 쇼츠 내레이션 톤으로 다음 문장만 읽으세요.",
    )



def clean_tts_text(text: str) -> str:
    """화자 기호 및 괄호 지문 제거, ㅋ/ㅎ 삭제, 말줄임표->쉼표 호흡 치환, 감탄사 쉼표 보정, 질문(?) 억양 보존."""
    if not text:
        return ""
    # 1. 화자 라벨 기호 제거 (예: A:, B:, 해설:, 관장: 등)
    cleaned = re.sub(r"^\s*\[?[A-Za-z가-힣0-9]+\]?\s*[:：]\s*", "", text)
    # 2. 괄호 지문 완전 제거 (속마음, 독백, 한숨, 웃음 등 소괄호/대괄호/특수괄호 및 내부 텍스트)
    cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]|（[^）]*）|【[^】]*】", "", cleaned)
    # 3. ㅋ, ㅎ 등 기계적 웃음소리 연속 문자열 완전 삭제 (무음 처리)
    cleaned = re.sub(r"[ㅋㅎ]+", "", cleaned)
    # 4. 의미 전달용 인터넷 초성 및 슬랭 치환
    for pat, repl in SLANG_REPLACEMENTS:
        cleaned = pat.sub(repl, cleaned)

    # 5. 말줄임표(..., …)는 쉼표(', ')로 치환하여 자연스러운 반 박자 호흡 유도
    cleaned = re.sub(r"[\.…]{2,}", ", ", cleaned)

    # 6. 구어체 감탄사/호칭 뒤에 쉼표가 없으면 쉼표(,) 삽입하여 사람다운 호흡 부여
    for _ in range(2):
        for w in sorted(INTERJECTIONS, key=len, reverse=True):
            cleaned = re.sub(rf"(^|[.?!,]\s*)({w})\s+(?![,])", rf"\1\2, ", cleaned)

    # 7. 허용 부호(한글, 영숫자, 공백, ?, !, ,, .) 외의 특수문자 정리
    cleaned = re.sub(r"[^\w\s\?!,\.]", " ", cleaned)

    # 8. 쉼표 중복 정리 및 연속 공백 정리
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\.\s*\.+", ". ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def apply_pcm_padding_and_fade(pcm: array, pad_sec: float = 0.035, fade_ms: float = 8.0) -> array:
    """PCM 시작과 끝에 미세한 무음 패딩(0.035s)과 스무스 페이드 인/아웃(8ms)을 적용하여 팝 노이즈/클리핑/글리치 원천 차단."""
    if not pcm or len(pcm) == 0:
        return array("h")

    pad_samples = int(pad_sec * SR)
    fade_samples = max(2, int((fade_ms / 1000.0) * SR))
    n = len(pcm)
    actual_fade = min(fade_samples, n // 2)

    new_pcm = array("h", pcm)
    for i in range(actual_fade):
        factor = i / float(actual_fade)
        new_pcm[i] = int(new_pcm[i] * factor)

    for i in range(actual_fade):
        factor = (actual_fade - 1 - i) / float(actual_fade)
        idx = n - 1 - i
        new_pcm[idx] = int(new_pcm[idx] * factor)

    silence = array("h", [0] * pad_samples)
    result = array("h")
    result.extend(silence)
    result.extend(new_pcm)
    result.extend(silence)
    return result


def split_long_narration(text: str) -> List[str]:
    """긴 문장이나 복합 대사를 문장부호(. ? !) 기준으로 자연스러운 호흡 단위로 분할."""
    text = (text or "").strip()
    if not text:
        return []
    # 마침표, 물음표, 느낌표 뒤에서 분할
    sentences = re.split(r"(?<=[.?!])\s+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) > 1:
        return sentences

    # 45자 초과하는 장문일 경우 쉼표 기준 분할
    if len(text) > 45 and "," in text:
        clauses = re.split(r"(?<=[,])\s+", text)
        merged: List[str] = []
        buf = ""
        for c in clauses:
            if not buf:
                buf = c
            elif len(buf) + len(c) < 40:
                buf += " " + c
            else:
                merged.append(buf.strip())
                buf = c
        if buf.strip():
            merged.append(buf.strip())
        if len(merged) > 1:
            return merged
    return [text]



def draw_bubble(d: ImageDraw.ImageDraw, speaker: str, text: str,
                head_a: Optional[Tuple[float, float]] = None,
                head_b: Optional[Tuple[float, float]] = None,
                target: Optional[Tuple[float, float]] = None,
                y_offset: int = 0) -> int:
    """
    화자별 말풍선/해설 박스 렌더링.
    - 모든 박스는 타이틀 바 하단 + 안전 마진(BUBBLE_TOP_MIN) 아래에서 시작.
    - y_offset > 0 이면 해당 Y(앞선 박스 하단) 아래 BUBBLE_GAP 만큼 띄워 배치.
    - A: 중심 X=280 / B: 중심 X=720, 좌우 화면 밖(40px 패딩)으로 나가지 않음.
    반환값: 렌더링된 박스 하단 Y 좌표(y1). 텍스트가 없으면 y_offset 그대로 반환.
    """
    raw_text = (text or "").strip()
    if not raw_text:
        return y_offset

    spk_raw = (speaker or "").strip()
    spk_u = spk_raw.upper()

    is_narration = not spk_raw or spk_raw in ("해설", "내레이션", "전체", "상황")
    is_a = not is_narration and (spk_u in ("A", "주인공", "남자") or ("A" in spk_u and "B" not in spk_u))
    is_b = not is_narration and not is_a and (spk_u in ("B", "빌런", "상대", "여자") or "B" in spk_u)
    if not is_narration and not is_a and not is_b:
        is_a = True  # 미분류 화자(C 등)는 좌측 풍선으로 폴백

    if target is not None:
        if is_b:
            head_b = target
        else:
            head_a = target

    # 1. 폰트/줄바꿈 폭: 좌우 풍선이 서로 겹치지 않도록 캐릭터 풍선은 최대 440px
    chosen_size = 40 if is_narration else 42
    pad_v, pad_h = (18, 32) if is_narration else (20, 28)
    max_box_w = (W - 2 * BUBBLE_EDGE_PAD) if is_narration else 440
    font = get_font(chosen_size)
    lines = wrap_text(raw_text, font, max_box_w - 2 * pad_h)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1][:-1] + "…"

    line_h = int(chosen_size * 1.32)
    max_text_w = max((font.getlength(ln) for ln in lines), default=200)
    bh = pad_v * 2 + len(lines) * line_h
    bw = min(max_box_w, max(int(max_text_w + pad_h * 2), 300))

    # 2. Y축: 타이틀 바 안전 마진 이후, 앞선 박스가 있으면 그 아래
    y0 = max(BUBBLE_TOP_MIN, y_offset + BUBBLE_GAP if y_offset > 0 else 0)
    y1 = y0 + bh

    # 3. X축/스타일: 중심 고정 후 좌우 패딩 클램프
    tail_head = None
    if is_narration:
        x0 = (W - bw) // 2
        border_color, border_width, box_fill = (245, 158, 11), 5, (255, 251, 235)
    else:
        key = "A" if is_a else "B"
        x0 = BUBBLE_CENTER_X[key] - bw // 2
        x0 = min(max(x0, BUBBLE_EDGE_PAD), W - BUBBLE_EDGE_PAD - bw)
        tail_head = (head_a if is_a else head_b) or BUBBLE_TAIL_HEAD[key]
        border_color = (59, 130, 246) if is_a else (239, 68, 68)
        border_width, box_fill = 6, (255, 255, 255)
    x1 = x0 + bw

    # 4. 꼬리표: A 는 박스 하단 좌측, B 는 하단 우측에서 캐릭터 머리 방향 삼각형
    has_tail = tail_head is not None and tail_head[1] > y1 + 10
    if has_tail:
        base_x = x0 + 70 if is_a else x1 - 70
        base_x = min(max(base_x, x0 + 45), x1 - 45)
        tip_x = min(max(tail_head[0], BUBBLE_EDGE_PAD), W - BUBBLE_EDGE_PAD)
        tip_y = min(tail_head[1] - 40, y1 + 190)
        d.polygon([(base_x - 22, y1 - 2), (base_x + 22, y1 - 2), (tip_x, tip_y)],
                  fill=box_fill, outline=border_color)

    # 5. 본체
    d.rounded_rectangle((x0, y0, x1, y1), radius=24, fill=box_fill, outline=border_color, width=border_width)
    if has_tail:
        d.rectangle((base_x - 18, y1 - 5, base_x + 18, y1 + 2), fill=box_fill)

    # 6. 텍스트
    text_color = (25, 25, 35)
    for i, ln in enumerate(lines):
        ly = y0 + pad_v + i * line_h
        d.text(((x0 + x1) / 2, ly), ln, font=font, fill=text_color, anchor="mt")

    return y1



# =====================================================================
# 7. 씬 비주얼 렌더러
# =====================================================================
class SceneVisuals:
    def __init__(self, scene: SceneBeat, idx: int, total: int, title: str, pose: str,
                 dur: float, hit_t: float, timeline: Optional[List[Dict]] = None,
                 episode_setting: str = "") -> None:
        self.scene, self.idx, self.total, self.dur = scene, idx, total, dur
        self.n_frames = max(1, int(round(dur * FPS)))
        self.ctx = {"hit_t": hit_t, "land_t": 0.9}
        self.cam = parse_camera(scene.camera_work)
        self.scene_text = f"{scene.stickman_action} {scene.tts_narration} {scene.subtitle_text}"
        self.episode_setting = episode_setting or detect_setting(scene.stickman_action, title, [self.scene_text])
        self.visual_props = resolve_visual_props(scene, idx, total, self.episode_setting, self.scene_text)
        bg_color = POSE_BG.get(pose, BG_COLORS[idx % len(BG_COLORS)])
        self.bg = make_background(bg_color, pose, self.scene_text, setting_type=self.episode_setting,
                                  scene_idx=idx, total_scenes=total, visual_props=self.visual_props)
        overlay = make_overlay(title, idx, total, scene.subtitle_text, scene.stickman_action)
        # 상단 오버레이는 타이틀 바 높이(182px)까지만 크롭하여 말풍선과 절대 겹치지 않도록 보장
        self.top = overlay.crop((0, 0, W, 182))
        self.bottom = overlay.crop((0, 1480, W, H))
        self.bubble_text = scene.tts_narration

        self.timeline = timeline or []
        if not self.timeline and (scene.tts_narration or "").strip():
            segs = parse_speaker_segments(scene.tts_narration)
            if segs:
                sdur = dur / len(segs)
                self.timeline = [{
                    "speaker": spk,
                    "text": txt,
                    "start": j * sdur,
                    "end": (j + 1) * sdur
                } for j, (spk, txt) in enumerate(segs)]

        # 항상 2인 등장: 주인공 A(좌측 360)와 상대방/빌런 B(우측 720) 구도
        self.main_x = MAIN_X
        self.foe_x = FOE_X
        self.main_pose, self.foe_pose = self._determine_poses(pose, scene.stickman_action, scene.tts_narration, idx, total)

    def get_active_segments(self, t: float) -> List[Dict]:
        """현재 시간(t)에 실제로 음성이 발화 중인 세그먼트 목록 반환 (발화 전/후에는 자동 퇴장)."""
        if not self.timeline:
            return []
        active = []
        for seg in self.timeline:
            # 발화 음성 구간 동안만 노출 (자연스러운 입출력을 위한 미세 여운 -0.05s ~ +0.10s 허용)
            if (seg["start"] - 0.05) <= t <= (seg["end"] + 0.10):
                active.append(seg)
        return active

    def get_active_segment(self, t: float) -> Optional[Dict]:
        """주 발화 세그먼트 반환 (발화 중이 아닐 때는 None 반환)."""
        segs = self.get_active_segments(t)
        return segs[0] if segs else None

    @staticmethod
    def _determine_poses(pose: str, action: str, narration: str, idx: int, total: int) -> Tuple[str, str]:
        act = (action or "").lower()
        narr = (narration or "").lower()
        full = f"{act} {narr}"

        is_last_scene = (idx == total - 1 and total > 1)
        is_retribution = any(k in full for k in [
            "잠만", "뭐하", "살려", "으악", "으아악", "사람 살려", "당황",
            "짓눌", "깔려", "납작", "개구리", "참교육", "득근", "역관광", "사이다", "응징", "200kg"
        ])

        # 1. 클라이맥스(마지막 씬) 또는 바벨 짓누르기: 빌런 B가 바닥에 납작 찌부러지는 슬랩스틱 확실 발동!
        if is_last_scene or any(k in full for k in ["짓눌", "깔려", "납작", "개구리", "살려", "사람 살려", "으아악"]):
            return ("retribution", "crushed")

        # 1-1. 무전기/본부 호출 씬 (씬 5 등): 주인공은 입가에 무전기를 대는 radio_call 포즈
        if any(k in full for k in ["본부", "무전", "투하 요망", "투하요망", "10톤 투하", "좌표"]):
            return ("radio_call", "shock")

        # 2. 씬 5~7 참교육/역관광 구간 (예: 씬 6 "너 뭐하... / 득근하세요"): 주인공은 안광/미소, 빌런은 패닉!
        if is_retribution or (idx >= max(3, total - 3) and any(k in full for k in ["너 뭐하", "잠만", "비키", "어?"])):
            return ("retribution", "shock")

        # 3. 물리적 액션에 따른 매핑
        if pose == "punch":
            return ("punch", "hit")
        if pose == "grab":
            return ("grab", "grabbed")
        if pose == "fall":
            if "a가" in act and "쓰러" in act:
                return ("fall", "smirk")
            return ("smirk", "fall")
        if pose == "angry":
            return ("angry", "cower")
        if pose == "freeze":
            return ("smirk", "freeze")
        if pose == "run":
            return ("run", "cower")
        if pose == "shock":
            return ("smirk", "shock")
        if pose == "twist":
            return ("retribution", "shock")
        if pose == "smirk":
            return ("smirk", "cower")

        # 4. 기본 대화 티키타카
        has_a = ("a:" in narr or "a :" in narr or "주인공" in act)
        has_b = ("b:" in narr or "b :" in narr or "빌런" in act)
        if has_b and not has_a:
            return ("smirk", "talk")
        if has_a and not has_b:
            return ("talk", "smirk")
        return ("talk", "smirk")

    def _camera(self, img: Image.Image, i: int, t: float) -> Tuple[Image.Image, Tuple[float, float, float]]:
        u = i / max(1, self.n_frames - 1)
        text = (self.scene_text or "").lower()
        active_seg = self.get_active_segment(t)
        cur_speaker = (active_seg.get("speaker") or "").strip().upper() if active_seg else ""

        # 기본 카메라 설정
        if self.cam["zoom_out"]:
            z = _lerp(1.3, 1.0, u)
        elif self.cam["zoom_in"]:
            z = _lerp(1.0, 1.3, u)
        else:
            z = _lerp(1.0, 1.06, u)

        target_cx, target_cy = W / 2.0, H / 2.0
        blend = 0.0

        # 1. 빌런 단독 클로즈업 (Zoom-in 1.15~1.25x)
        # 빌런 B가 적반하장으로 소리치거나 깐족거리는 구간
        is_villain_rant = (self.idx == 3 or "적반하장" in text or (cur_speaker in ("B", "빌런") and self.idx <= 3))
        if is_villain_rant:
            target_cx = self.foe_x
            target_cy = 820.0
            blend = 0.65
            z = max(z, _lerp(1.12, 1.24, _ease(u)))

        # 2. 주인공 참교육 선언 클로즈업 (Zoom-in 1.20~1.25x)
        # 주인공 A가 무전기로 본부에 투하 요청하는 씬 5
        is_hero_call = (self.idx == 4 or any(k in text for k in ["본부", "투하", "특단", "요망"]))
        if is_hero_call and self.idx < 5:
            target_cx = self.main_x
            target_cy = 790.0
            blend = 0.70
            z = max(z, _lerp(1.12, 1.25, _ease(u)))

        # 3. 임팩트 카메라 쉐이크 (Camera Shake)
        # 10톤 당근이나 바벨이 떨어져 쿵!! 하고 충돌하는 순간 (씬 6~7 초반 0.6초간 강력 진동)
        amp = 16.0 if self.cam["shake"] else 0.0
        hit = self.ctx["hit_t"]

        is_impact_crush = (self.idx in (5, 6) and t <= 0.65) or (self.foe_pose in ("crushed", "fall") and t <= 0.65) or ("쿵" in text and t <= 0.65)
        if is_impact_crush:
            shake_fade = max(0.0, 1.0 - t / 0.65)
            amp = max(amp, 34.0 * shake_fade)

        if self.main_pose == "punch" and hit <= t <= hit + 0.45:
            amp = max(amp, 26.0)
        if (self.foe_pose in ("fall", "crushed") or self.main_pose == "fall") and self.ctx["land_t"] <= t <= self.ctx["land_t"] + 0.4:
            amp = max(amp, 24.0)
        if self.main_pose == "twist" and t < 0.4:
            amp = max(amp, 14.0)

        dx = dy = 0.0
        if amp > 0.5:
            rnd = random.Random(i * 7919)
            dx = amp * (rnd.random() * 2 - 1)
            dy = amp * (rnd.random() * 2 - 1)
            z = max(z, 1.06)

        if self.cam["pan"]:
            dx += _lerp(-40, 40, u)
            z = max(z, 1.06)

        cur_cx = _lerp(W / 2.0, target_cx, blend)
        cur_cy = _lerp(H / 2.0, target_cy, blend)

        if z <= 1.0005 and dx == 0 and dy == 0:
            return img, (0.0, 0.0, 1.0)
        w, h = W / z, H / z
        x0 = min(max(cur_cx + dx - w / 2, 0), W - w)
        y0 = min(max(cur_cy + dy - h / 2, 0), H - h)
        return img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h)), (x0, y0, z)

    def frame(self, i: int) -> Image.Image:
        t = i / FPS
        ta = t
        if self.cam["freeze"]:
            ta = min(t, self.dur * 0.6)
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        rnd = random.Random(i // 2)
        hit = self.ctx["hit_t"]

        # 화자 타임라인에 따른 실시간 발화자 감지
        active_seg = self.get_active_segment(t)
        cur_speaker = (active_seg.get("speaker") or "").strip().upper() if active_seg else ""
        is_voice_active = (active_seg["start"] - 0.05 <= t <= active_seg["end"] + 0.08) if active_seg else False

        is_a_talking = is_voice_active and (cur_speaker in ("A", "주인공", "남자") or ("A" in cur_speaker and "B" not in cur_speaker))
        is_b_talking = is_voice_active and (cur_speaker in ("B", "빌런", "상대", "여자") or ("B" in cur_speaker and "A" not in cur_speaker))

        main_p = POSES[self.main_pose](ta, self.ctx)
        foe_p = POSES[self.foe_pose](ta, self.ctx)

        # 실시간 말하는 사람의 입 애니메이션 (말할 때만 talk 개폐, 아닐 때는 원래 포즈 표정 유지)
        if is_a_talking:
            main_p["mouth"] = "talk"
            main_p["mo"] = abs(math.sin(ta * 13))
        elif main_p["mouth"] == "talk":
            main_p["mouth"] = "smirk" if self.main_pose in ("smirk", "retribution") else "smile"

        if is_b_talking:
            foe_p["mouth"] = "talk"
            foe_p["mo"] = abs(math.sin(ta * 13))
        elif foe_p["mouth"] == "talk":
            foe_p["mouth"] = "O" if self.foe_pose in ("shock", "twist", "crushed") else "smirk"

        main_fig = compute_figure(main_p, 1, self.main_x, FLOOR_Y)
        foe_fig = compute_figure(foe_p, -1, self.foe_x, FLOOR_Y)

        # --- 배경 쪽 이펙트
        if self.main_pose == "punch" and hit - 0.12 <= t <= hit + 0.5:
            fx_radial_lines(d, foe_fig["head"], rnd, 26, 140, 1300, (30, 30, 40))
        elif self.main_pose == "run":
            fx_horizontal_lines(d, rnd, 14, (255, 255, 255))
        elif self.idx == 4 or any(k in self.scene_text for k in ["본부", "투하"]):
            # 주인공 무전 교신 씬: 주인공 얼굴을 향한 강력한 웹툰 집중선 (Radial Focus Lines)
            fx_radial_lines(d, main_fig["head"], rnd, 28, 240, 1300, (40, 50, 80))
        elif self.main_pose == "angry":
            fx_radial_lines(d, main_fig["head"], rnd, 22, 330, 1300, (230, 70, 60))
        elif self.main_pose in ("twist", "shock") or self.foe_pose in ("twist", "shock"):
            fx_radial_lines(d, (W / 2, 900), rnd, 24, 320, 1300, (40, 40, 55))
        elif (self.main_pose == "fall" or self.foe_pose in ("fall", "crushed")) and t < self.ctx["land_t"]:
            fx_horizontal_lines(d, rnd, 8, (255, 255, 255))

        # --- 스틱맨 캐릭터 드로잉 (빌런 B & 주인공 A)
        draw_figure(d, foe_fig, foe_p, (170, 30, 40), (255, 235, 235), char_type="B", t=ta)
        draw_figure(d, main_fig, main_p, (20, 20, 30), (255, 255, 255), char_type="A", t=ta)

        # --- 상황별 소품 (Props) 자동 렌더링
        draw_props(d, main_fig, foe_fig, self.scene_text, self.main_pose, self.foe_pose, ta, setting_type=self.episode_setting)

        # 주차장 설정 시: 동적 전경 애니메이션 (견인차 사이렌 회전 빔, 0cm 밀착 스파크)
        if self.episode_setting == "parking_road":
            if "tow_truck_lifting" in self.visual_props:
                siren_x, siren_y = 210, 1065
                rot_ang = ta * 14
                dx1, dy1 = math.cos(rot_ang) * 90, math.sin(rot_ang) * 35
                d.line([(siren_x - dx1, siren_y - dy1), (siren_x + dx1, siren_y + dy1)], fill=(255, 235, 80), width=6)
                d.line([(siren_x - dy1, siren_y + dx1), (siren_x + dy1, siren_y - dx1)], fill=(255, 210, 40), width=3)
                d.ellipse((siren_x - 35, siren_y - 25, siren_x + 35, siren_y + 25), fill=(255, 180, 20))
            elif "tight_blocking_car" in self.visual_props:
                if int(ta * 12) % 2 == 0:
                    spk_y = 1080 + int(math.sin(ta * 20) * 40)
                    d.text((535, spk_y), "⚡", font=get_font(34), fill=(255, 230, 40), anchor="mm")

        # 편의점 설정 시: 주인공 A가 계산대 뒤에 서 있도록 카운터 상판 및 POS 전경 재합성 (A의 하반신을 자연스럽게 가림)
        if self.episode_setting == "convenience_store":
            d.rounded_rectangle((180, 1070, 560, FLOOR_Y), radius=8, fill=(32, 40, 55), outline=(90, 105, 130), width=4)
            d.rounded_rectangle((160, 1040, 580, 1075), radius=8, fill=(245, 245, 250), outline=(160, 175, 195), width=3)
            # POS 터미널
            d.rectangle((485, 990, 515, 1040), fill=(25, 28, 35), outline=(60, 65, 80))
            d.rounded_rectangle((435, 890, 565, 995), radius=8, fill=(20, 22, 28), outline=(90, 100, 120), width=3)
            d.rectangle((445, 900, 555, 985), fill=(25, 115, 215))
            for ply in (945, 958, 970):
                d.line([(452, ply), (510, ply)], fill=(255, 255, 255), width=2)
            # 영수증 출력기 & 카드 리더기
            d.rounded_rectangle((360, 990, 425, 1040), radius=5, fill=(30, 34, 44), outline=(70, 75, 90), width=2)
            d.rectangle((375, 975, 410, 990), fill=(255, 255, 255))
            d.rounded_rectangle((530, 1010, 575, 1040), radius=4, fill=(40, 45, 58), outline=(100, 110, 130))
            d.ellipse((560, 1018, 568, 1026), fill=(40, 220, 80))

        # --- 전경 이펙트
        main_hx, main_hy = main_fig["head"]
        foe_hx, foe_hy = foe_fig["head"]
        R = main_fig["R"]

        if self.main_pose == "punch":
            fx_anger(d, main_hx + R * 0.95, main_hy - R * 0.95, t)
            if t < hit:
                fx_sweat(d, foe_hx, foe_hy, R, t)
            if 0 <= t - hit < 0.4:
                k = (t - hit) / 0.4
                fx_burst(d, foe_hx - 40, foe_hy, 150 + 160 * k, "퍽!", get_font(110))
            elif t - hit >= 0.4:
                fx_stars(d, foe_hx, foe_hy, R, t)
        elif self.main_pose == "grab":
            fx_anger(d, main_hx + R * 0.95, main_hy - R * 0.95, t)
            fx_sweat(d, foe_hx, foe_hy, R, t)
        elif self.foe_pose in ("shock", "freeze"):
            fx_sweat(d, foe_hx, foe_hy, R, t)
            if self.foe_pose == "shock" and t < 0.8 and int(t * 10) % 2 == 0:
                fx_lightning(d, foe_hx, random.Random(int(t * 10)))
        elif self.main_pose == "shock":
            fx_sweat(d, main_hx, main_hy, R, t)
            if t < 0.8 and int(t * 10) % 2 == 0:
                fx_lightning(d, main_hx + 260, random.Random(int(t * 10)))
        elif self.main_pose == "run":
            fx_dust(d, main_fig["hip"][0], FLOOR_Y, t)
            fx_sweat(d, main_hx, main_hy, R, t)
        elif self.foe_pose in ("hit", "cower"):
            fx_sweat(d, foe_hx, foe_hy, R, t)
        elif self.foe_pose in ("fall", "crushed"):
            fx_sweat(d, foe_hx, foe_hy, R, t)
            fx_stars(d, foe_hx, foe_hy, R, t)
            if self.foe_pose == "crushed" and t < 0.7:
                fx_burst(d, foe_hx + 30, FLOOR_Y - 140, 160 + 100 * (t / 0.7), "쿵!!", get_font(110))
        elif self.main_pose == "angry":
            fx_anger(d, main_hx + R * 1.0, main_hy - R * 1.0, t, 1.2)
            fx_anger(d, main_hx - R * 1.3, main_hy - R * 0.7, t + 1, 0.9)
            fx_sweat(d, foe_hx, foe_hy, R, t)
        if self.main_pose == "freeze" or self.foe_pose == "freeze":
            img = Image.blend(img, Image.new("RGB", (W, H), (190, 225, 255)), 0.22)

        # --- 카메라 연출 적용 (배경, 캐릭터, 소품, 이펙트 줌/패닝)
        img, (cam_x0, cam_y0, cam_z) = self._camera(img, i, t)

        # --- 고정 UI 오버레이 레이어 (카메라 줌/패닝 영향 없음 -> 상단 이탈/꼬리 늘어남 원천 차단!)
        d_ui = ImageDraw.Draw(img)
        head_a_screen = ((main_fig["head"][0] - cam_x0) * cam_z,
                         (main_fig["head"][1] - cam_y0) * cam_z - (main_fig["R"] * cam_z) - 10)
        head_b_screen = ((foe_fig["head"][0] - cam_x0) * cam_z,
                         (foe_fig["head"][1] - cam_y0) * cam_z - (foe_fig["R"] * cam_z) - 10)

        # 현재 시간(t)에 발화 중인 화자별 대사/해설 말풍선 실시간 렌더링 (Y축 충돌 방지 및 타이밍 싱크)
        active_segs = self.get_active_segments(t)
        if active_segs:
            # 해설 우선 배치 후, 캐릭터 대사 말풍선이 겹칠 경우 Y축 오프셋을 주어 충돌 방지
            narr_segs = [s for s in active_segs if not s.get("speaker") or s.get("speaker") in ("해설", "내레이션", "전체", "상황")]
            char_segs = [s for s in active_segs if s not in narr_segs]

            # 해설은 최상단(타이틀 바 안전 마진 아래), 캐릭터 풍선은 해설 하단 아래에서 좌/우 열별로 스택
            cur_y_offset = 0
            for n_seg in narr_segs:
                cur_y_offset = max(cur_y_offset, draw_bubble(
                    d_ui, n_seg.get("speaker", ""), n_seg.get("text", ""),
                    head_a=head_a_screen, head_b=head_b_screen, y_offset=cur_y_offset))

            col_y = {"A": cur_y_offset, "B": cur_y_offset}
            for c_seg in char_segs:
                spk_u = (c_seg.get("speaker") or "").strip().upper()
                col = "B" if (spk_u in ("B", "빌런", "상대", "여자") or ("B" in spk_u and "A" not in spk_u)) else "A"
                col_y[col] = max(col_y[col], draw_bubble(
                    d_ui, c_seg.get("speaker", ""), c_seg.get("text", ""),
                    head_a=head_a_screen, head_b=head_b_screen, y_offset=col_y[col]))

        # 상단 타이틀 배너 / 하단 자막 오버레이
        img.paste(self.top, (0, 0), self.top)
        if t < 0.2:
            s = 1.25 - 0.25 * _ease(t / 0.2)
            bw, bh = int(W * s), int(self.bottom.height * s)
            scaled = self.bottom.resize((bw, bh), Image.BILINEAR)
            img.paste(scaled, (int(W / 2 - bw / 2), int(1480 + self.bottom.height / 2 - bh / 2)), scaled)
        else:
            img.paste(self.bottom, (0, 1480), self.bottom)
        return img


# =====================================================================
# 7.5. BGM 탐색 및 오디오 더킹(Auto-Ducking) 믹싱 파이프라인
# =====================================================================
def list_bgm_tracks(bgm_dir: Path = BGM_DIR) -> List[Dict[str, Any]]:
    """
    assets/bgm 디렉토리의 모든 음원 파일(.mp3, .wav)을 탐색하여
    파일명, 확장자, 재생 시간(초/시:분:초), 파일 크기를 반환한다.
    """
    if not bgm_dir.exists():
        return []
    audio_files = sorted([f for f in bgm_dir.iterdir() if f.suffix.lower() in [".mp3", ".wav"]])
    ffmpeg_exe = find_ffmpeg()
    results = []
    for f in audio_files:
        dur_str = "00:00.00"
        dur_sec = 0.0
        try:
            cmd = [ffmpeg_exe, "-i", str(f)]
            proc = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, errors="ignore")
            for line in proc.stderr.splitlines():
                if "Duration:" in line:
                    parts = line.split("Duration:")[1].split(",")[0].strip()
                    dur_str = parts
                    hms = parts.split(":")
                    if len(hms) == 3:
                        dur_sec = float(hms[0]) * 3600 + float(hms[1]) * 60 + float(hms[2])
                    break
        except Exception:
            pass
        results.append({
            "name": f.name,
            "stem": f.stem,
            "ext": f.suffix.upper(),
            "path": f,
            "duration_str": dur_str,
            "duration_sec": dur_sec,
            "size_mb": round(f.stat().st_size / (1024 * 1024), 2)
        })
    return results


def make_peaking_filter(f0: float, gain_db: float, Q: float, sr: int = SR) -> Tuple[np.ndarray, np.ndarray]:
    """Audio EQ Cookbook Peaking/Bell Biquad Filter 계수 계산."""
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2.0 * Q)
    b0 = 1.0 + alpha * A
    b1 = -2.0 * np.cos(w0)
    b2 = 1.0 - alpha * A
    a0 = 1.0 + alpha / A
    a1 = -2.0 * np.cos(w0)
    a2 = 1.0 - alpha / A
    b = np.array([b0, b1, b2], dtype=np.float64) / a0
    a = np.array([a0, a1, a2], dtype=np.float64) / a0
    return b, a


def apply_vocal_pocketing(
    bgm_stereo: np.ndarray,
    sr: int = SR,
    mid_scale: float = 0.60,
    side_scale: float = 1.25
) -> np.ndarray:
    """
    BGM 음원에 '보컬 포켓팅(Vocal Pocketing) 및 스테레오 분리'를 적용한다.
    1. Mid-Side 변환: L/R을 Mid(중앙 모노)와 Side(측면 스테레오)로 분리.
    2. Mid 채널 보컬 대역(400Hz~3,200Hz) EQ 감쇠: 800Hz(-9dB) & 2200Hz(-9dB) 듀얼 피킹 필터로 보컬 주파수를 -9~-12dB 감쇠.
       (반면 200Hz 이하 킥/베이스와 5kHz 이상 하이햇/신스는 원음 유지)
    3. Center 보컬 억제: Mid 채널을 0.60(-4.4dB)으로 감쇠하여 중앙을 성우 대사 전용 공간으로 확보.
    4. Side 채널 공간감 확장: Side 채널을 1.25(+1.94dB)로 부스트하여 입체 스테레오 확장.
    5. Stereo L/R 재합성: L = Mid + Side, R = Mid - Side.
    """
    L = bgm_stereo[:, 0].astype(np.float64)
    R = bgm_stereo[:, 1].astype(np.float64)

    mid = 0.5 * (L + R)
    side = 0.5 * (L - R)

    # 400Hz ~ 3200Hz 보컬 대역 EQ 감쇠 (Mid 채널 대상)
    b1, a1 = make_peaking_filter(800.0, -9.0, 0.7, sr=sr)
    b2, a2 = make_peaking_filter(2200.0, -9.0, 0.7, sr=sr)
    mid_filtered = signal.lfilter(b1, a1, mid)
    mid_filtered = signal.lfilter(b2, a2, mid_filtered)

    mid_pocket = mid_filtered * mid_scale
    side_expanded = side * side_scale

    L_out = mid_pocket + side_expanded
    R_out = mid_pocket - side_expanded
    return np.stack([L_out, R_out], axis=1).astype(np.float32)


def mix_bgm_with_auto_ducking(
    master_audio_path: Path,
    bgm_path: Path,
    output_path: Path,
    bgm_volume: float = 0.15,
    duck_gain: Optional[float] = None,
    open_gain: Optional[float] = None,
    fade_out_sec: float = 1.5,
    threshold: float = 300.0,
    target_sr: int = SR,
    vocal_pocketing: bool = True
) -> Path:
    """
    master_audio.wav (대사+SFX)에 BGM을 오디오 더킹(Auto-Ducking) 및 보컬 포켓팅으로 믹싱한다.
    - vocal_pocketing=True: 400Hz~3,200Hz 보컬 대역 감쇠 + Mid-Side 센터 보컬 억제
    - 대사 구간: BGM 볼륨을 bgm_volume(기본 15%, 약 -16dB~-18dB)으로 자동 감쇠
    - 무음/효과음 구간: BGM 볼륨을 자연스럽게 살려줌 (open_gain = min(0.35, bgm_volume * 2.0))
    - 부드러운 Attack(70ms) / Release(400ms) 엔벨로프
    - 영상 종료 직전 (마지막 fade_out_sec초) 부드러운 페이드아웃
    - 출력 파일은 master_audio와 정확히 동일한 샘플 수 및 44.1kHz 16-bit Stereo로 유지
    """
    ffmpeg_exe = find_ffmpeg()

    def decode_wav_stereo(p: Path) -> np.ndarray:
        cmd = [
            ffmpeg_exe, "-y", "-i", str(p),
            "-ar", str(target_sr), "-ac", "2",
            "-f", "s16le", "-"
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return np.frombuffer(proc.stdout, dtype=np.int16).reshape(-1, 2)

    master = decode_wav_stereo(master_audio_path)
    bgm = decode_wav_stereo(bgm_path)
    total_samples = len(master)

    if len(bgm) < total_samples:
        repeats = int(np.ceil(total_samples / len(bgm)))
        bgm_matched = np.tile(bgm, (repeats, 1))[:total_samples]
    else:
        bgm_matched = bgm[:total_samples]

    # 보컬 포켓팅 (EQ 감쇠 및 Mid-Side 센터 보컬 억제)
    if vocal_pocketing:
        bgm_matched = apply_vocal_pocketing(bgm_matched, sr=target_sr)

    if duck_gain is None:
        duck_gain = max(0.02, min(0.50, float(bgm_volume)))
    if open_gain is None:
        open_gain = min(0.35, duck_gain * 2.0)

    win_len = int(0.025 * target_sr)  # 25ms
    hop_len = int(0.010 * target_sr)  # 10ms
    voice_mono = np.abs(master.mean(axis=1, dtype=np.float32))
    n_frames = max(1, (total_samples - win_len) // hop_len + 1)
    target_gains = np.full(n_frames, open_gain, dtype=np.float32)

    for i in range(n_frames):
        start = i * hop_len
        end = min(total_samples, start + win_len)
        chunk = voice_mono[start:end]
        rms = np.sqrt(np.mean(chunk**2)) if len(chunk) > 0 else 0.0
        if rms > threshold:
            ratio = min(1.0, (rms - threshold) / 1000.0)
            target_gains[i] = open_gain - ratio * (open_gain - duck_gain)
        else:
            target_gains[i] = open_gain

    smoothed_gains = np.zeros(n_frames, dtype=np.float32)
    smoothed_gains[0] = target_gains[0]
    alpha_attack = np.exp(-1.0 / (0.070 / (hop_len / target_sr)))
    alpha_release = np.exp(-1.0 / (0.400 / (hop_len / target_sr)))

    for i in range(1, n_frames):
        tgt = target_gains[i]
        if tgt < smoothed_gains[i - 1]:
            smoothed_gains[i] = alpha_attack * smoothed_gains[i - 1] + (1.0 - alpha_attack) * tgt
        else:
            smoothed_gains[i] = alpha_release * smoothed_gains[i - 1] + (1.0 - alpha_release) * tgt

    t_frames = np.arange(n_frames) * hop_len + win_len // 2
    t_samples = np.arange(total_samples)
    sample_gain = np.interp(t_samples, t_frames, smoothed_gains, left=smoothed_gains[0], right=smoothed_gains[-1])

    fade_samples = int(fade_out_sec * target_sr)
    fade_start = total_samples - fade_samples
    if fade_start > 0:
        fade_curve = np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
        sample_gain[fade_start:] *= fade_curve

    mixed_float = master.astype(np.float32) + bgm_matched.astype(np.float32) * sample_gain[:, None]
    mixed_s16 = np.clip(mixed_float, -32767, 32767).astype(np.int16)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(target_sr)
        wf.writeframes(mixed_s16.tobytes())
    return output_path


def text_sha256(*parts: str) -> str:
    """오디오 캐시 키용 텍스트 SHA256 (대본이 바뀌면 키도 바뀌어 이전 음성이 재사용되지 않는다)."""
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
        h.update(b"")
    return h.hexdigest()


def wav_duration_sec(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes() / float(wf.getframerate() or 1)
    except Exception:
        return 0.0


def audio_cache_valid(audio: Path, digest: str, expected_sec: Optional[float] = None) -> bool:
    """캐시 오디오가 현재 텍스트와 일치하는지 검증.
    사이드카(.sha256)가 있으면 해시 비교, 없는 레거시 파일은 (길이 검증 통과 시) 현재 해시로 채택한다."""
    sidecar = audio.with_suffix(".sha256")
    if sidecar.exists():
        try:
            return sidecar.read_text(encoding="utf-8").strip() == digest
        except OSError:
            return False
    if expected_sec is not None and abs(wav_duration_sec(audio) - expected_sec) > 0.1:
        return False
    try:
        sidecar.write_text(digest, encoding="utf-8")
    except OSError:
        pass
    return True


def write_audio_digest(audio: Path, digest: str) -> None:
    try:
        audio.with_suffix(".sha256").write_text(digest, encoding="utf-8")
    except OSError:
        pass


# =====================================================================
# 8. 메인 렌더러
# =====================================================================
class StickmanShortRenderer:
    def __init__(self, voice: str = "ko-KR-InJoonNeural") -> None:
        self.voice = voice
        self.episode_context: str = ""
        self.current_episode_id: str = ""
        self.ffmpeg = find_ffmpeg()
        self.sfx = SfxSynth()
        self.sfx.ensure_all()

    # ---------------- 공통 FFmpeg ----------------
    def _run_ffmpeg(self, args: List[str], cwd: Optional[Path] = None) -> None:
        proc = subprocess.run([self.ffmpeg, "-y", "-v", "error"] + args, cwd=str(cwd) if cwd else None,
                              capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg 실패: {proc.stderr.decode('utf-8', 'ignore')[-800:]}")

    def _decode_pcm(self, path: Path) -> array:
        proc = subprocess.run([self.ffmpeg, "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                               "-ar", str(SR), "-"], capture_output=True)
        pcm = array("h")
        if proc.returncode == 0 and proc.stdout:
            pcm.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
        return pcm

    # ---------------- TTS ----------------
    def _tts_gemini(self, text: str, speaker: str, out: Path) -> bool:
        """Gemini Native Audio TTS (감정 연기형 다중 성우) + atempo=1.15 고속 배속 후처리."""
        if getattr(self, "_gemini_quota_exhausted", False):
            return False
        try:
            client = get_gemini_client()
            if not client:
                return False
            voice_name, direction = get_gemini_voice_profile(speaker, context_text=self.episode_context or text)
            prompt = f"""[성우 연기 지시]
당신은 전문 성우입니다.
{direction}
중요: 지시문이나 설명, 따옴표는 절대 소리 내어 읽지 말고, 아래 '대사'만 음성으로 연기하세요.

대사:
{text}"""
            cfg = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                    )
                ),
            )
            # 1순위: gemini-2.5-flash-preview-tts (일시적 오류 대비 2회 재시도), 호출 실패 시 2순위: gemini-2.0-flash / gemini-2.5-flash
            models_to_try = ["gemini-2.5-flash-preview-tts", "gemini-2.5-flash-preview-tts", "gemini-2.0-flash", "gemini-2.5-flash"]
            raw_audio = None
            for attempt, m in enumerate(models_to_try):
                try:
                    resp = client.models.generate_content(model=m, contents=prompt, config=cfg)
                    if resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts:
                        for part in resp.candidates[0].content.parts:
                            if part.inline_data and part.inline_data.data:
                                raw_audio = part.inline_data.data
                                break
                    if raw_audio:
                        break
                except Exception as me:
                    err_str = str(me)
                    print(f"[TTS] Gemini 모델 {m} 호출 재시도({attempt + 1}/{len(models_to_try)}) ({speaker}): {me}")
                    if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                        self._gemini_quota_exhausted = True
                        break
                    continue

            if not raw_audio:
                return False

            raw_wav = out.parent / f"{out.stem}_raw.wav"
            with wave.open(str(raw_wav), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(raw_audio)

            # 쇼츠 템포에 맞게 ffmpeg atempo=1.15 (약 1.15배속) 필터 적용
            cmd = [
                self.ffmpeg, "-y", "-v", "error",
                "-i", str(raw_wav),
                "-filter:a", "atempo=1.15",
                str(out)
            ]
            subprocess.run(cmd, capture_output=True, check=True)
            try:
                raw_wav.unlink(missing_ok=True)
            except Exception:
                pass
            return out.exists() and out.stat().st_size > 0
        except Exception as e:
            print(f"[TTS] Gemini Native TTS 합성 실패 ({speaker}): {e}")
            return False

    def _tts_edge(self, text: str, out: Path, voice: str, rate: str = "+0%", pitch: str = "+0Hz") -> bool:
        try:
            import edge_tts

            async def go():
                await asyncio.wait_for(
                    edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(out)),
                    timeout=40,
                )

            with ThreadPoolExecutor(max_workers=1) as ex:
                ex.submit(asyncio.run, go()).result()
            return out.exists() and out.stat().st_size > 0
        except Exception as e:
            print(f"[TTS] edge-tts 실패 ({voice}, rate={rate}, pitch={pitch}): {e}")
            return False

    @staticmethod
    def _tts_gtts(text: str, out: Path) -> bool:
        try:
            from gtts import gTTS
            gTTS(text=text, lang="ko").save(str(out))
            return out.exists() and out.stat().st_size > 0
        except Exception as e:
            print(f"[TTS] gTTS 실패: {e}")
            return False

    def _synthesize_subsegment_tts(self, text: str, speaker: str, tmp: Path, tag: str) -> array:
        cleaned = clean_tts_text(text)
        if not cleaned:
            return array("h")

        # 0. 기존 생성된 고품질 멀티 보이스 오디오 캐시 파일 확인 및 매핑
        cached_wav = None
        if self.current_episode_id:
            ep_audio_dir = AUDIO_DIR / self.current_episode_id
            # 캐시 키: 태그 + 화자 + (화자|정제 텍스트) SHA256 → 대본 수정 시 자동 무효화
            cached_wav = ep_audio_dir / f"{tag}_{speaker}_{text_sha256(speaker, cleaned)[:12]}.wav"
            if cached_wav.exists() and cached_wav.stat().st_size > 500:
                pcm = self._decode_pcm(cached_wav)
                if pcm and len(pcm) > 0:
                    print(f"[TTS] 기존 고품질 멀티 보이스 오디오 매핑 로드: '{speaker}' ({len(pcm) / SR:.2f}s)")
                    return pcm

        # 1. 1순위: Gemini Native Audio TTS (감정 연기형 다중 성우)
        gemini_wav = tmp / f"{tag}_gemini.wav"
        if self._tts_gemini(cleaned, speaker, gemini_wav):
            pcm = self._decode_pcm(gemini_wav)
            if pcm and len(pcm) > 0:
                print(f"[TTS] Gemini Native TTS 성공: '{speaker}' ({len(pcm) / SR:.2f}s)")
                if cached_wav:
                    try:
                        shutil.copy2(gemini_wav, cached_wav)
                    except Exception:
                        pass
                return pcm

        # 2. 2순위 안전한 Fallback: edge-tts
        print(f"[TTS] Gemini TTS 불가 -> edge-tts 폴백 ({speaker})")
        prof = get_voice_profile(speaker, default_voice=self.voice, context_text=self.episode_context or text)
        mp3 = tmp / f"{tag}.mp3"
        if self._tts_edge(cleaned, mp3, prof["voice"], rate=prof["rate"], pitch=prof["pitch"]):
            pcm = self._decode_pcm(mp3)
            if pcm and len(pcm) > 0 and cached_wav:
                try:
                    shutil.copy2(mp3, cached_wav.with_suffix(".mp3"))
                except Exception:
                    pass
            return pcm
        if prof["voice"] != "ko-KR-InJoonNeural":
            if self._tts_edge(cleaned, mp3, "ko-KR-InJoonNeural", rate=prof["rate"], pitch=prof["pitch"]):
                return self._decode_pcm(mp3)

        # 3. 3순위 Fallback: gTTS
        if self._tts_gtts(cleaned, mp3):
            return self._decode_pcm(mp3)
        print(f"[TTS] '{speaker}' 세그먼트 음성 합성 실패 → 무음 처리")
        return array("h")

    def _synthesize_segment_tts(self, text: str, speaker: str, tmp: Path, tag: str) -> array:
        cleaned = clean_tts_text(text)
        if not cleaned:
            return array("h")

        phrases = split_long_narration(cleaned)
        if len(phrases) <= 1:
            raw_pcm = self._synthesize_subsegment_tts(cleaned, speaker, tmp, tag)
            return apply_pcm_padding_and_fade(raw_pcm, pad_sec=0.035, fade_ms=8.0)

        combined = array("h")
        intra_silence = array("h", [0] * int(0.08 * SR))
        for p_idx, phrase in enumerate(phrases):
            sub_pcm = self._synthesize_subsegment_tts(phrase, speaker, tmp, f"{tag}_p{p_idx}")
            if sub_pcm and len(sub_pcm) > 0:
                padded = apply_pcm_padding_and_fade(sub_pcm, pad_sec=0.02, fade_ms=6.0)
                if len(combined) > 0:
                    combined.extend(intra_silence)
                combined.extend(padded)
        return combined

    def _synthesize_tts(self, text: str, tmp: Path, tag: str) -> Tuple[array, List[Dict]]:
        segments = parse_speaker_segments(text)
        if not segments:
            return array("h"), []

        seg_data: List[Tuple[str, str, array]] = []
        for s_idx, (speaker, seg_content) in enumerate(segments):
            pcm = self._synthesize_segment_tts(seg_content, speaker, tmp, f"{tag}_seg_{s_idx:02d}")
            if pcm and len(pcm) > 0:
                seg_data.append((speaker, seg_content, pcm))

        if not seg_data:
            return array("h"), []

        # 화자 간 티키타카 전환 시 자연스러운 핑퐁 호흡(0.10초 무음 완충) 삽입
        silence_samples = int(0.10 * SR)
        silence = array("h", [0] * silence_samples)
        combined = array("h")
        timeline: List[Dict] = []

        lead_offset = 0.08  # _build_scene_audio 의 lead = 0.08s 와 100% 일치시켜 완벽 싱크 보장
        cur_t = lead_offset

        for i, (speaker, content, pcm) in enumerate(seg_data):
            if i > 0:
                combined.extend(silence)
                cur_t += 0.10
            seg_start = cur_t
            seg_dur = len(pcm) / SR
            seg_end = seg_start + seg_dur
            combined.extend(pcm)
            cur_t = seg_end
            timeline.append({
                "speaker": speaker,
                "text": content,
                "start": seg_start,
                "end": seg_end
            })

        return combined, timeline

    # ---------------- 오디오 트랙 ----------------
    def _build_scene_audio(self, scene: SceneBeat, idx: int, pose: str, tmp: Path) -> Tuple[Path, float, float, List[Dict]]:
        # 0. 기존 생성된 고품질 멀티 성우 씬 오디오 트랙(Gemini Native TTS) 확인 및 우선 매핑
        existing_scene_wav = None
        if self.current_episode_id:
            ep_scene_wav = AUDIO_DIR / self.current_episode_id / f"scene_{idx:02d}.wav"
            scene_digest = text_sha256(scene.tts_narration)
            if (ep_scene_wav.exists() and ep_scene_wav.stat().st_size > 1000
                    and audio_cache_valid(ep_scene_wav, scene_digest)):
                existing_scene_wav = ep_scene_wav

        if existing_scene_wav:
            print(f"[TTS] 기존 고품질 멀티 성우 씬 오디오 매핑 및 복원: {existing_scene_wav.name}")
            wav_path = tmp / f"audio_{idx:02d}.wav"
            shutil.copy2(existing_scene_wav, wav_path)
            pcm = self._decode_pcm(wav_path)
            target_dur = len(pcm) / SR

            # 타임라인 재구성 (화자별 말풍선 스위칭, 0.2초 이하 타이트한 무음 완충)
            segments = parse_speaker_segments(scene.tts_narration)
            timeline: List[Dict] = []
            if segments:
                total_len = sum(len(clean_tts_text(t)) for _, t in segments) or 1
                lead = 0.08
                tail = 0.12
                avail = max(0.2, target_dur - lead - tail)
                cur_t = lead
                for s_i, (speaker, seg_text) in enumerate(segments):
                    prop = len(clean_tts_text(seg_text)) / total_len
                    seg_dur = avail * prop
                    timeline.append({
                        "speaker": speaker,
                        "text": seg_text,
                        "start": cur_t,
                        "end": cur_t + seg_dur
                    })
                    cur_t += seg_dur

            cues = [c for c in parse_sfx_cues(scene.sfx_cue)]
            if not cues:
                cues = list(DEFAULT_SFX.get(pose, [(0.0, "pop")]))
            hit_t = 0.55
            for ts, name in cues:
                if canonical_sfx(name) == "punch_hit":
                    hit_t = min(max(ts, 0.3), max(0.3, target_dur - 0.9))
                    break
            return wav_path, target_dur, hit_t, timeline

        tts, timeline = self._synthesize_tts(scene.tts_narration, tmp, f"tts_{idx:02d}")
        lead, tail = 0.08, 0.12
        tts_len = len(tts) / SR
        dur = tts_len + lead + tail if tts_len > 0 else float(scene.duration_sec)

        # 씬 비디오 프레임 수와 정확히 일치하는 오디오 샘플 수 계산
        n_frames = max(1, int(round(dur * FPS)))
        target_dur = n_frames / FPS
        total_samples = int(round(target_dur * SR))

        cues = [c for c in parse_sfx_cues(scene.sfx_cue)]
        if not cues:
            cues = list(DEFAULT_SFX.get(pose, [(0.0, "pop")]))
        cues = [(min(max(ts, 0.0), target_dur - 0.2), name) for ts, name in cues]

        hit_t = 0.55
        for ts, name in cues:
            if canonical_sfx(name) == "punch_hit":
                hit_t = min(max(ts, 0.3), max(0.3, target_dur - 0.9))
                break

        buf = array("i", [0]) * total_samples
        start = int(lead * SR)
        for i in range(min(len(tts), total_samples - start)):
            buf[start + i] += tts[i]
        for ts, name in cues:
            pcm = self._decode_pcm(self.sfx.path(name))
            off = int(ts * SR)
            for i in range(min(len(pcm), total_samples - off)):
                buf[off + i] += int(pcm[i] * 0.7)

        # 씬 오디오 끝부분(마지막 15ms)에 마이크로 페이드아웃을 적용하여 씬 전환 시 팝 노이즈 원천 차단
        fade_end = min(int(0.015 * SR), total_samples)
        for k in range(fade_end):
            idx_rev = total_samples - 1 - k
            factor = k / float(fade_end)
            buf[idx_rev] = int(buf[idx_rev] * factor)

        out = array("h", (max(-32768, min(32767, v)) for v in buf))
        wav_path = tmp / f"audio_{idx:02d}.wav"
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SR)
            wf.writeframes(out.tobytes())
        return wav_path, target_dur, hit_t, timeline

    # ---------------- 비주얼 + 씬 클립 ----------------
    def _render_scene_visuals(self, scene: SceneBeat, idx: int, total: int, title: str, pose: str,
                              dur: float, hit_t: float, timeline: List[Dict], wav: Path, clip: Path, tmp: Path) -> None:
        sv = SceneVisuals(scene, idx, total, title, pose, dur, hit_t, timeline=timeline,
                          episode_setting=getattr(self, "episode_setting", ""))
        n = sv.n_frames
        cmd = [self.ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
               "-r", str(FPS), "-i", "-", "-i", wav.name, "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "160k", "-ar", str(SR), "-ac", "1", "-t", f"{n / FPS:.3f}", clip.name]
        log = tmp / f"ffmpeg_scene_{idx:02d}.log"
        with open(log, "wb") as logf:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=logf, cwd=str(tmp))
            try:
                for i in range(n):
                    proc.stdin.write(sv.frame(i).convert("RGB").tobytes())
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            proc.wait()
        if proc.returncode != 0 or not clip.exists():
            raise RuntimeError(f"씬 {idx + 1} 렌더 실패: {log.read_text(errors='ignore')[-800:]}")

    # ---------------- 최종 조립 ----------------
    def render_episode(self, script: Union[StickmanScriptOutput, str, Path],
                       progress: Optional[Callable[[str, float], None]] = None,
                       bgm_path: Optional[Union[str, Path]] = None,
                       bgm_volume: float = 0.15,
                       vocal_pocketing: bool = True) -> Path:
        def report(msg: str, frac: float) -> None:
            print(f"[Render {int(frac * 100):3d}%] {msg}")
            if progress:
                progress(msg, frac)

        if isinstance(script, (str, Path)):
            script_path = Path(script)
            if not script_path.exists():
                script_path = APPROVED_DIR / f"{script}.json"
            if not script_path.exists():
                raise FileNotFoundError(f"에피소드 스크립트 파일을 찾을 수 없습니다: {script}")
            script = StickmanScriptOutput.model_validate_json(script_path.read_text(encoding="utf-8"))

        # 에피소드 전체 문맥 (성별 보이스 매칭 및 소품 연출에 활용)
        self.episode_context = f"{script.episode_title} " + " ".join(
            f"{s.stickman_action} {s.tts_narration} {s.subtitle_text}" for s in script.scenes
        )

        # 대본의 감정선 분석(transplanted_setting), 제목, 각 씬 지문/대사를 종합 분석하여 장소 자동 판별
        all_scene_texts = [f"{s.stickman_action} {s.tts_narration} {s.subtitle_text}" for s in script.scenes]
        setting_desc = getattr(getattr(script, "analysis", None), "transplanted_setting", "")
        self.episode_setting = detect_setting(setting_desc, script.episode_title, all_scene_texts)
        print(f"[Renderer:Setting] 대본 기반 장소 자동 감지 완료 -> '{self.episode_setting}' (설정 지문: {setting_desc})")

        self.current_episode_id = script.episode_id
        ep_audio_dir = AUDIO_DIR / script.episode_id
        ep_audio_dir.mkdir(parents=True, exist_ok=True)

        tmp = Path(tempfile.mkdtemp(prefix="stickman_"))
        try:
            total = len(script.scenes)
            clips: List[str] = []
            master_audio_pcm = array("h")
            for i, scene in enumerate(script.scenes):
                pose = select_pose(scene.stickman_action, i, total)
                report(f"씬 {i + 1}/{total} 음성·효과음 합성 ({pose})", i / total * 0.9)
                wav, dur, hit_t, timeline = self._build_scene_audio(scene, i, pose, tmp)
                scene_pcm = self._decode_pcm(wav)
                master_audio_pcm.extend(scene_pcm)

                report(f"씬 {i + 1}/{total} 스틱맨 작화·인코딩 ({dur:.1f}s)", (i + 0.3) / total * 0.9)
                clip = tmp / f"scene_{i:02d}.mp4"
                self._render_scene_visuals(scene, i, total, script.episode_title, pose, dur, hit_t, timeline, wav, clip, tmp)
                clips.append(clip.name)

            report("전체 씬 및 마스터 오디오 이어 붙이기", 0.93)
            # 1. 완벽한 연속 마스터 오디오 WAV 저장 (씬 간 오디오 글리치/삑사리 원천 차단)
            master_wav = tmp / "master_audio.wav"
            existing_master = ep_audio_dir / "master_audio.wav"
            master_digest = text_sha256(*(s_.tts_narration for s_ in script.scenes))
            if (existing_master.exists() and existing_master.stat().st_size > 10000
                    and audio_cache_valid(existing_master, master_digest,
                                          expected_sec=len(master_audio_pcm) / SR)):
                print(f"[Renderer] 기존 마스터 오디오 트랙 복원 및 매핑: {existing_master.name}")
                shutil.copy2(existing_master, master_wav)
            else:
                with wave.open(str(master_wav), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SR)
                    wf.writeframes(master_audio_pcm.tobytes())
                try:
                    shutil.copy2(master_wav, existing_master)
                    write_audio_digest(existing_master, master_digest)
                except Exception:
                    pass

            # 1-1. BGM 오디오 더킹(Auto-Ducking) 및 보컬 포켓팅 믹싱 적용
            final_audio_path = master_wav
            if bgm_path:
                bgm_p = Path(bgm_path)
                if not bgm_p.is_absolute():
                    bgm_p = BGM_DIR / bgm_p
                if bgm_p.exists() and bgm_p.name.lower() not in ("none", "bgm 없음", "없음"):
                    pocket_tag = " (보컬 포켓팅 ON)" if vocal_pocketing else ""
                    report(f"BGM 오디오 더킹{pocket_tag} 믹싱 적용 중...", 0.94)
                    # MP3 선택 시 동명의 무손실 WAV 파일이 존재하면 WAV 우선 매핑
                    if bgm_p.suffix.lower() == ".mp3":
                        wav_candidate = bgm_p.with_suffix(".wav")
                        if wav_candidate.exists():
                            bgm_p = wav_candidate
                    mixed_wav = tmp / "master_audio_with_bgm.wav"
                    mix_bgm_with_auto_ducking(
                        master_audio_path=master_wav,
                        bgm_path=bgm_p,
                        output_path=mixed_wav,
                        bgm_volume=bgm_volume,
                        vocal_pocketing=vocal_pocketing
                    )
                    final_audio_path = mixed_wav

            # 2. 비디오 클립 연결 파일
            (tmp / "concat.txt").write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")

            # 3. 비디오 concat + 연속 무중단 마스터 오디오 단일 인코딩 (Stereo AAC 192k)
            self._run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", "concat.txt",
                "-i", final_audio_path.name,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2",
                "-movflags", "+faststart", "-shortest", "final.mp4"
            ], cwd=tmp)

            out_path = RENDERED_DIR / f"{script.episode_id}.mp4"
            if out_path.exists():
                try:
                    out_path.unlink()
                except Exception as e:
                    print(f"[Renderer] 기존 MP4 파일 덮어쓰기 준비 (삭제 경고): {e}")
            shutil.copy2(tmp / "final.mp4", out_path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        script.rendered_video_path = str(out_path)
        json_path = APPROVED_DIR / f"{script.episode_id}.json"
        if json_path.exists():
            atomic_write_text(json_path, script.model_dump_json(indent=2))
        report("렌더링 완료", 1.0)
        return out_path


if __name__ == "__main__":
    files = sorted(APPROVED_DIR.glob("*.json"), reverse=True)
    if not files:
        print("approved/ 폴더에 승인된 대본이 없습니다.")
    else:
        data = StickmanScriptOutput.model_validate(json.loads(files[0].read_text(encoding="utf-8")))
        print(StickmanShortRenderer().render_episode(data))
