"""逐字稿核心功能；一般測試不下載語音模型。"""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from haixia.transcript import (course_for, create, filter_hallucinations,
                               is_low_confidence, load, save, validate)
from scripts.extract_audio import extract_audio
from scripts.lrc_to_transcript import convert
from scripts.transcribe import (PARAFORMER, FunASREngine, WhisperEngine,
                                _funasr_model, make_engine, transcribe_batch,
                                transcribe_one)


def engine_info(name="whisper"):
    return {"name": name, "model": "small", "version": "1", "params": {},
            "device": "cpu", "compute_type": "int8", "elapsed_sec": 1.0}


def example_segment(start=0.1, end=1.0, text="桂枝湯"):
    return {"start": start, "end": end, "text_raw": text, "text": text,
            "speaker": None, "confidence": None, "low_confidence": False}


def test_format_save_load_and_validation(tmp_path):
    document = create("影片/甲.mp4", 2, engine_info(), [example_segment()])
    target = tmp_path / "nested" / "甲.json"
    save(document, target)
    assert load(target) == document
    assert "桂枝湯" in target.read_text(encoding="utf-8")
    assert target.read_text(encoding="utf-8").startswith('{\n "schema"')
    assert not list(target.parent.glob("*.tmp"))
    broken = copy.deepcopy(document)
    broken["segments"][0]["end"] = 5
    with pytest.raises(ValueError, match="時間超出"):
        validate(broken)
    broken = copy.deepcopy(document)
    broken["source"] = "../outside.mp4"
    with pytest.raises(ValueError, match="相對路徑"):
        validate(broken)


@pytest.mark.parametrize("confidence, expected", [
    ({"avg_logprob": -1.01, "no_speech_prob": 0, "compression_ratio": 1}, True),
    ({"avg_logprob": 0, "no_speech_prob": 0.61, "compression_ratio": 1}, True),
    ({"avg_logprob": 0, "no_speech_prob": 0, "compression_ratio": 2.41}, True),
    ({"avg_logprob": -1.0, "no_speech_prob": 0.6, "compression_ratio": 2.4}, False),
    (None, False),
])
def test_low_confidence(confidence, expected):
    assert is_low_confidence(confidence) is expected


def test_hallucinations_and_repetition():
    segments = [example_segment(index, index + 0.5, phrase) for index, phrase in enumerate([
        "桂枝湯", "请不吝点赞、訂閱", "字幕由 Amara.org 提供", "謝謝觀看",
        "麻黃湯主之", "麻黄汤主之", "麻黃湯主之", "正常內容",
        "小柴胡湯。小柴胡汤。小柴胡湯。",
    ])]
    kept, dropped = filter_hallucinations(segments)
    assert [segment["text_raw"] for segment in kept] == ["桂枝湯", "正常內容"]
    assert len(dropped) == 7
    assert sum("重複" in item["reason"] for item in dropped) == 4
    assert any("幻聽詞" in item["reason"] for item in dropped)


def test_short_repetition_is_not_hallucination():
    phrases = ["好，好，好，我們今天講桂枝湯", "對，對，對", "對", "對", "對"]
    segments = [example_segment(index, index + 0.5, phrase)
                for index, phrase in enumerate(phrases)]
    kept, dropped = filter_hallucinations(segments)
    assert kept == segments
    assert dropped == []


def test_long_repetition_is_hallucination():
    phrase = "我們來看一下。"
    segments = [example_segment(0, 0.5, phrase * 3)]
    segments.extend(example_segment(index, index + 0.5, "這個條文大家看清楚。")
                    for index in range(1, 4))
    kept, dropped = filter_hallucinations(segments)
    assert kept == []
    assert len(dropped) == 4
    assert all("重複" in item["reason"] for item in dropped)


def test_course_longest_prefix():
    prompts = {"default": {"name": "預設"}, "courses": [
        {"prefix": "影片/", "name": "影片"},
        {"prefix": "影片/甲/", "name": "課程甲"},
    ]}
    assert course_for("影片/甲/第一集.mp4", prompts)["name"] == "課程甲"
    assert course_for("其他/第一集.mp4", prompts)["name"] == "預設"


def test_lrc_gb18030_speaker_and_end(tmp_path):
    content = ("[00:00.00]091226梁冬对话倪海厦第一讲\n"
               "[00:04.00]字幕制作：易健生活网\n"
               "[00:08.00]http://hi.baidu.com/eajian\n"
               "[00:18.52]梁冬：是的，重新发现中国文化太美，\n"
               "[00:21.00]我们继续聊。\n"
               "[00:24.50]倪海厦：桂枝汤主之。\n")
    path = tmp_path / "字幕.lrc"
    path.write_bytes(content.encode("gb18030"))
    target = tmp_path / "結果.json"
    document = convert(path, "影片/對話.mp4", 30, target, min_speaker_count=1)
    assert len(document["segments"]) == 3
    assert [segment["speaker"] for segment in document["segments"]] == ["梁冬", "梁冬", "倪海廈"]
    assert [segment["end"] for segment in document["segments"]] == [21, 24.5, 30]
    assert document["segments"][2]["text"] == "桂枝湯主之。"
    assert load(target)["engine"]["name"] == "lrc"


