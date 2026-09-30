"""第五步 Telegram bot：白名單、格式、切訊息、指令、每日上限、自動重置、排隊、進度頻率、原文查詢。

全部用假的 bot 物件（只有 send_message、edit_message_text、send_chat_action）與假的 Answerer，
不連 Telegram、不打 Anthropic 或 Vertex，也不讀真的 .env。醫案姓名都是虛構的測試資料。
"""

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from haixia import answer as core
from haixia import telegram_bot as tg
from haixia.index_store import build_db
from haixia.search import CitationStore

USER = 1001
CHAT = 5005
STRANGER = 999


# ---------- 假的 Telegram bot ----------

class BadRequest(Exception):
    """模擬 telegram.error.BadRequest（BotCore 依類別名稱判斷）。"""


class FakeBot:
    def __init__(self, reject_html=False):
        self.calls = []
        self.next_id = 100
        self.reject_html = reject_html

    def _check(self, kwargs):
        if self.reject_html and kwargs.get("parse_mode") == "HTML":
            raise BadRequest("Can't parse entities: unsupported start tag")

    async def send_message(self, **kwargs):
        self._check(kwargs)
        self.next_id += 1
        self.calls.append(("send", kwargs))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, **kwargs):
        self._check(kwargs)
        self.calls.append(("edit", kwargs))
        return True

    async def send_chat_action(self, **kwargs):
        self.calls.append(("action", kwargs))

    def texts(self, kind=None):
        return [kwargs["text"] for name, kwargs in self.calls if name != "action" and kind in (None, name)]

    def messages(self):
        return [(name, kwargs) for name, kwargs in self.calls if name != "action"]


class FakeCallback:
    def __init__(self, data, message_id=101, chat_id=CHAT):
        self.data = data
        self.message = SimpleNamespace(message_id=message_id, chat_id=chat_id)
        self.answers = []

    async def answer(self, **kwargs):
        self.answers.append(kwargs)


# ---------- 假的問答 ----------

def make_answer(text="答案", model=core.DEFAULT_MODEL, cost=0.1, context=20_000, tool_calls=None, **extra):
    return core.Answer(text=text, stop_reason="end_turn", model=model, cost_usd=cost, elapsed_sec=30.0,
                       context_tokens=context, tool_calls=tool_calls or [], **extra)


class FakeAnswerer:
    def __init__(self, model, script):
        self.model = model
        self.script = script
        self.calls = []

    def ask(self, conversation, question, on_tool=None):
        self.calls.append({"question": question, "conversation": conversation,
                           "thread": threading.get_ident(), "messages": len(conversation.messages)})
        step = self.script(self.model, question, on_tool) if callable(self.script) else None
        if isinstance(step, Exception):
            raise step
        conversation.messages.append({"role": "user", "content": question})
        conversation.messages.append({"role": "assistant", "content": "…"})
        return step if isinstance(step, core.Answer) else make_answer(f"{self.model}：{question}", self.model)

    def close(self):
        pass


class FakePool:
    def __init__(self, script=None, store=None):
        self.script = script
        self.answerers = {}
        self._store = store
        self.closed = False

    def get(self, model):
        if model not in self.answerers:
            self.answerers[model] = FakeAnswerer(model, self.script)
        return self.answerers[model]

    @property
    def store(self):
        return self._store

    def close(self):
        self.closed = True

    def all_calls(self):
        return [call for answerer in self.answerers.values() for call in answerer.calls]


@pytest.fixture
def make_core(tmp_path):
    made = []

    def factory(script=None, store=None, **options):
        pool = FakePool(script, store)
        options.setdefault("log_dir", tmp_path / "logs")
        options.setdefault("typing_interval", 0.05)
        bot_core = tg.BotCore({USER}, pool, **options)
        made.append(bot_core)
        return bot_core, pool

    yield factory
    for bot_core in made:
        bot_core.close()


def run(coroutine):
    return asyncio.run(coroutine)


def write_log(log_dir, entries):
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / core.LOG_NAME, "w", encoding="utf-8") as out:
        for when, cost in entries:
            out.write(json.dumps({"time": when, "cost_usd": cost}) + "\n")


# ---------- 設定與白名單 ----------

def test_parse_allowed_ids():
    assert tg.parse_allowed_ids("123, 456，789") == frozenset({123, 456, 789})
    assert tg.parse_allowed_ids(" 42 ") == frozenset({42})
    with pytest.raises(tg.ConfigError, match="只能是逗號分隔的數字"):
        tg.parse_allowed_ids("123,abc")
    with pytest.raises(tg.ConfigError, match="空的"):
        tg.parse_allowed_ids(" , ")


def test_load_config_env_first_and_errors_hide_token(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=file-token-123\nTELEGRAM_ALLOWED_USER_IDS=1,2\n"
                        "ANTHROPIC_API_KEY=sk-file\nDAILY_BUDGET_USD=5\n", encoding="utf-8")
    config = tg.load_config({"TELEGRAM_BOT_TOKEN": "env-token-456"}, env_file)
    assert config.token == "env-token-456"
    assert config.allowed == frozenset({1, 2}) and config.daily_budget == 5.0 and config.api_key == "sk-file"
    # 沒有設定上限時預設 3 美元
    env_file.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USER_IDS=1\nANTHROPIC_API_KEY=k\n", encoding="utf-8")
    assert tg.load_config({}, env_file).daily_budget == 3.0
    # 缺白名單：拒絕啟動，訊息不含 token
    env_file.write_text("TELEGRAM_BOT_TOKEN=secret-token-789\nANTHROPIC_API_KEY=k\n", encoding="utf-8")
    with pytest.raises(tg.ConfigError) as problem:
        tg.load_config({}, env_file)
    assert "TELEGRAM_ALLOWED_USER_IDS" in str(problem.value) and "secret-token-789" not in str(problem.value)
    with pytest.raises(tg.ConfigError, match="TELEGRAM_BOT_TOKEN"):
        tg.load_config({}, tmp_path / "missing.env")
    env_file.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_ALLOWED_USER_IDS=1\nANTHROPIC_API_KEY=k\n"
                        "DAILY_BUDGET_USD=abc\n", encoding="utf-8")
    with pytest.raises(tg.ConfigError, match="DAILY_BUDGET_USD"):
        tg.load_config({}, env_file)
    # 錯放到其他欄位的 token 也不能出現在設定錯誤裡。
    env_file.write_text("TELEGRAM_BOT_TOKEN=secret-token-789\nTELEGRAM_ALLOWED_USER_IDS=secret-token-789\n"
                        "ANTHROPIC_API_KEY=k\n", encoding="utf-8")
    with pytest.raises(tg.ConfigError) as problem:
        tg.load_config({}, env_file)
    assert "secret-token-789" not in str(problem.value)


