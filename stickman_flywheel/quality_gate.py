"""대본 생성 직후 품질·안전성 검증(Quality Gate).

Gemini 가 만든 대본을 pending 에 저장하기 전에 세 가지를 검사한다.
  1) 안전성: 비속어/혐오/성적/자해/약물/심각한 폭력 표현(노란딱지·가이드라인 위반 방지)
  2) 구조: 6컷, 총 길이, 대사·자막·제목 길이, 프롬프트 규칙(괄호 지문/자음 남발 금지)
  3) 소재 중복: 최근 소재(Avoid List)와의 의미 유사도 (임베딩 코사인, 불가 시 문자열 유사도로 대체)
severity=fail 이 하나라도 있으면 불합격(재발상), warn 은 기록만 한다. 순수 함수 위주라 API 없이 테스트할 수 있다.
"""
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

LOG_FILE = Path(__file__).resolve().parent / "pipeline_outputs" / "quality_gate_log.jsonl"

# --- 임계값 (기존 에피소드 17편 + 신규 샘플로 보정: 같은 이야기 0.87~0.93, 새 소재 최대 0.82)
SIM_FAIL = 0.86            # 임베딩 코사인 유사도
LEX_SIM_FAIL = 0.60        # 임베딩 불가 시 문자열 바이그램 Jaccard (사실상 복붙 수준만 탐지)
SIM_WARN_MARGIN = 0.04
REQUIRED_CUTS = 6
DURATION_RANGE = (22, 42)  # 시스템 프롬프트 규격 25~40초 ± 허용
MAX_NARRATION_PER_SCENE = 90
MAX_NARRATION_TOTAL = 400
SUBTITLE_SOFT = 20          # 권장(프롬프트 규격): 초과해도 경고만
MAX_SUBTITLE = 24           # 하드 한도: 렌더러가 2줄·최소 글자 크기로 소화할 수 있는 상한
MAX_TITLE = 45


@dataclass
class Issue:
    code: str
    severity: str      # "fail" | "warn"
    message: str       # 재발상 프롬프트에 그대로 쓰일 수 있는 사유(원문 비속어는 넣지 않는다)
    detail: str = ""   # 로그 전용(매칭된 원문 등)


@dataclass
class GateReport:
    issues: List[Issue] = field(default_factory=list)
    similarity: Dict[str, Any] = field(default_factory=dict)

    @property
    def fails(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == "fail"]

    @property
    def passed(self) -> bool:
        return not self.fails


# --------------------------------------------------------------------------- 1) 안전성
# (코드, 라벨, 정규식, 심각도). 정규식은 NFKC 정규화한 소문자 텍스트에 적용한다.
SAFETY_RULES = [
    ("profanity", "강한 욕설/비속어",
     r"씨발|씨팔|시발(?!점|역|지)|ㅅㅂ|ㅂㅅ|병신|지랄|염병|좆|존나|개새끼|쌍년|썅|씹새|씹쌔|씹창|씹할|엿먹|엿 먹|애미|느금|니미", "fail"),
    ("hate", "혐오·차별 표현",
     r"한남(?!동|대|역)|김치녀|된장녀|메갈|일베|맘충|틀딱|급식충|짱깨|쪽발이|쪽바리|똥남아|흑형|조센징|장애인 ?새끼|정신병자", "fail"),
    ("sexual", "성적 표현/성범죄",
     r"강간|성폭행|성추행|성희롱|몰카|야동|섹스|성관계|자위(?!대|권)", "fail"),
    ("self_harm", "자해·자살 관련",
     r"자살|자해|극단적 선택|목을 ?매|투신|손목을 ?긋", "fail"),
    ("drugs", "마약류",
     r"마약(?!\s?(?:김밥|옥수수|떡볶이|토스트|치킨|계란|같|처럼))|필로폰|대마초|코카인|히로뽕|엑스터시", "fail"),
    ("violence_severe", "심각한 폭력/살인",
     r"살인(?!적|미소|스케줄)|살해|시체|사체|토막 ?내|토막 ?살|칼부림|난도질|방화(?!벽|문|셔터)|인질", "fail"),
    ("gore_visual", "잔인/선정적 시각 지시(이미지 생성 위험)",
     r"\b(gore|corpse|nude|naked|cocaine|porn|beheaded)\b", "fail"),
    # 기록만 하는 경고
    ("violence_mild", "폭력 암시 표현", r"죽여|죽어|죽을|죽인|식칼|칼날|칼을|흉기|총기|총알|권총|총을|피투성|피를|폭탄", "warn"),
    ("weapon_visual", "무기류 시각 지시", r"\b(gun|rifle|pistol|knife|bomb|blood)\b", "warn"),
    ("brand", "실제 상호/브랜드 노출",
     r"\bcu\b|gs ?25|세븐일레븐|이마트|롯데|삼성|쿠팡|배민|당근마켓|스타벅스|맥도날드", "warn"),
    ("sensitive", "정치·종교 등 민감 소재", r"대통령|국회의원|정당|종교|교회|무당", "warn"),
]


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[​-‏⁠﻿]", "", t)
    return t.lower()


