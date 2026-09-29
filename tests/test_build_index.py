"""build_index chunks／db 的端對端測試，以及 sync_index.sh（假的 ssh、rclone）。

測試資料全部是自己編的；「Doe,Jane」是虛構的病人姓名，用來確認報告不會印出檔名。
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from haixia import correction
from haixia.transcript import create, save_corrected
from scripts import build_index
from tests.test_index_chunks import make_pdf

ROOT = Path(__file__).resolve().parents[1]
EBOOKS = "文字資料/01.倪海厦电子书全集"
CASES = "文字資料/03.倪海厦诊疗日志 医案"
FAKE_PATIENT = "Doe,Jane"
MOJIBAKE = "病歷".encode("big5").decode("gbk")      # 真實檔名是 Big5 被當成 GBK 的亂碼


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def corrected_transcript(out_dir, source):
    engine = {"name": "whisper", "model": "large-v3", "version": "1", "params": {},
              "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}
    segments = [{"start": float(i * 5), "end": float(i * 5 + 4), "text_raw": f"第{i}句", "text": f"第{i}句",
                 "speaker": None, "confidence": None, "low_confidence": False} for i in range(40)]
    document = create(source, 205, engine, segments)
    chunks = correction.split_chunks(document["segments"])
    results = [{"start": c["start"], "end": c["end"], "status": "ok", "attempts": 1, "searches": 0,
                "elapsed_sec": 1.0,
                "lines": [(f"校正後第{i}句講桂枝湯的用法", True) for i in range(c["start_index"], c["end_index"])]}
               for c in chunks]
    doc = correction.corrected_document(document, chunks, results, "m", 5, "0" * 64)
    save_corrected(doc, out_dir / f"{source}.json")


@pytest.fixture
def raw(tmp_path):
    raw = tmp_path / "raw"
    renji = raw / EBOOKS / "人纪 《伤寒论》-(（守候诚实）淘宝店）.pdf"
    renji.parent.mkdir(parents=True)
    make_pdf(renji, [["第一章 總論", "桂枝湯主治太陽中風，汗出惡風，這是第一段很重要的內容。"],
                     ["麻黃湯主治太陽傷寒，無汗而喘，第二頁的內容在這裡。"], ["小柴胡湯治少陽病，往來寒熱。"],
                     ["附錄：煎藥方法，先煮麻黃去上沫。"]])
    tianji = raw / EBOOKS / "天纪  《天纪》-(（守候诚实）淘宝店）.pdf"
    make_pdf(tianji, [["天紀第一課：陰陽五行。"], ["天紀第二課：天干地支。"], ["天紀第三課：八卦。"]])
    shutil.copy(tianji, raw / EBOOKS / "天纪  地脉道-(（守候诚实）淘宝店）.pdf")
    make_pdf(raw / EBOOKS / "天纪  天机道-(（守候诚实）淘宝店）.pdf", [["掃描頁"]])
    shutil.copy(renji, write(raw / "電子書/倪海厦人纪系列之伤寒论.pdf", b""))
    # 醫案：一篇與人紀講義段落相同（要被段落去重）、一篇 MD5 重複、一篇以虛構姓名命名
    case_dir = raw / CASES / "倪海厦人纪班学的诊疗医案(神州医料库）"
    write(case_dir / "案例 (1).txt", "2008年3月5日\n桂枝湯主治太陽中風，汗出惡風，這是第一段很重要的內容。\n"
          "病人惡寒發熱，脈浮緩，處方桂枝湯三劑後痊癒。".encode("gb18030"))
    person_dir = raw / CASES / "倪海厦08年医案959篇-按人名分类(神州医料库）" / "月份"
    write(person_dir / f"{FAKE_PATIENT}20080102{MOJIBAKE}.txt", "病人口渴、便秘，處方大承氣湯。".encode("utf-8"))
    write(person_dir / f"{FAKE_PATIENT}副本.txt", "病人口渴、便秘，處方大承氣湯。".encode("utf-8"))
    write(raw / CASES / "倪海厦诊疗日志医案-全(（守候诚实）淘宝店）.rar", b"Rar!")
    write(raw / "文字資料/07.倪海厦国学堂/国学堂-刘力红感悟《身心性》(（守候诚实）淘宝店）/梁冬对话刘力红第一讲.txt", "非倪師")
    lrc = "".join(f"[{i // 60:02d}:{i % 60:02d}.00]倪海廈：第{i}句對話內容講中醫。\n" for i in range(12, 90, 2))
    write(raw / "文字資料/07.倪海厦国学堂/国学堂-倪海厦对话梁冬MP3(（守候诚实）淘宝店）/091226梁冬对话倪海厦第一讲.Lrc",
          "[00:01.00]國學堂\n[00:03.00]梁冬：大家好\n" + lrc)
    return raw


def run_chunks(tmp_path, raw, ocr=True):
    corrected = tmp_path / "corrected"
    corrected_transcript(corrected, "影片/05 傷寒論/傷寒論 DVD (7)/伤寒论6（2）.rmvb")
    out = tmp_path / "out"
    if ocr:
        write(out / "ocr/tianjidao.pages.json", json.dumps({"source": build_index.TIANJIDAO, "pages": [
            {"page": 1, "text": "天機道第一頁的文字很長很長\n接續的第二行文字。"},
            {"page": 2, "text": "第二頁的內容講風水。"}]}, ensure_ascii=False))
    code = build_index.main(["chunks", "--raw-dir", str(raw), "--corrected-dir", str(corrected),
                             "--out-dir", str(out), "--jobs", "1"])
    report = json.loads((out / "build_report.json").read_text(encoding="utf-8"))
    chunks = [json.loads(line) for line in (out / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    return code, out, report, chunks


def test_chunks_end_to_end(tmp_path, raw):
    code, out, report, chunks = run_chunks(tmp_path, raw)
    assert code == 0, report["errors"] + report["warnings"]
    by_title = {}
    for chunk in chunks:
        by_title.setdefault(chunk["title"], []).append(chunk)
        assert set(chunk) == {"id", "kind", "source", "title", "episode", "section", "page_start", "page_end",
                              "start", "end", "date", "text", "chars"}
    # 逐字稿
    transcript = by_title["人紀・傷寒論"][0]
    assert transcript["kind"] == "transcript" and transcript["episode"] == "傷寒論6（2）"
    assert transcript["source"] == "影片/05 傷寒論/傷寒論 DVD (7)/伤寒论6（2）.rmvb"
    assert transcript["start"] == 0.0 and transcript["end"] > transcript["start"]
    # LRC
    lrc = by_title["梁冬對話倪海廈"][0]
    assert lrc["episode"] == "第一講" and lrc["date"] == "2009-12-26" and lrc["kind"] == "transcript"
    # 人紀講義：頁碼、章節、正體
    renji = by_title["人紀《傷寒論》"]
    assert renji[0]["page_start"] == 1 and renji[-1]["page_end"] == 4
    assert renji[0]["section"] == "第一章 總論"
    # 天機道用文字辨識結果
    assert by_title["天紀 天機道"][0]["page_start"] == 1
    # 醫案：和人紀相同的段落被去掉，其餘保留，日期從第一行抓
    case = by_title["案例 (1)"][0]
    assert "桂枝湯主治太陽中風" not in case["text"] and "脈浮緩" in case["text"]
    assert case["date"] == "2008-03-05"
    assert by_title[f"{FAKE_PATIENT}20080102病歷"][0]["date"] == "2008-01-02"   # 檔名亂碼已修正
    # 報告
    reasons = {item["path"]: item["reason"] for item in report["skipped"]}
    assert any("地脉道" in path and "已確認 MD5" in reason for path, reason in reasons.items())
    assert any(path.startswith("電子書/") and "已確認 MD5" in reason for path, reason in reasons.items())
    assert any("刘力红" in path and "劉力紅" in reason for path, reason in reasons.items())
    assert any(reason.startswith("MD5 與") for reason in reasons.values())
    assert report["groups"]["cases"]["dropped_paragraph"] == 1
    assert report["groups"]["renji"]["chars_after"] > 0 and report["total_chunks"] == len(chunks)
    assert report["rar_check"]["status"] in {"已比對", "找不到 unar，未比對（brew install unar）"} or "失敗" in report["rar_check"]["status"]
    report_text = (out / "build_report.json").read_text(encoding="utf-8")
    assert FAKE_PATIENT not in report_text            # 以病人姓名命名的檔案只留雜湊
    assert "校正後第" not in report_text and "脈浮緩" not in report_text   # 不印內文
    assert len({c["id"] for c in chunks}) == len(chunks)

    # db
    assert build_index.main(["db", "--out-dir", str(out)]) == 0
    from haixia.index_store import IndexStore

    store = IndexStore(out)
    assert store.count == len(chunks) and store.vectors is None
    assert store.bm25("脈浮緩")


def test_chunks_reports_missing_ocr_and_is_rerunnable(tmp_path, raw):
    code, out, report, _ = run_chunks(tmp_path, raw, ocr=False)
    assert code == 1
    assert any("天机道" in e["path"] and "文字辨識" in e["error"] for e in report["errors"])
    first = (out / "chunks.jsonl").read_bytes()
    code, out, report, _ = run_chunks(tmp_path, raw, ocr=False)
    assert (out / "chunks.jsonl").read_bytes() == first      # 重跑結果相同（有快取、穩定 id）


def test_expected_duplicate_without_twin_warns(tmp_path, raw):
    (raw / "電子書/倪海厦人纪系列之伤寒论.pdf").write_bytes(b"%PDF-1.4 different")
    code, _, report, _ = run_chunks(tmp_path, raw)
    assert code == 1
    assert any("電子書" in warning for warning in report["warnings"])


@pytest.mark.skipif(shutil.which("textutil") is None, reason="需要 macOS textutil")
def test_doc_conversion_with_textutil(tmp_path, raw):
    html = "<html><meta charset='utf-8'><body><p>事實評論：西藥的副作用很大。</p><p>第二段。</p></body></html>"
    write(raw / "文字資料/05.倪海厦事实评论/測試篇.doc", html)
    subprocess.run(["textutil", "-convert", "doc", str(raw / "文字資料/05.倪海厦事实评论/測試篇.doc"),
                    "-output", str(raw / "文字資料/05.倪海厦事实评论/測試篇.doc")], check=True)
    _, _, _, chunks = run_chunks(tmp_path, raw)
    chunk, = [c for c in chunks if c["title"] == "測試篇"]
    assert "西藥的副作用很大" in chunk["text"]


# ---------- sync_index.sh ----------

FAKE_SSH = """#!/usr/bin/env bash
# 模擬 ssh：略過選項與主機，把其餘參數用空白接起來交給本機 bash（和真的 ssh 一樣）。
while [[ $1 == -o ]]; do shift 2; done
shift
HOME="$FAKE_REMOTE_HOME" exec bash -c "$*"
"""
FAKE_RCLONE = """#!/usr/bin/env bash
echo "rclone $*" >> "$FAKE_LOG"
if [[ $1 == copy ]]; then
  list=$3; src=$4; dst=$5
  mkdir -p "$FAKE_GCS/${dst#*:}"
  while read -r name; do cp "$src/$name" "$FAKE_GCS/${dst#*:}/$name"; done < "$list"
