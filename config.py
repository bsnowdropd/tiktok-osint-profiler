"""
config.py — Unified configuration file for the entire pipeline.

Previously, constants (Ollama URL, model name, port) were scattered
across all modules (sometimes with different values). Now there's a single source of truth.
"""

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_URL: str = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL: str = "qwen2.5"
OLLAMA_VISION_MODEL: str = "moondream"

# Timeouts (seconds)
OLLAMA_TIMEOUT_TAGS: int = 60      # sublimator / analyzer
OLLAMA_TIMEOUT_PROFILE: int = 1200  # profiler (large context)
OLLAMA_VISION_TIMEOUT: int = 90     # moondream per frame

# ── Whisper ───────────────────────────────────────────────────────────────────
# "base" — fast, but poor for UK/RU. Recommended: "small" or "medium".
WHISPER_MODEL_SIZE: str = "small"

# ── Paths ─────────────────────────────────────────────────────────────────────
COOKIES_FILE: str = "tiktok_cookies.json"   # replaced .pkl (pickle) with JSON
VIDEO_DIR: str = "downloaded_videos"

# ── Parser ────────────────────────────────────────────────────────────────────
SCROLL_PAUSE_MIN: float = 3.5   # Jitter: min pause between scrolls
SCROLL_PAUSE_MAX: float = 6.0   # Jitter: max pause
