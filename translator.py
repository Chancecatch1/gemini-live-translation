import asyncio
import sys
import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv

import pyaudio
from google import genai
from google.genai import types

load_dotenv()

# Audio configuration
INPUT_SAMPLE_RATE = 16000
CHUNK_SIZE = 1024

# Translation settings
CHUNK_DURATION_SEC = 10       # Time buffer before translation
MIN_CHUNK_LENGTH = 5         # Skip short chunks
SENTENCE_FLUSH_MIN = 1.0      # Min wait before sentence-end flush
SESSION_TIMEOUT = 840         # Auto-reconnect at 14 min

# Models
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
TRANSLATE_MODEL = "gemini-2.5-flash-lite"
HISTORY_DIR = "history"

# Supported languages
LANGUAGES = {
    "en": {"name": "English", "instruction": "Transcribe ONLY English speech. Ignore any non-English audio completely."},
    "ja": {"name": "Japanese", "instruction": "日本語の音声のみを書き起こしてください。日本語以外の音声は完全に無視してください。"},
    "ko": {"name": "Korean", "instruction": "한국어 음성만 받아 적으세요. 한국어가 아닌 음성은 무시하세요."},
    "fr": {"name": "French", "instruction": "Transcrivez UNIQUEMENT le discours en français. Ignorez complètement tout audio non français."},
}

# Sentence ending punctuation
SENTENCE_ENDERS = {'.', '!', '?', '。', '！', '？'}


def list_input_devices():
    p = pyaudio.PyAudio()
    devices = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info["maxInputChannels"] > 0:
            devices.append((i, info["name"]))
    p.terminate()
    return devices


def select_device(devices):
    print("\nAudio Input:")
    for idx, (dev_id, name) in enumerate(devices):
        print(f"  [{idx}] {name}")
    while True:
        try:
            choice = input(f"Select [0-{len(devices)-1}]: ").strip()
            choice = 0 if choice == "" else int(choice)
            if 0 <= choice < len(devices):
                print(f"- {devices[choice][1]}")
                return devices[choice][0]
        except (ValueError, KeyboardInterrupt):
            pass


def select_language():
    print("\nSource Language:")
    lang_list = list(LANGUAGES.keys())
    for idx, lang_code in enumerate(lang_list):
        print(f"  [{idx}] {LANGUAGES[lang_code]['name']}")
    while True:
        try:
            choice = input(f"Select [0-{len(lang_list)-1}]: ").strip()
            choice = 0 if choice == "" else int(choice)
            if 0 <= choice < len(lang_list):
                selected = lang_list[choice]
                print(f"- {LANGUAGES[selected]['name']}")
                return selected
        except (ValueError, KeyboardInterrupt):
            pass


def ends_with_sentence(text: str) -> bool:
    """Check if text ends with a sentence-ending punctuation"""
    text = text.rstrip()
    return len(text) > 0 and text[-1] in SENTENCE_ENDERS


class TranslatorSession:
    def __init__(self):
        self.session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.pairs = []
        self.chunk_count = 0
        os.makedirs(HISTORY_DIR, exist_ok=True)
        self.filepath = os.path.join(HISTORY_DIR, f"session_{self.session_id}.jsonl")
        
    def add_pair(self, input_text: str, output_text: str):
        if input_text.strip() and output_text.strip():
            self.chunk_count += 1
            pair = {
                "chunk": self.chunk_count,
                "timestamp": datetime.now().isoformat(),
                "input": input_text.strip(),
                "output": output_text.strip()
            }
            self.pairs.append(pair)
            # Incremental save (append to JSONL)
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(pair, ensure_ascii=False) + "\n")
            return True
        return False
    
    def get_context(self, n=3):
        """Get last n pairs for translation context"""
        return self.pairs[-n:] if self.pairs else []


def is_korean(text: str) -> bool:
    """Check if text contains mostly Korean characters"""
    korean_count = sum(1 for c in text if '\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff')
    non_space = len(text.replace(" ", ""))
    return non_space > 0 and (korean_count / non_space) > 0.3


def is_japanese(text: str) -> bool:
    """Check if text contains Japanese characters (hiragana, katakana, kanji)"""
    jp_count = sum(1 for c in text if 
        '\u3040' <= c <= '\u309f' or  # Hiragana
        '\u30a0' <= c <= '\u30ff' or  # Katakana
        '\u4e00' <= c <= '\u9fff')    # CJK (Kanji)
    non_space = len(text.replace(" ", ""))
    return non_space > 0 and (jp_count / non_space) > 0.3


