# Milestones 7-10: local speech pipeline

> **历史归档，不是当前运行说明。** 本文只记录本地语音流水线的开发阶段与验收方法；当前远程控制、分析 API、发布和运维行为以根目录 README、`docs/CURRENT_ARCHITECTURE.md`、`docs/OPERATIONS.md` 为准。

This stage processes an existing capture Session. It does not join OOPZ and does not upload audio.

## Components

- Milestone 7: Silero VAD (ONNX) at 16 kHz, with configurable threshold and padding.
- Milestone 8: generic `ASRBackend` plus local SenseVoiceSmall under `models/SenseVoiceSmall`.
- Milestone 9: chronological, overlap-aware `transcript.jsonl` as the canonical format.
- Milestone 10: UTF-8 `transcript.md`, with nickname, OOPZ UID, and Agora UID on every segment.

## One-command processing

```powershell
.\.venv\Scripts\oopz-transcribe.exe process `
  "D:\Codex\OOPZ_Capture\output\SESSION_ID" `
  --device cpu `
  --language auto
```

CPU is the tested default. The installed PyTorch wheel is CPU-only even when an NVIDIA GPU is present.

## Separate commands

```powershell
.\.venv\Scripts\oopz-transcribe.exe vad "D:\Codex\OOPZ_Capture\output\SESSION_ID"
 .\.venv\Scripts\oopz-transcribe.exe transcribe "D:\Codex\OOPZ_Capture\output\SESSION_ID" --device cpu --language auto
.\.venv\Scripts\oopz-transcribe.exe render "D:\Codex\OOPZ_Capture\output\SESSION_ID"
```

## VAD defaults

```text
threshold=0.5
min_speech_ms=250
min_silence_ms=300
speech_pad_ms=200
```

## Outputs

```text
output/SESSION_ID/
  vad/segments.jsonl
  vad/summary.json
  transcript.jsonl
  transcript_summary.json
  transcript.md
```

`start_ms` and `end_ms` use the common capture clock. Absolute timestamps prefer the recorded `capture_started` event. Overlapping speakers keep overlapping ranges.

## Acceptance

1. Confirm VAD boundaries do not clip speech.
2. Read `transcript.md` while listening to the same WAV ranges.
3. Confirm speaker labels and both IDs.
4. Confirm simultaneous speech appears as overlapping entries.
5. Treat recognition mistakes as ASR quality findings; never alter source audio or identity data to hide them.
