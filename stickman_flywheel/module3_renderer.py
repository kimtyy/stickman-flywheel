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
for _d in (APPROVED_DIR, RENDERED_DIR, AUDIO_DIR, SFX_DIR, BGM_DIR):
    _d.mkdir(parents=True, exist_ok=True)

W, H, FPS = 1080, 1920, 30
SR = 44100
SC = 1.6                      # 스틱맨 스케일
FLOOR_Y = 1330                # 바닥선 y
MAIN_X, FOE_X = 360, 720
LINE_W = 22

FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/NanumGothicBold.ttf", "C:/Windows/Fonts/gulim.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]
_FONT_CACHE: Dict[int, ImageFont.FreeTypeFont] = {}


def get_font(size: int):
    size = int(size)
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
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
               pose_a: str, pose_b: str, t: float) -> None:
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

    # 4. 맞춤형 참교육 소품 (10T 강철 당근, 20KG 바벨, 100T 참교육 철퇴/무게추)
    is_climax_crush = (pose_b in ("crushed", "fall", "hit") or any(k in text for k in [
        "짓눌", "깔려", "납작", "개구리", "살려", "사람 살려", "으아악", "200kg", "100kg", "참교육"
    ]))
    has_barbell_theme = any(k in text for k in [
        "바벨", "원판", "스쿼트", "랙", "덤벨", "벤치", "쇠질", "무반동", "세트", "중량"
    ])
    has_carrot_theme = any(k in text for k in ["당근", "중고", "네고", "매너온도"])

    if is_climax_crush:
        bx = fig_b["hip"][0] + 15
        by = FLOOR_Y - 45
        if has_carrot_theme or "당근" in text:
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


