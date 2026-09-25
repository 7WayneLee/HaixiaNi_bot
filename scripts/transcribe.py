#!/usr/bin/env python3
"""單檔或批次轉錄 16 kHz FLAC，輸出可續跑的逐字稿 JSON。"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from haixia.textnorm import to_traditional
from haixia.transcript import (course_for, create, filter_hallucinations,
                               is_low_confidence, save)

ROOT = Path(__file__).resolve().parents[1]
SENSEVOICE = "iic/SenseVoiceSmall"
PARAFORMER = "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
OFFICIAL_HF_MODELS = {"FunAudioLLM/SenseVoiceSmall", "funasr/fsmn-vad", "funasr/ct-punc"}
TAGS = re.compile(r"<\|[^|>]+\|>")


def audio_duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"無法讀取音檔時長：{result.stderr.strip()}")
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise ValueError("音檔長度必須大於零")
    return duration


def _installed_version(package):
    try:
        return version(package)
    except PackageNotFoundError:
        return "未知"


def choose_device(requested):
    if requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


class WhisperEngine:
    def __init__(self, model, device, compute_type, beam_size, batch_size, batched):
        try:
            from faster_whisper import BatchedInferencePipeline, WhisperModel
        except ImportError as error:
            raise RuntimeError("缺少 faster-whisper；請執行 pip install faster-whisper") from error
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.batch_size = batch_size
        self.batched = batched
        self.name = "whisper-batched" if batched else "whisper"
        self.language = "zh"
        self.version = _installed_version("faster-whisper")
        whisper = WhisperModel(model, device=device, compute_type=compute_type)
        self.pipeline = BatchedInferencePipeline(model=whisper) if batched else whisper

    def transcribe(self, audio, prompt, hotwords):
        options = {"beam_size": self.beam_size, "language": self.language, "vad_filter": True,
                   "condition_on_previous_text": False, "initial_prompt": prompt or None}
        if self.batched:
            options["batch_size"] = self.batch_size
        began = time.monotonic()
        segments, _info = self.pipeline.transcribe(str(audio), **options)
        result = [{"start": segment.start, "end": segment.end, "text_raw": segment.text.strip(),
                   "confidence": {"avg_logprob": segment.avg_logprob,
                                  "no_speech_prob": segment.no_speech_prob,
                                  "compression_ratio": segment.compression_ratio}}
                  for segment in segments]
        return result, time.monotonic() - began


def _funasr_model(automodel, model_id, fallback_id, device):
    """SeACo 只用 ModelScope 官方權重；其餘引擎可退回官方 HF 倉庫。"""
    if model_id == PARAFORMER:
        try:
            return automodel(model=model_id, hub="ms", device=device,
                             disable_update=True), model_id
        except Exception as error:
            raise RuntimeError(f"ModelScope 官方 SeACo-Paraformer 載入失敗：{error}；請稍後重試或檢查網路連線") from error

    if fallback_id not in OFFICIAL_HF_MODELS:
        raise ValueError(f"不允許從非官方 Hugging Face 倉庫載入模型：{fallback_id}")

    def from_hf():
        return automodel(model=fallback_id, hub="hf", device=device,
                         disable_update=True), fallback_id

    if os.environ.get("HAIXIA_MODEL_HUB") == "hf":
        try:
            return from_hf()
        except Exception as error:
            raise RuntimeError(f"Hugging Face 模型 {fallback_id} 載入失敗：{error}") from error
    try:
        return automodel(model=model_id, hub="ms", device=device, disable_update=True), model_id
    except Exception as first_error:
        print(f"ModelScope 載入 {model_id} 失敗：{first_error}；改試 Hugging Face。", file=sys.stderr)
        try:
            return from_hf()
        except Exception as second_error:
            raise RuntimeError(f"模型載入失敗：ModelScope：{first_error}；Hugging Face：{second_error}") from second_error


class FunASREngine:
    def __init__(self, name, device):
        try:
            from funasr import AutoModel
            import soundfile
            from opencc import OpenCC
        except ImportError as error:
            raise RuntimeError("缺少 FunASR、soundfile 或 OpenCC；請執行 pip install funasr soundfile opencc") from error
        self.name = name
        self.language = "zh"
        self.device = device
        self.compute_type = "float32"
        self.beam_size = 5
        self.version = _installed_version("funasr")
        self.soundfile = soundfile
        self.t2s = OpenCC("t2s")
        model_id = SENSEVOICE if name == "sensevoice" else PARAFORMER
        fallback = "FunAudioLLM/SenseVoiceSmall" if name == "sensevoice" else None
        self.asr, self.model = _funasr_model(AutoModel, model_id, fallback, device)
        self.vad, _ = _funasr_model(AutoModel, "fsmn-vad", "funasr/fsmn-vad", device)
        self.punc = None
        if name == "paraformer":
            self.punc, _ = _funasr_model(AutoModel, "ct-punc", "funasr/ct-punc", device)

    def transcribe(self, audio, prompt, hotwords):
        speech, sample_rate = self.soundfile.read(str(audio), dtype="float32")
        if sample_rate != 16000 or speech.ndim != 1:
            raise ValueError("FunASR 音檔必須是 16 kHz 單聲道；請先執行 extract_audio.py")
        began = time.monotonic()
        vad_output = self.vad.generate(input=str(audio))
        spans = vad_output[0].get("value", [])
        hotword = " ".join(self.t2s.convert(word) for word in hotwords)
        result = []
        for start_ms, end_ms in spans:
            start = max(0, int(start_ms * 16))
            end = min(len(speech), int(end_ms * 16))
            if end <= start:
                continue
            options = {"input": speech[start:end]}
            if self.name == "sensevoice":
                options["use_itn"] = True
                options["language"] = self.language
            elif hotword:
                options["hotword"] = hotword
            output = self.asr.generate(**options)
            raw = output[0].get("text", "") if output else ""
            if self.name == "sensevoice":
                raw = TAGS.sub("", raw).strip()
            elif self.punc is not None and raw:
                punctuated = self.punc.generate(input=raw)
                raw = punctuated[0].get("text", raw) if punctuated else raw
            result.append({"start": start / 16000, "end": end / 16000,
                           "text_raw": raw.strip(), "confidence": None})
        return result, time.monotonic() - began


def make_engine(name, model="large-v3", device="auto", compute_type=None, beam_size=5, batch_size=16):
    device = choose_device(device)
    compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    if name in {"whisper", "whisper-prompt", "whisper-noprompt", "whisper-batched"}:
        return WhisperEngine(model, device, compute_type, beam_size, batch_size, name == "whisper-batched")
    if name in {"sensevoice", "paraformer"}:
        return FunASREngine(name, device)
    raise ValueError(f"不支援的引擎：{name}")


def transcribe_one(audio, source, out, engine, prompts, clip_start=None, no_prompt=False,
                   overwrite=False):
    """engine 可注入假物件，不需下載模型即可測試批次。"""
    if Path(out).exists() and not overwrite:
        return False
    duration = audio_duration(audio)
    course = course_for(source, prompts)
    prompt = "" if no_prompt else course.get("prompt", "")
    hotwords = [] if no_prompt else course.get("hotwords", [])
    raw_segments, elapsed = engine.transcribe(audio, prompt, hotwords)
    offset = clip_start or 0.0
    segments = []
    for raw in raw_segments:
        start = max(0.0, min(duration, float(raw["start"]))) + offset
        end = max(start, min(duration + offset, float(raw["end"]) + offset))
        text_raw = raw["text_raw"].strip()
        if not text_raw:
            continue
        confidence = raw.get("confidence")
        segments.append({"start": start, "end": end, "text_raw": text_raw,
                         "text": to_traditional(text_raw), "speaker": None,
                         "confidence": confidence, "low_confidence": is_low_confidence(confidence)})
    segments.sort(key=lambda segment: segment["start"])
    segments, dropped = filter_hallucinations(segments, prompt=prompt)
    params = {"prompt": prompt, "hotwords": hotwords, "language": getattr(engine, "language", "zh"),
              "beam_size": getattr(engine, "beam_size", None),
              "vad": True}
    if getattr(engine, "name", "") == "whisper-batched":
        params["batch_size"] = getattr(engine, "batch_size", 16)
    document = create(source, duration,
                      {"name": engine.name, "model": engine.model, "version": engine.version,
                       "params": params, "device": engine.device,
                       "compute_type": engine.compute_type, "elapsed_sec": elapsed},
                      segments, clip={"start": offset, "duration": duration} if clip_start is not None else None,
                      dropped=dropped)
    save(document, out)
    return True


def transcribe_batch(audio_root, out_dir, engine_factory, prompts, includes=(), overwrite=False,
                     no_prompt=False):
    """尋找 FLAC 後只對未完成項目建模；engine_factory 為零參數工廠。"""
    audio_root, out_dir = Path(audio_root), Path(out_dir)
    jobs = []
    for audio in sorted(audio_root.rglob("*.flac")):
        relative = audio.relative_to(audio_root).as_posix()
        source = relative[:-5]
        if includes and not any(source.startswith(prefix) for prefix in includes):
            continue
        out = out_dir / (source + ".json")
        if out.exists() and not overwrite:
            print(f"已存在，跳過：{out}", file=sys.stderr)
            continue
        jobs.append((audio, source, out))
    if not jobs:
        print("沒有待轉錄音檔。", file=sys.stderr)
        return {"完成": 0, "失敗": 0}
    engine = engine_factory()
    counts = {"完成": 0, "失敗": 0}
    for index, (audio, source, out) in enumerate(jobs, 1):
        try:
            transcribe_one(audio, source, out, engine, prompts, no_prompt=no_prompt,
                           overwrite=overwrite)
            counts["完成"] += 1
            print(f"已轉錄 {index}/{len(jobs)}：{source}", file=sys.stderr)
        except Exception as error:
            counts["失敗"] += 1
            print(f"轉錄失敗 {index}/{len(jobs)}：{source}：{error}", file=sys.stderr)
    return counts


def main():
    parser = argparse.ArgumentParser(description="轉錄音檔並輸出逐字稿 JSON")
    parser.add_argument("audio", nargs="?", type=Path)
    parser.add_argument("--engine", required=True, choices=["whisper", "whisper-prompt", "whisper-noprompt", "whisper-batched", "sensevoice", "paraformer"])
    parser.add_argument("--source", help="raw 底下的相對路徑")
    parser.add_argument("--clip-start", type=float)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--audio-root", type=Path)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--include", action="append", default=[])
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--prompts", type=Path, default=ROOT / "data/course_prompts.json")
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--compute-type")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--beam-size", type=int, default=5)
    args = parser.parse_args()
    if args.clip_start is not None and args.clip_start < 0:
        parser.error("--clip-start 必須是非負數")
    if args.beam_size < 1 or args.batch_size < 1:
        parser.error("--beam-size 和 --batch-size 必須大於零")
    with args.prompts.open(encoding="utf-8") as file:
        prompts = json.load(file)
    factory = lambda: make_engine(args.engine, args.model, args.device, args.compute_type,
                                  args.beam_size, args.batch_size)
    if args.audio_root:
        if not args.out_dir or args.audio or args.source or args.out or args.clip_start is not None:
            parser.error("批次模式需要 --out-dir，且不能指定單檔選項")
        transcribe_batch(args.audio_root, args.out_dir, factory, prompts, args.include,
                         args.overwrite, args.no_prompt or args.engine == "whisper-noprompt")
    else:
        if not args.audio or not args.source or not args.out:
            parser.error("單檔模式需要音檔、--source 與 --out")
        if args.out.exists() and not args.overwrite:
            print(f"已存在，跳過：{args.out}")
            return
        engine = factory()
        transcribe_one(args.audio, args.source, args.out, engine, prompts,
                       args.clip_start, args.no_prompt or args.engine == "whisper-noprompt",
                       args.overwrite)
        print(f"完成：{args.out}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"轉錄失敗：{error}")