def test_lrc_speaker_frequency_and_false_labels(tmp_path):
    lines = ["091226梁冬对话倪海厦第一讲", "字幕制作：易健生活网",
             "http://hi.baidu.com/eajian", "我说：這句在講者出現前也要保留。"]
    lines += [f"梁冬：第{i}句。" for i in range(10)]
    lines += ["义务工作群：製作名單"]
    lines += [f"倪海厦：第{i}句。" for i in range(9)]
    lines += ["倪海夏：這是名字打錯的一句。"]
    lines += ["我说：這是內文。"] * 4
    lines += ["他说：這也是內文。"] * 3
    lines += ["西医说：照原樣。", "比如说：照原樣。", "中医认为：照原樣。"]
    content = "\n".join(f"[00:{index:02d}.00]{line}" for index, line in enumerate(lines))
    path = tmp_path / "假字幕.lrc"
    path.write_bytes(content.encode("gb18030"))
    document = convert(path, "影片/對話.mp4", len(lines) + 2, tmp_path / "結果.json")
    segments = document["segments"]
    assert len(segments) == len(lines) - 4
    assert segments[0]["text_raw"] == lines[3]
    assert segments[0]["speaker"] is None
    by_start = {segment["start"]: segment for segment in segments}
    ni_start = lines.index("倪海厦：第0句。")
    assert [by_start[index]["speaker"] for index in range(ni_start, ni_start + 10)] == ["倪海廈"] * 10
    fake = [segment for segment in segments if segment["text_raw"].startswith(("我说：", "他说：", "西医说：", "比如说：", "中医认为："))]
    assert len(fake) == 11
    assert fake[1]["speaker"] == "倪海廈"
    assert all(not segment["text_raw"].startswith(("字幕制作", "义务工作群")) for segment in segments)
    assert segments[-1]["end"] == len(lines) + 2


def test_whisper_alias(monkeypatch):
    calls = []
    class StubWhisper:
        def __init__(self, model, device, compute_type, beam_size, batch_size, batched):
            calls.append((model, device, compute_type, beam_size, batch_size, batched))
            self.name = "whisper-batched" if batched else "whisper"

    monkeypatch.setattr("scripts.transcribe.WhisperEngine", StubWhisper)
    assert make_engine("whisper", model="small", device="cpu").name == "whisper"
    assert make_engine("whisper-prompt", model="small", device="cpu").name == "whisper"
    assert calls[0] == calls[1]


@pytest.mark.parametrize("batched", [False, True])
def test_whisper_explicit_language(batched):
    class Pipeline:
        def transcribe(self, audio, **options):
            assert options["language"] == "zh"
            assert options["vad_filter"] is True
            return [], None

    engine = object.__new__(WhisperEngine)
    engine.pipeline = Pipeline()
    engine.language = "zh"
    engine.beam_size = 5
    engine.batch_size = 16
    engine.batched = batched
    assert engine.transcribe("任意.flac", "提示詞", [])[0] == []


def test_sensevoice_explicit_language():
    class Speech:
        ndim = 1
        def __len__(self):
            return 16000
        def __getitem__(self, index):
            return self

    class Soundfile:
        def read(self, audio, dtype):
            return Speech(), 16000

    class Vad:
        def generate(self, **kwargs):
            return [{"value": [[0, 500]]}]

    class Asr:
        def generate(self, **kwargs):
            assert kwargs["language"] == "zh"
            assert kwargs["use_itn"] is True
            return [{"text": "<|zh|>桂枝湯"}]

    engine = object.__new__(FunASREngine)
    engine.soundfile = Soundfile()
    engine.vad = Vad()
    engine.asr = Asr()
    engine.name = "sensevoice"
    engine.language = "zh"
    engine.t2s = None
    results, _elapsed = engine.transcribe("任意.flac", "", [])
    assert results[0]["text_raw"] == "桂枝湯"


def test_paraformer_never_uses_nonofficial_fallback(monkeypatch):
    monkeypatch.setenv("HAIXIA_MODEL_HUB", "hf")
    calls = []
    def failing_model(**options):
        calls.append(options)
        raise OSError("官方來源無法連線")

    with pytest.raises(RuntimeError, match="請稍後重試或檢查網路連線"):
        _funasr_model(failing_model, PARAFORMER, None, "cpu")
    assert len(calls) == 1
    assert calls[0]["model"] == PARAFORMER
    assert calls[0]["hub"] == "ms"