def make_background(color, pose: str, scene_text: str = "") -> Image.Image:
    text = (scene_text or "").lower()
    img = Image.new("RGB", (W, H), color)
    d = ImageDraw.Draw(img)

    is_carrot = any(k in text for k in ["당근", "중고", "네고", "지하철", "거래", "출구", "역", "봉투", "차비", "쿨거래", "매너"])
    is_gym = any(k in text for k in ["헬스", "스쿼트", "바벨", "덤벨", "쇠질", "무반동", "세트", "중량"])

    if is_carrot:
        # [야외 당근마켓 / 지하철역 4번 출구 앞 노을빛 도심 거리 배경]
        # 1. 하늘 그라디언트 (상단 딥 네이비 퍼플 -> 중간 노을빛 마젠타/오렌지 -> 지평선 웜 피치)
        for y in range(0, FLOOR_Y):
            ratio = y / float(FLOOR_Y)
            if ratio < 0.45:
                # 딥 네이비 퍼플 -> 노을빛 마젠타
                k = ratio / 0.45
                r = int(24 * (1 - k) + 115 * k)
                g = int(22 * (1 - k) + 48 * k)
                b = int(48 * (1 - k) + 85 * k)
            else:
                # 노을빛 마젠타 -> 따뜻한 노을 오렌지/피치
                k = (ratio - 0.45) / 0.55
                r = int(115 * (1 - k) + 245 * k)
                g = int(48 * (1 - k) + 140 * k)
                b = int(85 * (1 - k) + 80 * k)
            d.line([(0, y), (W, y)], fill=(r, g, b))

        # 2. 먼 도심 스카이라인 실루엣 (Layer 1 - 원경 빌딩군)
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
            # 아파트 창문 (노란/시안 불빛)
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

        # 5. 지하철역 4번 출구 폴대 간판 (Iconic Subway Exit 4 Pole)
        pole_x = 940
        pole_top = FLOOR_Y - 380
        # 메탈 기둥
        d.rectangle((pole_x - 10, pole_top + 100, pole_x + 10, FLOOR_Y), fill=(45, 50, 60), outline=(20, 22, 28), width=2)
        # 상단 사각 간판 박스
        d.rounded_rectangle((pole_x - 70, pole_top, pole_x + 70, pole_top + 100), radius=10, fill=(30, 35, 45), outline=(220, 220, 230), width=3)
        # 지하철 라인 동그라미 심볼 (노란색 3호선)
        d.ellipse((pole_x - 52, pole_top + 14, pole_x - 14, pole_top + 52), fill=(255, 175, 20), outline=(255, 255, 255), width=2)
        d.text((pole_x - 33, pole_top + 33), "4", font=get_font(26), fill=(255, 255, 255), anchor="mm")
        # 텍스트
        d.text((pole_x + 24, pole_top + 28), "지하철", font=get_font(18), fill=(240, 240, 250), anchor="mm")
        d.text((pole_x + 24, pole_top + 52), "4번출구", font=get_font(20), fill=(255, 225, 40), anchor="mm")
        # 하단 화살표
        d.polygon([(pole_x - 16, pole_top + 78), (pole_x + 16, pole_top + 78), (pole_x, pole_top + 92)], fill=(255, 255, 255))

        # 6. 클래식 도심 가로등 (좌측 x=140) & 노란빛 원뿔 (Light Cone)
        lx = 140
        ly_head = FLOOR_Y - 440
        # 가로등 조명 빛 원뿔 (가로등에서 바닥으로 은은하게 쏟아지는 조명)
        cone_poly = [
            (lx - 25, ly_head + 30), (lx + 25, ly_head + 30),
            (lx + 260, FLOOR_Y), (lx - 120, FLOOR_Y)
        ]
        d.polygon(cone_poly, fill=(120, 95, 60))
        # 가로등 기둥
        d.rectangle((lx - 8, ly_head + 30, lx + 8, FLOOR_Y), fill=(35, 38, 44), outline=(15, 18, 22), width=2)
        # 가로등 갓 및 등
        d.polygon([(lx - 45, ly_head + 30), (lx + 45, ly_head + 30), (lx + 20, ly_head), (lx - 20, ly_head)],
                  fill=(45, 50, 58), outline=(15, 18, 22))
        d.ellipse((lx - 26, ly_head + 22, lx + 26, ly_head + 46), fill=(255, 245, 160), outline=(255, 200, 50), width=3)

        # 7. 보도블록 바닥 (FLOOR_Y to H)
        d.rectangle((0, FLOOR_Y, W, H), fill=(95, 95, 110))
        # 연석(Curbstone) 라인
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 16), fill=(40, 42, 50), outline=(20, 20, 25))
        # 격자 보도블록 패턴
        tile_h = 52
        for r_idx, ty in enumerate(range(FLOOR_Y + 16, H, tile_h)):
            d.line([(0, ty), (W, ty)], fill=(45, 45, 55), width=3)
            shift = 45 if r_idx % 2 == 1 else 0
            for tx in range(shift, W + 90, 90):
                d.line([(tx, ty), (tx, min(H, ty + tile_h))], fill=(50, 50, 62), width=3)
        return img

    elif is_gym:
        # [헬스장 배경]
        d.rectangle((0, 0, W, FLOOR_Y), fill=(45, 48, 56))
        # 대형 거울
        d.rectangle((80, 180, W - 80, FLOOR_Y - 40), fill=(65, 72, 85), outline=(180, 185, 195), width=8)
        # 거울 반사 사선 하이라이트
        for sx in (260, 620):
            d.polygon([(sx, 180), (sx + 80, 180), (sx - 60, FLOOR_Y - 40), (sx - 140, FLOOR_Y - 40)], fill=(85, 95, 112))
        # 고무 매트 바닥
        d.rectangle((0, FLOOR_Y, W, H), fill=(32, 34, 40))
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 14), fill=(18, 18, 24))
        for gy in range(FLOOR_Y + 14, H, 60):
            d.line([(0, gy), (W, gy)], fill=(22, 24, 28), width=3)
            for gx in range(0, W, 80):
                d.line([(gx, gy), (gx, min(H, gy + 60))], fill=(22, 24, 28), width=3)
        return img

    else:
        # 기본 패턴 벽지 (리치한 웹툰 톤)
        dot = tuple(max(0, c - 28) for c in color)
        for row, y in enumerate(range(0, FLOOR_Y + 40, 46)):
            r = max(2, int(13 * (1 - y / (FLOOR_Y + 40))) + 2)
            for x in range(-23 if row % 2 else 0, W + 46, 46):
                d.ellipse((x - r, y - r, x + r, y + r), fill=dot)
        floor = tuple(max(0, c - 70) for c in color)
        d.rectangle((0, FLOOR_Y, W, H), fill=floor)
        d.rectangle((0, FLOOR_Y, W, FLOOR_Y + 12), fill=(25, 25, 35))
        for x in range(0, W + 120, 120):
            d.polygon([(x, FLOOR_Y + 12), (x + 60, FLOOR_Y + 12), (x + 20, FLOOR_Y + 160), (x - 40, FLOOR_Y + 160)],
                      fill=tuple(max(0, c - 85) for c in color))
        return img


