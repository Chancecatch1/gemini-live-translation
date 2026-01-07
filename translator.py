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

INPUT_SAMPLE_RATE = 16000
CHUNK_SIZE = 1024
CHUNK_DURATION_SEC = 10  # Increased to 10 seconds
SESSION_TIMEOUT = 840  # 14 minutes (reconnect before 15-min limit)
LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
TRANSLATE_MODEL = "gemini-2.5-flash-lite"
HISTORY_DIR = "history"

# Sentence ending detection
SENTENCE_ENDERS = {'.', '!', '?'}


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
    print("\nINPUT:")
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
    
    def get_context(self, n=5):
        """Get last n pairs for translation context"""
        return self.pairs[-n:] if self.pairs else []


def is_korean(text: str) -> bool:
    """Check if text contains mostly Korean characters"""
    korean_count = sum(1 for c in text if '\uac00' <= c <= '\ud7af' or '\u1100' <= c <= '\u11ff')
    # Consider Korean if more than 30% of non-space characters are Korean
    non_space = len(text.replace(" ", ""))
    return non_space > 0 and (korean_count / non_space) > 0.3


async def translate_text(client, source_text: str, context: list) -> str:
    """Translate text bidirectionally: Korean→English, others→Korean"""
    try:
        context_str = ""
        if context:
            context_str = "\n".join([f"- {p['input']} -> {p['output']}" for p in context])
            context_str = f"Previous translations for context:\n{context_str}\n\n"
        
        # Detect language and set target
        if is_korean(source_text):
            target_lang = "English"
            source_lang = "Korean"
        else:
            target_lang = "Korean"
            source_lang = "the source language"
        
        prompt = f"""{context_str}This is real-time speech transcription. Translate the following from {source_lang} to natural {target_lang}.
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


async def run_session(input_id: int, session: TranslatorSession, client: genai.Client):
    """Run a single Live API session (max 14 minutes)"""
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16, channels=1, rate=INPUT_SAMPLE_RATE,
        input=True, input_device_index=input_id, frames_per_buffer=CHUNK_SIZE
    )
    
    queue = asyncio.Queue()
    translation_queue = asyncio.Queue()
    running = True
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="Listen and transcribe English speech. Do not respond, just listen.",
        input_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=50,
                silence_duration_ms=100,
            )
        ),
    )
    
    async def capture():
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
        """Background task that translates queued English text"""
        nonlocal running
        while running:
            try:
                english_text = await asyncio.wait_for(translation_queue.get(), timeout=0.1)
                korean_text = await translate_text(client, english_text, session.get_context())
                
                # Show translation below the streamed input
                print(f"{korean_text}\n")
                
                session.add_pair(english_text, korean_text)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"\n[translator error: {e}]")
    
    async def receive(session_api):
        """Receive transcription and flush buffer on time OR sentence end"""
        nonlocal running
        current_buffer = ""
        last_chunk_time = time.time()
        
        while running:
            try:
                turn = session_api.receive()
                async for resp in turn:
                    if not running:
                        break
                    
                    sc = resp.server_content
                    if sc is None:
                        continue
                    
                    if sc.interrupted:
                        if current_buffer.strip():
                            print()  # New line after interrupted input
                        current_buffer = ""
                        last_chunk_time = time.time()
                        continue
                    
                    # Stream input transcription in real-time
                    if sc.input_transcription and sc.input_transcription.text:
                        chunk = sc.input_transcription.text
                        current_buffer += chunk
                        print(chunk, end="", flush=True)  # Real-time streaming
                    
                    elapsed = time.time() - last_chunk_time
                    
                    # Flush conditions:
                    # 1. Time limit reached (10 sec) AND buffer has content
                    # 2. Sentence ended AND minimum 3 seconds passed (avoid too frequent)
                    should_flush = False
                    
                    if current_buffer.strip():
                        if elapsed >= CHUNK_DURATION_SEC:
                            should_flush = True
                        elif elapsed >= 3.0 and ends_with_sentence(current_buffer):
                            should_flush = True
                    
                    if should_flush:
                        print()  # New line after completed input
                        await translation_queue.put(current_buffer.strip())
                        current_buffer = ""
                        last_chunk_time = time.time()
                        
            except Exception as e:
                if running:
                    print(f"\n[receive error: {e}]")
                break
        
        # Flush remaining buffer
        if current_buffer.strip():
            await translation_queue.put(current_buffer.strip())
    
    capture_task = asyncio.create_task(capture())
    translator_task = asyncio.create_task(translator())
    
    try:
        async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session_api:
            print("Connected!")
            send_task = asyncio.create_task(send(session_api))
            await receive(session_api)
            send_task.cancel()
                    
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:
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


async def run_translator(input_id: int):
    """Run translator with auto-reconnect for 15-min limit"""
    session = TranslatorSession()
    client = genai.Client()
    
    try:
        while True:
            try:
                await asyncio.wait_for(
                    run_session(input_id, session, client),
                    timeout=SESSION_TIMEOUT
                )
                break  # Normal exit
            except asyncio.TimeoutError:
                print(f"\n[Auto-reconnecting at {SESSION_TIMEOUT}s...]")
                continue
            except KeyboardInterrupt:
                break
    finally:
        print(f"\nSession saved: {session.filepath}")
        print(f"[{session.chunk_count} chunks translated]")


async def main():
    devices = list_input_devices()
    if not devices:
        print("No input devices")
        sys.exit(1)
    
    print("Hybrid Translator")
    print("=" * 50)
    
    input_id = select_device(devices)
    await run_translator(input_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
