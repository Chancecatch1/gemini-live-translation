"""
Audio File Transcription Tool
Transcribes audio files using Gemini API.
Supports multilingual transcription (English, Korean, French) - single or mixed.

Usage:
    python transcribe.py path/to/audio.mp3
    python transcribe.py path/to/audio.mp3 --output transcript.txt
"""

import asyncio
import sys
import os
import argparse
import subprocess
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from google import genai
from google.genai import types

load_dotenv()

# Model for audio transcription
TRANSCRIBE_MODEL = "gemini-3-pro-preview"

# Supported audio formats
SUPPORTED_FORMATS = {'.mp3', '.wav', '.m4a', '.flac', '.ogg', '.webm'}


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg is installed and available in PATH"""
    return shutil.which('ffmpeg') is not None


def get_mime_type(file_path: Path) -> str:
    """Get MIME type based on file extension"""
    mime_types = {
        '.mp3': 'audio/mp3',
        '.wav': 'audio/wav',
        '.m4a': 'audio/mp4',
        '.flac': 'audio/flac',
        '.ogg': 'audio/ogg',
        '.webm': 'audio/webm',
    }
    return mime_types.get(file_path.suffix.lower(), 'audio/mpeg')


def preprocess_audio(input_path: Path) -> Path:
    """
    Preprocess audio using ffmpeg to reduce noise and normalize volume.
    Returns path to temporary processed wav file.
    """
    if not check_ffmpeg_available():
        print("Warning: ffmpeg not found. Skipping preprocessing.")
        return input_path

    print("Preprocessing audio (Noise Reduction & Normalization)...")
    
    # Create temp file
    temp_dir = Path(tempfile.gettempdir())
    output_path = temp_dir / f"processed_{input_path.stem}_{int(datetime.now().timestamp())}.wav"
    
    # FFmpeg command:
    # - highpass=f=200: Remove low frequency rumble
    # - lowpass=f=3000: Keep human voice range
    # - loudnorm: Normalize loudness
    # - ar 16000: Resample to 16kHz (optimal for ASR)
    # - ac 1: Convert to mono
    cmd = [
        'ffmpeg', '-y', '-i', str(input_path),
        '-af', 'highpass=f=200,lowpass=f=3000,loudnorm',
        '-ar', '16000', '-ac', '1',
        str(output_path)
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path
    except subprocess.CalledProcessError as e:
        print(f"Error during preprocessing: {e}")
        print("Falling back to original file.")
        return input_path


async def transcribe_audio(file_path: Path, output_path: Path = None) -> str:
    """Transcribe audio file using Gemini API"""
    
    if not file_path.exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)
    
    if file_path.suffix.lower() not in SUPPORTED_FORMATS:
        print(f"Error: Unsupported format. Use: {', '.join(SUPPORTED_FORMATS)}")
        sys.exit(1)
    
    print(f"Transcribing: {file_path.name}")
    print(f"File size: {file_path.stat().st_size / (1024*1024):.1f} MB")
    
    processed_path = preprocess_audio(file_path)
    is_processed = processed_path != file_path
    
    print("-" * 40)
    
    client = genai.Client()
    file_ref = None
    
    try:
        # Upload file using File API
        print("Uploading to Gemini...")
        file_ref = await client.aio.files.upload(file=processed_path)
        print(f"Uploaded: {file_ref.name}")
        
        # Multilingual transcription prompt - accuracy focused
        prompt = """You are a professional transcriber. Transcribe the audio with maximum accuracy.

Instructions:
- Detect and transcribe all languages spoken (English, Korean, French).
- Use native scripts: Korean in Hangul (proper 띄어쓰기), French with diacritics (é, è, ç).
- Handle non-native accents - infer intended words from context.
- Spell technical/academic terms correctly.
- If a word is genuinely unclear, mark [unclear].
- Remove excessive filler words (um, uh, 어, 음).
- Separate speakers if distinguishable.

Output the transcript in clean, readable Markdown."""

        print("Generating transcript...")
        response = await client.aio.models.generate_content(
            model=TRANSCRIBE_MODEL,
            contents=[
                file_ref,
                prompt
            ],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="low")
            )
        )
        
        transcript = response.text
        
        # Default output path
        if output_path is None:
            output_path = file_path.with_suffix('.txt')
        
        # Save transcript
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(f"# Transcription: {file_path.name}\n")
            f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            if is_processed:
                f.write(f"# Note: Processed with noise reduction\n")
            f.write("-" * 40 + "\n\n")
            f.write(transcript)
        
        print(transcript)
        print("-" * 40)
        print(f"Saved: {output_path}")
        
        return transcript
        
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        # Cleanup Gemini file
        if file_ref:
            try:
                print("Cleaning up Gemini file...")
                await client.aio.files.delete(name=file_ref.name)
            except Exception as e:
                print(f"Warning: Failed to delete Gemini file: {e}")

        # Cleanup temp local file
        if is_processed and processed_path.exists():
            try:
                os.remove(processed_path)
            except OSError:
                pass


async def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio files (English + French bilingual)"
    )
    parser.add_argument("audio_file", type=Path, help="Path to audio file (mp3, wav, m4a)")
    parser.add_argument("-o", "--output", type=Path, help="Output file path (default: same name .txt)")
    
    args = parser.parse_args()
    
    await transcribe_audio(args.audio_file, args.output)


if __name__ == "__main__":
    asyncio.run(main())
