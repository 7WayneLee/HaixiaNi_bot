# 第三步後半：切段、向量與混合搜尋（建索引）

把校正後的逐字稿與文字資料切成約 500 字一段，建成「向量＋關鍵字」的混合搜尋索引，給之後的 Claude 問答與 Telegram bot 使用。

所有產物都放在 repo 外的 `~/haixia-index-build/`（裡面有原始文字與病人姓名，**不能**放進 repo）。

## 資料範圍

範圍規則寫在 `haixia/corpus.py` 的 `classify()`，依 `raw/` 底下的相對路徑判斷，測試逐條檢查（`tests/test_index_chunks.py`）。

**放進索引**

| 組別（段落去重優先序） | 內容 |
|---|---|
| 影片逐字稿 | `~/haixia-corrected/` 的 440 份校正版（用 `text`，不用 `text_asr`） |
| 梁冬對話倪海廈 | 國學堂 7 個 `.Lrc`，用 `scripts/lrc_to_transcript.py` 解析，當有時間點的逐字稿 |
| 1 人紀講義 | 電子書全集裡的人紀 5 本 PDF |
| 2 天紀 | 《天紀》PDF、人間道 PDF、天機道（Cloud Vision 文字辨識結果）、天機道聽課筆記 doc |
| 3 單篇醫案 | 08 年醫案 959 篇、358 篇、人紀班醫案（一篇一案），以及「分雜」資料夾裡的單篇診療日誌 |
| 4 診療日誌與漢唐日誌 | 兩個診療日誌 doc、漢唐中醫日誌、「分雜」裡的年度日誌 |
| 5 文章、事實評論、方劑講解、藥方 | 漢唐中醫的文章、方劑講解（doc 與 PDF）、藥方、事實評論 8 篇等 |
| 6 彙編 | 文集及醫桉 PDF、先生醫案 PDF、醫案 htm、CHM（05–08 年診療日誌）、「分雜」裡的彙編 doc |

**不放**（每個檔的原因都寫在 `build_report.json` 的 `skipped`）

- 國學堂的劉力紅、郭生白《說白傷寒論》等非倪師內容；王文遠平衡針 RAR。
- `MP3 人纪全/` 的人紀版神農本草經掃描檔（和文字版重複）。
- 大 RAR「倪海厦诊疗日志医案-全」：`chunks` 會用 `unar` 解開，逐檔以 MD5 比對醫案資料夾，結果在 `rar_check`；對不到的檔**不會**自動加入，清單只寫在 out-dir 的 `private_rar_unmatched.tsv`（檔名可能有病人姓名）。
- `電子書/` 的 PDF、`天纪 地脉道` PDF：規則上標為「應該是重複檔」，程式會確認 MD5 真的和保留的檔相同；對不到時列在 `warnings`。

## 流程

```
Mac                                   movie-nas                         GCS
───────────────────────────────────   ───────────────────────────────   ─────────────────────
                                      build_index.py ocr  ───────────→  ocr/tianjidao/*.json
sync_index.sh pull-ocr  ←───────────  ~/haixia-index-build/ocr/
build_index.py chunks（不連網）
build_index.py db
sync_index.sh push  ─────────────────→ ~/haixia-index-build/ ─────────→ index/
                                      build_index.py embed（花錢）
                                      sync_index.sh push-embed ──────→ index/
sync_index.sh pull-embed ←──────────
                                      bot：haixia/search.py
```

### 1. 文字辨識（movie-nas）

```bash
.venv/bin/python scripts/build_index.py ocr
```

- 用 Cloud Vision `files:asyncBatchAnnotate`（`DOCUMENT_TEXT_DETECTION`，語言提示 `zh`）直接讀 `gs://…/raw/文字資料/01.倪海厦电子书全集/天纪  天机道-(（守候诚实）淘宝店）.pdf`，結果寫到 `gs://…/ocr/tianjidao/`。
- 送出後把工作名稱記在 `ocr/tianjidao.operation.json`；中斷後重跑會接著等，不會重送。GCS 已有輸出時直接讀取。
- 讀回的逐頁文字存成 `~/haixia-index-build/ocr/tianjidao.pages.json`；頁數不是 82 會回報失敗。
- 認證用 metadata server 的預設服務帳號 token，不用金鑰。82 頁在 Vision 每月免費 1,000 頁內。

