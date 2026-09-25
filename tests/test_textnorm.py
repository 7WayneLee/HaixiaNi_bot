"""正體轉換、詞典與搜尋鍵的回歸測試。"""

from pathlib import Path

import pytest
from opencc import OpenCC

from haixia.textnorm import search_key, to_traditional


TERMS_PATH = Path(__file__).resolve().parents[1] / "data" / "tcm_terms_tw.txt"
TERMS = TERMS_PATH.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("simple", "traditional"),
    [
        ("太冲", "太衝"),
        ("谷芽", "穀芽"),
        ("白术", "白朮"),
        ("苍术", "蒼朮"),
        ("干姜", "乾薑"),
        ("生姜", "生薑"),
        ("表里", "表裡"),
        ("里急后重", "裡急後重"),
        ("头发", "頭髮"),
        ("发汗", "發汗"),
        ("脏腑", "臟腑"),
        ("郁证", "鬱證"),
        ("余热", "餘熱"),
        ("冲脉", "衝脈"),
        ("面色", "面色"),
        ("伤寒论", "傷寒論"),
        ("金匮要略", "金匱要略"),
        ("针灸", "針灸"),
    ],
)
def test_easily_confused_words(simple, traditional):
    assert to_traditional(simple) == traditional


def test_every_tcm_term_round_trips():
    assert len(TERMS) >= 800
    assert len(TERMS) == len(set(TERMS))
    converter = OpenCC("t2s")
    errors = [(term, to_traditional(converter.convert(term))) for term in TERMS
              if to_traditional(converter.convert(term)) != term]
    assert errors == []


def test_fourteen_meridian_points_are_complete():
    points = TERMS[:361]
    assert len(points) == len(set(points)) == 361
    assert points[-1] == "承漿"
    assert {"太溪", "後溪", "解溪", "陽溪", "通里", "建里", "足通谷"} <= set(points)


def test_every_tcm_term_is_idempotent():
    assert [(term, to_traditional(term)) for term in TERMS
            if to_traditional(term) != term] == []


@pytest.mark.parametrize(
    "text",
    [
        "太衝穴與穀芽",
        "白朮、蒼朮、乾薑與生薑",
        "傷寒論記載桂枝湯，金匱要略記載當歸芍藥散。",
        "表裡、裡急後重、頭髮與發汗",
        "山谷、皇后、乾淨、頭髮、衝突",
    ],
)
def test_idempotent(text):
    assert to_traditional(to_traditional(text)) == to_traditional(text)


@pytest.mark.parametrize(
    ("traditional", "simple"),
    [("傷寒論", "伤寒论"), ("太衝", "太冲"), ("穀芽", "谷芽")],
)
def test_search_key_matches_both_scripts(traditional, simple):
    assert search_key(traditional) == search_key(simple)


def test_search_key_normalizes_width():
    assert search_key("ＡＢＣ傷寒論") == "ABC伤寒论"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("湿气", "濕氣"),
        ("祛湿", "祛濕"),
        ("湿热", "濕熱"),
        ("溼氣", "濕氣"),
        ("黄芪", "黃耆"),
        ("黃芪", "黃耆"),
        ("痹证", "痺證"),
        ("后溪", "後溪"),
        ("太溪", "太溪"),
        ("太谿", "太溪"),
        ("半表半里", "半表半裡"),
        ("里证", "裡證"),
        ("表寒里热", "表寒裡熱"),
        ("柴胡桂枝干姜汤", "柴胡桂枝乾薑湯"),
    ],
)
def test_taiwan_usage(source, expected):
    assert to_traditional(source) == expected


@pytest.mark.parametrize("point", ["通里", "建里", "足三里", "手三里", "手五里", "足五里"])
def test_point_names_keep_li(point):
    assert to_traditional(point) == point
    assert to_traditional(OpenCC("t2s").convert(point)) == point


@pytest.mark.parametrize(
    "spellings",
    [
        ("黃耆", "黄芪"),
        ("痺證", "痹证"),
        ("後谿", "后溪"),
        ("太谿", "太溪"),
        ("濕氣", "溼氣", "湿气"),
        ("神麴", "神曲"),
        ("太衝", "太冲"),
        ("穀芽", "谷芽"),
        ("乾薑", "干姜"),
        ("白朮", "白术"),
    ],
)
def test_search_key_matches_variants(spellings):
    assert len({search_key(word) for word in spellings}) == 1