def make_overlay(title: str, idx: int, total: int, subtitle: str, action: str) -> Image.Image:
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)

    # 1. 상단 타이틀 배너 (씬 번호 배지 완전 삭제, 타이틀 텍스트가 박스 중앙에 여유 있게 배치)
    banner_y0, banner_y1 = 36, 196
    d.rounded_rectangle((40, banner_y0, W - 40, banner_y1), radius=36, fill=(18, 18, 28, 235),
                        outline=(255, 214, 10, 255), width=6)

    # 제목 텍스트 (박스 중앙에 여유 있게 최대 880px 폭으로 배치)
    size, lines = 56, []
    while size >= 36:
        font = get_font(size)
        lines = wrap_text(title, font, 880)
        if len(lines) <= 2:
            break
        size -= 4
    lines = lines[:2]
    ly = 116 - (len(lines) - 1) * size * 0.58
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
                target: Optional[Tuple[float, float]] = None) -> None:
    raw_text = (text or "").strip()
    if not raw_text:
        return

    spk_raw = (speaker or "").strip()
    spk_u = spk_raw.upper()

    # 화자 유형 분류 (A, B, 해설)
    is_narration = not spk_raw or spk_raw in ("해설", "내레이션", "전체", "상황")
    is_a = not is_narration and (spk_u in ("A", "주인공", "남자") or ("A" in spk_u and "B" not in spk_u))
    is_b = not is_narration and (spk_u in ("B", "빌런", "상대", "여자") or ("B" in spk_u and "A" not in spk_u))

    # 하위 호환 target 파라미터 처리
    if target is not None:
        if is_b:
            head_b = target
        else:
            head_a = target

    # 1. 폰트 크기 및 줄바꿈
    max_wrap_w = 800 if is_narration else 680
    chosen_size = 46
    chosen_lines: List[str] = []
    chosen_font = None

    for sz in (48, 44, 40, 36):
        fnt = get_font(sz)
        lines = wrap_text(raw_text, fnt, max_wrap_w)
        chosen_size = sz
        chosen_font = fnt
        chosen_lines = lines
        if len(lines) <= 2:
            break

    if len(chosen_lines) > 3:
        chosen_lines = chosen_lines[:3]
        chosen_lines[-1] = chosen_lines[-1][:-1] + "…"

    font = chosen_font
    line_h = int(chosen_size * 1.34)
    max_text_w = max(font.getlength(ln) for ln in chosen_lines)

    # 2. 박스 크기 및 균형 잡힌 상하좌우 패딩 (미니 배지 제거로 상하 대칭 패딩 26px 적용)
    pad_v = 28
    pad_h = 44
    bh = pad_v * 2 + len(chosen_lines) * line_h
    bw = min(960 if is_narration else 840, max(int(max_text_w + pad_h * 2), 400))

    y0 = 224
    y1 = y0 + bh

    if is_a:
        # 좌측 상단 (화자 A 머리 위로 치우치게 배치)
        x0 = 60
        x1 = x0 + bw
        target_head = head_a or (360, 680)
        border_color = (25, 75, 210)  # Royal Blue
        border_width = 7
        box_fill = (255, 255, 255)
    elif is_b:
        # 우측 상단 (화자 B 머리 위로 치우치게 배치)
        x1 = W - 60
        x0 = x1 - bw
        target_head = head_b or (720, 680)
        border_color = (220, 35, 35)  # Crimson Red
        border_width = 7
        box_fill = (255, 255, 255)
    else:
        # 해설: 꼬리 없는 상단 중앙 와이드 자막 박스, 웜 아이보리/연노랑(#FFFBEB) 배경 + 굵은 골드 테두리
        x0 = int(W / 2 - bw / 2)
        x1 = x0 + bw
        target_head = None
        border_color = (245, 150, 15)  # Amber Gold
        border_width = 8
        box_fill = (255, 251, 235)  # Warm Ivory (#FFFBEB)

    # 3. 말풍선 꼬리 그리기 (화자 A 또는 B일 때만 동적 꼬리 생성, 해설은 꼬리 없음)
    if target_head is not None and target_head[1] > y1 + 15:
        tx = min(max(target_head[0], x0 + 70), x1 - 70)
        ty = min(target_head[1], y1 + 220)  # 꼬리 길이 상한 220px 제한
        d.polygon([(tx - 32, y1 - 4), (tx + 32, y1 - 4), (tx, ty)], fill=box_fill, outline=border_color)

    # 4. 말풍선 본체 (부드러운 라운디드 사각형)
    d.rounded_rectangle((x0, y0, x1, y1), radius=32, fill=box_fill, outline=border_color, width=border_width)

    # 꼬리 연결부 매꿈
    if target_head is not None and target_head[1] > y1 + 15:
        d.rectangle((tx - 26, y1 - 6, tx + 26, y1 + 2), fill=box_fill)

    # 5. 본문 텍스트 렌더링 (상하 정중앙 수직 밸런스 및 가독성 최적화)
    text_color = (20, 20, 30) if is_narration else (15, 15, 25)
    for i, ln in enumerate(chosen_lines):
        ly = y0 + pad_v + i * line_h
        if is_narration:
            # 해설은 박스 정중앙 정렬
            d.text((W / 2, ly), ln, font=font, fill=text_color, anchor="mt")
        else:
            # 대사는 좌우 패딩을 고르게 맞춘 깔끔한 좌측 정렬
            line_start_x = x0 + int((bw - max_text_w) / 2)
            d.text((line_start_x, ly), ln, font=font, fill=text_color)



