"""索引的儲存：SQLite（metadata、內文、FTS5 雙字 BM25）與 memmap 向量。

FTS5 的內容是 search_key 之後切成的雙字詞，以空白分隔交給 unicode61 斷詞。
每個中文連續字串除了雙字詞，最後一個字另外當單字 token；
這樣任一個字都是某個 token 的開頭，單字查詢用前綴查詢（"汗"*）就能找到所有出現處。
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from haixia.textnorm import search_key

DB_NAME = "index.sqlite"
VECTORS_NAME = "embeddings.f16.npy"
VECTORS_META = "embeddings.meta.json"
COLUMNS = ("id", "kind", "source", "title", "episode", "section", "page_start", "page_end",
           "start", "end", "date", "text", "chars", "quality", "raw_text", "unit_ids")
_RUN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]+|[0-9a-z]+")


def _runs(text):
    return _RUN.findall(search_key(text).casefold())


def _is_cjk(run):
    return not run[0].isascii()


def index_tokens(text):
    """內文 → token 清單（雙字詞＋每串最後一個字；英數字整個詞）。"""
    tokens = []
    for run in _runs(text):
        if not _is_cjk(run):
            tokens.append(run)
            continue
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        tokens.append(run[-1])
    return tokens


# 查詢裡含語助詞的雙字詞（「的組」「成呢」）只會帶進雜訊；全部都是這種時才保留。
PARTICLES = set("的了吗呢啊吧呀么什")


def match_query(text):
    """查詢 → FTS5 MATCH 字串；雙字詞與單字前綴以 OR 連接。空查詢回傳 None。"""
    terms, noisy = [], []
    for run in _runs(text):
        if not _is_cjk(run):
            terms.append(f'"{run}"')
        elif len(run) == 1:
            terms.append(f'"{run}"*')
        else:
            for i in range(len(run) - 1):
                pair = run[i:i + 2]
                (noisy if PARTICLES & set(pair) else terms).append(f'"{pair}"')
    unique = list(dict.fromkeys(terms or noisy))
    return " OR ".join(unique) if unique else None


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


SCHEMA = """
CREATE TABLE chunks (
  row INTEGER PRIMARY KEY,           -- 與 chunks.jsonl、向量的列順序相同（從 0 起）
  id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, source TEXT NOT NULL, title TEXT,
  episode TEXT, section TEXT, page_start INTEGER, page_end INTEGER,
  start REAL, "end" REAL, date TEXT, text TEXT NOT NULL, chars INTEGER NOT NULL,
  quality INTEGER, raw_text TEXT, unit_ids TEXT);
CREATE INDEX chunks_kind ON chunks(kind);
CREATE TABLE classic_links (
  classic_id TEXT NOT NULL, target_id TEXT NOT NULL, target_kind TEXT NOT NULL,
  score REAL NOT NULL, method TEXT NOT NULL, lecture_number TEXT,
  PRIMARY KEY (classic_id, target_id));
CREATE INDEX classic_links_classic ON classic_links(classic_id);
CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens, content='', tokenize='unicode61 remove_diacritics 0');
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def build_db(chunks_path, db_path, log=print):
    """從 chunks.jsonl 建 index.sqlite；先寫暫存檔再改名。"""
    db_path = Path(db_path)
    temp = db_path.with_name(db_path.name + ".tmp")
    temp.unlink(missing_ok=True)
    connection = sqlite3.connect(str(temp))
    try:
        connection.executescript(SCHEMA)
        count = 0
        with open(chunks_path, encoding="utf-8") as source:
            for row, line in enumerate(source):
                chunk = json.loads(line)
                values = [chunk.get(column) for column in COLUMNS]
                connection.execute(f"INSERT INTO chunks VALUES (?, {', '.join('?' * len(COLUMNS))})",
                                   [row, *values])
                connection.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                                   (row, " ".join(index_tokens(chunk["text"]))))
                count += 1
        connection.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("count", str(count)), ("chunks_sha256", file_sha256(chunks_path))])
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
        connection.commit()
    finally:
        connection.close()
    temp.replace(db_path)
    log(f"已寫入 {count} 段：{db_path}")
    return count


