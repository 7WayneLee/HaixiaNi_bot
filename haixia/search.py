"""混合搜尋：向量（Vertex RETRIEVAL_QUERY）與 BM25 各取前 50，用 RRF 合併。

Vertex 失敗（逾時、網路、認證）時退回只用 BM25，並在結果的 mode 與 vector_error 標明。
常駐記憶體：SQLite 連線、每段一個布林（段落種類）、向量用 memmap，不整份讀進來。

出處（citation）同時給 Claude 看（工具結果）和給人看（ask.py、Telegram），所以要短：
去掉講義章節裡的時間碼、條文只留條號與開頭幾個字；以病人姓名命名的醫案隱藏姓名。
每種段落都附編號，編號是段落 id 的前 6 碼（不唯一時加長），
用 /source 編號可以找回原始標題、路徑與全文。
"""

import re
from pathlib import PurePosixPath

from haixia.index_store import IndexStore
from haixia.textnorm import to_traditional

RRF_K = 60
CANDIDATES = 50


def rrf_merge(rankings, k=RRF_K):
    """rankings 是 {名稱: [row…]}（依相關度排序）；回傳 [(row, rrf 分數, {名稱: 名次})]。"""
    scores, ranks = {}, {}
    for name, rows in rankings.items():
        for rank, row in enumerate(rows, 1):
            scores[row] = scores.get(row, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(row, {})[name] = rank
    order = sorted(scores, key=lambda row: (-scores[row], row))
    return [(row, scores[row], ranks[row]) for row in order]


class CitationStore(IndexStore):
    """查詢時替每種段落補上索引內唯一的短編號，不改動索引。"""

    def count_prefix(self, prefix):
        return self.connection.execute(
            "SELECT count(*) FROM chunks WHERE id >= ? AND id < ?", (prefix, prefix + "\U0010ffff")
        ).fetchone()[0]

    def short_id(self, chunk_id):
        return unique_prefix(chunk_id, self.count_prefix)

    def find_prefix(self, prefix, limit=5):
        """id 以 prefix 開頭的段落（最多 limit 筆，依 id 排序）。"""
        rows = self.connection.execute(
            "SELECT * FROM chunks WHERE id >= ? AND id < ? ORDER BY id LIMIT ?",
            (prefix, prefix + "\U0010ffff", limit)).fetchall()
        return [self._annotate(dict(row)) for row in rows]

    def _annotate(self, record):
        record["short_id"] = self.short_id(record["id"])
        return record

    def rows(self, row_ids):
        records = super().rows(row_ids)
        for record in records.values():
            self._annotate(record)
        return records

    def classic_commentary(self, classic_id, limit=6):
        return [self._annotate(record) for record in super().classic_commentary(classic_id, limit)]


class Searcher:
    def __init__(self, index_dir, embedder=None, candidates=CANDIDATES, rrf_k=RRF_K):
        """embedder 是 EmbeddingClient（或同介面物件）；None 時只用 BM25。"""
        self.store = CitationStore(index_dir)
        self.embedder = embedder
        self.candidates = candidates
        self.rrf_k = rrf_k

    def _query_vector(self, query):
        (values, _tokens, _truncated), = self.embedder.embed([query], "RETRIEVAL_QUERY")
        return values

    def search(self, query, k=10, kind=None):
        """回傳 {"mode", "vector_error", "results": [段落 dict，含 bm25、vector、rrf 與名次]}。"""
        if kind not in (None, "transcript", "document", "classic"):
            raise ValueError("kind 只能是 transcript、document、classic 或 None")
        bm25 = self.store.bm25(query, self.candidates, kind)
        vector, error = [], None
        if self.embedder is None:
            error = "未設定向量查詢"
        elif self.store.vectors is None:
            error = self.store.vector_problem
        else:
            try:
                vector = self.store.vector_top(self._query_vector(query), self.candidates, kind)
            except Exception as problem:  # noqa: BLE001 — 任何向量錯誤都退回 BM25
                error = f"{type(problem).__name__}: {problem}"
        rankings = {"bm25": [row for row, _ in bm25]}
        if vector:
            rankings["vector"] = [row for row, _ in vector]
        merged = rrf_merge(rankings, self.rrf_k)[:k]
        bm25_scores, vector_scores = dict(bm25), dict(vector)
        records = self.store.rows([row for row, _score, _ranks in merged])
        results = []
        for row, score, ranks in merged:
            record = records[row]
            record.update(rrf=score, bm25=bm25_scores.get(row), vector=vector_scores.get(row),
                          bm25_rank=ranks.get("bm25"), vector_rank=ranks.get("vector"))
            results.append(record)
        return {"mode": "hybrid" if error is None else "bm25", "vector_error": error, "results": results}

    def close(self):
        self.store.close()


def _clock(seconds):
    seconds = int(seconds or 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


# ---------- 出處 ----------

SHORT_ID_LEN = 6
SECTION_MAX = 20        # 章節最多幾個字（超過截斷加「…」）
ARTICLE_CHARS = 10      # 條文章節：條號後面保留幾個字
COMPLAINT_MAX = 20      # 醫案主訴最多幾個字
# 單篇醫案的資料夾（raw/ 路徑裡的原始簡體名稱）；959 篇的檔名以病人姓名開頭。
CASE_FOLDERS = ("倪海厦08年医案959篇", "倪海厦08年医案358篇", "倪海厦人纪班学的诊疗医案")
# 這個資料夾的檔名是 Big5 位元組被當成 GB 解讀的亂碼（corpus.py 用 gbk 修復，約 4 成修不回來）。
MOJIBAKE_FOLDER = "倪海厦08年医案959篇"

_TIMECODE = re.compile(r"\s*[（(]\s*\d+\s*[-－]\s*\d{1,2}:\d{2}:\d{2}\s*[）)]")
_ARTICLE = re.compile(r"(?:^|\s)((?:[一二三四五六七八九十百〇零]{1,5}|\d{1,3})[：:])\s*(\S.*)$")
_DATE8 = re.compile(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_LATIN = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ]")
_HAN_NAME = re.compile(r"[\u3400-\u9fff]{2,3}")
_NOT_NAMES = {"醫案", "医案", "病案", "病例", "病患", "診療", "诊疗", "日誌", "日志"}
_LEADING_NAME = re.compile(r"^[A-Za-zＡ-Ｚａ-ｚ0-9０-９ ,，.'’_\-]+")
_DOC_SUFFIXES = {".doc", ".docx", ".htm", ".html", ".txt"}


def _clip(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip(" ,，、：:;；") + "…"


def unique_prefix(chunk_id, count_prefix, minimum=SHORT_ID_LEN):
    """最短、至少 minimum 碼、在索引裡唯一的 id 前綴。count_prefix(前綴) → 以它開頭的段落數。"""
    for length in range(minimum, len(chunk_id) + 1):
        prefix = chunk_id[:length]
        if count_prefix(prefix) <= 1:
            return prefix
    return chunk_id


def short_section(section):
    """縮短章節：去掉時間碼；條文只留條號與開頭幾個字；太長就截斷。

    章節是「第一層 第二層」（空白隔開）。太長時第一層短（≤10 字）就留著、截斷第二層；
    第一層太長（例如針灸教程的章名）就只留第二層。
    """
    if not section:
        return ""
    text = re.sub(r"\s+", " ", _TIMECODE.sub("", section)).strip()
    brackets = {"【": "】", "[": "]", "（": "）", "(": ")"}
    closers = {right: left for left, right in brackets.items()}
    pending = {left: [] for left in brackets}
    drop = set()
    for index, char in enumerate(text):
        if char in brackets:
            pending[char].append(index)
        elif char in closers:
            openings = pending[closers[char]]
            if openings:
                openings.pop()
            else:
                drop.add(index)
    drop.update(index for openings in pending.values() for index in openings)
    if drop:
        text = "".join(char for index, char in enumerate(text) if index not in drop)
    article = _ARTICLE.search(text)
    if article:
        number, body = article.groups()
        return number + _clip(body, ARTICLE_CHARS)
    if len(text) <= SECTION_MAX or " " not in text:
        return _clip(text, SECTION_MAX)
    head, tail = text.split(" ", 1)
    if len(head) <= 10:
        return head + " " + _clip(tail, SECTION_MAX - len(head) - 1)
    return _clip(tail, SECTION_MAX)


def repair_filename(name):
    """959 篇資料夾的亂碼檔名轉回 Big5 原文（gb18030 → big5）；有轉不回來的字就丟掉那幾個字。"""
    try:
        return name.encode("gb18030").decode("big5")
    except UnicodeEncodeError:
        return name
    except UnicodeDecodeError:
        return name.encode("gb18030").decode("big5", errors="replace").replace("\ufffd", "")


_GARBLED_CASE_DIR = re.compile(r"^((?:畍羬洛|畍羬洛)(?:6|7|8|9|10|11)_2008)(.*)$")


def _repair_path_part(name):
    """修復能解碼的片段；無法轉回 Big5 的原字元保留，不刪除。"""
    if not name:
        return name
    encoded = [char.encode("gb18030") for char in name]
    combined = b"".join(encoded)
    try:
        return combined.decode("big5")
    except UnicodeDecodeError as problem:
        offsets = []
        position = 0
        for unit in encoded:
            offsets.append((position, position + len(unit)))
            position += len(unit)
        first = next(i for i, (_start, end) in enumerate(offsets) if end > problem.start)
        last = next(i for i, (_start, end) in enumerate(offsets) if end >= problem.end)
        return (_repair_path_part(name[:first]) + name[first:last + 1]
                + _repair_path_part(name[last + 1:]))


def display_source_path(source):
    """959 篇的路徑只修復已知亂碼目錄與檔名；正常的路徑片段照原樣顯示。"""
    if MOJIBAKE_FOLDER not in source:
        return source
    parts = source.split("/")
    folder = next((i for i, part in enumerate(parts) if MOJIBAKE_FOLDER in part), None)
    if folder is None:
        return source
    for i in range(folder + 1, len(parts)):
        part = parts[i]
        found = _GARBLED_CASE_DIR.match(part)
        if found:
            parts[i] = _repair_path_part(found.group(1)) + found.group(2)
        elif i == len(parts) - 1 and PurePosixPath(part).suffix.lower() in _DOC_SUFFIXES:
            # 959 篇的文件檔名是整段 Big5 位元組誤解碼；副檔名是 ASCII。
            stem, suffix = part[:-len(PurePosixPath(part).suffix)], PurePosixPath(part).suffix
            parts[i] = _repair_path_part(stem) + suffix
    return "/".join(parts)


def original_title(record):
    """原始標題（可能含病人姓名，只給使用者本人看）。959 篇用修復後的檔名。"""
    source = record.get("source") or ""
    if MOJIBAKE_FOLDER in source:
        name = PurePosixPath(source).name
        if PurePosixPath(name).suffix.lower() in _DOC_SUFFIXES:
            name = name[: -len(PurePosixPath(name).suffix)]
        name = repair_filename(name).strip()
        if re.search(r"\w", name):
            return name
    return record.get("title") or ""


def split_case_title(title):
    """「姓名＋8 位數日期＋主訴」→ (YYYY-MM-DD, 主訴)；不是這種格式回傳 None。

    姓名在日期前面，可以是英文或 2–3 字中文姓名；
    「診療日誌20060615真武湯症」這種標題不算。主訴前的「-」「_」、第幾診的「-2-」、
    日期區間的「~20071107」都去掉。
    """
    found = _DATE8.search(title or "")
    if not found:
        return None
    name = title[:found.start()].strip(" _-－,，")
    if not (_LATIN.search(name) or (_HAN_NAME.fullmatch(name) and name not in _NOT_NAMES)):
        return None
    rest = title[found.end():]
    rest = re.sub(r"^\s*[~～]\s*(?:19|20)\d{6}", "", rest)
    rest = re.sub(r"^[\s_\-－]*(?:\d{1,2}[_\-－])?", "", rest).strip(" _-－,，")
    return f"{found.group(1)}-{found.group(2)}-{found.group(3)}", rest


def case_info(record):
    """單篇醫案 → {"date", "complaint"}（不含姓名）；不是醫案回傳 None。

    判斷：文件段落，且（來源在單篇醫案資料夾，或標題是「姓名＋8 位數日期＋主訴」）。
    """
    if record.get("kind") != "document":
        return None
    source = record.get("source") or ""
    in_folder = any(folder in source for folder in CASE_FOLDERS)
    title = original_title(record)
    parts = split_case_title(title)
    if parts is None and not in_folder:
        return None
    if parts is not None:
        date, complaint = parts
    else:
        # 資料夾本身表示這是單篇醫案。959 篇可剝掉開頭的拉丁字母姓名；
        # 其他無法可靠拆出姓名的標題不能當主訴顯示。
        date, complaint = None, ""
        if MOJIBAKE_FOLDER in source and _LATIN.search(title[:1] or ""):
            complaint = _LEADING_NAME.sub("", title)
    if MOJIBAKE_FOLDER in source and complaint:
        complaint = to_traditional(complaint)
    complaint = re.sub(r"\s+", " ", complaint).strip(" _-－,，")
    return {"date": date or record.get("date"), "complaint": _clip(complaint, COMPLAINT_MAX)}


def case_code(record):
    return record.get("short_id") or record["id"][:SHORT_ID_LEN]


def citation(record):
    """各種出處都附可查原文的唯一編號。"""
    code = f"（編號 {case_code(record)}）"
    if record["kind"] == "classic":
        location = f"《{record['title']}》{short_section(record.get('section'))}"
        location = f"{location} {record['episode']}" if record.get("episode") else location
        return location + code
    if record["kind"] == "transcript":
        parts = [record["title"], record.get("episode")]
        where = f"{_clock(record['start'])}–{_clock(record['end'])}"
        return " ".join(p for p in parts if p) + f" {where}" + code
    case = case_info(record)
    if case is not None:
        parts = ["醫案", case["date"], case["complaint"]]
        return " ".join(p for p in parts if p) + code
    parts = [record["title"]]
    section = short_section(record.get("section"))
    if section:
        parts.append(section)
    if record.get("date"):
        parts.append(record["date"])
    if record.get("page_start"):
        pages = (f"第 {record['page_start']} 頁" if record["page_start"] == record["page_end"]
                 else f"第 {record['page_start']}–{record['page_end']} 頁")
        parts.append(pages)
    return " ".join(parts) + code
