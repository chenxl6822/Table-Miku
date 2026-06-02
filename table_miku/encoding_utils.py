from __future__ import annotations

import re


MOJIBAKE_MARKERS = (
    "锛",
    "銆",
    "鏄",
    "澶",
    "绋",
    "鐨",
    "璁",
    "妗",
    "�",
)

_SPACES_RE = re.compile(r"[ \t\r\f\v]+")
_NEWLINES_RE = re.compile(r"\n{3,}")
_ZH_HANS_TRANSLATION = str.maketrans(
    {
        "網": "网",
        "絡": "络",
        "數": "数",
        "碼": "码",
        "電": "电",
        "腦": "脑",
        "設": "设",
        "備": "备",
        "會": "会",
        "過": "过",
        "節": "节",
        "點": "点",
        "連": "连",
        "換": "换",
        "據": "据",
        "傳": "传",
        "輸": "输",
        "為": "为",
        "線": "线",
        "無": "无",
        "兩": "两",
        "類": "类",
        "纖": "纤",
        "纜": "缆",
        "資": "资",
        "訊": "讯",
        "發": "发",
        "許": "许",
        "與": "与",
        "關": "关",
        "層": "层",
        "協": "协",
        "議": "议",
        "應": "应",
        "用": "用",
        "體": "体",
        "系": "系",
        "統": "统",
        "儲": "储",
        "簡": "简",
        "稱": "称",
    }
)


def looks_mojibake(text: str) -> bool:
    """Return True when text contains common Chinese mojibake fragments."""
    if not text:
        return False
    marker_hits = sum(1 for marker in MOJIBAKE_MARKERS if marker in text)
    if marker_hits:
        return True
    replacement_ratio = text.count("\ufffd") / max(len(text), 1)
    return replacement_ratio > 0.02


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Try to reverse UTF-8 bytes that were decoded as GBK/GB18030."""
    if not text or not looks_mojibake(text):
        return text, False

    repaired_full = _repair_chunk(text)
    if repaired_full != text:
        return repaired_full, True

    changed = False
    parts: list[str] = []
    for part in re.split(r"(\s+)", text):
        if not part or part.isspace() or not looks_mojibake(part):
            parts.append(part)
            continue
        repaired_part = _repair_chunk(part)
        parts.append(repaired_part)
        changed = changed or repaired_part != part
    if changed:
        return "".join(parts), True
    return text, False


def _repair_chunk(text: str) -> str:
    candidates: list[str] = []
    for source_encoding in ("gb18030", "gbk", "cp936"):
        try:
            candidate = text.encode(source_encoding).decode("utf-8")
        except UnicodeError:
            continue
        candidates.append(candidate)

    if not candidates:
        return text

    best = min(candidates, key=_mojibake_score)
    if _mojibake_score(best) < _mojibake_score(text):
        return best
    return text


def normalize_zh_text(text: str) -> str:
    precleaned = text.replace("\ufeff", "").replace("\u3000", " ")
    repaired, _ = repair_mojibake(precleaned)
    normalized = repaired.translate(_ZH_HANS_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _SPACES_RE.sub(" ", normalized)
    normalized = _NEWLINES_RE.sub("\n\n", normalized)
    return normalized.strip()


def _mojibake_score(text: str) -> int:
    score = text.count("\ufffd") * 5
    for marker in MOJIBAKE_MARKERS:
        score += text.count(marker) * 2
    return score