def is_french(text: str) -> bool:
    """Check if text contains French characters (Latin with accents)"""
    # French-specific accented characters
    french_chars = set('àâäéèêëïîôùûüçœæÀÂÄÉÈÊËÏÎÔÙÛÜÇŒÆ')
    french_count = sum(1 for c in text if c in french_chars)
    ascii_alpha = sum(1 for c in text if c.isascii() and c.isalpha())
    non_space = len(text.replace(" ", ""))
    # French: has Latin letters AND either has French accents OR is mostly ASCII
    return non_space > 0 and (ascii_alpha / non_space) > 0.5 and (french_count > 0 or ascii_alpha > 0)


def is_valid_transcription(text: str, source_lang: str) -> bool:
    """Check if transcription matches expected language"""
    # Skip noise markers
    if text.strip() in ['<noise>', '<sound>', '']:
        return False
    
    if source_lang == "ja":
        return is_japanese(text)
    elif source_lang == "ko":
        return is_korean(text)
    elif source_lang == "en":
        # English: mostly ASCII letters, no French accents
        french_chars = set('àâäéèêëïîôùûüçœæÀÂÄÉÈÊËÏÎÔÙÛÜÇŒÆ')
        has_french = any(c in french_chars for c in text)
        ascii_count = sum(1 for c in text if c.isascii() and c.isalpha())
        non_space = len(text.replace(" ", ""))
        return non_space > 0 and (ascii_count / non_space) > 0.5 and not has_french
    elif source_lang == "fr":
        return is_french(text)
    return True


async def translate_text(client, source_text: str, source_lang: str, context: list) -> str:
    """Translate text: en/jp→ko, ko→en/jp"""
    try:
        context_str = ""
        if context:
            context_str = "\n".join([f"- {p['input']} -> {p['output']}" for p in context])
            context_str = f"Previous translations for context:\n{context_str}\n\n"
        
        # Set target language based on source
        if source_lang == "ko":
            target_lang = "English"
            source_name = "Korean"
        else:
            target_lang = "Korean"
            source_name = LANGUAGES[source_lang]["name"]
        
        prompt = f"""{context_str}This is real-time speech transcription. Translate the following from {source_name} to natural {target_lang}.
Consider the context above for consistent terminology and natural flow.
Output ONLY the {target_lang} translation, nothing else.

{source_text}"""

        response = await client.aio.models.generate_content(
            model=TRANSLATE_MODEL,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"[Translation error: {e}]"


async def run_session(input_id: int, source_lang: str, session: TranslatorSession, client: genai.Client, resume_handle: str = None):
    """Run a single Live API session with auto-resume support"""
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16, channels=1, rate=INPUT_SAMPLE_RATE,
        input=True, input_device_index=input_id, frames_per_buffer=CHUNK_SIZE
    )
    
    queue = asyncio.Queue()
    translation_queue = asyncio.Queue()
    running = True
    new_resume_handle = None  # Store new handle for reconnection
    
    # Use language-specific instruction for STT
    lang_instruction = LANGUAGES[source_lang]["instruction"]
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=f"{lang_instruction} Do not respond, just listen and transcribe.",
        input_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=200,
                silence_duration_ms=100,
            )
        ),
        # Enable unlimited session duration with context compression
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        # Enable session resumption for reconnection
        session_resumption=types.SessionResumptionConfig(
            handle=resume_handle  # Pass previous handle or None for new session
        ),
    )
    
    async def capture():
        """Capture audio from microphone and send to queue"""
        nonlocal running
        while running:
            try:
                data = await asyncio.to_thread(stream.read, CHUNK_SIZE, exception_on_overflow=False)
                await queue.put(data)
            except Exception:
                break
    
    async def send(session_api):
        nonlocal running
        while running:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=0.1)
                await session_api.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}"))
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
    
    async def translator():
        """Background task that translates queued text"""
        nonlocal running
        while running:
            try:
                source_text = await asyncio.wait_for(translation_queue.get(), timeout=0.1)
                
                # Skip too-short chunks (often produce bad translations)
                if len(source_text.strip()) < MIN_CHUNK_LENGTH:
                    continue
                
                # Skip if text doesn't match source language (filter noise/other languages)
                if not is_valid_transcription(source_text, source_lang):
                    continue
                
                translated = await translate_text(client, source_text, source_lang, session.get_context(n=5))
                
                # Show translation below the streamed input
                print(f"{translated}\n")
                
                session.add_pair(source_text, translated)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"\n[translator error: {e}]")
    
    async def receive(session_api):
        """Receive transcription and flush buffer on time OR sentence end"""
        nonlocal running, new_resume_handle
        current_buffer = ""
        last_chunk_time = time.time()
        
        while running:
            try:
                turn = session_api.receive()
                async for resp in turn:
                    if not running:
                        break
                    
                    # Handle session resumption updates
                    if resp.session_resumption_update:
                        update = resp.session_resumption_update
                        if update.resumable and update.new_handle:
                            new_resume_handle = update.new_handle
                    
                    # Handle GoAway message (connection about to close)
                    if resp.go_away is not None:
                        print(f"\n[Server closing connection, time left: {resp.go_away.time_left}]")
                    
                    sc = resp.server_content
                    if sc is None:
                        continue
                    
                    if sc.interrupted:
                        if current_buffer.strip():
                            print()  # New line after interrupted input
                            await translation_queue.put(current_buffer.strip())  # Translate interrupted speech too
                        current_buffer = ""
                        last_chunk_time = time.time()
                        continue
                    
                    # Stream input transcription in real-time
                    if sc.input_transcription and sc.input_transcription.text:
                        chunk = sc.input_transcription.text
                        # Filter: only add to buffer if source language detected
                        if is_valid_transcription(chunk, source_lang) or chunk.strip() in ['<noise>', '<sound>', '']:
                            current_buffer += chunk
                            print(chunk, end="", flush=True)  # Real-time streaming
                        # Skip non-source-language chunks silently
                    
                    elapsed = time.time() - last_chunk_time
                    
                    # Flush conditions:
                    # 1. Time limit reached (10 sec) AND buffer has content
                    # 2. Sentence ended AND minimum 3 seconds passed (avoid too frequent)
                    should_flush = False
                    
                    if current_buffer.strip():
                        if elapsed >= CHUNK_DURATION_SEC:
                            should_flush = True
                        elif elapsed >= 1.0 and ends_with_sentence(current_buffer):
                            should_flush = True
                    
                    if should_flush:
                        print()  # New line after completed input
                        await translation_queue.put(current_buffer.strip())
                        current_buffer = ""
                        last_chunk_time = time.time()
                        
            except Exception as e:
                if running:
                    print(f"\n[receive error: {e}]")
                    # Flush remaining buffer before raising
                    if current_buffer.strip():
                        await translation_queue.put(current_buffer.strip())
                    raise  # Re-raise to trigger reconnection
                break
        
        # Flush remaining buffer (normal exit)
        if current_buffer.strip():
            await translation_queue.put(current_buffer.strip())
    
    capture_task = asyncio.create_task(capture())
    translator_task = asyncio.create_task(translator())
    session_error = None  # Store error to re-raise after cleanup
    
    try:
        async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session_api:
            status = "Resumed" if resume_handle else "Connected"
            print(f"{status}!")
            send_task = asyncio.create_task(send(session_api))
            await receive(session_api)
            send_task.cancel()
                    
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:
        session_error = e  # Store error to re-raise
        print(f"\n[Session error: {e}]")
    finally:
        running = False
        capture_task.cancel()
        translator_task.cancel()
        
        # Wait for pending translations
        await asyncio.sleep(0.5)
        
        stream.stop_stream()
        stream.close()
        audio.terminate()
    
    # Re-raise exception after cleanup so run_translator can reconnect
    if session_error:
        raise session_error
    
    return new_resume_handle  # Return handle for next session