class IndexStore:
    """唯讀開啟索引。向量檔不存在時只有 BM25。"""

    def __init__(self, index_dir):
        import numpy as np

        self.dir = Path(index_dir)
        self.connection = sqlite3.connect(f"file:{self.dir / DB_NAME}?mode=ro", uri=True,
                                          check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        meta = dict(self.connection.execute("SELECT key, value FROM meta").fetchall())
        self.count = int(meta["count"])
        self.chunks_sha256 = meta["chunks_sha256"]
        self.vectors = None
        self.vector_problem = None
        path = self.dir / VECTORS_NAME
        if not path.exists():
            self.vector_problem = "沒有向量檔"
            return
        vectors = np.load(path, mmap_mode="r")
        meta_path = self.dir / VECTORS_META
        vector_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if vectors.shape[0] != self.count:
            self.vector_problem = f"向量有 {vectors.shape[0]} 列，但索引有 {self.count} 段"
        elif vector_meta.get("chunks_sha256") not in (None, self.chunks_sha256):
            self.vector_problem = "向量與索引不是用同一份 chunks.jsonl 建的"
        else:
            self.vectors = vectors
            self.vector_meta = vector_meta
            kinds = self.connection.execute("SELECT kind FROM chunks ORDER BY row").fetchall()
            self.kinds = np.array([k[0] for k in kinds], dtype="U10")

    def bm25(self, query, limit=50, kind=None):
        """回傳 [(row, 分數)]，分數越大越相關（FTS5 bm25 取負號）。"""
        expression = match_query(query)
        if expression is None:
            return []
        sql = ("SELECT f.rowid, bm25(chunks_fts) FROM chunks_fts f "
               + ("JOIN chunks c ON c.row = f.rowid " if kind else "")
               + "WHERE chunks_fts MATCH ? " + ("AND c.kind = ? " if kind else "")
               + "ORDER BY bm25(chunks_fts) LIMIT ?")
        parameters = [expression, *([kind] if kind else []), limit]
        return [(row, -score) for row, score in self.connection.execute(sql, parameters)]

    def vector_top(self, query_vector, limit=50, kind=None, block=2048):
        """逐塊計算內積（向量已正規化即為 cosine），回傳 [(row, 分數)]。"""
        import numpy as np

        if self.vectors is None:
            raise RuntimeError(self.vector_problem or "沒有向量")
        query = np.asarray(query_vector, dtype=np.float32)
        query = query / np.linalg.norm(query)
        scores = np.empty(self.count, dtype=np.float32)
        for start in range(0, self.count, block):
            scores[start:start + block] = self.vectors[start:start + block].astype(np.float32) @ query
        if kind is not None:
            scores[self.kinds != kind] = -np.inf
        limit = min(limit, self.count)
        top = np.argpartition(-scores, limit - 1)[:limit] if limit else np.array([], dtype=int)
        top = top[np.argsort(-scores[top])]
        return [(int(row), float(scores[row])) for row in top if np.isfinite(scores[row])]

    def rows(self, row_ids):
        if not row_ids:
            return {}
        marks = ",".join("?" * len(row_ids))
        result = {}
        for record in self.connection.execute(f"SELECT * FROM chunks WHERE row IN ({marks})", list(row_ids)):
            result[record["row"]] = dict(record)
        return result

    def neighbors(self, chunk_id, before=1, after=1):
        """段落 id → (該段, [前面的段…], [後面的段…])，都依 row 排序。

        只取同一來源、row 連續相鄰的段落：往前（往後）遇到別的來源就停。
        找不到這個 id 時丟 KeyError。
        """
        found = self.connection.execute("SELECT row, source FROM chunks WHERE id = ?",
                                        (chunk_id,)).fetchone()
        if found is None:
            raise KeyError(chunk_id)
        row, source = found["row"], found["source"]
        records = self.rows(list(range(max(row - before, 0), row + after + 1)))

        def walk(rows):
            taken = []
            for other in rows:
                record = records.get(other)
                if record is None or record["source"] != source:
                    break
                taken.append(record)
            return taken

        previous = walk(range(row - 1, row - before - 1, -1))[::-1]
        following = walk(range(row + 1, row + after + 1))
        return records[row], previous, following

    def close(self):
        self.connection.close()

    def classic_commentary(self, classic_id, limit=6):
        target = self.connection.execute("SELECT kind FROM chunks WHERE id = ?", (classic_id,)).fetchone()
        if target is None or target[0] != "classic":
            raise KeyError(classic_id)
        rows = self.connection.execute(
            "SELECT c.*, l.score, l.method, l.lecture_number FROM classic_links l "
            "JOIN chunks c ON c.id = l.target_id WHERE l.classic_id = ? "
            "ORDER BY l.score DESC LIMIT ?", (classic_id, limit)).fetchall()
        return [dict(row) for row in rows]
