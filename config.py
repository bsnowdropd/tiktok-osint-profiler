"""
config.py — Єдиний файл конфігурації для всього конвеєра.

Раніше константи (URL Ollama, назва моделі, порт) були розкидані
по всіх модулях (іноді різні значення). Тепер одне місце правди.
"""

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_URL: str = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL: str = "qwen2.5"
OLLAMA_VISION_MODEL: str = "moondream"

# Тайм-аути (секунди)
OLLAMA_TIMEOUT_TAGS: int = 60      # sublimator / analyzer
OLLAMA_TIMEOUT_PROFILE: int = 1200  # profiler (великий контекст)
OLLAMA_VISION_TIMEOUT: int = 90     # moondream на один кадр

# ── Whisper ───────────────────────────────────────────────────────────────────
# "base" — швидко, але погано для UK/RU.  Рекомендовано: "small" або "medium".
WHISPER_MODEL_SIZE: str = "small"

# ── Шляхи ─────────────────────────────────────────────────────────────────────
COOKIES_FILE: str = "tiktok_cookies.json"   # було .pkl (pickle) — замінено на JSON
VIDEO_DIR: str = "downloaded_videos"

# ── Парсер ────────────────────────────────────────────────────────────────────
SCROLL_PAUSE_MIN: float = 3.5   # Jitter: мін. пауза між кадрами
SCROLL_PAUSE_MAX: float = 6.0   # Jitter: макс. пауза