# =====================================================================
# 7. 씬 비주얼 렌더러
# =====================================================================
class SceneVisuals:
    def __init__(self, scene: SceneBeat, idx: int, total: int, title: str, pose: str,
                 dur: float, hit_t: float, timeline: Optional[List[Dict]] = None) -> None:
        self.scene, self.idx, self.total, self.dur = scene, idx, total, dur
        self.n_frames = max(1, int(round(dur * FPS)))
        self.ctx = {"hit_t": hit_t, "land_t": 0.9}
        self.cam = parse_camera(scene.camera_work)
        self.scene_text = f"{scene.stickman_action} {scene.tts_narration} {scene.subtitle_text}"
        bg_color = POSE_BG.get(pose, BG_COLORS[idx % len(BG_COLORS)])
        self.bg = make_background(bg_color, pose, self.scene_text)
        overlay = make_overlay(title, idx, total, scene.subtitle_text, scene.stickman_action)
        # 상단 오버레이는 타이틀 바 높이(204px)까지만 크롭하여 말풍선과 절대 겹치지 않도록 보장
        self.top = overlay.crop((0, 0, W, 204))
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

    def get_active_segment(self, t: float) -> Optional[Dict]:
        if not self.timeline:
            return None
        if t < self.timeline[0]["start"]:
            return self.timeline[0]
        if t >= self.timeline[-1]["end"]:
            return self.timeline[-1]
        for k in range(len(self.timeline)):
            seg = self.timeline[k]
            if seg["start"] <= t <= seg["end"]:
                return seg
            if k + 1 < len(self.timeline):
                next_seg = self.timeline[k + 1]
                if seg["end"] < t < next_seg["start"]:
                    mid = (seg["end"] + next_seg["start"]) / 2.0
                    return seg if t < mid else next_seg
        return self.timeline[-1]

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
        draw_props(d, main_fig, foe_fig, self.scene_text, self.main_pose, self.foe_pose, ta)

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

        # 현재 시간(t)에 발화 중인 화자별 대사/해설 말풍선 실시간 렌더링
        if active_seg:
            draw_bubble(d_ui, active_seg.get("speaker", ""), active_seg.get("text", ""),
                        head_a=head_a_screen, head_b=head_b_screen)

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
            cached_wav = ep_audio_dir / f"{tag}_{speaker}.wav"
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

        lead_offset = 0.25  # _build_scene_audio 에서 lead = 0.25s 뒤에 tts 배치
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
            if ep_scene_wav.exists() and ep_scene_wav.stat().st_size > 1000:
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
        sv = SceneVisuals(scene, idx, total, title, pose, dur, hit_t, timeline=timeline)
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
            if existing_master.exists() and existing_master.stat().st_size > 10000:
                print(f"[Renderer] 기존 마스터 오디오 트랙 복원 및 매핑: {existing_master.name}")
                shutil.copy2(existing_master, master_wav)
            else:
                with wave.open(str(master_wav), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SR)
                    wf.writeframes(master_audio_pcm.tobytes())
                try:
                    shutil.copy2(master_wav, ep_audio_dir / "master_audio.wav")
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
            json_path.write_text(script.model_dump_json(indent=2), encoding="utf-8")
        report("렌더링 완료", 1.0)
        return out_path


if __name__ == "__main__":
    files = sorted(APPROVED_DIR.glob("*.json"), reverse=True)
    if not files:
        print("approved/ 폴더에 승인된 대본이 없습니다.")
    else:
        data = StickmanScriptOutput.model_validate(json.loads(files[0].read_text(encoding="utf-8")))
        print(StickmanShortRenderer().render_episode(data))