@pytest.mark.parametrize("word", ["山谷", "皇后", "乾淨", "頭髮", "衝突", "這裡", "心裡"])
def test_ordinary_words_are_untouched(word):
    assert to_traditional(word) == word


def test_longest_tcm_term_has_priority():
    assert to_traditional("炒谷芽和谷芽") == "炒穀芽和穀芽"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("加姜", "加薑"),
        ("姜枣", "薑棗"),
        ("煨姜", "煨薑"),
        ("姜附", "薑附"),
        ("姜辛", "薑辛"),
        ("苓甘五味姜辛汤", "苓甘五味薑辛湯"),
        ("加术", "加朮"),
        ("去术", "去朮"),
        ("术附汤", "朮附湯"),
        ("苓术", "苓朮"),
        ("术甘", "朮甘"),
        ("莪术", "莪朮"),
        ("干姜", "乾薑"),
        ("生姜", "生薑"),
        ("姜汁", "薑汁"),
        ("白术", "白朮"),
        ("苍术", "蒼朮"),
        ("干呕", "乾嘔"),
    ],
)
def test_medicine_phrase_fixes(source, expected):
    assert to_traditional(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("姜春华", "姜春華"),
        ("姜太公", "姜太公"),
        ("姜子牙", "姜子牙"),
        ("姜佐景", "姜佐景"),
        ("手术", "手術"),
        ("技术", "技術"),
        ("学术", "學術"),
        ("医术", "醫術"),
        ("针术", "針術"),
        ("艺术", "藝術"),
        ("美术", "美術"),
        ("算术", "算術"),
        ("战术", "戰術"),
        ("武术", "武術"),
        ("法术", "法術"),
        ("魔术", "魔術"),
        ("巫术", "巫術"),
        ("心术", "心術"),
        ("术语", "術語"),
        ("术后", "術後"),
        ("术前", "術前"),
        ("手术附近", "手術附近"),
    ],
)
def test_phrase_exceptions(source, expected):
    assert to_traditional(source) == expected


@pytest.mark.parametrize("spellings", [("姜辛", "薑辛"), ("術附湯", "朮附湯")])
def test_search_key_matches_medicine_phrases(spellings):
    assert len({search_key(word) for word in spellings}) == 1


@pytest.mark.parametrize("word", ["薑", "朮", "手術", "加朮", "姜春華", "手術附近"])
def test_phrase_fixes_are_idempotent(word):
    assert to_traditional(word) == word
    assert to_traditional(to_traditional(word)) == word


def test_requested_terms_are_present_and_old_names_removed():
    requested = set("""旋覆代赭湯 桂枝加厚朴杏子湯 麻黃細辛附子湯 黃耆芍藥桂枝苦酒湯
        苓甘五味薑辛湯 桂苓五味甘草湯 茯苓戎鹽湯 大黃附子湯 人參湯 風引湯
        防己地黃湯 侯氏黑散 蛇床子散 狼牙湯 橘枳薑湯 甘草麻黃湯 麻黃附子湯
        大黃硝石湯 赤丸 滑石白魚散 大黃䗪蟲丸 禹餘糧丸 燒褌散 訶梨勒散 朮附湯
        芫花 甘遂 大戟 巴豆 虻蟲 䗪蟲 蜀漆 鉛丹 通草 秦皮 雞子黃 粳米 飴糖
        膠飴 香豉 赤小豆 蜀椒 烏頭 天雄 禹餘糧 瓜蒂 芍藥 苦酒 連軺 文蛤
        郁李仁 硃砂 殭蠶 針灸大成 人紀 天紀 地紀 倪海廈 漢唐中醫
        乾薑黃芩黃連人參湯 厚朴生薑半夏甘草人參湯 梔子厚朴湯 莪朮""".split())
    removed = {"乾薑黃連黃芩人參湯", "厚朴生薑甘草半夏人參湯",
               "梔子厚朴枳實湯", "莪術", "桂枝去芍藥加蜀漆龍骨牡蠣救逆湯"}
    assert requested <= set(TERMS)
    assert removed.isdisjoint(TERMS)
