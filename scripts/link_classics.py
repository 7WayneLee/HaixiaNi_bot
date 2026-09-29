#!/usr/bin/env python3
"""離線關聯經典與人紀講義、逐字稿。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.classic_links import link_database


def main(argv=None):
    p = argparse.ArgumentParser(description="建立經典與倪師講解連結")
    p.add_argument("--out-dir", type=Path, default=Path.home() / "haixia-index-build")
    args = p.parse_args(argv)
    report = link_database(args.out_dir / "index.sqlite", Path(__file__).resolve().parents[1] / "data/tcm_terms_tw.txt")
    (args.out_dir / "classic_links_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, result in report.items():
        print(f"{name}：講義 {result['講義連結率']:.1%}；逐字稿 {result['逐字稿連結率']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
