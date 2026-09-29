"""Gemini 問答全用假的 client 與搜尋器，不連外也不讀 .env。"""

import json
from types import SimpleNamespace

import pytest
from google.genai import types

from haixia import answer as core
from haixia import answer_gemini as gemini


class FakeSearcher:
    def __init__(self):
        self.store = SimpleNamespace(classic_commentary=lambda _id: [])

    def search(self, query, k, kind):
        return {"results": [], "mode": "bm25"}


class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, replies):
        self.models = FakeModels(replies)


def reply(parts=None, reason="STOP", usage=None, blocked=None):
    candidate = types.Candidate(content=types.Content(role="model", parts=parts or []), finish_reason=reason)
    return types.GenerateContentResponse(
        candidates=[] if blocked else [candidate],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=blocked) if blocked else None,
        usage_metadata=usage or types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5))


def make(replies, **kwargs):
    client = FakeClient(replies)
    answerer = gemini.Answerer(None, client=client, searcher=FakeSearcher(),
                               system_prompt="測試指示", log_dir=None, **kwargs)
    return answerer, client


def test_function_declarations_and_config():
    answerer, _ = make([])
    declarations = answerer.tool.function_declarations
    assert [item.name for item in declarations] == ["search", "classic_commentary", "read_context"]
    assert declarations[0].parameters_json_schema["properties"]["kind"]["enum"][-1] == "classic"
    config = answerer.request_config()
    assert config.automatic_function_calling.disable is True
    assert config.thinking_config is None
    assert config.max_output_tokens == 8192
    assert answerer.request_config(False).tool_config.function_calling_config.mode == "NONE"
    assert answerer.request_config(False).tools == [answerer.tool]
    medium, _ = make([], thinking_level="medium")
    assert medium.request_config().thinking_config.thinking_level == "MEDIUM"
    with pytest.raises(ValueError):
        make([], thinking_level="minimal")


def test_vertex_client_uses_global_and_sdk_retries(monkeypatch):
    captured = {}
    monkeypatch.setattr(gemini.genai, "Client", lambda **kwargs: captured.update(kwargs))
    gemini.make_client()
    assert captured["vertexai"] is True
    assert captured["project"] == "vmdemo1-507014" and captured["location"] == "global"
    options = captured["http_options"]
    assert options.api_version == "v1"
    assert options.retry_options.attempts == 3
    assert {429, 500, 503} <= set(options.retry_options.http_status_codes)


def test_multiple_calls_one_response_and_signature_preserved():
    signature = b"opaque-signature"
    call_part = types.Part(function_call=types.FunctionCall(name="search", args={"query": "傷寒", "kind": "classic"}),
                           thought_signature=signature)
    second = types.Part(function_call=types.FunctionCall(name="classic_commentary", args={"id": "a1"}))
    first = reply([call_part, second])
    final = reply([types.Part.from_text(text="答案")])
    answerer, client = make([first, final])
    conversation = core.Conversation()
    seen = []
    result = answerer.ask(conversation, "問題", on_tool=seen.append)
    assert result.text == "答案" and result.rounds == 1 and result.requests == 2
    assert len(seen) == 2 and [record["name"] for record in seen] == ["search", "classic_commentary"]
    assert conversation.messages[1] is first.candidates[0].content
    assert client.models.calls[1]["contents"][1].parts[0] is call_part
    assert client.models.calls[1]["contents"][1].parts[0].thought_signature == signature
    tool_content = conversation.messages[2]
    assert tool_content.role == "tool" and len(tool_content.parts) == 2
    assert all(part.function_response for part in tool_content.parts)


def test_gemini_display_citation_does_not_change_history_or_log(tmp_path):
    record = {"id": "a1b2c30000000001", "short_id": "a1b2c3", "kind": "transcript",
              "title": "測試課", "episode": "第1集", "start": 10, "end": 20, "text": "測試原文。"}

    class OneResult(FakeSearcher):
        def search(self, query, k, kind):
            return {"results": [record], "mode": "bm25"}

    original = "測試答案（出處：測試課 第1集 00:10–00:20）"
    answerer, _ = make([
        reply([types.Part(function_call=types.FunctionCall(name="search", args={"query": "測試"}))]),
        reply([types.Part.from_text(text=original)]),
    ])
    answerer.searcher = OneResult()
    answerer.log_dir = tmp_path
    conversation = core.Conversation()
    result = answerer.ask(conversation, "問題")
    assert result.text == original
    assert core.display_answer(result.text, conversation) == original[:-1] + "（編號 a1b2c3））"
    assert conversation.messages[-1].parts[0].text == original
    assert json.loads((tmp_path / core.LOG_NAME).read_text().strip())["answer"] == original


def test_tool_error_is_returned_to_model():
    answerer, client = make([reply([types.Part(function_call=types.FunctionCall(
        name="search", args={"query": ""}))]), reply([types.Part.from_text(text="已修正")])])
    result = answerer.ask(core.Conversation(), "問題")
    assert result.tool_calls[0]["error"]
    assert "error" in client.models.calls[1]["contents"][2].parts[0].function_response.response


