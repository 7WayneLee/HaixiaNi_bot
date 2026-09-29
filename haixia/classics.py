"""中醫笈成文字檔的離線解析、宋本條號與經典切段。"""

import html
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

from haixia.chunking import chunk_id, make_chunk
from haixia.textnorm import search_key

BOOKS = {
    "傷寒論（宋本）": ["傷寒論_宋本/index.txt"],
    "金匱要略方論": ["金匱要略方論/index.txt"],
    "八十一難經": ["難經/index.txt"],
    "黃帝內經素問": [f"黃帝內經/素問/{n}.txt" for n in range(1, 27)],
    "黃帝內經靈樞": ["黃帝內經/靈樞/index.txt"],
    "針灸大成": [f"針灸大成/{n}.txt" for n in range(1, 11)],
    "神農本草經": ["神農本草經/index.txt"],
}
QUALITY = {"傷寒論（宋本）": 90, "金匱要略方論": 90, "八十一難經": 90,
           "黃帝內經素問": 2, "黃帝內經靈樞": 0, "針灸大成": 0, "神農本草經": 0}
HEADING = re.compile(r"^(={4,6})\s*(.*?)\s*\1\s*$")
TAG = re.compile(r"</?(?:j|z|l|F|book|menu)>|<&/>|\*\*")
NOTE = re.compile(r"<(?:j|z)>(.*?)</(?:j|z)>")
SMALL = re.compile(r"<l>(.*?)</l>")
PUNCT = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
PRONUNCIATION = re.compile(r"（[^）]*(?:切|音)[^）]*）")
GLOSS_PREFIX = re.compile(r"^(?:序|[\u3400-\u9fff]{1,12}(?:論|篇))：")


def comparison_key(value):
    return PUNCT.sub("", search_key(value)).casefold()


def display_text(raw):
    """只套台灣用字；保留缺字全形空白與未改字原文。"""
    from haixia.textnorm import _variant_table  # 單用用字表，不做簡轉繁
    return raw.translate(_variant_table())


def clean_markup(raw):
    annotations = NOTE.findall(raw) + SMALL.findall(raw)
    value = SMALL.sub(lambda m: f"（{m.group(1)}）", raw)
    value = NOTE.sub("", value)
    value = TAG.sub("", value)
    value = html.unescape(value).strip(" \t\r\n")
    return display_text(value), annotations


def is_pronunciation_gloss(text, continuing=False):
    """辨認篇名引出的音釋詞表，以及緊接其後的續段。"""
    if any(mark in text for mark in "。！？；") or "（" not in text:
        return False
    prefix = GLOSS_PREFIX.match(text)
    if prefix:
        return bool(PRONUNCIATION.search(text) or continuing)
    return continuing and prefix is None and bool(PRONUNCIATION.search(text))


class ModernHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.div_depth = 0
        self.current = None
        self.hidden = 0
        self.parts = []
        self.entries = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self.div_depth:
                self.div_depth += 1
            elif attrs.get("data-sec") == "p" and (attrs.get("id") or "").isdigit():
                self.div_depth = 1
                self.current = attrs["id"]
                self.parts = []
        elif self.div_depth:
            if tag in ("span", "jc-t") and (attrs.get("data-rev") == "古版" or
                    attrs.get("attr-data-rev") == "古版-元素"):
                self.hidden += 1
            elif tag == "br" and not self.hidden:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "div" and self.div_depth:
            self.div_depth -= 1
            if not self.div_depth:
                self.entries.append((self.current, "".join(self.parts)))
                self.current = None
        elif tag in ("span", "jc-t") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if self.div_depth and not self.hidden:
            self.parts.append(data)


def song_numbers(path):
    parser = ModernHTML()
    parser.feed(Path(path).read_text(encoding="utf-8"))
    return parser.entries


def assign_song_numbers(units, entries):
    """只接受唯一的精確文字匹配；差異留給報告人工核對。"""
    by_key = defaultdict(list)
    for number, text in entries:
        key = comparison_key(text)
        if key:
            by_key[key].append(number)
    used = set()
    for unit in units:
        key = comparison_key(unit["顯示文字"])
        candidates = [n for n in by_key[key] if n not in used]
        if len(candidates) == 1:
            unit["條號"] = candidates[0]
            used.add(candidates[0])
    numbered_sections = {u["篇"] for u in units if u["條號"]}
    unmatched = [{"單位序號": u["單位序號"], "篇": u["篇"], "開頭": u["顯示文字"][:50]}
                 for u in units if not u["條號"] and u["篇"] in numbered_sections and
                 not u["原文"].lstrip().startswith("<F>")]
    unmatched.extend({"HTML條號": number, "開頭": text[:50]} for number, text in entries
                     if number not in used)
    return unmatched