def test_redact_filter_hides_token_in_message_and_traceback():
    token = "123456:SECRET-TOKEN"
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Keep()
    handler.addFilter(tg.RedactFilter(token, None))
    logger = logging.getLogger("test.redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("POST https://api.telegram.org/bot%s/getUpdates", token)
        try:
            raise RuntimeError(f"連線失敗 https://api.telegram.org/bot{token}/sendMessage")
        except RuntimeError:
            logger.exception("出錯")
    finally:
        logger.removeHandler(handler)
    assert records and all(token not in record for record in records)
    assert "bot***/getUpdates" in records[0] and "bot***/sendMessage" in records[1]


def test_stranger_gets_no_reply_and_no_api_call(make_core, caplog):
    bot_core, pool = make_core()
    bot = FakeBot()
    with caplog.at_level(logging.WARNING, logger="haixia.bot"):
        run(bot_core.handle_message(bot, CHAT, STRANGER, "少陽病的提綱？"))
        run(bot_core.handle_message(bot, CHAT, STRANGER, "/help"))
        run(bot_core.handle_message(bot, CHAT, None, "問題"))
    assert bot.calls == [] and pool.answerers == {}
    assert "拒絕 user_id=999" in caplog.text


# ---------- 格式與切訊息 ----------

def test_to_html_escapes_then_bolds():
    text = "【倪師原文依據】\n- **桂枝湯**：桂枝 3 錢 < 5 錢 & 芍藥\n### 小結\n**【推論（非倪師原話）】**\n一般 *斜體* 不處理"
    assert tg.to_html(text) == ("<b>【倪師原文依據】</b>\n- <b>桂枝湯</b>：桂枝 3 錢 &lt; 5 錢 &amp; 芍藥\n"
                                "<b>小結</b>\n<b>【推論（非倪師原話）】</b>\n一般 *斜體* 不處理")
    # 答案裡像標籤的文字一律跳脫
    assert tg.to_html("<b>不是標籤</b>") == "&lt;b&gt;不是標籤&lt;/b&gt;"
    # 沒有成對的 ** 原樣保留；【】後面還有內文的不是標題行
    assert tg.to_html("2**3") == "2**3"
    assert tg.to_html("【注意】這一行有內文") == "【注意】這一行有內文"
    assert tg.html_to_plain(tg.to_html("<a> & **b**")) == "<a> & b"


def _balanced(chunk):
    return chunk.count("<b>") == chunk.count("</b>") and "<b></b>" not in chunk


def test_split_html_on_paragraph_boundaries():
    paragraphs = [("【段落%d】\n" % i) + ("字" * 900) for i in range(10)]
    text = tg.to_html("\n\n".join(paragraphs))
    chunks = tg.split_html(text)
    assert len(chunks) > 1
    assert all(tg.utf16_len(chunk) <= tg.TELEGRAM_LIMIT for chunk in chunks)
    # 每則都從段落開頭切，合起來就是原文
    assert all(chunk.startswith("<b>【段落") for chunk in chunks)
    assert "\n\n".join(chunks) == text
    assert all(_balanced(chunk) for chunk in chunks)
    assert tg.split_html("短訊息") == ["短訊息"] and tg.split_html("  \n ") == []


def test_split_html_long_line_never_cuts_inside_tags():
    line = ("<b>" + "粗" * 50 + "</b>" + "字&amp;" * 30) * 60     # 一行、沒有換行，遠超過上限
    chunks = tg.split_html(line, limit=500)
    assert len(chunks) > 5
    for chunk in chunks:
        assert tg.utf16_len(chunk) <= 500
        assert _balanced(chunk)
        assert "&am" not in chunk.replace("&amp;", "")        # 不切在 &amp; 中間
        assert not chunk.endswith("<") and "<b" not in chunk.replace("<b>", "")
    assert tg.html_to_plain("".join(chunks)) == tg.html_to_plain(line)


def test_split_html_counts_utf16_units():
    wide = "𠀀" * 3000         # 擴充區漢字，每個算 2 個單位
    chunks = tg.split_html(wide)
    assert len(chunks) == 2 and all(tg.utf16_len(chunk) <= tg.TELEGRAM_LIMIT for chunk in chunks)


def test_html_parse_error_falls_back_to_plain_text(make_core, caplog):
    bot_core, _ = make_core(lambda model, question, on_tool: make_answer("**粗體** 與 <符號>"))
    bot = FakeBot(reject_html=True)
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    final = bot.messages()[-1][1]
    assert "parse_mode" not in final
    assert "粗體 與 <符號>" in final["text"]
    assert "<blockquote" not in final["text"]
    assert "HTML 被 Telegram 拒絕：BadRequest" in caplog.text
    # 直接送 HTML 失敗時也一樣
    run(bot_core.send_html(bot, CHAT, "<b>標題</b> &amp; 內文"))
    assert bot.messages()[-1] == ("send", {"chat_id": CHAT, "text": "標題 & 內文"})


def test_two_blocks_title_escaping_and_no_nested_entities():
    answer = make_answer("**重點**：<方>&\n【倪師原文依據】\n查 /s_a1b2c3", cost=0.036,
                         thinking="先看 <原文> & 講義")
    answer.elapsed_sec = 38.5
    messages = tg.answer_messages("這題 <問>&" + "甲" * 31, answer, answer.text)
    assert len(messages) == 1
    markup = messages[0]
    assert markup.startswith("<blockquote expandable><b>Opus 5.5｜思考 39 秒｜0.036 USD ≈ 1.2 TWD</b>")
    assert "<b>這題 &lt;問&gt;&amp;" in markup and "…</b>" in markup
    assert "先看 &lt;原文&gt; &amp; 講義" in markup
    assert "<b>重點</b>：&lt;方&gt;&amp;" in markup
    assert "/s_a1b2c3" in markup and "— Opus" not in markup
    assert markup.count("<blockquote") == 2 and markup.count("</blockquote>") == 2
    assert "<pre>" not in markup and "<code>" not in markup
    assert "</blockquote>\n<blockquote" in markup


def test_only_thinking_block_is_expandable():
    markup = tg.answer_messages("問題", make_answer("答案"), "答案")[0]
    assert markup.count("<blockquote expandable>") == 1
    assert markup.count("<blockquote>") == 1


def test_thinking_markdown_is_converted_inside_quote():
    answer = make_answer("答案", thinking="# 判斷 <方>&\n**核對** 原文")
    markup = tg.answer_messages("問題", answer, answer.text)[0]
    thinking = markup.split("</blockquote>", 1)[0]
    assert "<b>判斷 &lt;方&gt;&amp;</b>" in thinking
    assert "<b>核對</b> 原文" in thinking
    assert "**" not in thinking
    assert "<pre>" not in thinking and "<code>" not in thinking


@pytest.mark.parametrize("prefix", ["總結：", "總結:", "【總結】"])
def test_summary_prefix_becomes_title_and_leaves_answer_body(prefix):
    original = f"\n{prefix}先查桂枝湯（編號 a1b2c3）\n\n【經典原文】\n原文"
    store = SimpleNamespace(find_prefix=lambda code, limit: [object()])
    displayed = tg.clickable_citations(original, store)
    answer = make_answer(original, thinking="摘要")
    markup = tg.answer_messages("問題", answer, displayed)[0]
    assert "<blockquote><b>先查桂枝湯 /s_a1b2c3</b>\n" in markup
    assert "<b>【經典原文】</b>\n原文</blockquote>" in markup
    assert prefix not in markup
    assert answer.text == original


def test_summary_title_is_truncated_and_html_is_escaped():
    summary = "<方>&" + "甲" * 61
    answer = make_answer(f"總結：{summary}\n答案")
    markup = tg.answer_messages("問題", answer, answer.text)[0]
    assert f"<blockquote><b>&lt;方&gt;&amp;{'甲' * 56}…</b>\n答案</blockquote>" in markup
    assert "<方>" not in markup


def test_summary_title_does_not_split_source_command():
    answer = make_answer("總結：" + "甲" * 54 + " /s_a1b2c3 後文\n答案")
    markup = tg.answer_messages("問題", answer, answer.text)[0]
    assert f"<blockquote><b>{'甲' * 54}…</b>\n答案</blockquote>" in markup
    assert "/s_a1" not in markup


def test_missing_summary_uses_question_and_logs_model(caplog):
    answer = make_answer("【倪師原文依據】\n答案")
    with caplog.at_level(logging.INFO, logger="haixia.bot"):
        markup = tg.answer_messages("問題" * 20, answer, answer.text)[0]
    assert f"<blockquote><b>{'問題' * 15}…</b>" in markup
    assert "答案缺總結行：claude-opus-5-5" in caplog.text
    assert "倪師原文依據" not in caplog.text


def test_summary_display_does_not_change_conversation_history(make_core):
    original = "總結：先查桂枝湯\n【經典原文】\n原文"
    answer = make_answer(original)
    bot_core, _ = make_core(lambda model, question, on_tool: answer)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    assert bot_core.state(CHAT).conversation.messages == [
        {"role": "user", "content": "問題"}, {"role": "assistant", "content": "…"}]
    assert answer.text == original
    assert "總結：" not in bot.texts()[-1]
    assert "<blockquote><b>先查桂枝湯</b>" in bot.texts()[-1]


def test_long_thinking_is_trimmed_and_answer_messages_continue():
    answer = make_answer("總結：先看原文\n" + "字" * 8000,
                         thinking="# 判斷 <方>&\n**核對** 原文\n" + "想" * 8000)
    messages = tg.answer_messages("問題", answer, answer.text)
    assert len(messages) >= 4
    assert "思考過程較長，後面省略約" in messages[0]
    assert "<b>判斷 &lt;方&gt;&amp;</b>" in messages[0]
    assert "<b>核對</b> 原文" in messages[0]
    assert "**" not in messages[0]
    assert "<pre>" not in messages[0] and "<code>" not in messages[0]
    assert messages[0].count("<blockquote") == 1
    assert "<blockquote><b>先看原文</b>" in messages[1]
    assert all("<b>（續）</b>" in message for message in messages[2:])
    assert all("<blockquote expandable>" not in message for message in messages[1:])
    assert all(tg._plain_length(message) <= tg.ANSWER_LIMIT for message in messages)
    assert all(message.count("<blockquote") == message.count("</blockquote>") == 1
               for message in messages)


def test_split_answer_keeps_bold_tags_balanced():
    answer = make_answer("【倪師原文依據】\n**" + "重" * 5000 + "**", thinking="摘要")
    messages = tg.answer_messages("問題", answer, answer.text)
    assert any("<b>重" in message for message in messages[1:])
    assert all(tg.html_to_plain(message.split("\n", 1)[1]).strip() for message in messages[1:])
    assert all(message.count("<b>") == message.count("</b>") for message in messages)
    assert all(tg._plain_length(message) <= tg.ANSWER_LIMIT for message in messages)


def test_split_answer_breaks_at_newlines_and_keeps_source_commands(monkeypatch):
    monkeypatch.setattr(tg, "ANSWER_LIMIT", 160)
    lines = [f"第{i:02d}行：{'字' * 15} /s_a1b2c3" for i in range(12)]
    answer = make_answer("\n".join(lines), thinking="摘要")
    messages = tg.answer_messages("問題", answer, answer.text)
    assert len(messages) > 2
    bodies = [message.split("\n", 1)[1].removesuffix("</blockquote>") for message in messages[1:]]
    assert [line for body in bodies for line in body.splitlines()] == lines
    assert all(body.startswith("第") and body.endswith("/s_a1b2c3") for body in bodies)
    assert all(message.count("<blockquote>") == message.count("</blockquote>") == 1
               for message in messages[1:])
    assert all(message.count("/s_a1b2c3") == len(body.splitlines())
               for message, body in zip(messages[1:], bodies))
    assert all(tg._plain_length(message) <= tg.ANSWER_LIMIT for message in messages)


def test_answer_length_counts_utf16_after_html_parsing():
    answer = make_answer("**" + "😀" * 2500 + "**", thinking="🧠" * 3000)
    messages = tg.answer_messages("問題", answer, answer.text)
    assert "思考過程較長" in messages[0]
    assert all(tg._plain_length(message) <= tg.ANSWER_LIMIT for message in messages)
    assert all(message.count("<b>") == message.count("</b>") for message in messages)


def test_one_message_when_plain_content_fits_even_with_escaped_characters():
    answer = make_answer("<&>" * 500, thinking="摘要")
    messages = tg.answer_messages("問題", answer, answer.text)
    assert len(messages) == 1
    assert tg._plain_length(messages[0]) < tg.ANSWER_LIMIT


def test_extra_notice_does_not_split_two_blocks(monkeypatch):
    monkeypatch.setattr(tg, "ANSWER_LIMIT", 110)
    answer = make_answer("答案", thinking="摘要")
    messages = tg.answer_messages("問題", answer, answer.text, ["提示" * 50])
    assert messages[0].count("<blockquote") == 2
    assert messages[1] == "提示" * 50


def test_entity_counts_are_logged_after_send_and_refusal_stays_plain(make_core, caplog):
    class EntityBot(FakeBot):
        async def edit_message_text(self, **kwargs):
            await super().edit_message_text(**kwargs)
            return SimpleNamespace(entities=[SimpleNamespace(type="expandable_blockquote"),
                                             SimpleNamespace(type="expandable_blockquote"),
                                             SimpleNamespace(type="bold"),
                                             SimpleNamespace(type="bot_command")])

    bot_core, _ = make_core(lambda model, question, on_tool: make_answer("出處 /s_a1b2c3"))
    bot = EntityBot()
    with caplog.at_level(logging.INFO, logger="haixia.bot"):
        run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    assert "expandable_blockquote 2" in caplog.text
    assert "bold 1" in caplog.text and "bot_command 1" in caplog.text
    refused = make_answer(core.REFUSAL_TEXT.format(category=""), refused=True)
    other, _ = make_core(lambda model, question, on_tool: refused)
    plain_bot = FakeBot()
    run(other.handle_message(plain_bot, CHAT, USER, "問題"))
    assert "<blockquote" not in plain_bot.texts()[-1]


# ---------- 指令 ----------

def test_parse_command_variants():
    assert tg.parse_command("/new") == tg.Command("new", "", "new")
    assert tg.parse_command("/model sonnet") == tg.Command("model", "sonnet", "model")
    assert tg.parse_command("/model@HaixiaBot opus") == tg.Command("model", "opus", "model")
    assert tg.parse_command("/COST").name == "cost"
    assert tg.parse_command("/source 3e13af") == tg.Command("source", "3e13af", "source")
    assert tg.parse_command("/s_3e13af") == tg.Command("source", "3e13af", "s_3e13af")
    assert tg.parse_command("/s_3e13af@HaixiaBot") == tg.Command("source", "3e13af", "s_3e13af")
    assert tg.parse_command("/s_badname").name == "unknown"
    assert tg.parse_command("原文 3e13af") == tg.Command("source", "3e13af", "原文")
    assert tg.parse_command("原文：3E13AF9").arg == "3E13AF9"
    assert tg.parse_command("原文3e13af").name == "source"
    assert tg.parse_command("/foo").name == "unknown"
    # 一般問題不是指令（包括以「原文」開頭的問題）
    assert tg.parse_command("原文 桂枝湯是怎麼寫的？") is None
    assert tg.parse_command("少陽病的提綱？") is None
    assert tg.normalize_code("編號 3E13AF") == "3e13af"
    assert tg.normalize_code("xyz") is None and tg.normalize_code("abc") is None
    assert tg.resolve_model("Sonnet") == "claude-sonnet-5-5" and tg.resolve_model("opus") == "claude-opus-5-5"
    assert tg.resolve_model("gpt") is None


def test_model_switch_starts_new_conversation(make_core):
    bot_core, pool = make_core()
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "第一題"))
    state = bot_core.state(CHAT)
    old = state.conversation
    assert old.messages
    run(bot_core.handle_message(bot, CHAT, USER, "/model"))
    assert "目前模型：Opus 5.5" in bot.texts()[-1]
    run(bot_core.handle_message(bot, CHAT, USER, "/model opus"))
    assert "已經是 Opus 5.5" in bot.texts()[-1] and state.conversation is old
    run(bot_core.handle_message(bot, CHAT, USER, "/model gpt"))
    assert "不認得" in bot.texts()[-1] and state.model == core.DEFAULT_MODEL
    run(bot_core.handle_message(bot, CHAT, USER, "/model sonnet"))
    assert "已切換到 Sonnet 5.5" in bot.texts()[-1] and "開新對話" in bot.texts()[-1]
    assert state.model == "claude-sonnet-5-5" and state.conversation is not old and not state.conversation.messages
    run(bot_core.handle_message(bot, CHAT, USER, "第二題"))
    assert pool.answerers["claude-sonnet-5-5"].calls[0]["messages"] == 0
    assert "Sonnet 5.5" in bot.texts()[-1]


