"""把資料轉成正體中文，並產生跨字體的搜尋比對鍵。

用法：
    python3 -m haixia.textnorm [--search-key] < in.txt > out.txt
"""

import argparse
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

from opencc import OpenCC


_DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "tcm_s2tw.txt"
_PHRASE_PATH = Path(__file__).resolve().parent.parent / "data" / "tw_phrase_fixes.txt"
_VARIANTS_PATH = Path(__file__).resolve().parent.parent / "data" / "tw_variants.txt"
_END = "\0"


@lru_cache(maxsize=1)
def _converters():
    """延遲載入 OpenCC，讓重複轉換共用詞典。"""
    return OpenCC("s2tw"), OpenCC("t2s")


@lru_cache(maxsize=1)
def _term_tree():
    """載入覆寫詞條，建成逐字樹供最長匹配使用。"""
    root = {}
    with _DICT_PATH.open(encoding="utf-8") as source:
        for number, raw_line in enumerate(source, 1):
            line = raw_line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0] or not parts[1]:
                raise ValueError(f"詞典第 {number} 行格式錯誤")
            simple, traditional, _note = parts
            node = root
            for char in simple:
                node = node.setdefault(char, {})
            if _END in node:
                raise ValueError(f"詞典第 {number} 行詞條重複：{simple}")
            node[_END] = traditional
    return root


@lru_cache(maxsize=1)
def _phrase_tree():
    """載入 OpenCC 後的正體詞組修正與不改字例外。"""
    root = {}
    with _PHRASE_PATH.open(encoding="utf-8") as source:
        for number, raw_line in enumerate(source, 1):
            line = raw_line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0] or not parts[1]:
                raise ValueError(f"正體詞組表第 {number} 行格式錯誤")
            original, corrected, _note = parts
            node = root
            for char in original:
                node = node.setdefault(char, {})
            if _END in node:
                raise ValueError(f"正體詞組表第 {number} 行詞條重複：{original}")
            node[_END] = corrected
    return root


def _longest_match(tree, text, index):
    """回傳從 index 開始的最長詞組終點與替換值。"""
    node = tree
    end = index
    best_end = index
    best_value = None
    while end < len(text) and text[end] in node:
        node = node[text[end]]
        end += 1
        if _END in node:
            best_end = end
            best_value = node[_END]
    return best_end, best_value


def _fix_phrases(text):
    """逐字向右掃描一次，不把修正後的文字重新送回規則。"""
    tree = _phrase_tree()
    output = []
    index = 0
    while index < len(text):
        end, value = _longest_match(tree, text, index)
        if value is None:
            output.append(text[index])
            index += 1
        else:
            output.append(value)
            index = end
    return "".join(output)


@lru_cache(maxsize=1)
def _variant_table():
    """載入台灣顯示用字；同一異體字只能指定一個採用字。"""
    variants = {}
    with _VARIANTS_PATH.open(encoding="utf-8") as source:
        for number, raw_line in enumerate(source, 1):
            line = raw_line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3 or len(parts[0]) != 1 or len(parts[1]) != 1:
                raise ValueError(f"異體字表第 {number} 行格式錯誤")
            variant, preferred, _note = parts
            if variant in variants:
                raise ValueError(f"異體字表第 {number} 行重複：{variant}")
            variants[variant] = preferred
    return str.maketrans(variants)


def to_traditional(text: str) -> str:
    """先轉 OpenCC，再修正正體詞組與台灣異體用字。"""
    converter, _ = _converters()
    tree = _term_tree()
    output = []
    plain = []
    index = 0
    while index < len(text):
        best_end, best_value = _longest_match(tree, text, index)
        if best_value is None:
            plain.append(text[index])
            index += 1
            continue
        if plain:
            output.append(converter.convert("".join(plain)))
            plain.clear()
        output.append(best_value)
        index = best_end
    if plain:
        output.append(converter.convert("".join(plain)))
    return _fix_phrases("".join(output)).translate(_variant_table())


def search_key(text: str) -> str:
    """依序做 NFKC、台灣異體統一及 t2s；結果僅供搜尋比對。"""
    _, converter = _converters()
    normalized = unicodedata.normalize("NFKC", text).translate(_variant_table())
    return converter.convert(normalized)


def main():
    parser = argparse.ArgumentParser(description="正體中文轉換及搜尋鍵產生工具")
    parser.add_argument("--search-key", action="store_true", help="輸出搜尋比對鍵")
    args = parser.parse_args()
    source = sys.stdin.buffer.read().decode("utf-8")
    result = search_key(source) if args.search_key else to_traditional(source)
    sys.stdout.buffer.write(result.encode("utf-8"))


if __name__ == "__main__":
    main()
