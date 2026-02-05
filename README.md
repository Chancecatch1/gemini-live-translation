# Live Translator

Real-time speech translator using Gemini 2.5 Live API.

## Translation

| Source | Target |
|--------|--------|
| English | → Korean |
| Japanese | → Korean |
| Korean | → English |

## Setup

```bash
pip install -r requirements.txt
```

Create `.env`:
```
GOOGLE_API_KEY=your_key_here
```

- Session history saved as JSONL
- **Audio File Transcription**: Supports long lectures (>1 hr) using Gemini 3 Pro

## Usage

### Live Translator
```bash
python translator.py
```
1. Select audio input device
2. Select source language

### File Transcription
```bash
python transcribe.py lecture.mp3
```
- Supports: mp3, wav, m4a, flac, etc.
- Optimized for **Bilingual (English/French)** lectures.

## Requirements

- Python 3.10+
- Gemini API key