def parse_book(name, src_dir, html_path=None, excluded_glosses=None):
    units = []
    number = 0
    for rel in BOOKS[name]:
        path = Path(src_dir) / rel
        text = path.read_text(encoding="utf-8-sig")
        meta = dict(re.findall(r"^([^=\n]+)=([^\n]*)$", "\n".join(re.findall(
            r"<book>(.*?)</book>", text, re.S)), re.M))
        quality = int(meta.get("品質", f"{QUALITY[name]}%").rstrip("%"))
        text = re.sub(r"<(?:book|menu)>.*?</(?:book|menu)>", "", text, flags=re.S)
        volume = section = ""
        heading5 = ""
        block = []
        in_gloss = False

        def flush():
            nonlocal number, in_gloss
            raw = "\n".join(block).strip(" \t\r\n")
            block.clear()
            if not raw:
                return
            shown, annotations = clean_markup(raw)
            if not shown:
                return
            if is_pronunciation_gloss(shown, in_gloss):
                in_gloss = True
                if excluded_glosses is not None:
                    excluded_glosses.append({"卷": volume, "來源檔": rel})
                return
            in_gloss = False
            number += 1
            units.append({"書名": name, "卷": volume, "篇": section or heading5 or volume,
                          "單位序號": number, "條號": None, "顯示文字": shown, "原文": raw,
                          "夾注": annotations, "校對品質": quality})

        in_formula = False
        in_drug = False
        in_point = False
        for line in text.splitlines():
            match = HEADING.match(line)
            if match:
                flush()
                in_gloss = False
                in_point = False
                in_drug = False
                level, title = len(match.group(1)), match.group(2).strip()
                if level == 6:
                    volume, heading5, section = title, "", ""
                elif level == 5:
                    if name in ("傷寒論（宋本）", "針灸大成", "黃帝內經素問") and (title.startswith("卷") or title.startswith("序")):
                        volume, section = title, ""
                    else:
                        heading5, section = title, title
                else:
                    section = title if name != "針灸大成" or not heading5 else f"{heading5} {title}"
                    in_drug = name == "神農本草經"
                continue
            stripped = line.strip(" \t\r\n")
            point = re.fullmatch(r"__(.+?)__", stripped) if name == "針灸大成" else None
            if point:
                flush()
                in_point = True
                section = f"{heading5} {point.group(1)}".strip()
                continue
            if stripped == "<F>":
                flush()
                in_formula = True
            if stripped:
                block.append(line)
            else:
                if not (in_formula or in_drug or in_point):
                    flush()
            if stripped == "</F>":
                in_formula = False
                flush()
        flush()
    unmatched = assign_song_numbers(units, song_numbers(html_path)) if name == "傷寒論（宋本）" and html_path else []
    return units, unmatched


def split_long(text, limit=600):
    if len(text) <= limit:
        return [text]
    pieces, current = [], ""
    for sentence in re.findall(r"[^。]*。|[^。]+$", text):
        if current and len(current) + len(sentence) > limit:
            pieces.append(current)
            current = ""
        if len(sentence) > limit:
            while len(sentence) > limit:
                pieces.append(sentence[:limit])
                sentence = sentence[limit:]
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def classic_chunks(units, target=300):
    chunks = []
    pending = []

    def flush():
        if not pending:
            return
        first = pending[0][0]
        text = "\n".join(piece for _, piece in pending)
        ids = list(dict.fromkeys(unit["單位序號"] for unit, _ in pending))
        number = first["條號"] if len(ids) == 1 and first["書名"] == "傷寒論（宋本）" else None
        source = f"jicheng:{first['書名']}#{first['單位序號']}"
        if number:
            source += f"-第{number}條"
        chunks.append(make_chunk("classic", source, first["書名"], text,
                                 id=chunk_id(source, ids[-1], len(chunks)), section=first["篇"],
                                 episode=f"第{number}條" if number else None,
                                 quality=first["校對品質"], unit_ids=json.dumps(ids),
                                 raw_text="\n".join(unit["原文"] for unit, _ in pending)))
        pending.clear()

    for unit in units:
        for piece in split_long(unit["顯示文字"]):
            if pending and (pending[0][0]["篇"] != unit["篇"] or
                            len("\n".join(p for _, p in pending)) + len(piece) > target or
                            pending[0][0]["條號"] or unit["條號"]):
                flush()
            pending.append((unit, piece))
            if len(piece) >= target:
                flush()
    flush()
    return chunks
