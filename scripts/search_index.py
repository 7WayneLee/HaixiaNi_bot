#!/usr/bin/env python3
"""用混合搜尋查索引，印出每段的出處與前 120 字。

    scripts/search_index.py "桂枝湯的組成" [-k 10] [--kind transcript|document] [--bm25-only]

向量查詢走 Vertex AI（RETRIEVAL_QUERY，需在有 metadata server 的 GCE 上），
失敗時自動退回只用 BM25，並在結果上方標明。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia import vertex
from haixia.search import Searcher, citation

DEFAULT_INDEX = Path.home() / "haixia-index-build"


def make_embedder(args):
    api = vertex.GoogleApi(timeout=args.timeout, max_attempts=2)
    return vertex.EmbeddingClient(api, args.project, args.location, args.model, args.dims)


def main(argv=None):
    parser = argparse.ArgumentParser(description="查詢倪師資料索引")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=10, help="回傳幾段（預設 10）")
    parser.add_argument("--kind", choices=["transcript", "document"])
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--bm25-only", action="store_true", help="不呼叫 Vertex，只用關鍵字搜尋")
    parser.add_argument("--project", default=vertex.DEFAULT_PROJECT)
    parser.add_argument("--location", default=vertex.DEFAULT_LOCATION)
    parser.add_argument("--model", default=vertex.MODEL)
    parser.add_argument("--dims", type=int, default=vertex.DIMS)
    parser.add_argument("--timeout", type=float, default=10, help="向量查詢逾時秒數")
    args = parser.parse_args(argv)

    searcher = Searcher(args.index_dir, None if args.bm25_only else make_embedder(args))
    try:
        result = searcher.search(args.query, k=args.k, kind=args.kind)
    finally:
        searcher.close()
    if result["mode"] != "hybrid":
        print(f"［只用關鍵字搜尋：{result['vector_error']}］")
    if not result["results"]:
        print("找不到相關段落")
        return 1
    for number, record in enumerate(result["results"], 1):
        scores = [f"RRF {record['rrf']:.4f}"]
        if record["vector"] is not None:
            scores.append(f"向量 {record['vector']:.3f}（第 {record['vector_rank']}）")
        if record["bm25"] is not None:
            scores.append(f"BM25 {record['bm25']:.2f}（第 {record['bm25_rank']}）")
        print(f"{number}. {citation(record)}")
        print(f"   {'、'.join(scores)}")
        print(f"   {record['text'][:120].replace(chr(10), ' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
