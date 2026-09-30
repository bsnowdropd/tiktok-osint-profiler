# TikTok OSINT Profiler 🕵️‍♂️

> Автоматизований OSINT-конвеєр для цифрових розслідувань: **парсинг → аналіз відео → LLM-профайлінг → інтерактивний дашборд**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.64-red.svg)](https://streamlit.io)
[![Ollama](https://img.shields.io/badge/LLM-Ollama%20%2F%20Qwen2.5-green.svg)](https://ollama.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🧭 Що робить цей проєкт

Система збирає публічну активність TikTok-акаунту (репости), аналізує кожне відео через три незалежних канали (комп'ютерний зір, OCR, транскрипція аудіо), витягує семантичні патерни через локальну LLM та формує психологічне досьє. Всі результати доступні через веб-дашборд з інтерактивними графами.

```
@target_user
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 1 · tiktok_parser_cdp.py                                  │
│  Selenium CDP → збір метаданих репостів → SQLite (reposts)      │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 2 · vision_manager_3.py                                   │
│  Розбивка відео на кадри → OCR (EasyOCR) → Moondream (опис)    │
│  Fallback: ffmpeg якщо OpenCV не підтримує кодек               │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 3 · audio_manager.py                                      │
│  Whisper STT → транскрипція аудіодоріжок                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 4 · sublimator_cdp.py                                     │
│  Qwen2.5 → семантичні теги: інтереси / хобі / стосунки         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 5 · analyzer_cdp.py                                       │
│  Fallback: текстовий аналіз для відео без відеофайлів           │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  КРОК 6 · profiler_cdp.py                                       │
│  Агрегація тегів → TXT-звіт + PDF-досьє                        │
└─────────────────────────────────────────────────────────────────┘
                             │
                             ▼
                    databases/osint_<user>.db
                    reports/profile_<user>.txt
                    reports/profile_<user>.pdf
```

---

## 📁 Структура проєкту

```
analyzer/
├── app.py                    # Streamlit-дашборд (головний UI)
├── run_pipeline.py           # Оркестратор конвеєра (кроки 1–6)
│
├── tiktok_parser_cdp.py      # Крок 1: CDP-парсер репостів
├── vision_manager_3.py       # Крок 2: OCR + Moondream + ffmpeg fallback
├── audio_manager.py          # Крок 3: Whisper STT
├── sublimator_cdp.py         # Крок 4: Qwen2.5 семантичні теги
├── analyzer_cdp.py           # Крок 5: fallback текстовий аналіз
├── profiler_cdp.py           # Крок 6: генерація TXT/PDF-досьє
│
├── cross_analyzer.py         # Крос-аналіз спільних репостів + семантика
├── cross_platform_linker.py  # Пошук профілів на ін. платформах
├── external_osint.py         # Maigret + Instaloader розвідка
├── network_analyzer.py       # Аналіз мережі контактів (топ-5 + LLM)
├── retro_profiler.py         # Ретроспективний профайлінг із готової БД
├── migrate_done_file.py      # Міграція старих баз даних → databases/
├── reset_db.py               # Скидання / очищення БД
│
├── db_schema.py              # Єдина схема SQLite (ініціалізація + міграція)
├── db_manager.py             # Repository-патерн — єдина точка доступу до БД
├── config.py                 # Централізована конфігурація (Ollama, Whisper)
├── utils.py                  # Утиліти: sanitize_username, db_path_for
│
├── databases/                # SQLite бази (osint_<user>.db) — у .gitignore
├── reports/                  # TXT та PDF звіти — у .gitignore
└── downloaded_videos/        # Тимчасові MP4 (очищуються автоматично)
```

---

## 🖥️ Системні вимоги

| Компонент | Мінімум | Рекомендовано |
|-----------|---------|---------------|
| ОС | Ubuntu 20.04+ / Debian | Ubuntu 22.04 / 24.04 |
| Python | 3.10 | 3.11+ |
| RAM | 8 GB | 16+ GB |
| GPU VRAM | — | 8+ GB (Nvidia, для швидкого Whisper/Moondream) |
| Місце на диску | 10 GB | 50+ GB |
| Google Chrome | остання стабільна | — |
| Ollama | ≥ 0.3 | останній реліз |
| ffmpeg | будь-яка | — |

---

## 🔧 Встановлення

### 1. Системні залежності

```bash
sudo apt update && sudo apt install -y \
    python3-venv python3-pip git \
    ffmpeg libsm6 libxext6 \
    wkhtmltopdf google-chrome-stable
```

### 2. Клонування та середовище

```bash
git clone https://github.com/YOUR_USERNAME/tiktok-osint-profiler.git
cd tiktok-osint-profiler

python3 -m venv venv
source venv/bin/activate
```

### 3. Python-залежності

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

### 4. Ollama — локальні моделі

```bash
# Встановлення Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Завантаження моделей
ollama pull qwen2.5        # основна LLM (тегування, профайлінг, RAG)
ollama pull moondream      # мультимодальна (аналіз кадрів)
```

> **Важливо:** переконайтеся що `ollama serve` запущений перед стартом конвеєра

---

## ⚙️ Конфігурація

Усі параметри у [`config.py`](config.py):

```python
# Адреса та моделі Ollama
OLLAMA_URL          = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL        = "qwen2.5"         # текстовий аналіз
OLLAMA_VISION_MODEL = "moondream"       # аналіз зображень

# Тайм-аути (секунди)
OLLAMA_TIMEOUT_TAGS    = 60             # sublimator / analyzer
OLLAMA_TIMEOUT_PROFILE = 1200           # profiler (великий контекст)
OLLAMA_VISION_TIMEOUT  = 90             # moondream на один кадр

# Whisper STT
WHISPER_MODEL_SIZE  = "small"           # base | small | medium | large

# Антидетект парсера
SCROLL_PAUSE_MIN = 3.5
SCROLL_PAUSE_MAX = 6.0
```

---

## 🚀 Запуск

### Варіант A: Streamlit-дашборд (рекомендовано)

```bash
source venv/bin/activate
streamlit run app.py
```

Відкрийте браузер: `http://localhost:8501`

**Що є в дашборді:**

| Вкладка | Функція |
|---------|---------|
| 📄 **Досьє** | Повний психологічний звіт + кнопка завантаження PDF |
| 🕸️ **Соціальний граф** | Граф @-згадок або Граф інтересів (перемикач) |
| 💬 **ШІ-слідчий** | RAG-чат із базою — задавайте питання по профілю |
| 🖼️ **Доказова база** | Сітка відео/кадрів з OCR-текстом та транскрипцією |
| 🔗 **Перетини БД** | Точні збіги репостів між профілями + семантика через Qwen |
| 📡 **Мережевий аналіз** | Топ-5 контактів + LLM-класифікація зв'язків |
| 🌐 **Пошук на платформах** | Перевірка нікнейму на Instagram, YouTube, Reddit, X |

### Варіант B: CLI-конвеєр

```bash
source venv/bin/activate

# Повний конвеєр (кроки 1–6)
python run_pipeline.py --user TARGET_USERNAME --limit 100 --workers 3

# Лише генерація досьє з готової БД
python run_pipeline.py --user TARGET_USERNAME --only-profiler
```

| Параметр | За замовч. | Опис |
|----------|------------|------|
| `--user` | — (обов'язк.) | Нікнейм TikTok без `@` |
| `--limit` | `100` | Макс. кількість відео |
| `--workers` | `3` | Паралельних потоків ШІ |
| `--only-profiler` | `false` | Пропустити збір, лише перегенерувати досьє |

### Перший запуск — авторизація TikTok

При першому запуску з'явиться вікно браузера:
1. Авторизуйтесь вручну у TikTok
2. Скрипт збереже сесію у `tiktok_cookies.json` (**локально, у `.gitignore`**)
3. Наступні запуски не потребують ручного входу

---

## 🗃️ Утиліти

### Міграція старих баз даних

Якщо у вас є бази з папки `done_file/` (стара версія):

```bash
python migrate_done_file.py
```

Скрипт копіює бази в `databases/`, оновлює схему, витягує `@-згадки`.

### Ретроспективний профайлінг

Перегенерувати досьє без повторного парсингу:

```bash
python retro_profiler.py --db databases/osint_username.db
```

### Скидання аналізу (зберігши репости)

```bash
python reset_db.py --user username
```

---

## 🚨 Вирішення проблем

**`ollama: connection refused`**
```bash
ollama serve &
curl http://127.0.0.1:11434/api/tags
```

**`wkhtmltopdf: cannot connect to X server`** (headless-сервер)
```bash
sudo apt install xvfb
Xvfb :99 -screen 0 1024x768x24 &
DISPLAY=:99 streamlit run app.py
```

**Selenium не знаходить Chrome**
```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt --fix-broken install
```

**Граф інтересів порожній**
> Запустіть повний конвеєр до кроку 4 (sublimator). Граф будується на основі таблиці `analysis`.

**OpenCV не відкриває відео**
> `vision_manager_3.py` автоматично перемикається на `ffmpeg`-fallback. Переконайтесь що `ffmpeg` встановлений.

---

## 🏗️ Архітектура даних

```sql
-- Таблиця reposts: сирі дані парсера
CREATE TABLE reposts (
    video_id   TEXT PRIMARY KEY,
    author     TEXT,
    url        TEXT,
    description TEXT,
    hashtags   TEXT,     -- JSON-масив хештегів
    mentions   TEXT,     -- JSON-масив @-згадок
    local_path TEXT,     -- шлях до завантаженого MP4
    parsed_at  DATETIME
);

-- Таблиця analysis: результати ШІ-аналізу
CREATE TABLE analysis (
    video_id       TEXT PRIMARY KEY,
    visual_context TEXT,  -- опис кадрів від Moondream
    video_text     TEXT,  -- OCR-текст з EasyOCR
    audio_text     TEXT,  -- транскрипція Whisper
    interests      TEXT,  -- теги від Qwen2.5
    hobbies        TEXT,
    relations      TEXT,
    music_taste    TEXT,
    raw_result     TEXT   -- повна відповідь LLM
);
```

---

## ⚠️ Відповідальне використання

> Цей інструмент призначений **виключно для легітимних OSINT-розслідувань** — журналістики, верифікації інформації, кібербезпеки та академічних досліджень.

- Аналізуйте лише **публічно доступні** дані
- Дотримуйтесь законодавства вашої країни щодо захисту персональних даних (GDPR, Закон України про захист персональних даних)
- Зберігайте зібрані дані відповідально — папки `databases/` та `reports/` заблоковані у `.gitignore` не випадково

---

## 📄 Ліцензія

MIT License — дивіться [LICENSE](LICENSE)