### 2. 切段（Mac，不連網）

```bash
scripts/sync_index.sh pull-ocr ~/haixia-index-build
.venv/bin/python scripts/build_index.py chunks
```

- 轉文字：`.doc`／`.docx`／`.htm` 用 macOS `textutil`；`.txt` 依序試 utf-8-sig、utf-16、gb18030、big5；PDF 用 PyMuPDF 逐頁；CHM 用 `7zz`（`brew install sevenzip`）解開後逐頁 `textutil`；沒有副檔名但檔頭是 OLE 的當 Word。
- PDF 的處理：依頁面旋轉轉座標、略過直排側欄與浮水印；在許多頁同一位置重複的頁首、頁尾、頁碼移除；一張紙印兩頁的先左欄後右欄；假粗體（同一字重畫多次）只留一次。
- 顯示文字一律經 `to_traditional`；比對、去重、BM25 一律用 `search_key`。
- 標題：去掉「（（守候诚实）淘宝店）」「(神州医料库）」「（二羊中医馆）」等來源標記和副檔名再轉正體；959 篇醫案的檔名是 Big5 被當成 GBK 的亂碼，會先轉回來。
- 轉文字的結果快取在 `~/haixia-index-build/cache/`，重跑約 20 秒。

**去重**

1. 檔案層級：MD5 相同只留一份（組別優先序 → 不在「分雜」→ 路徑）。
2. 段落層級（逐字稿不做）：依上表的組別順序處理，`search_key` 後去掉空白、標點並轉小寫當 key，長度 8 以上見過就丟。
   另外同一本書的 PDF 與 doc 分段方式不同、整段比不到，所以再把段落切成句子：八成以上的字數是見過的句子也丟。報告分別記 `dropped_paragraph`、`dropped_sentence`。
3. 來源聲明、只有頁碼、目錄點線的行不算內容（`boilerplate_paragraphs`）。

**切段**

- 逐字稿：依序合併相鄰 segment，目標 500 字（400–600），不切斷 segment；下一段和前一段重疊約 100 字；記起訖秒數。
- 文件：依段落合併成約 500 字；段落中間只在超過 600 字時於「。！？；」後切開；只有標題的短段併進下一段。PDF 記起訖頁碼；抓得到「第X章」「辨…病脈證並治」「一、」「【…】」這類標題時記 `section`（兩層）；日誌類抓日期標題更新 `date`，醫案從檔名或開頭抓日期。
- 每段欄位：`id`（穩定雜湊）、`kind`（transcript／document）、`source`、`title`、`episode`、`section`、`page_start`、`page_end`、`start`、`end`、`date`、`text`、`chars`。

**報告** `build_report.json`：各組檔數、段數、去重前後字數、每個文件的字數（以病人姓名命名的檔只印雜湊）、略過的檔與原因、錯誤、警告、RAR 比對結果。報告與 log 不印內文。

### 3. SQLite（Mac 或 movie-nas）

```bash
.venv/bin/python scripts/build_index.py db
```

`index.sqlite`：`chunks` 表存 metadata 與內文（`row` 從 0 起，和 `chunks.jsonl`、向量的列順序相同）；`chunks_fts` 是 contentless FTS5，內容是 `search_key` 後切成的雙字詞。每串中文最後一個字另外當單字 token，所以單字查詢（例如「汗」）用前綴查詢就能找到所有出現處。

### 4. 同步

```bash
scripts/sync_index.sh push ~/haixia-index-build   # chunks.jsonl、build_report.json、index.sqlite
```

只用 `rclone copy` 與 `rclone check --one-way`，不刪任何東西；傳之前會先列出檔案與總大小並要求確認（`--yes` 略過）。`cache/` 與 `private_*` 不會傳。

### 5. 向量（movie-nas，會花錢）

```bash
.venv/bin/python scripts/build_index.py embed --dry-run   # 先估計 token 與費用，不呼叫 API
.venv/bin/python scripts/build_index.py embed
scripts/sync_index.sh push-embed                        # 在 Mac 上執行，經 SSH 讓 movie-nas 上傳
scripts/sync_index.sh pull-embed ~/haixia-index-build
```

- Vertex AI `gemini-embedding-001`，768 維，文件用 `RETRIEVAL_DOCUMENT` 並帶標題（課名＋集數或章節），問題用 `RETRIEVAL_QUERY`。
- 官方文件的限制：一次請求最多 250 筆、合計 20,000 token（超過回 400）、單筆超過 2,048 token 會被截斷；同一頁也寫「gemini-embedding-001 每次請求只能一筆」。所以 `--batch-size` 預設 1，可以調大，但程式仍擋 250 筆／20,000 token。
- 同時 4 個請求；429、5xx、逾時用指數退避重試；401 會重新取 token。
- 快取 `embed_cache.sqlite` 以 (model, dims, task_type, sha1(title + text)) 為 key，中斷後重跑只送沒做完的段。
- 累計 token（含已快取）將超過 `--max-tokens`（預設 1,500 萬）就停，訊息會說怎麼放寬。
- 串流處理：先掃一遍算快取與估計量，再邊讀邊送，最後依列順序從快取寫進 memmap，不會把全部內文或向量放進記憶體。
- 產物：`embeddings.f16.npy`（N×768，L2 正規化的 float16）、`embeddings.meta.json`（模型、維度、筆數、token 數、估計費用、`chunks.jsonl` 的 SHA-256、建立時間）。

### 6. 搜尋

```bash
.venv/bin/python scripts/search_index.py "桂枝湯的組成" -k 10
.venv/bin/python scripts/search_index.py "汗" --kind transcript --bm25-only
```

`haixia/search.py` 的 `Searcher(index_dir).search(query, k=10, kind=None)`：向量取前 50、BM25 取前 50，用 RRF（k=60）合併。每段回傳內文、metadata、`bm25`、`vector`、`rrf` 分數與兩邊的名次。

- 查詢向量走 Vertex（逾時 10 秒、最多試 2 次）；失敗、沒有向量檔、或向量和 `index.sqlite` 不是同一份 `chunks.jsonl` 建的時，退回只用 BM25，結果的 `mode` 是 `bm25`，`vector_error` 寫原因。
- BM25 查詢把問題切成雙字詞以 OR 連接；含「的、了、嗎、呢、什、麼」等語助詞的雙字詞會略過。

## 在哪台機器跑

| 步驟 | 機器 | 連網／花錢 |
|---|---|---|
| ocr | movie-nas | Cloud Vision（免費額度內） |
| chunks | Mac | 不連網 |
| db | Mac 或 movie-nas | 不連網 |
| embed | movie-nas | Vertex AI（會花錢） |
| 搜尋、bot | movie-nas | 每次查詢一個短向量請求 |

movie-nas 只需要 `numpy`、`opencc`（`chunks` 才需要 `pymupdf` 和 macOS 的 `textutil`）。

## 費用

- 2026-09-29 實際 `chunks` 的結果：19,948 段；`embed --dry-run` 用保守估計（每個中文字算 1 token）得約 953 萬 token。gemini-embedding-001 線上價格每百萬 token 0.15 美元，約 **1.4 美元（約 46 台幣）**；實際 token 數以 `embeddings.meta.json` 為準。
- 查詢時每個問題約數十個 token，費用可忽略。
- 天機道 82 頁在 Cloud Vision 每月 1,000 頁的免費額度內。
- GCS `index/` 約 120 MB（chunks、sqlite、向量），每月不到 1 台幣。

## 記憶體（movie-nas 可用約 400 MB）

- 搜尋模組常駐：SQLite 連線、每段一個布林（段落種類），向量用 memmap（約 30 MB 的檔案頁，可被系統回收），逐塊（2,048 列）轉 float32 計算內積。
- 在 Mac 上用真實的約 2 萬段索引和假向量量過：開啟與 5 次查詢後最大 RSS 約 119 MB，其中 OpenCC 詞典約 42 MB、向量檔頁約 30 MB；每次查詢約 20 ms（不含 Vertex）。
- `embed` 不把全部內文或向量讀進記憶體；快取在 SQLite。
