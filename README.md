# TikTok OSINT Profiler 🕵️‍♂️

> Automated OSINT pipeline for digital investigations: **parsing → video analysis → LLM profiling → interactive dashboard**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.64-red.svg)](https://streamlit.io)
[![Ollama](https://img.shields.io/badge/LLM-Ollama%20%2F%20Qwen2.5-green.svg)](https://ollama.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🧭 What this project does

The system collects the public activity of a TikTok account (reposts), analyzes each video through three independent channels (computer vision, OCR, audio transcription), extracts semantic patterns using a local LLM, and creates a psychological dossier. All results are available via a web dashboard with interactive graphs.

```
@target_user
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 1 · tiktok_parser_cdp.py                                  │
│  Selenium CDP → collect repost metadata → SQLite (reposts)      │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 2 · vision_manager_3.py                                   │
│  Split video into frames → OCR (EasyOCR) → Moondream (desc)     │
│  Fallback: ffmpeg if OpenCV doesn't support the codec           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 3 · audio_manager.py                                      │
│  Whisper STT → audio track transcription                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 4 · sublimator_cdp.py                                     │
│  Qwen2.5 → semantic tags: interests / hobbies / relationships   │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 5 · analyzer_cdp.py                                       │
│  Fallback: text analysis for videos without video files         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STEP 6 · profiler_cdp.py                                       │
│  Tag aggregation → TXT report + PDF dossier                     │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
                    databases/osint_<user>.db
                    reports/profile_<user>.txt
                    reports/profile_<user>.pdf
```

---

## 📁 Project Structure

```
analyzer/
├── app.py                    # Streamlit dashboard (main UI)
├── run_pipeline.py           # Pipeline orchestrator (steps 1–6)
│
├── tiktok_parser_cdp.py      # Step 1: CDP repost parser
├── vision_manager_3.py       # Step 2: OCR + Moondream + ffmpeg fallback
├── audio_manager.py          # Step 3: Whisper STT
├── sublimator_cdp.py         # Step 4: Qwen2.5 semantic tags
├── analyzer_cdp.py           # Step 5: fallback text analysis
├── profiler_cdp.py           # Step 6: TXT/PDF dossier generation
│
├── cross_analyzer.py         # Cross-analysis of common reposts + semantics
├── cross_platform_linker.py  # Search for profiles on other platforms
├── external_osint.py         # Maigret + Instaloader reconnaissance
├── network_analyzer.py       # Contact network analysis (top-5 + LLM)
├── retro_profiler.py         # Retrospective profiling from a ready DB
├── migrate_done_file.py      # Migration of old databases → databases/
├── reset_db.py               # Reset / clear DB
│
├── db_schema.py              # Single SQLite schema (init + migration)
├── db_manager.py             # Repository pattern — single DB access point
├── config.py                 # Centralized configuration (Ollama, Whisper)
├── utils.py                  # Utilities: sanitize_username, db_path_for
│
├── databases/                # SQLite databases (osint_<user>.db) — in .gitignore
├── reports/                  # TXT and PDF reports — in .gitignore
└── downloaded_videos/        # Temporary MP4s (cleared automatically)
```

---

## 🖥️ System Requirements

| Component | Minimum | Recommended |
|-----------|---------|---------------|
| OS | Ubuntu 20.04+ / Debian | Ubuntu 22.04 / 24.04 |
| Python | 3.10 | 3.11+ |
| RAM | 8 GB | 16+ GB |
| GPU VRAM | — | 8+ GB (Nvidia, for fast Whisper/Moondream) |
| Disk Space | 10 GB | 50+ GB |
| Google Chrome | latest stable | — |
| Ollama | ≥ 0.3 | latest release |
| ffmpeg | any | — |

---

## 🔧 Installation

### 1. System Dependencies

```bash
sudo apt update && sudo apt install -y \
    python3-venv python3-pip git \
    ffmpeg libsm6 libxext6 \
    wkhtmltopdf google-chrome-stable
```

### 2. Cloning and Environment

```bash
git clone https://github.com/YOUR_USERNAME/tiktok-osint-profiler.git
cd tiktok-osint-profiler

python3 -m venv venv
source venv/bin/activate
```

### 3. Python Dependencies

```bash
pip install --upgrade pip
pip install \
    streamlit \
    selenium webdriver-manager \
    requests \
    opencv-python easyocr \
    openai-whisper \
    yt-dlp \
    networkx pyvis \
    pandas \
    markdown pdfkit \
    maigret instaloader
```

### 4. Ollama — Local Models

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Download models
ollama pull qwen2.5        # main LLM (tagging, profiling, RAG)
ollama pull moondream      # multimodal (frame analysis)
```

> **Important:** make sure `ollama serve` is running before starting the pipeline

---

## ⚙️ Configuration

All parameters are in [`config.py`](config.py):

```python
# Ollama address and models
OLLAMA_URL          = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL        = "qwen2.5"         # text analysis
OLLAMA_VISION_MODEL = "moondream"       # image analysis

# Timeouts (seconds)
OLLAMA_TIMEOUT_TAGS    = 60             # sublimator / analyzer
OLLAMA_TIMEOUT_PROFILE = 1200           # profiler (large context)
OLLAMA_VISION_TIMEOUT  = 90             # moondream per frame

# Whisper STT
WHISPER_MODEL_SIZE  = "small"           # base | small | medium | large

# Parser anti-detect
SCROLL_PAUSE_MIN = 3.5
SCROLL_PAUSE_MAX = 6.0
```

---

## 🚀 Usage

### Option A: Streamlit Dashboard (Recommended)

```bash
source venv/bin/activate
streamlit run app.py
```

Open a browser: `http://localhost:8501`

**What's in the dashboard:**

| Tab | Function |
|---------|---------|
| 📄 **Dossier** | Full psychological report + PDF download button |
| 🕸️ **Social Graph** | @-mentions Graph or Interests Graph (toggle) |
| 💬 **AI Investigator** | RAG chat with the database — ask questions about the profile |
| 🖼️ **Evidence Base** | Grid of videos/frames with OCR text and transcription |
| 🔗 **DB Intersections** | Exact repost matches between profiles + semantics via Qwen |
| 📡 **Network Analysis** | Top-5 contacts + LLM relationship classification |
| 🌐 **Cross-Platform Search** | Check nickname on Instagram, YouTube, Reddit, X |

### Option B: CLI Pipeline

```bash
source venv/bin/activate

# Full pipeline (steps 1–6)
python run_pipeline.py --user TARGET_USERNAME --limit 100 --workers 3

# Only dossier generation from a ready DB
python run_pipeline.py --user TARGET_USERNAME --only-profiler
```

| Parameter | Default | Description |
|----------|------------|------|
| `--user` | — (required) | TikTok nickname without `@` |
| `--limit` | `100` | Max number of videos |
| `--workers` | `3` | Parallel AI workers |
| `--only-profiler` | `false` | Skip collection, just regenerate dossier |

### First Run — TikTok Authorization

On the first run, a browser window will appear:
1. Log in manually to TikTok
2. The script will save the session in `tiktok_cookies.json` (**locally, in `.gitignore`**)
3. Subsequent runs do not require manual login

---

## 🗃️ Utilities

### Old Database Migration

If you have databases from the `done_file/` folder (old version):

```bash
python migrate_done_file.py
```

The script copies databases to `databases/`, updates the schema, and extracts `@-mentions`.

### Retrospective Profiling

Regenerate dossier without parsing again:

```bash
python retro_profiler.py --db databases/osint_username.db
```

### Reset Analysis (keeping reposts)

```bash
python reset_db.py --user username
```

---

## 🚨 Troubleshooting

**`ollama: connection refused`**
```bash
ollama serve &
curl http://127.0.0.1:11434/api/tags
```

**`wkhtmltopdf: cannot connect to X server`** (headless server)
```bash
sudo apt install xvfb
Xvfb :99 -screen 0 1024x768x24 &
DISPLAY=:99 streamlit run app.py
```

**Selenium cannot find Chrome**
```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt --fix-broken install
```

**Interests Graph is empty**
> Run the full pipeline up to step 4 (sublimator). The graph is built based on the `analysis` table.

**OpenCV doesn't open video**
> `vision_manager_3.py` automatically switches to the `ffmpeg`-fallback. Make sure `ffmpeg` is installed.

---

## 🏗️ Data Architecture

```sql
-- reposts table: raw parser data
CREATE TABLE reposts (
    video_id   TEXT PRIMARY KEY,
    author     TEXT,
    url        TEXT,
    description TEXT,
    hashtags   TEXT,     -- JSON array of hashtags
    mentions   TEXT,     -- JSON array of @-mentions
    local_path TEXT,     -- path to downloaded MP4
    parsed_at  DATETIME
);

-- analysis table: AI analysis results
CREATE TABLE analysis (
    video_id       TEXT PRIMARY KEY,
    visual_context TEXT,  -- frame description from Moondream
    video_text     TEXT,  -- OCR text from EasyOCR
    audio_text     TEXT,  -- Whisper transcription
    interests      TEXT,  -- tags from Qwen2.5
    hobbies        TEXT,
    relations      TEXT,
    music_taste    TEXT,
    raw_result     TEXT   -- full LLM response
);
```

---

## ⚠️ Responsible Use

> This tool is intended **exclusively for legitimate OSINT investigations** — journalism, fact-checking, cybersecurity, and academic research.

- Analyze only **publicly available** data
- Comply with your country's legislation regarding personal data protection (e.g., GDPR)
- Store collected data responsibly — the `databases/` and `reports/` folders are blocked in `.gitignore` for a reason

---

## 📄 License

MIT License — see [LICENSE](LICENSE)