def generate_source(path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", str(path)], check=True)


def test_extract_audio_clip_and_resume(tmp_path):
    source, target = tmp_path / "source.wav", tmp_path / "nested" / "clip.flac"
    generate_source(source)
    assert extract_audio(source, target, start=0.75, duration=0.75)
    info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "stream=sample_rate,channels:format=duration",
        "-of", "json", str(target)], text=True))
    assert info["streams"][0]["sample_rate"] == "16000"
    assert info["streams"][0]["channels"] == 1
    assert float(info["format"]["duration"]) == pytest.approx(0.75, abs=0.02)
    # 切點在靜音末尾之前；片段後半應有可辨識的音量。
    pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(target),
                                   "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    from array import array
    samples = array("h")
    samples.frombytes(pcm)
    assert max(abs(value) for value in samples[:2000]) == 0
    assert max(abs(value) for value in samples[-2000:]) > 100
    before = target.stat().st_mtime_ns
    assert extract_audio(source, target, start=0, duration=0.2) is False
    assert target.stat().st_mtime_ns == before


class FakeEngine:
    name = "whisper"
    model = "fake"
    version = "1"
    device = "cpu"
    compute_type = "int8"
    beam_size = 5

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, prompt, hotwords):
        self.calls.append((Path(audio).name, prompt, hotwords))
        return [{"start": 0, "end": 0.5, "text_raw": "桂枝汤",
                 "confidence": {"avg_logprob": -0.1, "no_speech_prob": 0.1,
                                "compression_ratio": 1.0}}], 0.01


def test_batch_filter_and_resume(tmp_path):
    audio_root, out_dir = tmp_path / "audio", tmp_path / "out"
    for name in ("影片/甲/第一.mp4", "影片/乙/第二.mp4"):
        path = audio_root / (name + ".flac")
        path.parent.mkdir(parents=True, exist_ok=True)
        generate_source(path.with_suffix(".wav"))
        assert extract_audio(path.with_suffix(".wav"), path)
    prompts = {"default": {"prompt": "預設", "hotwords": []}, "courses": [
        {"prefix": "影片/甲/", "prompt": "課程甲", "hotwords": ["桂枝湯"]}]}
    instances = []
    def factory():
        engine = FakeEngine()
        instances.append(engine)
        return engine
    counts = transcribe_batch(audio_root, out_dir, factory, prompts, includes=["影片/甲/"])
    assert counts == {"完成": 1, "失敗": 0}
    assert instances[0].calls[0][1:] == ("課程甲", ["桂枝湯"])
    document = load(out_dir / "影片/甲/第一.mp4.json")
    assert document["source"] == "影片/甲/第一.mp4"
    assert document["segments"][0]["text"] == "桂枝湯"
    assert document["engine"]["params"]["language"] == "zh"
    assert transcribe_batch(audio_root, out_dir, factory, prompts) == {"完成": 1, "失敗": 0}
    assert len(instances) == 2
    assert instances[1].calls[0][0] == "第二.mp4.flac"
    assert transcribe_batch(audio_root, out_dir, factory, prompts) == {"完成": 0, "失敗": 0}
    assert len(instances) == 2
    assert transcribe_batch(audio_root, out_dir, factory, prompts, overwrite=True,
                            no_prompt=True) == {"完成": 2, "失敗": 0}
    assert all(prompt == "" and hotwords == [] for _, prompt, hotwords in instances[2].calls)


def test_clip_timestamps_are_absolute(tmp_path):
    source, audio = tmp_path / "source.wav", tmp_path / "clip.flac"
    generate_source(source)
    extract_audio(source, audio, start=1, duration=0.5)
    target = tmp_path / "result.json"
    prompts = {"default": {"prompt": "", "hotwords": []}, "courses": []}
    transcribe_one(audio, "影片/來源.wav", target, FakeEngine(), prompts, clip_start=600)
    result = load(target)
    assert result["clip"]["start"] == 600
    assert result["segments"][0]["start"] == 600
    assert result["segments"][0]["end"] == 600.5


@pytest.mark.slow
@pytest.mark.skipif(os.environ.get("HAIXIA_SLOW") != "1", reason="設定 HAIXIA_SLOW=1 才下載真實模型")
@pytest.mark.parametrize("setting", ["whisper-prompt", "whisper-noprompt", "whisper-batched", "sensevoice", "paraformer"])
def test_real_engine(setting, tmp_path):
    audio = os.environ.get("HAIXIA_TEST_AUDIO")
    if not audio:
        pytest.skip("請設定 HAIXIA_TEST_AUDIO 為 16 kHz 單聲道 FLAC")
    from scripts.transcribe import make_engine, transcribe_one
    engine = make_engine(setting, model="small", device="cpu", batch_size=2)
    prompts = {"default": {"prompt": "中醫講座", "hotwords": ["桂枝湯"]}, "courses": []}
    target = tmp_path / f"{setting}.json"
    transcribe_one(audio, "測試/語音.flac", target, engine, prompts,
                   no_prompt=setting == "whisper-noprompt")
    validate(load(target))