def test_gemini_models_are_listed_and_switch_resets_conversation(make_core):
    bot_core, _ = make_core()
    bot = FakeBot()
    state = bot_core.state(CHAT)
    old = state.conversation
    old.messages.append({"role": "user", "content": "舊問題"})
    run(bot_core.handle_message(bot, CHAT, USER, "/model"))
    listing = bot.messages()[-1][1]
    assert listing["text"] == "目前模型：Opus 5.5\n請選擇模型服務："
    assert [button.text for button in listing["reply_markup"].inline_keyboard[0]] == ["Claude", "Gemini"]
    run(bot_core.handle_message(bot, CHAT, USER, "/model gemini-flash"))
    assert state.model == "gemini-3.8-flash" and state.conversation is not old
    previous = state.conversation
    run(bot_core.handle_message(bot, CHAT, USER, "/model gemini-pro"))
    assert state.model == "gemini-3.1-pro-preview" and state.conversation is not previous
    run(bot_core.handle_message(bot, CHAT, USER, "/model opus"))
    assert state.model == "claude-opus-5-5" and not state.conversation.messages


def test_model_callback_menu_navigation_and_switch(make_core):
    bot_core, pool = make_core()
    bot = FakeBot()
    state = bot_core.state(CHAT)
    old = state.conversation
    old.messages.append({"role": "user", "content": "舊題"})
    run(bot_core.handle_message(bot, CHAT, USER, "/model"))
    assert len(bot.messages()) == 1
    for data, expected in [("m:c", ["Opus 5.5（目前）", "Sonnet 5.5", "‹ 返回"]),
                           ("m:b", ["Claude", "Gemini"]),
                           ("m:g", ["Gemini 3.8 Flash", "Gemini 3.1 Pro", "‹ 返回"])]:
        callback = FakeCallback(data)
        run(bot_core.handle_model_callback(bot, callback, USER))
        assert callback.answers == [{}]
        method, edit = bot.messages()[-1]
        assert method == "edit" and edit["message_id"] == 101
        if data == "m:b":
            assert [button.text for button in edit["reply_markup"].inline_keyboard[0]] == expected
        else:
            assert [row[0].text for row in edit["reply_markup"].inline_keyboard] == expected
        assert "目前模型：Opus 5.5" in edit["text"]
    callback = FakeCallback("m:p")
    run(bot_core.handle_model_callback(bot, callback, USER))
    assert callback.answers == [{}]
    assert state.model == "gemini-3.1-pro-preview" and state.conversation is not old
    assert not state.conversation.messages
    assert bot.messages()[-1][1]["reply_markup"] is None
    assert "已切換到 Gemini 3.1 Pro" in bot.messages()[-1][1]["text"]
    run(bot_core.handle_message(bot, CHAT, USER, "下一題"))
    assert pool.answerers["gemini-3.1-pro-preview"].calls[0]["messages"] == 0
    current = state.conversation
    callback = FakeCallback("m:p")
    run(bot_core.handle_model_callback(bot, callback, USER))
    assert "已經是 Gemini 3.1 Pro" in bot.messages()[-1][1]["text"]
    assert state.conversation is current