fi
"""
FAKE_SHA = """#!/usr/bin/env bash
shasum -a 256 "$@"
"""


@pytest.fixture
def fake_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("rclone", FAKE_RCLONE), ("sha256sum", FAKE_SHA)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    remote_home = tmp_path / "remote"
    remote_home.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_REMOTE_HOME=str(remote_home),
               FAKE_LOG=str(tmp_path / "rclone.log"), FAKE_GCS=str(tmp_path / "gcs"))
    return env, remote_home, tmp_path


def sync(env, *args, stdin=""):
    return subprocess.run(["bash", str(ROOT / "scripts/sync_index.sh"), *args], env=env, input=stdin,
                          capture_output=True, text=True)


def test_sync_push_lists_sizes_asks_and_uses_copy_and_check(fake_env):
    env, remote_home, tmp = fake_env
    local = tmp / "build"
    write(local / "chunks.jsonl", "{}\n")
    write(local / "index.sqlite", b"x" * 2048)
    write(local / "cache/text/secret.json", "不能傳")
    result = sync(env, "push", str(local), stdin="n\n")
    assert result.returncode == 1 and "已取消" in result.stdout
    assert "chunks.jsonl" in result.stdout and "2048" in result.stdout and "合計 2051 bytes" in result.stdout
    assert not (remote_home / "haixia-index-build").exists()

    result = sync(env, "push", str(local), "--yes")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (remote_home / "haixia-index-build").iterdir()) == ["chunks.jsonl", "index.sqlite"]
    log = (tmp / "rclone.log").read_text(encoding="utf-8")
    assert "rclone copy --files-from" in log and "rclone check --one-way --files-from" in log
    assert "haixiani-bot-data-507014/index/" in log
    assert not any(word in log for word in ("delete", "sync ", "purge", "move"))
    assert (tmp / "gcs/haixiani-bot-data-507014/index/index.sqlite").exists()


def test_sync_pull_embed_and_ocr_verify_checksums(fake_env):
    env, remote_home, tmp = fake_env
    remote = remote_home / "haixia-index-build"
    write(remote / "embeddings.f16.npy", b"\x93NUMPY" + b"0" * 100)
    write(remote / "embeddings.meta.json", "{}")
    write(remote / "ocr/tianjidao.pages.json", "{}")
    local = tmp / "mac"
    result = sync(env, "pull-embed", str(local), "--yes")
    assert result.returncode == 0, result.stderr
    assert "embeddings.f16.npy" in result.stdout and "核對 SHA-256" in result.stdout
    assert (local / "embeddings.f16.npy").read_bytes() == (remote / "embeddings.f16.npy").read_bytes()
    result = sync(env, "pull-ocr", str(local), "--yes")
    assert result.returncode == 0, result.stderr
    assert (local / "ocr/tianjidao.pages.json").exists()
    result = sync(env, "push-embed", "--yes")
    assert result.returncode == 0, result.stderr
    assert (tmp / "gcs/haixiani-bot-data-507014/index/embeddings.meta.json").exists()
