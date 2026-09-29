"""混合搜尋：向量（Vertex RETRIEVAL_QUERY）與 BM25 各取前 50，用 RRF 合併。

Vertex 失敗（逾時、網路、認證）時退回只用 BM25，並在結果的 mode 與 vector_error 標明。
常駐記憶體：SQLite 連線、每段一個布林（段落種類）、向量用 memmap，不整份讀進來。
"""

from haixia.index_store import IndexStore

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


class Searcher:
    def __init__(self, index_dir, embedder=None, candidates=CANDIDATES, rrf_k=RRF_K):
        """embedder 是 EmbeddingClient（或同介面物件）；None 時只用 BM25。"""
        self.store = IndexStore(index_dir)
        self.embedder = embedder
        self.candidates = candidates
        self.rrf_k = rrf_k

    def _query_vector(self, query):
        (values, _tokens, _truncated), = self.embedder.embed([query], "RETRIEVAL_QUERY")
        return values

    def search(self, query, k=10, kind=None):
        """回傳 {"mode", "vector_error", "results": [段落 dict，含 bm25、vector、rrf 與名次]}。"""
        if kind not in (None, "transcript", "document"):
            raise ValueError("kind 只能是 transcript、document 或 None")
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


def citation(record):
    """出處：課名、集數與時間 mm:ss–mm:ss，或書名與頁碼。"""
    if record["kind"] == "transcript":
        parts = [record["title"], record.get("episode")]
        where = f"{_clock(record['start'])}–{_clock(record['end'])}"
        return " ".join(p for p in parts if p) + f" {where}"
    parts = [record["title"]]
    if record.get("section"):
        parts.append(record["section"])
    if record.get("date"):
        parts.append(record["date"])
    if record.get("page_start"):
        pages = (f"第 {record['page_start']} 頁" if record["page_start"] == record["page_end"]
                 else f"第 {record['page_start']}–{record['page_end']} 頁")
        parts.append(pages)
    return " ".join(parts)