def test_model_callback_rejects_strangers_and_expired_data(make_core, caplog):
    bot_core, _ = make_core()
    bot = FakeBot()
    state = bot_core.state(CHAT)
    with caplog.at_level(logging.WARNING):
        stranger = FakeCallback("m:s")
        run(bot_core.handle_model_callback(bot, stranger, STRANGER))
    assert stranger.answers == [{}] and not bot.messages()
    assert state.model == core.DEFAULT_MODEL and "拒絕 model callback user_id=999" in caplog.text
    for data in ("unknown", None, ["m:s"]):
        callback = FakeCallback(data)
        run(bot_core.handle_model_callback(bot, callback, USER))
        assert callback.answers == [{"text": "選單已過期，請重新輸入 /model"}]
    assert not bot.messages() and state.model == core.DEFAULT_MODEL


def test_new_command_and_help(make_core):
    bot_core, _ = make_core(daily_budget=2.5)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "第一題"))
    run(bot_core.handle_message(bot, CHAT, USER, "/new"))
    assert bot.texts()[-1] == "已開新對話。" and not bot_core.state(CHAT).conversation.messages
    run(bot_core.handle_message(bot, CHAT, USER, "/start"))
    help_text = bot.texts()[-1]
    for words in ("/new", "/model", "/cost", "/source", "【倪師原文依據】", "重新啟動後對話會清空", "US$2.50"):
        assert words in help_text
    run(bot_core.handle_message(bot, CHAT, USER, "/foo"))
    assert "不認得 /foo" in bot.texts()[-1]


