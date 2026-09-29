"""把逐字稿與文件切成約 500 字的段落，並做段落層級去重。"""

import hashlib
import re
import unicodedata

from haixia.textnorm import search_key

TARGET = 500
MIN_CHARS = 400
MAX_CHARS = 600
OVERLAP = 100
SENTENCE_END = "。！？；!?;"

FIELDS = ("id", "kind", "source", "title", "episode", "section", "page_start", "page_end",
          "start", "end", "date", "text", "chars")


def chunk_id(*parts):
    """由來源與位置算出穩定的段落代號。"""
    digest = hashlib.sha1("\x1f".join(str(part) for part in parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def make_chunk(kind, source, title, text, **fields):
    chunk = dict.fromkeys(FIELDS)
    chunk.update(kind=kind, source=source, title=title, text=text, chars=len(text))
    chunk.update(fields)
    return chunk


def char_count(text):
    """計算字數：不算空白。"""
    return sum(1 for char in text if not char.isspace())


# ---------- 逐字稿 ----------

def _join_segments(texts):
    parts = []
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if parts and parts[-1][-1] not in SENTENCE_END + "，、：,:" and not text[0] in SENTENCE_END:
            parts.append(" ")
        parts.append(text)
    return "".join(parts)


def chunk_transcript(segments, source, title, episode, date=None,
                     target=TARGET, max_chars=MAX_CHARS, overlap=OVERLAP):
    """依序合併相鄰 segment，不切斷 segment，下一段與前一段重疊約 overlap 字。

    segments 是 [{"start", "end", "text"}]。回傳段落 dict 清單。
    """
    items = [(s["start"], s["end"], s["text"].strip()) for s in segments if s["text"].strip()]
    chunks = []
    begin = 0
    while begin < len(items):
        end = begin
        size = 0
        while end < len(items):
            length = char_count(items[end][2])
            if end > begin and size >= target:
                break
            if end > begin and size + length > max_chars and size >= target - overlap:
                break
            size += length
            end += 1
        text = _join_segments(item[2] for item in items[begin:end])
        start_sec, end_sec = items[begin][0], items[end - 1][1]
        chunks.append(make_chunk("transcript", source, title, text, episode=episode,
                                 start=round(start_sec, 2), end=round(end_sec, 2), date=date,
                                 id=chunk_id(source, round(start_sec, 2), round(end_sec, 2))))
        if end >= len(items):
            break
        # 下一段從尾端往回算約 overlap 字的 segment 開始，但一定要前進。
        back = end
        tail = 0
        while back - 1 > begin and tail < overlap:
            back -= 1
            tail += char_count(items[back][2])
        begin = max(back, begin + 1)
    return chunks


# ---------- 文件 ----------

_CN_NUM = "一二三四五六七八九十百千零〇两兩"
# 第一層：章、講、篇等大標題；第二層：「一、」「十六：」「（八）」「1、」等編號標題。
_HEADING_1 = [
    re.compile(rf"^第[{_CN_NUM}\d]+[章講讲節节篇回課课卷部]"),
    re.compile(rf"^[^，,。：:；]{{2,20}}第[{_CN_NUM}\d]+$"),
    re.compile(r"^辨.{1,16}[病脉脈].{0,10}[治篇法]"),
    re.compile(r"^[壹貳贰參叁肆伍陸陆柒捌玖拾]+[、．.]"),
    # 【…】整行當標題，但新聞來源（【中央社】【TVBS新聞】等）不算。
    re.compile(r"^【(?![^】]*(?:社|報|报|網|网|新聞|新闻|記者|记者|電|电))[^】]{1,30}】$"),
]
_HEADING_2 = [
    re.compile(rf"^[{_CN_NUM}]{{1,4}}\s*[、．.：:]\s*\S"),
    re.compile(r"^[（(][一二三四五六七八九十\d]{1,3}[)）]\s*\S"),
    re.compile(r"^\d{1,3}\s*[、．.]\s*[^\d\s]"),
]
_DATES = [
    (re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})(?!\d)"), "mdy"),
    (re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月"), "ym"),
]


def heading_level(paragraph):
    """回傳標題層級（1 或 2），不是標題回傳 0。標題要短（40 字內）。"""
    text = paragraph.strip()
    if not text or len(text) > 40:
        return 0
    if any(pattern.match(text) for pattern in _HEADING_1):
        return 1
    if any(pattern.match(text) for pattern in _HEADING_2):
        return 2
    return 0


def is_heading(paragraph):
    return heading_level(paragraph) > 0


def find_date(text):
    """回傳 YYYY-MM-DD 或 YYYY-MM；找不到回傳 None。"""
    for pattern, order in _DATES:
        for match in pattern.finditer(text):
            if order == "ymd":
                year, month, day = match[1], match[2], match[3]
            elif order == "mdy":
                month, day, year = match[1], match[2], match[3]
            else:
                year, month, day = match[1], match[2], None
            if not 1 <= int(month) <= 12 or (day is not None and not 1 <= int(day) <= 31):
                continue
            return f"{year}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")
    return None


def split_sentences(paragraph, max_chars=MAX_CHARS):
    """在「。！？；」後切開；仍過長的句子再硬切。"""
    sentences = re.findall(rf"[^{SENTENCE_END}]*[{SENTENCE_END}]+|[^{SENTENCE_END}]+$", paragraph)
    result = []
    for sentence in sentences:
        while len(sentence) > max_chars:
            result.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if sentence.strip():
            result.append(sentence)
    return result


def chunk_document(paragraphs, source, title, date=None, track_dates=False,
                   target=TARGET, min_chars=MIN_CHARS, max_chars=MAX_CHARS, min_keep=10):
    """paragraphs 是 [(頁碼或 None, 段落正體文字)]。

    依段落合併成約 target 字一段；段落邊界滿 min_chars 就收段，
    段落中間只在超過 max_chars 時於句尾切開。track_dates 時，
    短段落裡的日期會更新之後段落的 date（日誌類）。
    50 字內的短段（多半只有標題）併進下一段；最後不到 min_keep 字的段丟掉。
    """
    chunks = []
    units = []       # (頁碼, 文字, 是否段落開頭)
    section = None
    sections = [None, None]   # 第一層、第二層標題
    current_date = date
    state = {"section": None, "date": date}   # 目前這一段開頭時的章節與日期

    def flush():
        if not units:
            return
        text = "".join(("\n" if index and starts else "") + text
                       for index, (_page, text, starts) in enumerate(units)).strip()
        pages = [page for page, _text, _starts in units if page is not None]
        if text:
            chunks.append(make_chunk(
                "document", source, title, text, section=state["section"], date=state["date"],
                page_start=pages[0] if pages else None, page_end=pages[-1] if pages else None))
        units.clear()

    size = 0
    for page, paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        level = heading_level(paragraph)
        heading = level > 0
        dated = find_date(paragraph) if track_dates and len(paragraph) <= 40 else None
        if units and (size >= target or (size >= min_chars and size + len(paragraph) > max_chars)
                      or ((heading or dated) and size >= min_chars // 2)):
            flush()
            size = 0
        if level == 1:
            sections = [paragraph, None]
        elif level == 2:
            sections[1] = paragraph
        if heading:
            section = " ".join(part for part in sections if part)
        if dated:
            current_date = dated
        if not units:
            state["section"], state["date"] = section, current_date
        for index, sentence in enumerate(split_sentences(paragraph, max_chars)):
            length = len(sentence)
            if units and size + length > max_chars:
                flush()
                size = 0
                state["section"], state["date"] = section, current_date
            units.append((page, sentence, index == 0))
            size += length
    flush()

    # 只有標題之類的短段（50 字內）併進下一段；太短的尾段（100 字內）併進前一段。
    merged = []
    carry = None
    for chunk in chunks:
        if carry is not None:
            chunk["text"] = carry["text"] + "\n" + chunk["text"]
            chunk["chars"] = len(chunk["text"])
            if carry["page_start"] is not None:
                chunk["page_start"] = carry["page_start"]
            chunk["section"] = chunk["section"] or carry["section"]
            chunk["date"] = chunk["date"] or carry["date"]
            carry = None
        if chunk["chars"] < 50 and chunk is not chunks[-1]:
            carry = chunk
            continue
        merged.append(chunk)
    chunks = merged
    if len(chunks) >= 2 and chunks[-1]["chars"] < 100 and chunks[-2]["chars"] + chunks[-1]["chars"] <= max_chars + 100:
        last = chunks.pop()
        previous = chunks[-1]
        previous["text"] += "\n" + last["text"]
        previous["chars"] = len(previous["text"])
        if last["page_end"] is not None:
            previous["page_end"] = last["page_end"]
    chunks = [chunk for chunk in chunks if char_count(chunk["text"]) >= min_keep]
    for index, chunk in enumerate(chunks):
        chunk["id"] = chunk_id(source, "doc", index, hashlib.sha1(chunk["text"].encode("utf-8")).hexdigest()[:8])
    return chunks


# ---------- 段落去重 ----------

def dedupe_key(paragraph):
    """search_key、轉小寫後去掉空白與標點。"""
    key = search_key(paragraph).casefold()
    return "".join(char for char in key if not (char.isspace() or unicodedata.category(char)[0] in "PSZ"))


class ParagraphDeduper:
    """段落去重：只保存 16 位元組摘要，省記憶體。

    1. 段落 key（見 dedupe_key）長度 8 以上、見過就丟（reason「段落相同」）。
    2. 同一本書的 PDF 與 doc 分段方式不同，整段比不到；所以另外把段落切成句子，
       長度 8 以上的句子有 coverage（預設八成）以上的字數都見過，也丟（reason「句子重複」）。
    保留的段落，其段落 key 與句子 key 都記下來。
    """

    def __init__(self, min_length=8, coverage=0.8):
        self.min_length = min_length
        self.coverage = coverage
        self.paragraphs = set()
        self.sentences = set()

    @staticmethod
    def _digest(key):
        return hashlib.blake2b(key.encode("utf-8"), digest_size=16).digest()

    def check(self, paragraph):
        """回傳 None 表示保留，否則回傳丟掉的原因。"""
        key = dedupe_key(paragraph)
        if len(key) < self.min_length:
            return None
        digest = self._digest(key)
        if digest in self.paragraphs:
            return "段落相同"
        sentence_keys = [k for k in (dedupe_key(s) for s in re.split(r"[。！？；!?;\n]+", paragraph))
                         if len(k) >= self.min_length]
        sentence_digests = [self._digest(k) for k in sentence_keys]
        covered = sum(len(k) for k, d in zip(sentence_keys, sentence_digests) if d in self.sentences)
        if covered and covered >= self.coverage * len(key):
            return "句子重複"
        self.paragraphs.add(digest)
        self.sentences.update(sentence_digests)
        return None

    def keep(self, paragraph):
        return self.check(paragraph) is None
