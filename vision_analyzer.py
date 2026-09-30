import sqlite3
import cv2
import base64
import requests
import os
import time
import yt_dlp
import easyocr
from pathlib import Path

# ==========================================
# CONFIGURATION
# ==========================================
DB_FILE = "tiktok_reposts.db"
COOKIE_TXT = "temp_cookies.txt"
OLLAMA_URL = "http://127.0.0.1:11435/api/generate"
MODEL_NAME = "moondream"

VIDEO_DIR = Path("temp_videos")
VIDEO_DIR.mkdir(exist_ok=True)

# OCR Initialization (loaded once)
print("[i] Loading OCR models...")
reader = easyocr.Reader(['uk', 'en', 'ru'])


def download_video(url, video_id):
    """Downloads video via yt-dlp."""
    output_path = VIDEO_DIR / f"{video_id}.mp4"
    ydl_opts = {
        'format': 'mp4',
        'outtmpl': str(output_path),
        'quiet': True,
        'cookiefile': COOKIE_TXT if os.path.exists(COOKIE_TXT) else None,
        'impersonate': 'chrome110',
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return output_path
    except Exception as e:
        print(f"  [!] Download error {video_id}: {e}")
        return None


def get_frame_and_text(video_path):
    """Extracts a frame and recognizes text on it."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None, ""

    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * 2))  # Frame at 2nd second
    success, frame = cap.read()

    on_screen_text = ""
    img_base64 = None

    if success:
        # OCR
        text_results = reader.readtext(frame, detail=0)
        on_screen_text = " | ".join(text_results)

        # Preparation for AI
        frame_resized = cv2.resize(frame, (640, 360))
        _, buffer = cv2.imencode('.jpg', frame_resized)
        img_base64 = base64.b64encode(buffer).decode('utf-8')

    cap.release()
    return img_base64, on_screen_text


def get_visual_description(frame_b64):
    """Request to Moondream via Ollama."""
    payload = {
        "model": MODEL_NAME,
        "prompt": "Describe this TikTok frame briefly. What is the main action?",
        "images": [frame_b64],
        "stream": False
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=60)
        return response.json().get('response', '')
    except Exception as e:
        print(f"  [!] Ollama error: {e}")
        return ""


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row

    # Table preparation
    conn.execute(
        "CREATE TABLE IF NOT EXISTS analysis (video_id TEXT PRIMARY KEY, visual_context TEXT, video_text TEXT)")
    try:
        conn.execute("ALTER TABLE analysis ADD COLUMN video_text TEXT")
    except:
        pass

    # Taking 20 videos for processing
    rows = conn.execute("""
                        SELECT r.video_id, r.url
                        FROM reposts r
                        WHERE
                            r.video_id NOT IN (SELECT video_id FROM analysis WHERE visual_context IS NOT NULL) LIMIT 20
                        """).fetchall()

    if not rows:
        print("[i] No new videos for analysis.")
        return

    print(f"[i] Starting processing of {len(rows)} videos...")

    for row in rows:
        v_id, url = row['video_id'], row['url']
        print(f"\n[→] Processing {v_id}...")

        video_path = download_video(url, v_id)

        if video_path and video_path.exists():
            frame_b64, screen_text = get_frame_and_text(video_path)

            if frame_b64:
                description = get_visual_description(frame_b64)

                conn.execute("""
                             INSERT INTO analysis (video_id, visual_context, video_text)
                             VALUES (?, ?, ?) ON CONFLICT(video_id) DO
                             UPDATE SET
                                 visual_context = excluded.visual_context,
                                 video_text = excluded.video_text
                             """, (v_id, description, screen_text))
                conn.commit()

                print(f"  [✓] Text: {screen_text[:50]}...")
                print(f"  [✓] Vision: {description[:50]}...")

            # Deleting video after analysis
            video_path.unlink()
            time.sleep(1)
        else:
            print(f"  [!] Skipped.")

    conn.close()
    print("\n[✓] Batch processing completed!")


if __name__ == "__main__":
    main()