# ---------- 花費 ----------

def test_summarize_costs_uses_taipei_day():
    now = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)          # 台灣 9/29 09:00
    lines = [
        json.dumps({"time": "2026-09-28T15:59:00+00:00", "cost_usd": 1.0}),   # 台灣 9/28 23:59
        json.dumps({"time": "2026-09-28T16:00:00+00:00", "cost_usd": 0.5}),   # 台灣 9/29 00:00
        json.dumps({"time": "2026-09-29T00:30:00+00:00", "cost_usd": 0.25}),
        json.dumps({"time": "2026-08-31T16:30:00+00:00", "cost_usd": 9.0}),   # 台灣 9/1，算本月
        json.dumps({"time": "2026-08-31T15:30:00+00:00", "cost_usd": 7.0}),   # 台灣 8/31，不算
        "不是 JSON", json.dumps({"cost_usd": 3}),
    ]
    summary = tg.summarize_costs(lines, now)
    assert summary.day == "2026-09-29"
    assert (summary.today_count, summary.today_usd) == (2, pytest.approx(0.75))
    assert (summary.month_count, summary.month_usd) == (4, pytest.approx(10.75))


def test_budget_blocks_api_call(make_core, tmp_path):
    now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    write_log(tmp_path / "logs", [("2026-09-29T01:00:00+00:00", 2.0), ("2026-09-29T02:00:00+00:00", 1.2)])
    bot_core, pool = make_core(daily_budget=3.0, now=lambda: now)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    assert pool.all_calls() == []
    final = bot.texts()[-1]
    assert "達到每日上限" in final and "DAILY_BUDGET_USD" in final and "US$3.20" in final
    # 上限提高後就能問
    bot_core.daily_budget = 5.0
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    assert len(pool.all_calls()) == 1


