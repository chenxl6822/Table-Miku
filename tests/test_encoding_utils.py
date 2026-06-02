import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from table_miku.encoding_utils import looks_mojibake, normalize_zh_text, repair_mojibake


def test_normal_chinese_is_not_flagged():
    text = "计算机网络关注分层模型、协议、寻址、路由和网络安全。"
    assert looks_mojibake(text) is False
    repaired, changed = repair_mojibake(text)
    assert repaired == text
    assert changed is False


def test_common_mojibake_can_be_repaired():
    text = "澶╂皵鎻愰啋"
    repaired, changed = repair_mojibake(text)
    assert changed is True
    assert repaired == "天气提醒"


def test_normalize_repairs_and_cleans_spacing():
    text = "\ufeff澶╂皵鎻愰啋\r\n\r\n\r\n  计算机网络  "
    normalized = normalize_zh_text(text)
    assert "天气提醒" in normalized
    assert "\ufeff" not in normalized
    assert "\r" not in normalized
    assert "\n\n\n" not in normalized


def test_normalize_converts_common_traditional_terms():
    text = "電腦網絡會交換數據，傳輸媒介可分為有線及無線兩類。"
    normalized = normalize_zh_text(text)
    assert normalized == "电脑网络会交换数据，传输媒介可分为有线及无线两类。"
