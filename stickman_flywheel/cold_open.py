"""콜드 오픈 인트로(0.0~1.2초): 컷 5 티저 비주얼 + 임팩트 효과음 1회 → 글리치/화이트 플래시로 컷 1 진입.

- 비주얼: thumbnail_maker.compose_teaser() 가 만든 PNG(= 썸네일)를 그대로 쓴다. 0프레임이 썸네일과 픽셀 단위로 같다.
- 오디오: 인트로 구간은 효과음 1회 + 무음만. 본편(TTS+BGM 믹스)은 인트로 길이만큼 뒤로 밀려 1.2초 시점에 시작한다.
- 싱크: 인트로 프레임 수 n 에서 샘플 수를 round(n * sr / fps) 로 계산해 영상/오디오 오프셋이 정확히 같다.
- 설정: config/upload_settings.json 의 cold_open_sec (0 이면 끔, 기본 1.2). 환경변수 COLD_OPEN_SEC 가 우선.
"""
import json
import math
import os
import wave
from array import array
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import numpy as np
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "config" / "upload_settings.json"
SFX_PATH = BASE_DIR / "assets" / "sfx" / "dramatic_thud.wav"
DEFAULT_SEC = 1.2
GLITCH_TAIL = 4          # 인트로 마지막 N프레임: 글리치 + 플래시 상승
FLASH_HEAD = (0.55, 0.32, 0.14)   # 컷 1 첫 프레임들의 화이트 플래시 잔광


def cold_open_sec() -> float:
    try:
        raw = os.getenv("COLD_OPEN_SEC")
        if raw is None:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("cold_open_sec", DEFAULT_SEC)
        return max(0.0, min(3.0, float(raw)))
    except (OSError, ValueError, TypeError):
        return DEFAULT_SEC


# ------------------------------------------------------------------ 비주얼
def _ease(u: float) -> float:
    return u * u * (3 - 2 * u)


def _glitch(arr: np.ndarray, k: float, seed: int) -> np.ndarray:
    """RGB 채널 가로 분리 + 가로 띠 어긋남. k 는 0~1 강도."""
    rnd = np.random.default_rng(seed)
    out = arr.copy()
    sh = int(10 + 36 * k)
    out[..., 0] = np.roll(arr[..., 0], sh, axis=1)
    out[..., 2] = np.roll(arr[..., 2], -sh, axis=1)
    h = arr.shape[0]
    for _ in range(int(4 + 8 * k)):
        y0 = int(rnd.integers(0, h - 80))
        bh = int(rnd.integers(20, 90))
        out[y0:y0 + bh] = np.roll(out[y0:y0 + bh], int(rnd.integers(-90, 90) * (0.4 + k)), axis=1)
    return out


def _flash(arr: np.ndarray, alpha: float) -> np.ndarray:
    return (arr * (1 - alpha) + 255 * alpha).astype(np.uint8)


def intro_frames(teaser: Image.Image, n: int) -> Iterator[Image.Image]:
    """n 프레임. 0프레임은 티저 원본 그대로, 이후 서서히 줌인(최대 1.07배)하다가 마지막 GLITCH_TAIL 프레임에 글리치+플래시."""
    base = teaser.convert("RGB")
    W, H = base.size
    for i in range(n):
        u = i / max(1, n - 1)
        z = 1.0 + 0.07 * _ease(u)
        if z > 1.0:
            vw, vh = W / z, H / z
            fr = base.resize((W, H), Image.BILINEAR, box=((W - vw) / 2, (H - vh) / 2, (W + vw) / 2, (H + vh) / 2))
        else:
            fr = base
        tail = i - (n - GLITCH_TAIL)
        if tail >= 0:
            k = (tail + 1) / GLITCH_TAIL
            arr = _flash(_glitch(np.asarray(fr), k, seed=i), 0.15 + 0.75 * k * k)
            fr = Image.fromarray(arr)
        yield fr


def entry_effect(frame: Image.Image, k: int) -> Image.Image:
    """컷 1 의 k(0부터) 번째 프레임에 흰 플래시 잔광과 약한 글리치를 입힌다. k 가 범위를 벗어나면 원본."""
    if k >= len(FLASH_HEAD):
        return frame
    arr = np.asarray(frame.convert("RGB"))
    arr = _glitch(arr, 0.35 * (1 - k / len(FLASH_HEAD)), seed=100 + k)
    return Image.fromarray(_flash(arr, FLASH_HEAD[k]))


# ------------------------------------------------------------------ 오디오
def _read_wav(p: Path):
    with wave.open(str(p), "rb") as w:
        ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    if sw != 2:
        raise ValueError(f"16bit PCM 만 지원: {p.name}")
    return np.frombuffer(data, dtype="<i2").reshape(-1, ch).astype(np.float32), sr


def build_intro_audio(body_wav: Path, out_wav: Path, n_frames: int, fps: int, sfx: Path = SFX_PATH) -> Path:
    """out_wav = [효과음 1회 + 무음 = n_frames/fps 초] + body_wav 전체. body 의 샘플레이트/채널에 맞춘다."""
    body, sr = _read_wav(body_wav)
    ch = body.shape[1]
    n_intro = int(round(n_frames * sr / fps))
    fx, fsr = _read_wav(sfx)
    fx = fx.mean(axis=1)
    if fsr != sr:                                           # 선형 보간 리샘플
        fx = np.interp(np.linspace(0, len(fx) - 1, int(len(fx) * sr / fsr)), np.arange(len(fx)), fx)
    fx = fx[:n_intro].copy()
    fade = min(len(fx), int(0.05 * sr))
    if fade:
        fx[-fade:] *= np.linspace(1, 0, fade)               # 끝이 잘려도 틱 소리가 나지 않게
    peak = float(np.abs(fx).max()) or 1.0
    fx *= min(1.0, 0.92 * 32767 / peak)
    intro = np.zeros((n_intro, ch), dtype=np.float32)
    intro[:len(fx)] = fx[:, None]
    mixed = np.clip(np.concatenate([intro, body]), -32768, 32767).astype("<i2")
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(mixed.tobytes())
    return out_wav


# ------------------------------------------------------------------ 렌더러 연동 묶음
class ColdOpen:
    def __init__(self, sec: float, fps: int, teaser: Image.Image, teaser_path: Path) -> None:
        self.fps, self.teaser, self.teaser_path = fps, teaser, teaser_path
        self.n = int(round(sec * fps))
        self.sec = self.n / fps

    @classmethod
    def prepare(cls, script: Any, fps: int, log=print) -> Optional["ColdOpen"]:
        """티저/썸네일 세트를 만들고 ColdOpen 을 돌려준다. 꺼져 있거나 실패하면 None(본편만 렌더)."""
        sec = cold_open_sec()
        if sec <= 0:
            return None
        try:
            from thumbnail_maker import make_thumbnails
            r = make_thumbnails(script)
            return cls(sec, fps, Image.open(r["vertical_png"]).convert("RGB"), Path(r["vertical_png"]))
        except Exception as e:  # noqa: BLE001  - 인트로 실패가 렌더 실패가 되어서는 안 된다
            log(f"[ColdOpen] 인트로 생략: {type(e).__name__}: {e}")
            return None

    def frames(self) -> Iterator[Image.Image]:
        return intro_frames(self.teaser, self.n)

    def with_audio(self, body_wav: Path, out_wav: Path) -> Path:
        return build_intro_audio(body_wav, out_wav, self.n, self.fps)
