# HaixiaNi_bot

用倪海廈老師的課程影片與講義、醫案做檢索式問答（RAG）的個人研讀助手，在 Telegram 上使用。

> 僅供個人研讀，不是醫療建議。有急重症請立即就醫。

## 功能

- **Telegram 問答**：每個論點附出處（課名、集數、時間點，或書名、頁碼）。
- **看原文**：出處後面的 `/s_…` 點了就看原文和前後段（也可以打 `/source 編號`）。
- **回答格式**：先列【經典原文】，再分開【倪師原文依據】和【推論（非倪師原話）】，推論標把握程度；資訊不夠時先問診。
- **思考過程**：答案上方有可收合的區塊，標題是「模型｜思考 N 秒｜花費」，內容是模型的思考摘要。
- **換模型**：`/model` 切換 Opus 5.5、Sonnet 5.5、Gemini 3.8 Flash、Gemini 3.1 Pro（預設 Opus 5.5）。
- **花費**：`/cost` 分開列 Claude（Anthropic 帳單）和 Gemini（GCP 帳單），另有每日上限。

## 架構

```
一次性的資料處理
  Google Drive ─(rclone)─► GCS
    ─► GPU VM（L4）：ffmpeg 抽音訊 → Whisper large-v3 轉錄 → OpenCC 轉正體
    ─► LLM 校正逐字稿（Antigravity 的 Gemini 3.8 Flash 為主、Codex 補空檔，可上網查證）
    ─► 切段（約 500 字）
    ─► Vertex AI gemini-embedding-001 向量（768 維）＋ SQLite FTS5 關鍵字索引

問答（常駐）
  Telegram ◄─(long polling)─► movie-nas（e2-micro）上的 bot
    ─► 混合搜尋：向量＋BM25，用 RRF 合併
    ─► Claude（Anthropic API）或 Gemini（Vertex AI）的工具迴圈：搜尋、讀前後文、查倪師對經文的講解
```

## 資料規模

- 原始資料 2,058 個檔、約 71.5 GiB。
- 轉錄 440 個影片、220.9 小時；校正 3,503 段、全部通過驗收。
- 另有梁冬對話倪海廈 7 集（現成字幕）、人紀講義 5 本、天紀三本、醫案與診療日誌、漢唐中醫文章。
- 七部經典：傷寒論（宋本）、金匱要略、難經、素問、靈樞、針灸大成、神農本草經（來源：中醫笈成，CC0）。
- 索引共 23,146 段；經典與倪師講解的關聯 3,289 筆。

## 進度

- [x] 第一步：Drive → GCS、盤點資料
- [x] 第二步：抽音訊、Whisper 轉錄
- [x] 第三步：校正、切段、建索引
- [x] 第四步：問答核心（Claude、Gemini）
- [x] 第五步：Telegram bot（已部署，使用中）
- [ ] 第六步：用倪師醫案測試準確率

## 文件

1. [Drive → GCS、盤點](docs/01-drive-to-gcs.md)
2. [抽音訊與轉錄](docs/02-transcribe.md)
3. [校正逐字稿](docs/03-correct.md)
4. [切段、向量、混合搜尋](docs/04-index.md)
5. [Claude 問答](docs/05-answer.md)
6. [Telegram bot](docs/06-telegram.md)
7. [經典原文與倪師講解關聯](docs/07-classics.md)
8. [Gemini 問答](docs/08-gemini.md)

## 目錄

| 路徑 | 用途 |
|---|---|
| `scripts/setup_worker.sh`、`scripts/transfer.sh` | 在 VM 上裝工具、把 Drive 資料夾搬到 GCS |
| `scripts/inventory.py`、`scripts/sample_frames.py` | 盤點檔案與影音時數、影片截圖總覽 |
| `scripts/create_gpu_vm.sh`、`scripts/setup_gpu.sh`、`scripts/gpu_job.sh` | 建 GPU VM、裝轉錄環境、用 systemd 跑長時間工作 |
| `scripts/extract_audio.py`、`scripts/transcribe.py` | 抽 16kHz FLAC、轉錄 |
| `scripts/lrc_to_transcript.py` | 現成 `.lrc` 字幕轉逐字稿 |
| `scripts/run_bakeoff.sh`、`scripts/bakeoff_score.py` | 轉錄引擎小規模比較與評分 |
| `scripts/correct_transcripts.py`、`scripts/watch_correction.py`、`scripts/sync_transcripts.sh` | 批次校正、監視、同步逐字稿 |
| `tools/agy_hook/`、`tools/claude_hook/` | 校正時限制 CLI 只能上網搜尋的工具閘門 |
| `scripts/parse_classics.py`、`scripts/link_classics.py` | 解析經典、關聯經典與倪師講解 |
| `scripts/build_index.py`、`scripts/sync_index.sh` | 建索引（文字辨識、切段、向量、SQLite）、同步索引 |
| `scripts/search_index.py`、`scripts/ask.py` | 命令列搜尋、命令列問答 |
| `scripts/telegram_bot.py`、`deploy/haixia-bot.service` | 啟動 bot、systemd 服務設定 |
| `haixia/textnorm.py` | 簡轉正體（中醫詞典、台灣用字）與搜尋比對鍵 |
| `haixia/transcript.py`、`haixia/correction.py` | 逐字稿格式與幻聽過濾、校正的切段與驗收 |
| `haixia/corpus.py`、`haixia/chunking.py` | 文字資料的收錄範圍與轉文字、切段與去重 |
| `haixia/classics.py`、`haixia/classic_links.py` | 經典解析與條號、經典關聯 |
| `haixia/vertex.py`、`haixia/index_store.py`、`haixia/search.py` | Vertex 向量與文字辨識、索引儲存、混合搜尋 |
| `haixia/answer.py`、`haixia/answer_gemini.py` | Claude、Gemini 問答核心與費用紀錄 |
| `haixia/telegram_bot.py` | Telegram bot |
| `data/` | 中醫詞表、轉換規則、回答規則（`system_prompt.md`） |
| `tests/` | 測試（`python -m pytest`） |

## 費用

- 架設（第一步到第六步）預算約 1,100 台幣，目前累計約 660 台幣（不含 Claude API 測試費）。
- GCS 每月約 46 台幣；bot 跑在原本就有的 VM 上。
- 每題約（5 題實測平均）：

| 模型 | 每題 | 時間 |
|---|---|---|
| Opus 5.5 | 0.08–0.12 USD | 約 30 秒 |
| Sonnet 5.5 | 0.04 USD | 約 15 秒 |
| Gemini 3.8 Flash | 0.02 USD | 約 40 秒 |
| Gemini 3.1 Pro | 0.08 USD | 約 35 秒 |

## 隱私與機密

- 這個 repo 是公開的：API 金鑰、bot token、`.env`、病人資料都不放進來，機密只放在 VM 的 `.env`。
- 原始資料、逐字稿與索引放在私人的 Google Drive 和 GCS。
- 醫案出處只顯示日期、主訴和編號，不顯示病人姓名。
- bot 只回應白名單裡的使用者。