def test_cost_command(make_core, tmp_path):
    now = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    write_log(tmp_path / "logs", [("2026-09-29T01:00:00+00:00", 0.5), ("2026-09-02T01:00:00+00:00", 1.0)])
    bot_core, _ = make_core(now=lambda: now)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "/cost"))
    text = bot.texts()[-1]
    assert "今天（台灣時間 2026-09-29）：1 題，US$0.50（約 NT$16）" in text
    assert "本月：2 題，US$1.50（約 NT$48）" in text and "每日上限：US$3.00" in text


# ---------- 對話自動重置 ----------

def test_reset_reason_idle_and_context():
    state = tg.ChatState()
    assert tg.reset_reason(state, 10**9) is None                         # 空對話不用重置
    state.conversation.messages.append({"role": "user", "content": "q"})
    state.last_active = 1000.0
    assert tg.reset_reason(state, 1000.0 + 6 * 3600) is None
    assert "6 小時" in tg.reset_reason(state, 1000.0 + 6 * 3600 + 1)
    state.context_tokens = 150_000
    assert tg.reset_reason(state, 1001.0) is None
    state.context_tokens = 150_001
    assert "15 萬" in tg.reset_reason(state, 1001.0)


def test_question_auto_resets_after_idle_and_long_context(make_core):
    clock = [1000.0]
    contexts = iter([160_000, 10_000, 10_000])
    bot_core, pool = make_core(lambda model, question, on_tool: make_answer(context=next(contexts)),
                               clock=lambda: clock[0])
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "一"))
    state = bot_core.state(CHAT)
    assert state.context_tokens == 160_000 and state.last_active == 1000.0
    # 對話太長：下一題前自動開新對話並告訴使用者
    run(bot_core.handle_message(bot, CHAT, USER, "二"))
    calls = pool.all_calls()
    assert calls[1]["messages"] == 0 and calls[1]["conversation"] is not calls[0]["conversation"]
    assert "已自動開新對話" in bot.texts()[-1] and "15 萬" in bot.texts()[-1]
    # 閒置超過 6 小時
    clock[0] += 6 * 3600 + 5
    run(bot_core.handle_message(bot, CHAT, USER, "三"))
    assert pool.all_calls()[2]["messages"] == 0 and "6 小時" in bot.texts()[-1]


# ---------- 單一 worker 排隊 ----------

def test_single_worker_thread_queues_questions(make_core):
    release = threading.Event()
    active, peak = [0], [0]
    lock = threading.Lock()

    def script(model, question, on_tool):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        if question == "一":
            assert release.wait(5)
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return make_answer(f"答：{question}")

    bot_core, pool = make_core(script)
    bot = FakeBot()

    async def scenario():
        first = asyncio.create_task(bot_core.handle_message(bot, CHAT, USER, "一"))
        await asyncio.sleep(0.1)
        second = asyncio.create_task(bot_core.handle_message(bot, CHAT, USER, "二"))
        await asyncio.sleep(0.1)
        assert bot_core.pending == 2
        release.set()
        await asyncio.gather(first, second)

    run(scenario())
    sends = bot.texts("send")
    assert sends[0] == "查詢中…" and sends[1] == "排隊中：前面還有 1 題，輪到時會開始查詢。"
    calls = pool.all_calls()
    assert [call["question"] for call in calls] == ["一", "二"]
    assert peak[0] == 1
    assert len({call["thread"] for call in calls}) == 1 and calls[0]["thread"] != threading.get_ident()
    edits = bot.texts("edit")
    assert "查詢中…" in edits                           # 輪到第二題時把「排隊中」改掉
    assert "答：二</blockquote>" in edits[-1] and any("答：一</blockquote>" in text for text in edits)
    assert bot_core.pending == 0


def test_pool_is_closed_on_worker_thread(make_core):
    bot_core, pool = make_core()
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "一"))
    worker_id = pool.all_calls()[0]["thread"]
    closed_on = []
    pool.close = lambda: closed_on.append(threading.get_ident())
    bot_core.close()
    assert closed_on == [worker_id]


# ---------- 進度訊息 ----------

def test_progress_throttle():
    now = [0.0]
    throttle = tg.ProgressThrottle(2.0, lambda: now[0])
    results = []
    for moment in (0.5, 1.9, 2.0, 3.0, 4.1, 4.2):
        now[0] = moment
        results.append(throttle.ready())
    assert results == [False, False, True, False, True, False]