async def run_translator(input_id: int, source_lang: str):
    """Run translator with auto-reconnect using session resumption"""
    session = TranslatorSession()
    client = genai.Client()
    resume_handle = None  # Store session handle for reconnection
    
    # Show translation direction
    if source_lang == "ko":
        direction = "Korean → English"
    else:
        direction = f"{LANGUAGES[source_lang]['name']} → Korean"
    print(f"\nTranslation: {direction}")
    
    try:
        while True:
            try:
                new_handle = await asyncio.wait_for(
                    run_session(input_id, source_lang, session, client, resume_handle),
                    timeout=SESSION_TIMEOUT
                )
                # Update handle for next reconnection
                if new_handle:
                    resume_handle = new_handle
                break  # Normal exit
            except asyncio.TimeoutError:
                print(f"\n[Timeout - auto-reconnecting...]")
                continue
            except KeyboardInterrupt:
                break
            except Exception as e:
                # Handle 1011 and other server errors with auto-reconnect
                print(f"\n[Connection error: {e}]")
                print("[Auto-reconnecting in 2 seconds...]")
                await asyncio.sleep(2)  # Brief delay before reconnecting
                continue
    finally:
        print(f"\nSession saved: {session.filepath}")
        print(f"[{session.chunk_count} chunks translated]")


async def main():
    devices = list_input_devices()
    if not devices:
        print("No input devices")
        sys.exit(1)
    
    print("Live Translator")
    print("=" * 40)
    
    input_id = select_device(devices)
    source_lang = select_language()
    await run_translator(input_id, source_lang)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
