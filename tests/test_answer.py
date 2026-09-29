"""第四步問答核心：工具定義、工具迴圈、read_context、多輪只附加、拒答、金鑰、費用、log。

全部用假的 Anthropic client（只模擬 client.beta.messages.stream）與假的 embedder，不連網。
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from haixia import answer as core
from haixia.index_store import IndexStore, build_db

REPO = Path(__file__).resolve().parents[1]
MODEL = "claude-opus-5-5"


def chunk(chunk_id, source, text, kind="transcript", title="人紀・傷寒論", episode="傷寒論1（1）",
          start=0.0, end=60.0, **extra):
    record = {"id": chunk_id, "kind": kind, "source": source, "title": title, "episode": episode,
              "section": None, "page_start": None, "page_end": None, "start": start, "end": end,
              "date": None, "text": text}
    record.update(extra)
    record["chars"] = len(record["text"])
    return record


CHUNKS = [
    chunk("a0", "影片/傷寒論1（1）.rmvb", "桂枝湯是五味藥，桂枝、芍藥、甘草、生薑、大棗。", start=0, end=120),
    chunk("a1", "影片/傷寒論1（1）.rmvb", "太陽中風，汗出惡風，脈浮緩，用桂枝湯。", start=110, end=230),
    chunk("a2", "影片/傷寒論1（1）.rmvb", "麻黃湯是無汗而喘，脈浮緊。", start=220, end=340),
    chunk("b0", "文字資料/人紀.pdf", "少陽之為病，口苦，咽乾，目眩也。", kind="document", title="人紀《傷寒論》",
          episode=None, start=None, end=None, section="辨少陽病", page_start=164, page_end=164),
    chunk("b1", "文字資料/人紀.pdf", "小柴胡湯：柴胡半斤，黃芩三兩。", kind="document", title="人紀《傷寒論》",
          episode=None, start=None, end=None, section="辨少陽病", page_start=165, page_end=165),
    chunk("c0", "影片/針灸1（1）.rmvb", "太衝穴在足背，肝經的俞穴。", title="人紀・針灸", episode="針灸1（1）",
          start=3600, end=3725),
]


@pytest.fixture
def index_dir(tmp_path):
    path = tmp_path / "chunks.jsonl"
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in CHUNKS), encoding="utf-8")
    build_db(path, tmp_path / "index.sqlite", log=lambda m: None)
    vectors = np.eye(len(CHUNKS), 8, dtype=np.float32)
    np.save(tmp_path / "embeddings.f16.npy", vectors.astype(np.float16))
    return tmp_path


class FakeEmbedder:
    def embed(self, texts, task_type, titles=None):
        return [([1.0] + [0.0] * 7, 3, False)]


# ---------- 假的 Anthropic client ----------

def text(value):
    return SimpleNamespace(type="text", text=value)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool(tool_id, name, data):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=data)


def usage(input_tokens=1000, read=0, write=0, output=200, iterations=None):
    return SimpleNamespace(input_tokens=input_tokens, cache_read_input_tokens=read,
                           cache_creation_input_tokens=write, output_tokens=output, iterations=iterations)


def reply(content, stop="end_turn", model=MODEL, use=None, **extra):
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=use or usage(),
                           stop_details=None, input_transformations=None, **extra)


class FakeStream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def stream(self, **params):
        self.calls.append(copy.deepcopy(params))
        return FakeStream(self.outcomes.pop(0))


class FakeClient:
    def __init__(self, outcomes):
        self.beta = SimpleNamespace(messages=FakeMessages(outcomes))

    @property
    def calls(self):
        return self.beta.messages.calls

    def add(self, *outcomes):
        self.beta.messages.outcomes.extend(outcomes)


def make(index_dir, outcomes, tmp_path=None, **options):
    client = FakeClient(outcomes)
    options.setdefault("log_dir", None)
    answerer = core.Answerer(index_dir, options.pop("model", MODEL), options.pop("effort", "medium"),
                             client=client, embedder=FakeEmbedder(), system_prompt="測試用 system prompt",
                             **options)
    return answerer, client


# ---------- 工具定義與請求設定 ----------

def test_tool_definitions_are_strict_and_well_formed():
    names = [t["name"] for t in core.TOOLS]
    assert names == ["search", "classic_commentary", "read_context"]
    for definition in core.TOOLS:
        schema = definition["input_schema"]
        assert definition["strict"] is True
        assert definition["eager_input_streaming"] is True
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])
        assert definition["description"]
        for prop in schema["properties"].values():
            assert prop["description"]
            # strict 不支援數值與字串長度限制，範圍一律用 enum
            assert not {"minimum", "maximum", "minLength", "maxLength"} & set(prop)
    search = core.TOOLS[0]["input_schema"]
    assert search["required"] == ["query"]
    assert search["properties"]["kind"]["enum"] == ["any", "transcript", "document", "classic"]
    assert search["properties"]["k"]["enum"] == list(range(1, 11))
    context = core.TOOLS[2]["input_schema"]
    assert context["required"] == ["id"]
    assert context["properties"]["before"]["enum"] == [0, 1, 2, 3]
    assert context["properties"]["after"]["enum"] == [0, 1, 2, 3]
    json.dumps(core.TOOLS)  # 可序列化


def test_request_params_for_opus_5_5(index_dir):
    answerer, _ = make(index_dir, [])
    params = answerer.request_params([{"role": "user", "content": "問"}])
    assert params["model"] == "claude-opus-5-5"
    assert params["max_tokens"] == 64000
    assert params["thinking"]["type"] == "adaptive" and "budget_tokens" not in params["thinking"]
    assert params["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert params["output_config"] == {"effort": "medium"}
    assert params["tool_choice"] == {"type": "auto"}
    assert params["tools"] is core.TOOLS
    assert params["system"] == [{"type": "text", "text": "測試用 system prompt",
                                 "cache_control": {"type": "ephemeral"}}]
    assert params["cache_control"] == {"type": "ephemeral"}
    assert params["fallbacks"] == "default"
    assert params["betas"] == [core.BINDING_BETA, core.FALLBACK_BETA]
    assert answerer.request_params([], allow_tools=False)["tool_choice"] == {"type": "none"}


def test_request_params_for_sonnet_and_options(index_dir):
    assert core.COMPARE_MODEL == "claude-sonnet-5-5"
    answerer, _ = make(index_dir, [], model=core.COMPARE_MODEL, effort="low", prefix_mismatch=None)
    params = answerer.request_params([])
    assert params["model"] == "claude-sonnet-5-5"
    assert params["output_config"] == {"effort": "low"}
    assert params["thinking"] == {"type": "adaptive"}
    assert "fallbacks" not in params and "betas" not in params
    forced, _ = make(index_dir, [], model="claude-sonnet-5", fallback=True)
    assert forced.request_params([])["fallbacks"] == "default"
    with pytest.raises(ValueError):
        make(index_dir, [], effort="huge")
    with pytest.raises(ValueError):
        make(index_dir, [], model="claude-unknown")


def test_system_prompt_file_covers_the_six_rules():
    prompt = core.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    for phrase in [
        "不是倪海廈本人", "出處", "工具這次對話裡實際回傳的段落",          # 身分、規則 1
        "【倪師原文依據】", "【推論（非倪師原話）】", "把握程度：高／中／低",   # 規則 2
        "【還需要問的】", "先問診", "寒熱", "口渴", "大便", "小便", "睡眠", "舌象", "脈象",  # 規則 3
        "六經辨證", "經方", "一般知識",                                   # 規則 4
        "立即就醫", "胸痛", "呼吸困難", "中風", "孕婦",                   # 規則 5
        "找不到", "不要編造",                                             # 規則 6
        "同音錯字", "劑量", "人紀講義",                                   # 資料特性
        "濕", "黃耆", "痺", "溪", "Markdown 表格",                        # 用字與格式
        "（出處：人紀・傷寒論 傷寒論3（7） 16:35–18:26）",
    ]:
        assert phrase in prompt, phrase


def test_system_prompt_keeps_emergency_retrieval_and_needle_details():
    prompt = core.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    emergency = prompt.split("# 急重症\n", 1)[1].split("\n# ", 1)[0]
    for phrase in ("答案第一句先提醒立即就醫或打急救電話", "仍要照常用工具查經典原文與倪師講義、逐字稿",
                   "穴位、取穴、手法與先後順序", "病人醒著就不要十宣放血", "本人懂針灸、會自己下針",
                   "針灸或放血的內容照資料完整提供"):
        assert phrase in emergency, phrase


# ---------- 工具迴圈 ----------

def test_tool_loop_runs_several_rounds(index_dir):
    answerer, client = make(index_dir, [
        reply([thinking(), tool("t1", "search", {"query": "桂枝湯"})], "tool_use"),
        reply([thinking(), tool("t2", "read_context", {"id": "a1", "before": 1, "after": 1})], "tool_use"),
        reply([thinking(), text("【倪師原文依據】桂枝湯五味藥。")]),
    ])
    conversation = core.Conversation()
    seen = []
    result = answerer.ask(conversation, "桂枝湯是什麼？", on_tool=seen.append)
    assert result.text == "【倪師原文依據】桂枝湯五味藥。"
    assert result.rounds == 2 and result.requests == 3 and not result.refused
    assert [call["name"] for call in result.tool_calls] == ["search", "read_context"]
    assert seen == result.tool_calls
    search_call = result.tool_calls[0]
    assert search_call["input"] == {"query": "桂枝湯", "kind": "any", "k": 8}
    assert search_call["mode"] == "hybrid" and search_call["hits"][0]["id"] == "a0"
    assert search_call["hits"][0]["citation"] == "人紀・傷寒論 傷寒論1（1） 00:00–02:00"
    # 第二次請求：最後是 search 的結果，內容含 id、出處與全文
    second = client.calls[1]["messages"]
    result_block = second[-1]["content"][0]
    assert result_block["tool_use_id"] == "t1" and "is_error" not in result_block
    assert "id=a0" in result_block["content"] and "出處：人紀・傷寒論 傷寒論1（1） 00:00–02:00" in result_block["content"]
    assert "桂枝、芍藥、甘草、生薑、大棗" in result_block["content"]
    # 第三次請求：read_context 的結果
    context = client.calls[2]["messages"][-1]["content"][0]["content"]
    assert "[前 1 段]" in context and "id=a0" in context and "[後 1 段]" in context and "id=a2" in context
    assert all(call["tool_choice"] == {"type": "auto"} for call in client.calls)


def test_parallel_tool_calls_answered_in_one_user_message(index_dir):
    answerer, client = make(index_dir, [
        reply([thinking(), tool("t1", "search", {"query": "少陽病", "kind": "document", "k": 2}),
               tool("t2", "search", {"query": "太衝穴", "kind": "transcript", "k": 3})], "tool_use"),
        reply([text("答")]),
    ])
    result = answerer.ask(core.Conversation(), "問")
    assert result.rounds == 1 and len(result.tool_calls) == 2
    last = client.calls[1]["messages"][-1]
    assert last["role"] == "user"
    assert [block["tool_use_id"] for block in last["content"]] == ["t1", "t2"]
    assert all(block["type"] == "tool_result" for block in last["content"])
    assert {hit["id"] for hit in result.tool_calls[0]["hits"]} <= {"b0", "b1"}
    transcript_hits = {hit["id"] for hit in result.tool_calls[1]["hits"]}
    assert "c0" in transcript_hits and transcript_hits <= {"a0", "a1", "a2", "c0"}


def test_tool_errors_are_returned_with_is_error(index_dir):
    answerer, client = make(index_dir, [
        reply([tool("t1", "search", {"query": "桂枝", "k": 99}),
               tool("t2", "read_context", {"id": "沒有這段"}),
               tool("t3", "search", {"query": "  "}),
               tool("t4", "nope", {}),
               tool("t5", "search", "不是物件"),
               tool("t6", "search", {"query": "桂枝"})], "tool_use"),
        reply([text("答")]),
    ])

    def broken(*args, **kwargs):
        raise RuntimeError("資料庫壞了")

    answerer.searcher.search = lambda *a, **k: broken() if a[0] == "桂枝" and k.get("k") == 8 else None
    result = answerer.ask(core.Conversation(), "問")
    blocks = client.calls[1]["messages"][-1]["content"]
    assert [block["tool_use_id"] for block in blocks] == ["t1", "t2", "t3", "t4", "t5", "t6"]
    assert all(block["is_error"] is True for block in blocks)
    assert "k 要是 1–10 的整數" in blocks[0]["content"]
    assert "找不到段落 id" in blocks[1]["content"]
    assert "query 不能是空的" in blocks[2]["content"]
    assert "沒有這個工具" in blocks[3]["content"]
    assert "不是物件" in blocks[4]["content"]
    assert "RuntimeError" in blocks[5]["content"] and "資料庫壞了" in blocks[5]["content"]
    assert all(call["error"] for call in result.tool_calls)
    assert result.text == "答"


def test_round_limit_asks_for_answer_without_tools(index_dir):
    looping = [reply([tool(f"t{n}", "search", {"query": f"查{n}"})], "tool_use") for n in range(3)]
    answerer, client = make(index_dir, looping + [reply([text("只用已有資料作答")])], max_tool_rounds=3)
    result = answerer.ask(core.Conversation(), "問")
    assert result.rounds == 3 and result.requests == 4
    assert result.text == "只用已有資料作答"
    assert [call["tool_choice"] for call in client.calls] == [{"type": "auto"}] * 3 + [{"type": "none"}]
    # 工具定義與 system 不變（不拿掉工具），上限說明接在最後一則工具結果後面
    assert all(call["tools"] == core.TOOLS and call["system"] == client.calls[0]["system"] for call in client.calls)
    last = client.calls[3]["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[-1] == {"type": "text", "text": core.LIMIT_NOTE}
    assert any("上限" in note for note in result.notes)


def test_default_round_limit_is_eight(index_dir):
    looping = [reply([tool(f"t{n}", "search", {"query": "桂枝"})], "tool_use") for n in range(8)]
    answerer, client = make(index_dir, looping + [reply([text("答")])])
    result = answerer.ask(core.Conversation(), "問")
    assert result.rounds == 8 and client.calls[-1]["tool_choice"] == {"type": "none"}


def test_truncated_tool_call_is_not_run_but_history_stays_valid(index_dir):
    answerer, client = make(index_dir, [
        reply([text("我先查"), tool("t1", "search", {"query": "桂"})], "max_tokens"),
        reply([text("第二題的答案")]),
    ])
    conversation = core.Conversation()
    first = answerer.ask(conversation, "第一題")
    assert first.tool_calls == [] and first.text == "我先查"
    assert any("max_tokens" in note for note in first.notes)
    pending = conversation.messages[-1]
    assert pending["role"] == "user" and pending["content"][0]["tool_use_id"] == "t1"
    assert pending["content"][0]["is_error"] is True
    answerer.ask(conversation, "第二題")
    assert client.calls[1]["messages"][-1] == {"role": "user", "content": "第二題"}


def test_unparseable_streamed_json_is_retried(index_dir):
    answerer, client = make(index_dir, [ValueError("bad json"), reply([text("答")])])
    assert answerer.ask(core.Conversation(), "問").text == "答"
    assert len(client.calls) == 2
    failing, _ = make(index_dir, [ValueError("bad")] * 3)
    with pytest.raises(ValueError):
        failing.ask(core.Conversation(), "問")


# ---------- read_context ----------

def test_neighbors_only_same_source_and_adjacent(index_dir):
    store = IndexStore(index_dir)
    target, before, after = store.neighbors("a1", 3, 3)
    assert target["id"] == "a1"
    assert [r["id"] for r in before] == ["a0"] and [r["id"] for r in after] == ["a2"]
    target, before, after = store.neighbors("b0", 2, 2)
    assert before == [] and [r["id"] for r in after] == ["b1"]      # 前面是別的來源
    target, before, after = store.neighbors("c0", 3, 3)
    assert before == [] and after == []                              # 前面是 b1，後面沒有了
    assert store.neighbors("a2", 0, 0)[1:] == ([], [])
    with pytest.raises(KeyError):
        store.neighbors("zz")
    store.close()


def test_read_context_tool_text(index_dir):
    store = IndexStore(index_dir)
    text_out, hits = core.run_read_context(store, {"id": "b1", "before": 3, "after": 3})
    assert [hit["id"] for hit in hits] == ["b0"]
    assert "[前 1 段] id=b0" in text_out and "出處：人紀《傷寒論》 辨少陽病 第 164 頁" in text_out
    assert "口苦，咽乾，目眩" in text_out and "a2" not in text_out and "c0" not in text_out
    lonely, hits = core.run_read_context(store, {"id": "c0", "before": 1, "after": 1})
    assert hits == [] and "沒有相鄰的段落" in lonely
    store.close()


def test_validate_input_defaults():
    assert core.validate_input("search", {"query": " 桂枝湯 "}) == {"query": "桂枝湯", "kind": "any", "k": 8}
    assert core.validate_input("read_context", {"id": "a1"}) == {"id": "a1", "before": 1, "after": 1}
    for bad in [{"query": "x", "k": True}, {"query": "x", "kind": "all"}, {"query": "x" * 201},
                {"query": "x", "extra": 1}]:
        with pytest.raises(core.ToolInputError):
            core.validate_input("search", bad)
    with pytest.raises(core.ToolInputError):
        core.validate_input("read_context", {"id": "a1", "before": 4})


def test_search_falls_back_to_bm25_note(index_dir):
    class Broken:
        def embed(self, *args, **kwargs):
            raise TimeoutError("vertex timeout")

    answerer, _ = make(index_dir, [])
    answerer.searcher.embedder = Broken()
    text_out, hits, mode = core.run_search(answerer.searcher, {"query": "太衝穴", "kind": "any", "k": 3})
    assert mode == "bm25" and "只用關鍵字搜尋" in text_out and hits[0]["id"] == "c0"
    empty, hits, _ = core.run_search(answerer.searcher, {"query": "。", "kind": "any", "k": 3})
    assert hits == [] and "找不到相關段落" in empty


# ---------- 多輪對話只附加 ----------

def test_multi_turn_history_is_append_only(index_dir):
    first_final = [thinking(), text("第一題答案")]
    tool_turn = [thinking(), tool("t1", "search", {"query": "麻黃湯"})]
    answerer, client = make(index_dir, [
        reply(tool_turn, "tool_use"),
        reply(first_final),
        reply([thinking(), tool("t2", "search", {"query": "桂枝湯"}),
               tool("t3", "read_context", {"id": "a0", "after": 2, "before": 0})], "tool_use"),
        reply([thinking(), text("第二題答案")]),
    ])
    conversation = core.Conversation()
    assert answerer.ask(conversation, "第一題").text == "第一題答案"
    snapshot = copy.deepcopy(conversation.messages)
    assert answerer.ask(conversation, "第二題").text == "第二題答案"
    assert conversation.messages[:len(snapshot)] == snapshot
    assert conversation.questions == 2
    # 每次請求的 messages 都是下一次請求的前綴；system 與工具每次都一樣
    for earlier, later in zip(client.calls, client.calls[1:]):
        assert later["messages"][:len(earlier["messages"])] == earlier["messages"]
        assert later["system"] == earlier["system"] and later["tools"] == earlier["tools"]
    # 放回歷史的是完整的 response.content（含空的 thinking 區塊），不是只有文字
    assistant = [m for m in conversation.messages if m["role"] == "assistant"]
    assert assistant[0]["content"] == tool_turn and assistant[1]["content"] == first_final
    assert assistant[1]["content"][0].type == "thinking"
    roles = [m["role"] for m in conversation.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "user", "assistant"]


def test_history_content_after_mid_stream_fallback():
    before = [thinking(), text("部分"), tool("x", "search", {"query": "q"})]
    marker = SimpleNamespace(type="fallback")
    after = [thinking(), text("後來的答案")]
    kept = core.history_content(before + [marker] + after)
    assert [b.type for b in kept] == ["text", "fallback", "thinking", "text"]
    first = [marker, thinking(), text("答")]
    assert core.history_content(first) == first
    plain = [thinking(), text("答")]
    assert core.history_content(plain) == plain


# ---------- 拒答 ----------

def test_refusal_returns_friendly_message_and_runs_no_tools(index_dir):
    refused = reply([text("部分輸出"), tool("t1", "search", {"query": "x"})], "refusal")
    refused.stop_details = SimpleNamespace(type="refusal", category="bio", explanation=None)
    answerer, client = make(index_dir, [refused, reply([text("下一題答案")])])
    conversation = core.Conversation()
    result = answerer.ask(conversation, "某個問題")
    assert result.refused and result.stop_reason == "refusal"
    assert "安全機制" in result.text and "類別：bio" in result.text and "/new" in result.text
    assert result.tool_calls == [] and result.rounds == 0
    assert conversation.messages == [{"role": "user", "content": "某個問題"}]
    # 拒答後還能繼續問（連續兩個 user 訊息由 API 合併）
    assert answerer.ask(conversation, "下一題").text == "下一題答案"
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "user"]


def test_refusal_without_category(index_dir):
    answerer, _ = make(index_dir, [reply([], "refusal")])
    result = answerer.ask(core.Conversation(), "問")
    assert result.refused and "類別" not in result.text


# ---------- 金鑰與 workspace ----------

SECRET = "sk-ant-api03-SECRET-VALUE-123"


def test_env_variable_wins_over_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"ANTHROPIC_API_KEY=from-file\nANTHROPIC_WORKSPACE_ID=wrkspc_file\n", encoding="utf-8")
    environ = {"ANTHROPIC_API_KEY": SECRET, "ANTHROPIC_WORKSPACE_ID": "wrkspc_env"}
    before = dict(environ)
    assert core.resolve_api_key(environ, env_file) == SECRET
    assert core.resolve_workspace_id(environ, env_file) == "wrkspc_env"
    assert environ == before  # 不修改環境變數


def test_env_file_used_when_variable_missing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 註解\n\nexport ANTHROPIC_API_KEY=\"" + SECRET + "\"\n"
                        "ANTHROPIC_WORKSPACE_ID='wrkspc_01abc'\nOTHER=1\n", encoding="utf-8")
    environ = {"ANTHROPIC_API_KEY": "  "}
    assert core.resolve_api_key(environ, env_file) == SECRET
    assert core.resolve_workspace_id(environ, env_file) == "wrkspc_01abc"
    assert environ == {"ANTHROPIC_API_KEY": "  "}
    assert core.resolve_workspace_id({}, tmp_path / "missing.env") is None


def test_missing_key_error_never_shows_secrets(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{SECRET}\nANTHROPIC_API_KEY\nbad line {SECRET}=x\n", encoding="utf-8")
    with pytest.raises(core.MissingApiKey) as caught:
        core.resolve_api_key({}, env_file)
    assert SECRET not in str(caught.value) and "ANTHROPIC_API_KEY" in str(caught.value)
    with pytest.raises(core.MissingApiKey):
        core.resolve_api_key({}, tmp_path / "missing.env")
    assert core.redact(f"錯誤 {SECRET} wrkspc_1", SECRET, "wrkspc_1", None) == "錯誤 *** ***"


def test_client_options_add_workspace_header():
    assert core.client_options(SECRET) == {"api_key": SECRET}
    assert core.client_options(SECRET, "wrkspc_01") == {
        "api_key": SECRET, "default_headers": {"anthropic-workspace-id": "wrkspc_01"}}
    anthropic = pytest.importorskip("anthropic")
    client = core.make_client(SECRET, "wrkspc_01")
    assert isinstance(client, anthropic.Anthropic)
    assert client.default_headers["anthropic-workspace-id"] == "wrkspc_01"
    plain = core.make_client(SECRET)
    assert "anthropic-workspace-id" not in {k.lower() for k in plain.default_headers}


def test_cli_without_key_exits_cleanly(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ask_cli", REPO / "scripts" / "ask.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(core, "ENV_PATH", tmp_path / "no.env")
    pytest.importorskip("anthropic")
    assert cli.main(["問題", "--index-dir", str(tmp_path)]) == 2
    assert "找不到 ANTHROPIC_API_KEY" in capsys.readouterr().err


# ---------- 費用與 log ----------

def test_usage_cost_uses_price_table():
    opus = core.PRICES["claude-opus-5-5"]
    assert opus == {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
    for sonnet in ("claude-sonnet-5-5", "claude-sonnet-5"):
        assert core.PRICES[sonnet] == {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5}
    one_each = dict.fromkeys(core.USAGE_FIELDS, 1_000_000)
    assert core.usage_cost(one_each, opus) == pytest.approx(4 + 20 + 0.2 + 5)
    use = {"input_tokens": 2000, "cache_read_input_tokens": 10000, "cache_creation_input_tokens": 3000,
           "output_tokens": 1500}
    assert core.usage_cost(use, opus) == pytest.approx((2000 * 4 + 10000 * 0.2 + 3000 * 5 + 1500 * 20) / 1e6)


def test_response_cost_with_fallback_iterations():
    iterations = [
        SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=1000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=10),
        SimpleNamespace(type="fallback_message", model="claude-opus-5", input_tokens=1000,
                        cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=100),
    ]
    response = reply([text("x")], model="claude-opus-5", use=usage(1000, 0, 0, 100, iterations))
    total, cost, notes = core.response_cost(response, MODEL, core.PRICES)
    assert total["input_tokens"] == 2000 and total["output_tokens"] == 110
    assert cost == pytest.approx((1000 * 4 + 10 * 20 + 1000 * 5 + 100 * 25) / 1e6)
    assert notes == []
    odd = reply([text("x")], model="claude-mystery", use=usage(1_000_000, 0, 0, 0))
    _, cost, notes = core.response_cost(odd, MODEL, core.PRICES)
    assert cost == pytest.approx(4.0) and "claude-mystery" in notes[0]


def test_load_prices_overrides(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"claude-sonnet-5": {"input": 3, "output": 15, "cache_read": 0.3,
                                                    "cache_write": 3.75}}), encoding="utf-8")
    prices = core.load_prices(path)
    assert prices["claude-sonnet-5"]["input"] == 3.0 and prices["claude-opus-5-5"]["input"] == 4.0
    assert core.PRICES["claude-sonnet-5"]["input"] == 2.0
    path.write_text(json.dumps({"x": {"input": 1}}), encoding="utf-8")
    with pytest.raises(ValueError):
        core.load_prices(path)


def test_answer_totals_and_jsonl_log(index_dir, tmp_path):
    clock = iter([10.0, 12.5])
    log_dir = tmp_path / "logs"
    answerer, _ = make(index_dir, [
        reply([tool("t1", "search", {"query": "桂枝湯"})], "tool_use", use=usage(3000, 0, 2500, 100)),
        reply([text("答案")], use=usage(500, 2500, 6000, 800)),
    ], log_dir=log_dir, clock=lambda: next(clock))
    result = answerer.ask(core.Conversation(), "桂枝湯？")
    assert result.usage == {"input_tokens": 3500, "cache_read_input_tokens": 2500,
                            "cache_creation_input_tokens": 8500, "output_tokens": 900}
    expected = (3500 * 4 + 2500 * 0.2 + 8500 * 5 + 900 * 20) / 1e6
    assert result.cost_usd == pytest.approx(expected) and result.elapsed_sec == 2.5
    lines = (log_dir / core.LOG_NAME).read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    assert entry["question"] == "桂枝湯？" and entry["answer"] == "答案"
    assert entry["model"] == MODEL and entry["effort"] == "medium" and entry["rounds"] == 1
    assert entry["usage"] == result.usage and entry["cost_usd"] == pytest.approx(expected)
    assert entry["tool_calls"][0]["hits"] and entry["error"] is None
    assert (log_dir.stat().st_mode & 0o777) == 0o700


def test_api_error_is_logged_without_message(index_dir, tmp_path):
    class Boom(Exception):
        status_code = 401

    log_dir = tmp_path / "logs"
    answerer, _ = make(index_dir, [Boom(f"invalid x-api-key {SECRET}")], log_dir=log_dir)
    with pytest.raises(Boom):
        answerer.ask(core.Conversation(), "問")
    content = (log_dir / core.LOG_NAME).read_text(encoding="utf-8")
    assert SECRET not in content
    assert json.loads(content)["error"] == {"type": "Boom", "status": 401}


# ---------- 真的 SDK＋假的 HTTP（不連網）：確認參數、header 與回應解析 ----------

def sse(*events):
    return "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events)


def sse_reply(message_id, blocks, stop):
    events = [("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant", "model": MODEL, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 3000}}})]
    for index, block in enumerate(blocks):
        if block["type"] == "thinking":
            events += [("content_block_start", {"type": "content_block_start", "index": index,
                                                "content_block": {"type": "thinking", "thinking": "", "signature": ""}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "signature_delta", "signature": "c2ln"}})]
        elif block["type"] == "tool_use":
            events += [("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                           "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                           "type": "input_json_delta", "partial_json": json.dumps(block["input"], ensure_ascii=False)}})]
        else:
            events += [("content_block_start", {"type": "content_block_start", "index": index,
                                                "content_block": {"type": "text", "text": ""}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": index,
                                                "delta": {"type": "text_delta", "text": block["text"]}})]
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                  "usage": {"output_tokens": 300}}),
               ("message_stop", {"type": "message_stop"})]
    return sse(*events)


def test_real_sdk_request_shape_and_round_trip(index_dir):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    replies = [
        sse_reply("msg_1", [{"type": "thinking"},
                            {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "桂枝湯", "k": 2}}],
                  "tool_use"),
        sse_reply("msg_2", [{"type": "thinking"}, {"type": "text", "text": "【倪師原文依據】桂枝湯五味藥。"}], "end_turn"),
    ]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=replies[len(requests) - 1].encode("utf-8"))

    client = anthropic.Anthropic(**core.client_options(SECRET, "wrkspc_test"), max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    answerer = core.Answerer(index_dir, client=client, embedder=FakeEmbedder(), system_prompt="規則",
                             log_dir=None)
    result = answerer.ask(core.Conversation(), "桂枝湯？")
    assert result.text == "【倪師原文依據】桂枝湯五味藥。" and result.rounds == 1
    assert result.usage["output_tokens"] == 600 and result.usage["cache_creation_input_tokens"] == 6000

    first, second = requests
    assert first.url.path == "/v1/messages"
    assert first.headers["anthropic-workspace-id"] == "wrkspc_test"
    assert set(first.headers["anthropic-beta"].split(",")) == {core.BINDING_BETA, core.FALLBACK_BETA}
    body = json.loads(first.content)
    assert body["model"] == MODEL and body["stream"] is True and body["max_tokens"] == 64000
    assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "medium"} and body["fallbacks"] == "default"
    assert body["tool_choice"] == {"type": "auto"} and body["cache_control"] == {"type": "ephemeral"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in body["tools"]] == ["search", "classic_commentary", "read_context"] and body["tools"][0]["strict"] is True
    # 第二個請求：完整放回 thinking（含 signature）與 tool_use，工具結果在同一則 user 訊息
    later = json.loads(second.content)
    assert later["system"] == body["system"] and later["tools"] == body["tools"]
    assert later["messages"][0] == {"role": "user", "content": "桂枝湯？"}
    assistant = later["messages"][1]["content"]
    assert assistant[0]["type"] == "thinking" and assistant[0]["signature"] == "c2ln"
    assert {key: assistant[1][key] for key in ("type", "id", "name", "input")} == {
        "type": "tool_use", "id": "toolu_1", "name": "search", "input": {"query": "桂枝湯", "k": 2}}
    results = later["messages"][2]["content"]
    assert results[0]["type"] == "tool_result" and results[0]["tool_use_id"] == "toolu_1"
    assert SECRET not in second.content.decode("utf-8")


# ---------- 第五步：對話長度與共用 Searcher ----------

def test_context_tokens_is_last_request_input_total(index_dir):
    answerer, _ = make(index_dir, [
        reply([tool("t1", "search", {"query": "桂枝湯"})], "tool_use", use=usage(3000, 0, 2500, 100)),
        reply([text("答案")], use=usage(500, 2500, 6000, 800)),
    ])
    result = answerer.ask(core.Conversation(), "桂枝湯？")
    # 只看最後一個請求：500＋2500＋6000（不是兩個請求加總）
    assert result.context_tokens == 9000
    assert result.usage["input_tokens"] == 3500


def test_shared_searcher_is_used_and_not_closed(index_dir):
    from haixia.search import Searcher

    searcher = Searcher(index_dir, FakeEmbedder())
    client = FakeClient([reply([text("一")]), reply([text("二")])])
    opus = core.Answerer(index_dir, MODEL, client=client, searcher=searcher, log_dir=None, system_prompt="s")
    sonnet = core.Answerer(index_dir, "claude-sonnet-5-5", client=client, searcher=searcher, log_dir=None,
                           system_prompt="s")
    assert opus.searcher is searcher and sonnet.searcher is searcher
    assert opus.ask(core.Conversation(), "問").text == "一"
    opus.close()
    sonnet.close()
    # 共用的 Searcher 由建立的人關；Answerer.close() 不會關掉它
    assert searcher.search("桂枝湯", k=1)["results"]
    searcher.close()