def test_blank_after_tool_retries_once_without_tools_and_keeps_history():
    call_part = types.Part(function_call=types.FunctionCall(name="search", args={"query": "中風"}),
                           thought_signature=b"signature")
    first = reply([call_part])
    blank = reply([types.Part(text=" ")])
    final = reply([types.Part.from_text(text="【倪師原文依據】答案")])
    answerer, client = make([first, blank, final], model="gemini-3.1-pro-preview")
    conversation = core.Conversation()
    result = answerer.ask(conversation, "問題")
    assert result.text == "【倪師原文依據】答案" and result.requests == 3
    assert len(result.notes) == 1 and "補答一次" in result.notes[0]
    assert result.cost_usd == pytest.approx(3 * (100 * 2 + 25 * 12) / 1_000_000)
    assert conversation.messages[1] is first.candidates[0].content
    assert conversation.messages[1].parts[0].thought_signature == b"signature"
    assert conversation.messages[3] is blank.candidates[0].content
    assert conversation.messages[4].role == "user" and "工具結果" in conversation.messages[4].parts[0].text
    assert conversation.messages[5] is final.candidates[0].content
    assert client.models.calls[2]["config"].tool_config.function_calling_config.mode == "NONE"


def test_blank_after_tool_only_retries_once():
    call = types.Part(function_call=types.FunctionCall(name="search", args={"query": "中風"}))
    answerer, client = make([reply([call]), reply([]), reply([])])
    result = answerer.ask(core.Conversation(), "問題")
    assert result.requests == 3 and len(client.models.calls) == 3
    assert "這次沒有產生文字回答" in result.text
    assert len(result.notes) == 1 and "補答一次" in result.notes[0]


def test_missing_content_after_tool_is_retried():
    call = types.Part(function_call=types.FunctionCall(name="search", args={"query": "中風"}))
    missing = types.GenerateContentResponse(candidates=[types.Candidate(finish_reason="STOP")])
    answerer, client = make([reply([call]), missing, reply([types.Part.from_text(text="補答")])])
    result = answerer.ask(core.Conversation(), "問題")
    assert result.text == "補答" and result.requests == 3
    assert client.models.calls[2]["config"].tool_config.function_calling_config.mode == "NONE"


def test_blank_without_tool_result_does_not_retry():
    answerer, client = make([reply([])])
    result = answerer.ask(core.Conversation(), "問題")
    assert result.requests == 1 and not result.notes and len(client.models.calls) == 1


def test_round_limit_disables_tools():
    call = types.Part(function_call=types.FunctionCall(name="search", args={"query": "桂枝湯"}))
    answerer, client = make([reply([call]), reply([types.Part.from_text(text="結論")])], max_tool_rounds=1)
    result = answerer.ask(core.Conversation(), "問題")
    assert result.rounds == 1 and "上限" in result.notes[0]
    assert client.models.calls[1]["config"].tool_config.function_calling_config.mode == "NONE"


@pytest.mark.parametrize("reason", ["SAFETY", "PROHIBITED_CONTENT", "RECITATION"])
def test_safety_refusal_does_not_append_model_content(reason):
    answerer, _ = make([reply([types.Part.from_text(text="部分輸出")], reason=reason)])
    conversation = core.Conversation()
    result = answerer.ask(conversation, "問題")
    assert result.refused and reason in result.text and len(conversation.messages) == 1


def test_prompt_block_without_candidate():
    answerer, _ = make([reply(blocked="SAFETY")])
    assert answerer.ask(core.Conversation(), "問題").refused


def test_usage_cost_counts_cache_thoughts_and_tool_prompt():
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=1000, cached_content_token_count=200, candidates_token_count=100,
        thoughts_token_count=50, tool_use_prompt_token_count=30)
    answerer, _ = make([reply([types.Part.from_text(text="答案")], usage=usage)])
    result = answerer.ask(core.Conversation(), "問題")
    assert result.usage["input_tokens"] == 830
    assert result.usage["cache_read_input_tokens"] == 200
    assert result.usage["output_tokens"] == 150
    assert result.usage["thoughts_tokens"] == 50
    assert result.context_tokens == 1030
    assert result.cost_usd == pytest.approx((830 * .75 + 200 * .075 + 150 * 3.75) / 1_000_000)


def test_pro_price_counts_thoughts_as_output():
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=100, cached_content_token_count=20, candidates_token_count=10,
        thoughts_token_count=30)
    answerer, _ = make([reply([types.Part.from_text(text="答案")], usage=usage)],
                       model="gemini-3.1-pro-preview")
    result = answerer.ask(core.Conversation(), "問題")
    assert result.cost_usd == pytest.approx((80 * 2 + 20 * .2 + 40 * 12) / 1_000_000)


def test_log_schema_matches_claude_and_hides_exception_message(tmp_path):
    answerer, _ = make([reply([types.Part.from_text(text="答案")])])
    answerer.log_dir = tmp_path
    answerer.ask(core.Conversation(), "問題")
    entry = json.loads((tmp_path / core.LOG_NAME).read_text().strip())
    expected = {"time", "model", "served_model", "effort", "fallback_enabled", "fallback_ran",
                "question", "answer", "stop_reason", "refused", "rounds", "requests", "tool_calls",
                "usage", "cost_usd", "elapsed_sec", "notes", "error"}
    assert set(entry) == expected and entry["model"] == "gemini-3.8-flash"
    failed, _ = make([RuntimeError("private-token")])
    failed.log_dir = tmp_path
    with pytest.raises(RuntimeError):
        failed.ask(core.Conversation(), "問題")
    assert "private-token" not in (tmp_path / core.LOG_NAME).read_text()
