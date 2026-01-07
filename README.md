# Live Translator

Real-time bidirectional speech translator using Gemini 2.5 Live API.

- **Korean** → English
- **Other languages** → Korean

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

## Features

- Real-time streaming transcription
- Context-aware translation (uses last 5 translations)
- Session history saved as JSONL

## Requirements

- Python 3.10+
- Gemini API key
