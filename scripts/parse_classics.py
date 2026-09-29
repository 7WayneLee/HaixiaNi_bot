#!/usr/bin/env python3
"""離線解析七部經典；完整文字與對號報告只寫私人目錄。"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.classics import BOOKS, parse_book


def parse_all(src_dir, out_dir, html_path=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    for name in BOOKS:
        excluded_glosses = []
        units, unmatched = parse_book(name, src_dir, html_path, excluded_glosses)
        path = out_dir / f"{name}.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(units, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(path)
        report[name] = {"單位數": len(units), "宋本條號數": sum(bool(u["條號"]) for u in units),
                        "未對上條號": unmatched, "排除音釋段數": len(excluded_glosses),
                        "音釋各卷": dict(sorted(Counter(item["卷"] for item in excluded_glosses).items()))}
        print(f"{name}：{len(units)} 單位；條號 {report[name]['宋本條號數']}；排除音釋 {len(excluded_glosses)} 段")
    (out_dir / "解析報告.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description="離線解析七部經典")
    p.add_argument("--src-dir", type=Path, default=Path.home() / "haixia-classics/src")
    p.add_argument("--out-dir", type=Path, default=Path.home() / "haixia-classics/parsed")
    p.add_argument("--html", type=Path, default=Path.home() / "haixia-classics/raw/傷寒論（宋本）.html")
    args = p.parse_args(argv)
    parse_all(args.src_dir, args.out_dir, args.html if args.html.exists() else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