def _script_texts(script) -> List[tuple]:
    """(필드명, 텍스트). 내부 메모(discarded_surface_topic)는 시청자에게 노출되지 않으므로 제외."""
    a = script.analysis
    out = [("title", script.episode_title), ("emotion", script.target_emotion),
           ("hashtags", " ".join(script.hashtags)), ("setting", a.transplanted_setting),
           ("twist", a.twist_point), ("desire", a.extracted_core_desire)]
    for s in script.scenes:
        n = s.scene_number
        out += [(f"scene{n}.action", s.stickman_action), (f"scene{n}.camera", s.camera_work),
                (f"scene{n}.narration", s.tts_narration), (f"scene{n}.subtitle", s.subtitle_text),
                (f"scene{n}.impact", s.impact_keyword), (f"scene{n}.props", " ".join(s.visual_props)),
                (f"scene{n}.sfx", s.sfx_cue)]
    return out


def check_safety(script) -> List[Issue]:
    issues: List[Issue] = []
    texts = [(name, _norm(t)) for name, t in _script_texts(script)]
    for code, label, pattern, severity in SAFETY_RULES:
        rx = re.compile(_norm(pattern))   # 텍스트와 같은 정규화(NFKC)를 적용해야 호환 자모(ㅅㅂ 등)가 일치한다
        hits = [(name, m.group(0)) for name, t in texts for m in rx.finditer(t)]
        if hits:
            where = ", ".join(sorted({h[0] for h in hits})[:4])
            issues.append(Issue(code, severity, f"{label} 포함({where})",
                                detail=", ".join(sorted({h[1] for h in hits})[:6])))
    return issues


# --------------------------------------------------------------------------- 2) 구조
_PAREN_RE = re.compile(r"[\(\[（［][^\)\]）］]*[\)\]）］]")
_JAMO_RE = re.compile(r"[ㅋㅎㅠㅜ]{2,}")


def check_structure(script) -> List[Issue]:
    issues: List[Issue] = []
    sc = script.scenes
    if len(sc) != REQUIRED_CUTS:
        issues.append(Issue("cuts", "fail", f"컷 수가 {len(sc)}개 (정확히 {REQUIRED_CUTS}개여야 함)"))
        return issues
    if [s.scene_number for s in sc] != list(range(1, REQUIRED_CUTS + 1)):
        issues.append(Issue("scene_numbers", "fail", "scene_number 가 1~6 연속이 아님"))
    if sc[2].tts_narration.strip():
        issues.append(Issue("silent_cut", "fail", "컷 3(무음 리액션)에 대사가 있음"))

    total = sum(s.duration_sec for s in sc)
    lo, hi = DURATION_RANGE
    if not lo <= total <= hi:
        issues.append(Issue("duration", "fail", f"총 길이 {total}초 (규격 {lo}~{hi}초)"))

    total_chars = 0
    for s in sc:
        n = s.scene_number
        narr = s.tts_narration.strip()
        total_chars += len(narr)
        if len(narr) > MAX_NARRATION_PER_SCENE:
            issues.append(Issue("narration_long", "fail", f"컷 {n} 대사가 {len(narr)}자 (최대 {MAX_NARRATION_PER_SCENE}자)"))
        if _PAREN_RE.search(narr):
            issues.append(Issue("narration_paren", "fail", f"컷 {n} 대사에 괄호 지문이 있음(금지 규칙)"))
        if _JAMO_RE.search(narr):
            issues.append(Issue("narration_jamo", "fail", f"컷 {n} 대사에 자음 남발(ㅋㅋ/ㅠㅠ 등)이 있음(금지 규칙)"))
        sub_len = len(s.subtitle_text.strip())
        if sub_len > MAX_SUBTITLE:
            issues.append(Issue("subtitle_long", "fail", f"컷 {n} 자막이 {sub_len}자 (공백 포함 {SUBTITLE_SOFT}자 이내로 짧게, 최대 {MAX_SUBTITLE}자)"))
        elif sub_len > SUBTITLE_SOFT:
            issues.append(Issue("subtitle_soft", "warn", f"컷 {n} 자막 {sub_len}자 (권장 {SUBTITLE_SOFT}자 이내)"))
        if not s.visual_props:
            issues.append(Issue("no_props", "warn", f"컷 {n} visual_props 비어 있음"))
    if total_chars > MAX_NARRATION_TOTAL:
        issues.append(Issue("narration_total", "fail", f"전체 대사가 {total_chars}자 (최대 {MAX_NARRATION_TOTAL}자, 쇼츠 길이 초과 위험)"))

    title = script.episode_title.strip()
    if not title or len(title) > MAX_TITLE:
        issues.append(Issue("title", "fail", f"제목 길이 {len(title)}자 (1~{MAX_TITLE}자)"))
    if _JAMO_RE.search(title):
        issues.append(Issue("title_jamo", "warn", "제목에 자음 남발(ㅋㅋ 등)이 있음"))
    if not 5 <= len(script.hashtags) <= 8:
        issues.append(Issue("hashtags", "warn", f"해시태그 {len(script.hashtags)}개 (규격 5~8개)"))
    if not sc[4].impact_keyword.strip():
        issues.append(Issue("impact", "warn", "컷 5 impact_keyword 비어 있음"))
    return issues


