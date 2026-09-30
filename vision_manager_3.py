"""
vision_manager_3.py — Visual analysis: OCR (EasyOCR) + Moondream.

Changes (refactoring):
  - easyocr.Reader() moved from module level to lazy-singleton get_reader()
  - Added threading.Lock around reader for thread-safety (EasyOCR is not thread-safe)
  - Added error handling for empty results (ERROR_CANT_OPEN/ERROR_NO_FILE) to avoid race conditions.
  - Added simple fallback to ffmpeg (via subprocess) if cv2 cannot open the video.
"""

import base64
import concurrent.futures
import logging
import threading
import subprocess
import os
import tempfile
from pathlib import Path

import cv2
import requests

import config
from db_schema import get_connection

logger = logging.getLogger(__name__)

_reader = None
_reader_lock = threading.Lock()

def get_reader():
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                import easyocr
                logger.info("Loading EasyOCR (UK, EN, RU)...")
                _reader = easyocr.Reader(["uk", "en", "ru"], gpu=True)
    return _reader

def extract_frames_ffmpeg(video_path: str, num_frames: int = 3) -> list[str]:
    """Fallback: extracts frames via ffmpeg if cv2 fails."""
    frames_b64 = []
    try:
        # Get video duration
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=5
        )
        duration = float(probe.stdout.strip())
        step = duration / (num_frames + 1)
        
        for i in range(1, num_frames + 1):
            ts = step * i
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_name = tmp.name
            
            cmd = [
                "ffmpeg", "-y", "-ss", str(ts), "-i", video_path, 
                "-vframes", "1", "-vf", "scale=640:360", "-q:v", "2", tmp_name
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            
            if os.path.exists(tmp_name) and os.path.getsize(tmp_name) > 0:
                with open(tmp_name, "rb") as f:
                    frames_b64.append(base64.b64encode(f.read()).decode("utf-8"))
            
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
    except Exception as e:
        logger.error("ffmpeg fallback error for %s: %s", video_path, e)
    return frames_b64

def get_frames_and_text(video_path: str, num_frames: int = 3) -> tuple[list[str], str]:
    """Extracts equidistant frames from video and recognizes text."""
    frames_b64: list[str] = []
    ocr_parts: list[str] = []
    
    cap = cv2.VideoCapture(str(video_path))
    if cap.isOpened() and int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) > 0:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        step = total_frames // (num_frames + 1)
        frame_indices = [step * i for i in range(1, num_frames + 1)]
        
        reader = get_reader()
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok:
                continue
            with _reader_lock:
                texts = reader.readtext(frame, detail=0)
            for t in texts:
                t = t.strip()
                if t and t not in ocr_parts:
                    ocr_parts.append(t)
            small = cv2.resize(frame, (640, 360))
            _, buf = cv2.imencode(".jpg", small)
            frames_b64.append(base64.b64encode(buf).decode("utf-8"))
        cap.release()
    else:
        logger.warning("cv2 could not open %s, trying ffmpeg...", video_path)
        cap.release()
        frames_b64 = extract_frames_ffmpeg(video_path, num_frames)
        # For ffmpeg-fallback skip OCR (or could recognize from images, but this is a fallback)
        
    return frames_b64, " | ".join(ocr_parts)

def get_vision_desc(frame_b64: str) -> str:
    payload = {
        "model": config.OLLAMA_VISION_MODEL,
        "prompt": "Describe this TikTok frame briefly. What is happening? Answer in 1 sentence, English.",
        "images": [frame_b64],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        res = requests.post(
            config.OLLAMA_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=config.OLLAMA_VISION_TIMEOUT,
        )
        if res.status_code == 200:
            return res.json().get("response", "").strip()
    except Exception as exc:
        logger.debug("Moondream error: %s", exc)
    return ""

def process_single_video(row) -> tuple[str, str, str]:
    v_id, local_path = row["video_id"], row["local_path"]

    if not local_path or not Path(local_path).exists():
        logger.warning("File not found: %s (ID: %s)", local_path, v_id)
        # Write marker to avoid processing this missing video again
        return v_id, "ERROR_NO_FILE", "ERROR_NO_FILE"

    frames_b64, screen_text = get_frames_and_text(local_path, num_frames=3)
    descriptions: list[str] = []

    for i, fb64 in enumerate(frames_b64, 1):
        desc = get_vision_desc(fb64)
        if desc:
            descriptions.append(f"[Frame {i}]: {desc}")

    if not descriptions:
        # Marker that video was broken, but file existed
        return v_id, "ERROR_CANT_OPEN", "ERROR_CANT_OPEN"

    return v_id, " ".join(descriptions), screen_text

def process_batch(limit: int = 100, max_workers: int = 3, db_path: str = "osint_unknown.db") -> None:
    conn = get_connection(db_path)
    rows = conn.execute(
        """
        SELECT r.video_id, r.local_path
        FROM reposts r
        LEFT JOIN analysis a ON r.video_id = a.video_id
        WHERE (a.visual_context IS NULL OR a.visual_context = '')
          AND r.local_path IS NOT NULL
          AND r.local_path != ''
        ORDER BY r.parsed_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        logger.info("No new videos found for visual analysis.")
        conn.close()
        return

    logger.info("Processing %d videos in %d threads...", len(rows), max_workers)

    results: list[tuple[str, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for res in ex.map(process_single_video, rows):
            if res:
                results.append(res)
                logger.info(
                    "Processed: %s | OCR: %.20s… | Vision: %.40s…",
                    res[0], res[2], res[1],
                )

    if results:
        logger.info("Saving %d results to DB...", len(results))
        for v_id, visual_desc, screen_text in results:
            conn.execute(
                """
                INSERT INTO analysis (video_id, visual_context, video_text) VALUES (?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    visual_context = excluded.visual_context,
                    video_text     = excluded.video_text
                """,
                (v_id, visual_desc, screen_text),
            )
        conn.commit()

    conn.close()
    logger.info("Visual analysis completed!")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    process_batch(limit=300, max_workers=3)