def test_progress_edits_are_rate_limited(make_core):
    now = [0.0]
    moments = [0.5, 1.0, 2.6, 3.0, 5.0]

    def script(model, question, on_tool):
        for number, moment in enumerate(moments, 1):
            now[0] = moment
            on_tool({"round": number, "name": "search", "input": {"query": f"查詢{number}"}})
        on_tool({"round": 6, "name": "read_context", "input": {"id": "a"}})
        return make_answer("最後答案")

    bot_core, _ = make_core(script, monotonic=lambda: now[0])
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    edits = bot.texts("edit")
    assert edits[:-1] == ["查詢中…\n已搜尋：查詢1、查詢2、查詢3",
                          "查詢中…\n已搜尋：查詢2、查詢3、查詢4、查詢5（共 5 次）"]
    assert "最後答案</blockquote>" in edits[-1]
    assert any(name == "action" and kwargs["action"] == "typing" for name, kwargs in bot.calls)
    assert tg.progress_text(["搜尋：甲", "讀前後文", "讀前後文"]) == "查詢中…\n已搜尋：甲\n已讀前後文 2 次"


def test_classic_tool_progress_labels():
    records = [
        {"name": "search", "input": {"query": "少陽", "kind": "classic"}},
        {"name": "classic_commentary", "input": {"id": "c1"}},
        {"name": "search", "input": {"query": "桂枝", "kind": "document"}},
        {"name": "read_context", "input": {"id": "d1"}},
    ]
    labels = [tg.tool_label(record) for record in records]
    assert labels == ["搜尋經典：少陽", "查倪師對經文的講解", "搜尋：桂枝", "讀前後文"]
    assert tg.progress_text(labels) == "查詢中…\n已搜尋：桂枝\n已搜尋經典：少陽\n已查倪師對經文的講解 1 次\n已讀前後文 1 次"


# ---------- 答案、錯誤 ----------

def test_long_answer_edits_status_then_sends_rest(make_core):
    long_text = "\n\n".join("【段落】\n" + "字" * 1500 for _ in range(5))
    bot_core, _ = make_core(lambda model, question, on_tool: make_answer(long_text))
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    messages = bot.messages()
    assert messages[0] == ("send", {"chat_id": CHAT, "text": "查詢中…"})
    assert messages[1][0] == "edit" and messages[1][1]["message_id"] == 101 and messages[1][1]["parse_mode"] == "HTML"
    rest = messages[2:]
    assert rest and all(name == "send" and kwargs["parse_mode"] == "HTML" for name, kwargs in rest)
    assert all(tg.utf16_len(kwargs["text"]) <= tg.TELEGRAM_LIMIT for _, kwargs in messages)
    assert "— Opus 5.5" not in rest[-1][1]["text"]
    assert "（續）" in rest[-1][1]["text"]


def test_bm25_fallback_and_server_fallback_are_noted(make_core):
    answer = make_answer("答案", model="claude-opus-5", fallback=True,
                         tool_calls=[{"name": "search", "mode": "bm25"}, {"name": "search", "mode": "hybrid"}])
    bot_core, _ = make_core(lambda model, question, on_tool: answer)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    final = bot.texts()[-1]
    assert "答案</blockquote>" in final
    assert "只用關鍵字搜尋" in final and "改由 claude-opus-5 回答" in final


def test_refusal_text_mentions_telegram_new():
    answer = make_answer(core.REFUSAL_TEXT.format(category=""), refused=True)
    text = tg.answer_text(answer)
    assert "輸入 /new" in text and "互動模式" not in text


class FakeStatusError(Exception):
    def __init__(self, status):
        super().__init__("raw body sk-ant-SECRET-KEY request-id xyz")
        self.status_code = status


@pytest.mark.parametrize("status, words", [(429, "流量限制"), (401, "金鑰"), (400, "/new"), (529, "忙碌"),
                                           (418, "HTTP 418"), (None, "RuntimeError")])
def test_api_errors_give_friendly_message_without_raw_text(make_core, status, words):
    problem = FakeStatusError(status) if status else RuntimeError("raw sk-ant-SECRET-KEY")
    bot_core, _ = make_core(lambda model, question, on_tool: problem)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "問題"))
    final = bot.texts()[-1]
    assert words in final and "SECRET" not in final and "raw" not in final


def test_timeout_and_connection_errors():
    import anthropic
    import httpx

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert "逾時" in tg.error_message(anthropic.APITimeoutError(request=request))
    assert "連不上" in tg.error_message(anthropic.APIConnectionError(request=request))
    assert "ANTHROPIC_API_KEY" in tg.error_message(core.MissingApiKey("x"))


def test_gemini_api_error_uses_vertex_wording_without_raw_text():
    problem = RuntimeError("raw token-secret")
    problem.code = 403
    message = tg.error_message(problem, "gemini-3.8-flash")
    assert "Gemini" in message and "服務帳號" in message
    assert "token-secret" not in message and "ANTHROPIC_API_KEY" not in message
    import httpx
    assert "逾時" in tg.error_message(httpx.ReadTimeout("private"), "gemini-3.8-flash")


def test_non_text_message_gets_hint(make_core):
    bot_core, pool = make_core()
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, None))
    assert "只接受文字" in bot.texts()[-1] and pool.answerers == {}


# ---------- /source ----------

def _doc(chunk_id, source, title, text):
    return {"id": chunk_id, "kind": "document", "source": source, "title": title, "episode": None,
            "section": None, "page_start": None, "page_end": None, "start": None, "end": None,
            "date": None, "text": text, "chars": len(text)}


