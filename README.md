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

## Usage

```bash
python translator.py
```

1. Select audio input device
2. Select source language
3. Speak or play audio

## Features

- Real-time STT streaming (Gemini Live API)
- Context-aware translation (last 5 pairs)
- Language filtering (filters non-source-language text)
- Session history saved as JSONL

## Requirements

- Python 3.10+
- Gemini API key