# --------------------------------------------------------------------------- 3) 소재 중복
def topic_text(title: str, setting: str, twist: str) -> str:
    return f"{title} / 공간: {setting} / 반전: {twist}"


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    d = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return d / (na * nb) if na and nb else 0.0


def _bigrams(s: str) -> set:
    s = re.sub(r"[^0-9a-z가-힣]", "", _norm(s))
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def check_similarity(script, recent: List[Dict[str, str]],
                     embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None):
    """최근 소재와의 최대 유사도를 구해 (issues, info) 반환. 임베딩 실패 시 문자열 유사도로 대체한다."""
    if not recent:
        return [], {"method": "none", "best": 0.0}
    new = topic_text(script.episode_title, script.analysis.transplanted_setting, script.analysis.twist_point)
    olds = [topic_text(t["title"], t["setting"], t["twist"]) for t in recent]
    issues: List[Issue] = []
    method, thr = "embedding", SIM_FAIL
    try:
        if embed_fn is None:
            raise RuntimeError("embed_fn 없음")
        vecs = embed_fn([new] + olds)
        sims = [_cos(vecs[0], v) for v in vecs[1:]]
    except Exception as e:  # noqa: BLE001  임베딩 API 장애가 후보 생성을 막지 않도록 대체
        method, thr = "lexical", LEX_SIM_FAIL
        sims = [_jaccard(new, o) for o in olds]
        issues.append(Issue("embed_unavailable", "warn", "임베딩 불가, 문자열 유사도로 대체", detail=f"{type(e).__name__}: {e}"))
    best = max(sims)
    top = recent[sims.index(best)]
    info = {"method": method, "best": round(best, 3), "against": top["title"], "threshold": thr}
    if best >= thr:
        issues.append(Issue("similar", "fail",
                            f"최근 소재 「{top['title']}」와 너무 유사함(유사도 {best:.2f} ≥ {thr}): 같은 공간·사건·반전의 변주는 금지"))
    elif best >= thr - SIM_WARN_MARGIN:
        issues.append(Issue("similar_near", "warn", f"최근 소재와 유사도가 높음({best:.2f})"))
    return issues, info


# --------------------------------------------------------------------------- 종합 / 재발상 지시 / 로그
def evaluate(script, recent: List[Dict[str, str]],
             embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None) -> GateReport:
    report = GateReport()
    report.issues += check_safety(script)
    report.issues += check_structure(script)
    sim_issues, report.similarity = check_similarity(script, recent, embed_fn)
    report.issues += sim_issues
    return report


def retry_instruction(report: GateReport, rejected: List[Dict[str, str]], round_no: int) -> str:
    """불합격 사유와 폐기된 소재를 담은 재발상 지시(원문 비속어는 싣지 않는다)."""
    lines = [f"\n\n[품질 검수 불합격 - 재발상 {round_no}회차] 직전에 만든 대본은 아래 사유로 폐기되었다. 같은 실수를 반복하지 말고 소재부터 새로 발상하라."]
    lines += [f"- {i.message}" for i in report.fails]
    if rejected:
        lines.append("폐기된 소재(유사한 사건·공간·반전 금지):")
        lines += [f"  · 「{t['title']}」 / 공간: {t['setting']} / 반전: {t['twist']}" for t in rejected]
    return "\n".join(lines)


def write_log(script, attempt: int, report: GateReport) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "episode_id": script.episode_id,
               "title": script.episode_title, "attempt": attempt, "passed": report.passed,
               "similarity": report.similarity,
               "issues": [{"code": i.code, "severity": i.severity, "message": i.message, "detail": i.detail}
                          for i in report.issues]}
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[QualityGate] 로그 기록 실패(무시): {e}")