@pytest.fixture
def case_store(tmp_path):
    source = "文字資料/03.倪海厦诊疗日志 医案/其他/Doe,Jane20080807-皮癢.doc"
    chunks = [
        _doc("3e13af0000000001", source, "Doe,Jane20080807-皮癢", "初診：皮膚癢 <兩週>。"),
        _doc("3e13af1000000002", source, "Doe,Jane20080807-皮癢", "處方：桂枝湯 & 加減。"),
        _doc("3e13af1000000003", source, "Doe,Jane20080807-皮癢", "複診：好轉。"),
        _doc("5a5a5a0000000004", "文字資料/人紀.pdf", "人紀《傷寒論》", "講義內文。"),
    ]
    path = tmp_path / "idx"
    path.mkdir()
    (path / "chunks.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks),
                                       encoding="utf-8")
    build_db(path / "chunks.jsonl", path / "index.sqlite", log=lambda m: None)
    store = CitationStore(path)
    yield store
    store.close()


def test_source_command_found(make_core, case_store):
    bot_core, _ = make_core(store=case_store)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "/source 3e13af0"))
    text = bot.texts()[-1]
    assert "原始標題：Doe,Jane20080807-皮癢" in text
    assert "檔案：文字資料/03.倪海厦诊疗日志 医案/其他/Doe,Jane20080807-皮癢.doc" in text
    assert "出處：醫案 2008-08-07 皮癢（編號 3e13af0）" in text
    assert "【這一段】</b>\n初診：皮膚癢 &lt;兩週&gt;。" in text
    assert "【後一段】</b>\n處方：桂枝湯 &amp; 加減。" in text and "【前一段】" not in text
    # 純文字「原文 編號」也可以；中間那段有前後各一段
    run(bot_core.handle_message(bot, CHAT, USER, "原文 3E13AF1000000002"))
    text = bot.texts()[-1]
    assert "【前一段】" in text and "【後一段】" in text and "複診：好轉。" in text
    run(bot_core.handle_message(bot, CHAT, USER, "/s_3e13af0@HaixiaBot"))
    assert "【這一段】" in bot.texts()[-1]


def test_source_command_ambiguous_and_missing(make_core, case_store):
    bot_core, _ = make_core(store=case_store)
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "/source 3e13af"))
    text = bot.texts()[-1]
    assert "對應到不只一段" in text and "3e13af0" in text and "3e13af1" in text
    assert "（編號 3e13af0）" in text and "（編號 3e13af1" in text
    run(bot_core.handle_message(bot, CHAT, USER, "/source 3e13af1"))
    assert "對應到不只一段" in bot.texts()[-1]
    run(bot_core.handle_message(bot, CHAT, USER, "/source ffffff"))
    assert "找不到編號 ffffff" in bot.texts()[-1]
    run(bot_core.handle_message(bot, CHAT, USER, "/source"))
    assert "用法：/source 編號" in bot.texts()[-1]
    run(bot_core.handle_message(bot, CHAT, USER, "/source 5a5a5a"))
    assert "原始標題：人紀《傷寒論》" in bot.texts()[-1]


def test_clickable_citations_verify_unique_index_code(case_store):
    text = "甲（編號 3e13af0）；乙（編號 3e13af）；丙（編號 ffffff）；丁（編號 5a5a5a）；戊（編號 3e13af0000000001）"
    shown = tg.clickable_citations(text, case_store)
    assert shown == "甲 /s_3e13af0；乙（編號 3e13af）；丙（編號 ffffff）；丁 /s_5a5a5a；戊 /s_3e13af0000000001"
    assert tg.to_html(shown) == shown


def test_answer_shows_clickable_source_in_worker(make_core, case_store):
    bot_core, _ = make_core(store=case_store, script=lambda model, question, on_tool:
                            make_answer("答案（出處：醫案 2008-08-07 皮癢（編號 3e13af0））", model))
    bot = FakeBot()
    run(bot_core.handle_message(bot, CHAT, USER, "測試問題"))
    assert "醫案 2008-08-07 皮癢 /s_3e13af0" in bot.texts()[-1]


def test_source_formats_repaired_case_path_and_classic_source():
    garbled = "Doe,Jane20080807-皮癢".encode("big5").decode("gb18030") + ".doc"
    source = ("文字資料/倪海厦08年医案959篇-按人名分类(神州医料库）/"
              "畍羬洛11_2008(神州医料库）/" + garbled)
    case = _doc("a1b2c30000000001", source, "亂碼標題", "虛構原文。")
    shown = tg.format_source(case, [], [])
    assert "檔案：文字資料/倪海厦08年医案959篇-按人名分类(神州医料库）/師臨醫11_2008(神州医料库）/Doe,Jane20080807-皮癢.doc" in shown
    assert "Drive 上的原檔名：" + source in shown
    classic = {**case, "kind": "classic", "source": "jicheng:傷寒論（宋本）#1",
               "title": "傷寒論（宋本）", "episode": "第1條"}
    shown = tg.format_source(classic, [], [])
    assert "檔案：中醫笈成《傷寒論（宋本）》" in shown
    assert "Drive 上的原檔名" not in shown


# ---------- python-telegram-bot 介面 ----------

def test_build_application_handlers_match_allowed_updates(make_core):
    pytest.importorskip("telegram")
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    bot_core, _ = make_core()
    application = tg.build_application(bot_core, "123456:TEST-TOKEN-NOT-REAL")
    handlers = [handler for group in application.handlers.values() for handler in group]
    assert len(handlers) == 2 and isinstance(handlers[0], MessageHandler)
    assert isinstance(handlers[1], CallbackQueryHandler)
    update_types = []
    for handler in handlers:
        if type(handler) is MessageHandler and handler.filters is filters.UpdateType.MESSAGE:
            update_types.append("message")
        elif type(handler) is CallbackQueryHandler:
            update_types.append("callback_query")
        else:
            pytest.fail(f"未確認更新種類的 handler：{handler!r}")
    assert set(update_types) == set(tg.ALLOWED_UPDATES)
    assert len(tg.ALLOWED_UPDATES) == len(set(tg.ALLOWED_UPDATES))
    assert application.update_processor.max_concurrent_updates > 1
