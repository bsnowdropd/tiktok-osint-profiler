"""
app.py — Streamlit OSINT Dashboard v2.0

Нові можливості (рефакторинг):
  - Миттєве перемикання між справами через Sidebar (як ChatGPT-чат)
  - Параметри ШІ (temperature, focus) передаються у ВСІХ запитах до Ollama
  - Повний контекст БД у RAG-чаті: description + OCR + audio + raw_result
  - TAB 2: перемикач "Зв'язки / Граф інтересів" (Semantic Interest Graph)
  - TAB 6: Мережевий аналіз (network_analyzer.py) — LLM-класифікація контактів
  - TAB 7: Пошук на інших платформах (cross_platform_linker.py)
  - check_same_thread=False на всіх sqlite3.connect() — безпека потоків
"""

import glob
import json
import os
import re
import sqlite3
import subprocess
import io
from collections import Counter

import networkx as nx
import pandas as pd
import requests
import streamlit as st
from pyvis.network import Network

import config
from db_manager import DatabaseManager
from utils import db_path_for, sanitize_username
from cross_analyzer import (
    find_shared_reposts,
    result_to_dataframe,
    overlaps_to_dataframe,
)

# ══════════════════════════════════════════════════════════════════════════════
# СТОРІНКА
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="TikTok OSINT Profiler",
    page_icon="🕵️‍♂️",
    layout="wide",
)
st.title("🕵️‍♂️ TikTok OSINT Profiler")
st.markdown(
    "Повна платформа для цифрових розслідувань: парсинг → аналіз → досьє → соціальний граф."
)

_project_dir = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# ХЕЛПЕР: підключення до SQLite
# ══════════════════════════════════════════════════════════════════════════════
def _db(db_path: str) -> sqlite3.Connection:
    """Відкриває SQLite-з'єднання. check_same_thread=False для Streamlit-потоків."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# МОДУЛЬ 1: Архів справ (Sidebar) — миттєве перемикання
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("📂 Архів справ")

found_dbs = sorted(glob.glob(os.path.join(_project_dir, "databases", "osint_*.db")))
available_profiles = [
    os.path.splitext(os.path.basename(p))[0].replace("osint_", "")
    for p in found_dbs
]

# Ініціалізація дефолтного активного профілю
if "active_username" not in st.session_state and available_profiles:
    st.session_state["active_username"] = available_profiles[0]
    st.session_state["profile_loaded"] = True

if available_profiles:
    # Синхронізуємо вибраний індекс із session_state щоб selectbox не скидався
    _current = st.session_state.get("active_username", available_profiles[0])
    _cur_idx = available_profiles.index(_current) if _current in available_profiles else 0

    archive_profile = st.sidebar.selectbox(
        "Оберіть профіль",
        options=available_profiles,
        index=_cur_idx,
        help="Всі локальні бази OSINT у директорії проєкту",
    )
    if st.sidebar.button("📂 Завантажити архівну справу", use_container_width=True):
        # Миттєве перемикання: зберігаємо та ребутимо
        st.session_state["active_username"] = archive_profile
        st.session_state["profile_loaded"] = True
        st.session_state["messages"] = []  # скидаємо чат при зміні справи
        st.rerun()
else:
    st.sidebar.info("Баз даних не знайдено. Запустіть аналіз для першого профілю.")

st.sidebar.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# МОДУЛЬ 2: Експорт даних (Sidebar)
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("📥 Експорт даних")

_active = st.session_state.get("active_username", "")
if _active:
    _export_db = db_path_for(_active)
    if os.path.exists(_export_db):
        try:
            _conn_exp = _db(_export_db)
            _df_rep = pd.read_sql_query("SELECT * FROM reposts", _conn_exp)
            _conn_exp.close()
            csv_bytes = _df_rep.to_csv(index=False, encoding="utf-8").encode("utf-8")
            st.sidebar.download_button(
                label="⬇️ Завантажити reposts.csv",
                data=csv_bytes,
                file_name=f"reposts_{_active}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        except Exception:
            st.sidebar.info("Дані недоступні для експорту.")
    else:
        st.sidebar.info("Оберіть або завантажте профіль для експорту.")
else:
    st.sidebar.info("Запустіть аналіз або оберіть архівну справу.")

st.sidebar.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# МОДУЛЬ 3: Налаштування ШІ (Sidebar) — підключено до всіх запитів
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("⚙️ Налаштування ШІ‑аналітика")

temperature = st.sidebar.slider(
    "Температура (creativity)",
    min_value=0.1,
    max_value=0.9,
    value=st.session_state.get("temperature", 0.3),
    step=0.1,
    help="Впливає на варіативність відповідей Qwen2.5 у всіх запитах",
)

focus_options = [
    "Базовий психологічний портрет",
    "Аналіз вразливостей та тригерів",
    "Оцінка соціального статусу",
]
focus_option = st.sidebar.selectbox(
    "Фокус аналізу",
    options=focus_options,
    index=focus_options.index(st.session_state.get("focus_option", focus_options[0]))
    if st.session_state.get("focus_option") in focus_options else 0,
    help="Визначає системний промпт у RAG-чаті та профайлері",
)

# Зберігаємо у session_state — не зникає при перемиканні вкладок
st.session_state["temperature"] = temperature
st.session_state["focus_option"] = focus_option

_FOCUS_PROMPTS: dict[str, str] = {
    "Базовий психологічний портрет": (
        "Ти — OSINT-аналітик. Надавай збалансовані психологічні висновки "
        "на основі даних TikTok. Відповідай коротко та конкретно."
    ),
    "Аналіз вразливостей та тригерів": (
        "Ти — профайлер соціальної інженерії. Зосередься на психологічних вразливостях, "
        "тригерах довіри та відторгнення об'єкта на основі його TikTok-поведінки."
    ),
    "Оцінка соціального статусу": (
        "Ти — соціолог та OSINT-аналітик. Оцінюй соціальний статус, кар'єрний вектор, "
        "мережу контактів та вплив об'єкта, спираючись на дані TikTok."
    ),
}
system_prompt = _FOCUS_PROMPTS.get(focus_option, _FOCUS_PROMPTS[focus_options[0]])

# Єдиний словник options для всіх запитів до Ollama
_ollama_options: dict = {"temperature": temperature}

st.sidebar.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# ХЕЛПЕР: виклик Ollama з правильними налаштуваннями
# ══════════════════════════════════════════════════════════════════════════════
def _ask_ollama(prompt: str, timeout: int = 120, model: str | None = None) -> str:
    """Відправляє запит до Ollama. Використовує глобальні _ollama_options."""
    try:
        res = requests.post(
            config.OLLAMA_URL,
            json={
                "model": model or config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": _ollama_options,
            },
            timeout=timeout,
        )
        if res.status_code == 200:
            return res.json().get("response", "").strip()
        return f"Помилка Ollama: HTTP {res.status_code}"
    except requests.exceptions.Timeout:
        return f"⏱️ Ollama не відповіла за {timeout}с. Перевірте сервер."
    except Exception as exc:
        return f"Помилка: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# ГОЛОВНА ПАНЕЛЬ: Налаштування нового аналізу
# ══════════════════════════════════════════════════════════════════════════════
with st.container():
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        username_input = st.text_input(
            "TikTok Username (без @)",
            placeholder="Наприклад: username123",
        )
    with col2:
        limit = st.slider("Ліміт відео:", min_value=10, max_value=300, value=100, step=10)
    with col3:
        workers = st.slider("Потоки ШІ:", min_value=1, max_value=10, value=3)

with st.container():
    st.markdown("### ⚙️ Режим роботи")
    only_profiler = st.checkbox(
        "⏩ Пропустити збір даних (тільки генерація досьє)",
        help="Якщо база вже зібрана — лише перегенерує TXT/PDF.",
    )

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК КОНВЕЄРА
# ══════════════════════════════════════════════════════════════════════════════
if st.button("🚀 Запустити аналіз", type="primary", use_container_width=True):
    if not username_input:
        st.warning("⚠️ Введіть нікнейм цілі.")
    else:
        try:
            safe_user = sanitize_username(username_input)
        except ValueError:
            st.error("❌ Некоректний нікнейм. Використовуйте лише літери, цифри, _ та -")
            st.stop()

        progress_bar = st.progress(0)
        status_text = st.empty()

        with st.expander("🛠 Живі логи терміналу", expanded=True):
            log_container = st.empty()

        status_text.info(f"🚀 Запуск конвеєра для @{safe_user}...")

        cmd = [
            "python", "-u", "run_pipeline.py",
            "--user", safe_user,
            "--limit", str(limit),
            "--workers", str(workers),
        ]
        if only_profiler:
            cmd.append("--only-profiler")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=_project_dir,
            )

            log_history: list[str] = []
            for line in process.stdout:
                line_clean = line.strip()
                if not line_clean:
                    continue

                log_history.append(line_clean)
                log_container.code("\n".join(log_history[-50:]), language="bash")

                step_m = re.search(r"\[КРОК (\d+)/(\d+)\]", line_clean)
                if step_m:
                    progress_bar.progress(int(step_m.group(1)) / int(step_m.group(2)))
                    status_text.info(f"🔄 {line_clean.split(']')[-1].strip()}")

                vid_m = re.search(r"\[(\d+)/(\d+)\]", line_clean)
                if vid_m and "КРОК" not in line_clean:
                    c, t = int(vid_m.group(1)), int(vid_m.group(2))
                    if t > 0:
                        status_text.info(f"⏳ Обробка відео: {c} із {t}")

            process.wait()

            if process.returncode == 0:
                progress_bar.progress(1.0)
                status_text.success(f"✅ Роботу з @{safe_user} успішно завершено!")
                st.session_state["active_username"] = safe_user
                st.session_state["profile_loaded"] = True
                st.session_state["messages"] = []
                st.rerun()
            else:
                status_text.error("❌ Помилка виконання конвеєра. Перевірте логи.")

        except Exception as exc:
            st.error(f"Критична помилка запуску: {exc}")

# ══════════════════════════════════════════════════════════════════════════════
# ВКЛАДКИ РЕЗУЛЬТАТІВ
# ══════════════════════════════════════════════════════════════════════════════
username = st.session_state.get("active_username", "")

if username and st.session_state.get("profile_loaded"):
    db_path = db_path_for(username)

    if not os.path.exists(db_path):
        st.warning(f"База даних для @{username} не знайдена. Запустіть аналіз.")
        st.stop()

    # ── Метрики ──────────────────────────────────────────────────────────────
    try:
        _mc = _db(db_path)
        total_reposts = _mc.execute("SELECT COUNT(*) FROM reposts").fetchone()[0]
        total_analyzed = _mc.execute(
            "SELECT COUNT(*) FROM analysis WHERE interests IS NOT NULL AND interests != ''"
        ).fetchone()[0]
        total_mentions = _mc.execute(
            "SELECT COUNT(*) FROM reposts WHERE mentions IS NOT NULL AND mentions != '[]'"
        ).fetchone()[0]
        _mc.close()

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("📹 Репостів у базі", total_reposts)
        m2.metric("🧠 Проаналізовано", total_analyzed)
        m3.metric("🔗 З @-згадками", total_mentions)
        m4.metric("👤 Активний профіль", f"@{username}")
    except Exception:
        pass

    st.markdown("---")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📄 Досьє",
        "🕸️ Соціальний граф",
        "💬 ШІ‑слідчий",
        "🖼️ Доказова база",
        "🔗 Перетини БД",
        "📡 Мережевий аналіз",
        "🌐 Пошук на платформах",
    ])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1: Досьє
    # ─────────────────────────────────────────────────────────────────────────
    with tab1:
        txt_file = os.path.join(_project_dir, "reports", f"profile_{username}.txt")
        pdf_file = os.path.join(_project_dir, "reports", f"profile_{username}.pdf")

        res_col1, res_col2 = st.columns([3, 1])
        with res_col1:
            st.subheader("📄 Психологічне досьє")
            if os.path.exists(txt_file):
                st.markdown(open(txt_file, encoding="utf-8").read())
            else:
                st.info("Текстовий звіт ще не згенеровано. Запустіть конвеєр.")

        with res_col2:
            st.subheader("📥 Експорт")
            if os.path.exists(pdf_file):
                with open(pdf_file, "rb") as fh:
                    st.download_button(
                        label="💾 Завантажити PDF",
                        data=fh,
                        file_name=f"profile_{username}.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
            else:
                st.warning("PDF не створено.")

            st.markdown("---")
            st.markdown("**♻️ Перегенерувати досьє**")
            st.caption(
                f"Фокус: **{focus_option}**  \nТемпература: **{temperature}**"
            )
            if st.button("🔄 Перегенерувати", use_container_width=True):
                with st.spinner("Генерація досьє через Qwen2.5..."):
                    try:
                        import profiler_cdp
                        profiler_cdp.main(
                            db_path=db_path,
                            output_file=txt_file,
                            temperature=temperature,
                        )
                        st.success("Досьє оновлено!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Помилка: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2: Соціальний граф + Граф інтересів
    # ─────────────────────────────────────────────────────────────────────────
    with tab2:
        # Перемикач режиму графа
        graph_mode = st.radio(
            "Режим графа",
            options=["🔗 Зв'язки (Меншени)", "🧠 Граф інтересів"],
            horizontal=True,
            key="graph_mode_radio",
        )

        st.markdown("---")

        # ── Режим 1: Граф згадок ─────────────────────────────────────────────
        if graph_mode == "🔗 Зв'язки (Меншени)":
            st.subheader("🕸️ Соціальний граф згадок")

            g_col1, g_col2, g_col3 = st.columns([1, 1, 1])
            with g_col1:
                graph_physics = st.toggle("⚛️ Фізика вузлів", value=True)
            with g_col2:
                min_edge_weight = st.slider(
                    "Мін. кількість згадок", min_value=1, max_value=10, value=1,
                )
            with g_col3:
                show_isolated = st.checkbox("Показати ізольовані вузли", value=False)

            try:
                _gc_mgr = DatabaseManager(db_path)
                _gc_rows = _gc_mgr.get_reposts_with_mentions()
                rows = [(r["author"], r["mentions"]) for r in _gc_rows]

                G_raw = nx.MultiDiGraph()
                for author, mentions_json in rows:
                    try:
                        for m in json.loads(mentions_json):
                            if m and m.strip():
                                G_raw.add_edge(author.strip(), m.strip())
                    except (json.JSONDecodeError, TypeError):
                        continue

                G = nx.DiGraph()
                for u, v, _ in G_raw.edges(data=True):
                    if G.has_edge(u, v):
                        G[u][v]["weight"] += 1
                    else:
                        G.add_edge(u, v, weight=1)

                if min_edge_weight > 1:
                    edges_to_remove = [
                        (u, v) for u, v, d in G.edges(data=True)
                        if d.get("weight", 1) < min_edge_weight
                    ]
                    G.remove_edges_from(edges_to_remove)
                    if not show_isolated:
                        G.remove_nodes_from(list(nx.isolates(G)))

                if G.number_of_edges() > 0 or (show_isolated and G.number_of_nodes() > 0):
                    in_deg  = dict(G.in_degree())
                    out_deg = dict(G.out_degree())
                    top_mentioned = sorted(in_deg.items(), key=lambda x: x[1], reverse=True)[:5]

                    mg1, mg2, mg3 = st.columns(3)
                    mg1.metric("🔵 Вершин у графі", G.number_of_nodes())
                    mg2.metric("🔗 Зв'язків", G.number_of_edges())
                    mg3.metric(
                        "👑 Найзгадуваніший",
                        top_mentioned[0][0] if top_mentioned else "—",
                        f"{top_mentioned[0][1]} згадок" if top_mentioned else "",
                    )

                    st.markdown("---")
                    net = Network(
                        height="650px", width="100%",
                        notebook=False, directed=True,
                        bgcolor="#1a1a2e", font_color="#e0e0e0",
                    )
                    net.set_options("""
                    {
                      "physics": {
                        "enabled": true,
                        "barnesHut": {
                          "gravitationalConstant": -8000,
                          "centralGravity": 0.3,
                          "springLength": 120,
                          "springConstant": 0.04,
                          "damping": 0.09
                        }
                      },
                      "edges": {
                        "arrows": { "to": { "enabled": true, "scaleFactor": 0.6 } },
                        "smooth": { "type": "curvedCW", "roundness": 0.2 },
                        "color": { "inherit": false, "color": "#4a6fa5", "opacity": 0.7 }
                      },
                      "nodes": { "font": { "size": 13, "face": "Arial" }, "borderWidth": 2 },
                      "interaction": { "hover": true, "tooltipDelay": 200, "navigationButtons": true }
                    }
                    """)

                    max_in = max(in_deg.values(), default=1) or 1
                    for node in G.nodes():
                        deg_in  = in_deg.get(node, 0)
                        deg_out = out_deg.get(node, 0)
                        size = 15 + (deg_in / max_in) * 45
                        if node == username:
                            color = {"background": "#e63946", "border": "#ff6b6b",
                                     "highlight": {"background": "#ff4757", "border": "#ff6b81"}}
                            shape, label = "star", f"★ @{node}"
                        elif deg_in >= 3:
                            color = {"background": "#f4a261", "border": "#e76f51",
                                     "highlight": {"background": "#ffb347", "border": "#ff8c00"}}
                            shape, label = "dot", f"@{node}\n({deg_in}↙)"
                        elif deg_out > 0 and deg_in == 0:
                            color = {"background": "#457b9d", "border": "#1d3557",
                                     "highlight": {"background": "#6aafe6", "border": "#2980b9"}}
                            shape, label = "dot", f"@{node}"
                        else:
                            color = {"background": "#2a9d8f", "border": "#264653",
                                     "highlight": {"background": "#3dbfb0", "border": "#1a7268"}}
                            shape, label = "dot", f"@{node}"

                        net.add_node(
                            node, label=label,
                            title=f"@{node}\nОтримав: {deg_in}\nЗгадує: {deg_out}",
                            size=size, color=color, shape=shape,
                        )

                    max_w = max((d.get("weight", 1) for _, _, d in G.edges(data=True)), default=1)
                    for u, v, data in G.edges(data=True):
                        w = data.get("weight", 1)
                        net.add_edge(u, v, width=1 + (w / max_w) * 5, title=f"{w} згадок")

                    _cross = st.session_state.get("_cross_result")
                    if _cross is not None and _cross.overlaps:
                        for (u_a, u_b), overlap_count in _cross.overlaps.items():
                            for nd in (u_a, u_b):
                                if nd not in G.nodes():
                                    net.add_node(
                                        nd, label=f"📁 @{nd}",
                                        title=f"@{nd}\n(інший профіль OSINT)",
                                        size=20,
                                        color={"background": "#9b5de5", "border": "#6a0dad",
                                               "highlight": {"background": "#c77dff", "border": "#7b2d8b"}},
                                        shape="database",
                                    )
                            net.add_edge(
                                u_a, u_b,
                                width=2 + min(overlap_count, 8),
                                title=f"Спільний контент: {overlap_count} відео",
                                color="#9b5de5", dashes=True,
                            )

                    if not graph_physics:
                        net.toggle_physics(False)

                    st.components.v1.html(net.generate_html(), height=670)

                    st.markdown("""
                    <div style="display:flex; gap:24px; padding:8px 0; flex-wrap:wrap; font-size:13px;">
                      <span>⭐ <b style="color:#e63946">Ціль</b></span>
                      <span>🟠 <b style="color:#f4a261">Популярний</b> (≥3 згадок)</span>
                      <span>🔵 <b style="color:#457b9d">Активний</b> (лише згадує)</span>
                      <span>🟢 <b style="color:#2a9d8f">Звичайний</b></span>
                      <span>🟣 <b style="color:#9b5de5">Пунктир</b> = Спільний контент між профілями</span>
                    </div>
                    """, unsafe_allow_html=True)

                    with st.expander("📊 Статистика вузлів", expanded=False):
                        tc1, tc2 = st.columns(2)
                        with tc1:
                            st.markdown("**👑 Топ-10 згадуваних**")
                            st.dataframe(
                                pd.DataFrame(
                                    sorted(in_deg.items(), key=lambda x: x[1], reverse=True)[:10],
                                    columns=["Нікнейм", "Отримано згадок"],
                                ),
                                use_container_width=True, hide_index=True,
                            )
                        with tc2:
                            st.markdown("**🗣️ Топ-10 активних**")
                            st.dataframe(
                                pd.DataFrame(
                                    sorted(out_deg.items(), key=lambda x: x[1], reverse=True)[:10],
                                    columns=["Нікнейм", "Згадує інших"],
                                ),
                                use_container_width=True, hide_index=True,
                            )
                else:
                    st.info("@-згадок не знайдено. Зменшіть мінімальний поріг.")
            except Exception as exc:
                st.error(f"Помилка побудови графа: {exc}")

        # ── Режим 2: Граф інтересів ──────────────────────────────────────────
        else:
            st.subheader("🧠 Граф інтересів")
            st.caption(
                "Центральний вузол — об'єкт аналізу. "
                "Навколишні вузли — теми/інтереси, витягнуті з аналізу відео. "
                "Товщина ребра — частота згадування теми."
            )

            int_col1, int_col2 = st.columns([1, 3])
            with int_col1:
                min_freq = st.number_input(
                    "Мін. частота теми", min_value=1, max_value=20, value=2,
                    help="Показувати лише теми, що зустрічаються ≥N разів",
                )
                int_physics = st.toggle("⚛️ Фізика", value=True, key="int_phys")

            with int_col2:
                try:
                    _ic = _db(db_path)
                    # Витягуємо всі інтереси, хобі, music_taste та hashtags
                    interest_rows = _ic.execute(
                        """
                        SELECT a.interests, a.hobbies, a.music_taste, r.hashtags
                        FROM reposts r
                        LEFT JOIN analysis a ON r.video_id = a.video_id
                        WHERE a.interests IS NOT NULL OR a.hobbies IS NOT NULL
                        """
                    ).fetchall()
                    _ic.close()

                    # Підрахунок частоти тем
                    topic_counter: Counter = Counter()
                    for row in interest_rows:
                        for field in row:
                            if not field:
                                continue
                            # Парсимо: може бути JSON-список або рядок через кому
                            try:
                                items = json.loads(field)
                                if isinstance(items, list):
                                    for item in items:
                                        t = str(item).strip().lower()
                                        if t and len(t) > 2:
                                            topic_counter[t] += 1
                                    continue
                            except (json.JSONDecodeError, TypeError):
                                pass
                            for item in str(field).split(","):
                                t = item.strip().strip('"\'[]').lower()
                                if t and len(t) > 2:
                                    topic_counter[t] += 1

                    # Фільтруємо за мінімальною частотою
                    filtered_topics = {
                        k: v for k, v in topic_counter.items() if v >= min_freq
                    }

                    if not filtered_topics:
                        st.info(
                            "Недостатньо даних для графа інтересів. "
                            "Зменшіть мін. частоту або запустіть повний конвеєр (кроки 4–5)."
                        )
                    else:
                        # Будуємо граф NetworkX
                        IG = nx.Graph()
                        IG.add_node(username, node_type="user")
                        for topic, freq in filtered_topics.items():
                            IG.add_node(topic, node_type="topic", freq=freq)
                            IG.add_edge(username, topic, weight=freq)

                        # Будуємо PyVis
                        inet = Network(
                            height="650px", width="100%",
                            notebook=False, directed=False,
                            bgcolor="#0f1923", font_color="#e0e0e0",
                        )
                        inet.set_options("""
                        {
                          "physics": {
                            "enabled": true,
                            "repulsion": {
                              "centralGravity": 0.2,
                              "springLength": 200,
                              "nodeDistance": 180
                            },
                            "solver": "repulsion"
                          },
                          "edges": {
                            "smooth": { "type": "continuous" },
                            "color": { "inherit": false, "color": "#3d9970", "opacity": 0.6 }
                          },
                          "nodes": { "font": { "size": 14, "face": "Arial" }, "borderWidth": 2 },
                          "interaction": { "hover": true, "navigationButtons": true }
                        }
                        """)

                        max_freq = max(filtered_topics.values(), default=1)

                        # Центральний вузол (користувач)
                        inet.add_node(
                            username,
                            label=f"★ @{username}",
                            title=f"Об'єкт аналізу\nТем у базі: {len(filtered_topics)}",
                            size=50,
                            color={"background": "#e63946", "border": "#ff6b6b",
                                   "highlight": {"background": "#ff4757", "border": "#ff6b81"}},
                            shape="star",
                        )

                        # Вузли-інтереси (колір за частотою: від синього до помаранчевого)
                        for topic, freq in sorted(filtered_topics.items(), key=lambda x: x[1], reverse=True):
                            ratio = freq / max_freq
                            # Інтерполяція кольору: синій → жовтий → помаранчевий
                            if ratio > 0.6:
                                bg_color = "#f4a261"
                            elif ratio > 0.3:
                                bg_color = "#e9c46a"
                            else:
                                bg_color = "#457b9d"

                            size = 12 + ratio * 35
                            inet.add_node(
                                topic,
                                label=f"{topic}\n({freq}×)",
                                title=f"Тема: {topic}\nЗустрічається: {freq} разів",
                                size=size,
                                color={"background": bg_color, "border": "#264653",
                                       "highlight": {"background": "#ffb347", "border": "#e76f51"}},
                                shape="ellipse",
                            )
                            inet.add_edge(
                                username, topic,
                                width=1 + ratio * 6,
                                title=f"{freq} згадок",
                            )

                        if not int_physics:
                            inet.toggle_physics(False)

                        st.components.v1.html(inet.generate_html(), height=670)

                        # Топ-таблиця тем
                        with st.expander("📊 Топ-20 інтересів/тем", expanded=False):
                            top_topics = sorted(
                                filtered_topics.items(), key=lambda x: x[1], reverse=True
                            )[:20]
                            st.dataframe(
                                pd.DataFrame(top_topics, columns=["Тема / Інтерес", "Частота"]),
                                use_container_width=True, hide_index=True,
                            )

                except Exception as exc:
                    st.error(f"Помилка побудови графа інтересів: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 3: ШІ-слідчий (RAG-чат) — повний контекст БД
    # ─────────────────────────────────────────────────────────────────────────
    with tab3:
        st.subheader("💬 ШІ‑слідчий")
        st.caption(f"Фокус: **{focus_option}** | Температура: **{temperature}**")

        if "messages" not in st.session_state:
            st.session_state.messages = []

        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        if st.button("🗑️ Очистити історію чату", key="clear_chat"):
            st.session_state.messages = []
            st.rerun()

        user_prompt = st.chat_input("Введіть запит до бази досьє...")
        if user_prompt:
            st.session_state.messages.append({"role": "user", "content": user_prompt})

            # ── Максимальний контекст із БД (КРОК 2 задачі) ─────────────────
            context = ""
            try:
                _cc = _db(db_path)
                ctx_rows = _cc.execute(
                    """
                    SELECT
                        r.author,
                        r.description,
                        r.hashtags,
                        a.interests,
                        a.hobbies,
                        a.relations,
                        a.music_taste,
                        a.video_text,
                        a.audio_text,
                        a.raw_result
                    FROM reposts r
                    LEFT JOIN analysis a ON r.video_id = a.video_id
                    WHERE a.interests IS NOT NULL OR a.video_text IS NOT NULL OR a.audio_text IS NOT NULL
                    ORDER BY r.parsed_at DESC
                    LIMIT 60
                    """
                ).fetchall()
                _cc.close()

                context_parts = []
                for r in ctx_rows:
                    parts = [f"[{r[0]}]"]
                    if r[1]: parts.append(f"опис:{r[1]}")
                    if r[2] and r[2] != "[]": parts.append(f"теги:{r[2]}")
                    if r[3]: parts.append(f"інтереси:{r[3]}")
                    if r[4]: parts.append(f"хобі:{r[4]}")
                    if r[5]: parts.append(f"стосунки:{r[5]}")
                    if r[6]: parts.append(f"музика:{r[6]}")
                    if r[7] and r[7] not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                        parts.append(f"OCR:{r[7]}")
                    if r[8] and r[8].strip(): parts.append(f"аудіо:{r[8]}")
                    if r[9]: parts.append(f"аналіз:{r[9][:200]}")
                    context_parts.append(" | ".join(parts))

                context = "\n".join(context_parts)
            except Exception as e:
                context = f"Контекст недоступний: {e}"

            full_prompt = (
                f"{system_prompt}\n\n"
                f"Контекст (аналіз TikTok-активності @{username}):\n"
                f"{context[:25000]}\n\n"
                f"Запит слідчого: {user_prompt}"
            )

            with st.chat_message("assistant"):
                with st.spinner("Qwen2.5 аналізує..."):
                    answer = _ask_ollama(full_prompt, timeout=120)
                    st.write(answer)
                    st.session_state.messages.append({"role": "assistant", "content": answer})

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 4: Доказова база
    # ─────────────────────────────────────────────────────────────────────────
    with tab4:
        st.subheader("🖼️ Доказова база")

        ev_col1, ev_col2 = st.columns([2, 1])
        with ev_col1:
            ev_limit = st.slider("Відео на сторінку", 6, 60, 30, 6, key="ev_limit")
        with ev_col2:
            ev_filter = st.selectbox(
                "Фільтр",
                ["Всі", "З OCR-текстом", "З аудіотранскрипцією", "З візуальним описом"],
                key="ev_filter",
            )

        try:
            _ec = _db(db_path)
            _where = {
                "Всі": "a.visual_context IS NOT NULL OR a.video_text IS NOT NULL OR a.audio_text IS NOT NULL",
                "З OCR-текстом": "a.video_text IS NOT NULL AND a.video_text NOT IN ('', 'ERROR_NO_FILE', 'ERROR_CANT_OPEN')",
                "З аудіотранскрипцією": "a.audio_text IS NOT NULL AND a.audio_text != ''",
                "З візуальним описом": "a.visual_context IS NOT NULL AND a.visual_context NOT IN ('', 'ERROR_NO_FILE', 'ERROR_CANT_OPEN')",
            }.get(ev_filter, "1=1")

            evidence_rows = _ec.execute(
                f"""
                SELECT r.video_id, r.author, r.url,
                       a.visual_context, a.video_text, a.audio_text
                FROM reposts r
                LEFT JOIN analysis a ON r.video_id = a.video_id
                WHERE {_where}
                ORDER BY r.parsed_at DESC
                LIMIT ?
                """,
                (ev_limit,),
            ).fetchall()
            _ec.close()
        except Exception as exc:
            st.error(f"Помилка читання БД: {exc}")
            evidence_rows = []

        if not evidence_rows:
            st.info("Доказова база порожня. Запустіть повний конвеєр (кроки 2–3).")
        else:
            cols = st.columns(3)
            for idx, row in enumerate(evidence_rows):
                v_id, author, url, visual, ocr_text, audio_text = row
                col = cols[idx % 3]

                with col:
                    video_path = os.path.join(_project_dir, "downloaded_videos", f"{v_id}.mp4")
                    frame_path = os.path.join(_project_dir, "downloaded_videos", f"{v_id}.jpg")

                    if os.path.exists(video_path):
                        st.video(video_path)
                    elif os.path.exists(frame_path):
                        st.image(frame_path, use_container_width=True)
                    else:
                        st.markdown(
                            f"[🎬 Відкрити у TikTok]({url})" if url else f"`{v_id}`"
                        )

                    with st.expander(f"@{author} — {v_id}", expanded=False):
                        if visual and visual not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                            st.markdown(f"**🎥 Візуальний опис:**\n{visual}")
                        if ocr_text and ocr_text not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                            st.markdown(f"**📝 OCR-текст:**\n`{ocr_text}`")
                        if audio_text and audio_text.strip():
                            st.markdown(f"**🎙️ Транскрипція:**\n{audio_text}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 5: Перетини БД
    # ─────────────────────────────────────────────────────────────────────────
    with tab5:
        st.subheader("🔗 Перетини між базами даних")
        st.caption(
            "Знаходить відео, які репостнули одночасно двоє або більше "
            "досліджуваних цілей, а також семантичні перетини їхніх інтересів."
        )

        tab_exact, tab_semantic = st.tabs(["Спільні відео (Точні збіги)", "Спільні інтереси (Семантика)"])

        # ── Хелпер таблиці (AgGrid / fallback) ───────────────────────────────
        def _render_table(df: pd.DataFrame, key: str, page_size: int = 50) -> None:
            try:
                from st_aggrid import AgGrid, GridOptionsBuilder, GridUpdateMode
                gb = GridOptionsBuilder.from_dataframe(df)
                gb.configure_default_column(filterable=True, sortable=True, resizable=True, editable=False)
                gb.configure_pagination(enabled=True, paginationAutoPageSize=False, paginationPageSize=page_size)
                gb.configure_side_bar(filters_panel=True)
                go = gb.build()
                AgGrid(df, gridOptions=go, update_mode=GridUpdateMode.NO_UPDATE,
                       fit_columns_on_grid_load=True, theme="streamlit", key=key)
            except ImportError:
                total_pages = max(1, (len(df) - 1) // page_size + 1)
                page_key = f"_page_{key}"
                if page_key not in st.session_state:
                    st.session_state[page_key] = 0
                pg_c1, pg_c2, pg_c3 = st.columns([1, 3, 1])
                with pg_c1:
                    if st.button("◀", key=f"prev_{key}", disabled=st.session_state[page_key] == 0):
                        st.session_state[page_key] -= 1
                with pg_c2:
                    st.caption(f"Стор. {st.session_state[page_key]+1}/{total_pages} ({len(df)} рядків)")
                with pg_c3:
                    if st.button("▶", key=f"next_{key}", disabled=st.session_state[page_key] >= total_pages - 1):
                        st.session_state[page_key] += 1
                start = st.session_state[page_key] * page_size
                st.dataframe(df.iloc[start:start + page_size], use_container_width=True, hide_index=True)

        with tab_exact:
            ca_c1, ca_c2 = st.columns([1, 1])
            with ca_c1:
                min_users_filter = st.number_input("Мін. профілів на відео", min_value=2, max_value=10, value=2)
            with ca_c2:
                aggrid_page = st.number_input("Рядків на сторінку", min_value=10, max_value=500, value=50, step=10)

            if st.button("🔍 Знайти спільні репости між усіма справами", use_container_width=True, type="primary"):
                with st.spinner("🔎 Сканую всі бази даних..."):
                    _cr = find_shared_reposts(project_dir=_project_dir, min_users=int(min_users_filter))
                st.session_state["_cross_result"] = _cr
                for _k in list(st.session_state.keys()):
                    if _k.startswith("_page_"):
                        del st.session_state[_k]

            _cr = st.session_state.get("_cross_result")
            if _cr is None:
                st.info("👆 Натисніть кнопку вище, щоб запустити крос-аналіз точних збігів.")
            elif not _cr.shared:
                st.warning("Спільних репостів не знайдено.")
            else:
                st.success(f"✅ Знайдено {_cr.stats.get('shared_videos', 0)} спільних відео.")
                _pairs_df = _cr.to_pairs_df()
                if not _pairs_df.empty:
                    _render_table(_pairs_df, key="tbl_pairs", page_size=int(aggrid_page))

                # Кнопки експорту
                ex1, ex2 = st.columns(2)
                with ex1:
                    st.download_button(
                        "🎯 Maltego CSV",
                        data=_cr.to_maltego_csv_bytes(),
                        file_name="osint_maltego_links.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                with ex2:
                    st.download_button(
                        "📋 Повний CSV",
                        data=_cr.to_pairs_csv_bytes(),
                        file_name="osint_shared_reposts.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )

        with tab_semantic:
            st.markdown(
                "Цей розділ використовує **LLM Qwen** для знаходження прихованих "
                "семантичних перетинів між користувачами на основі їхніх інтересів та тегів відео."
            )
            if st.button("🧠 Аналізувати семантичні перетини", use_container_width=True, type="primary"):
                with st.spinner("Запит до локальної LLM Qwen... (~1–3 хв)..."):
                    from cross_analyzer import find_shared_interests
                    semantic_res = find_shared_interests(project_dir=_project_dir)
                st.session_state["_semantic_result"] = semantic_res

            sem_res = st.session_state.get("_semantic_result")
            if sem_res:
                if "error" in sem_res:
                    st.error(sem_res["error"])
                else:
                    st.success(f"Проаналізовано: {', '.join(['@' + u for u in sem_res.get('users', [])])}")
                    st.markdown("### Звіт про семантичні перетини")
                    st.markdown(sem_res.get("report", "Немає результатів."))

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 6: Мережевий аналіз (network_analyzer.py)
    # ─────────────────────────────────────────────────────────────────────────
    with tab6:
        st.subheader("📡 Мережевий аналіз контактів")
        st.caption(
            "Визначає топ-5 @-контактів цілі та класифікує їх через Qwen2.5 "
            "(друзі / колеги / значущі особи / медіа-акаунти)."
        )

        if st.button("🔬 Запустити мережевий аналіз", use_container_width=True, type="primary"):
            with st.spinner("Qwen2.5 аналізує мережу контактів..."):
                try:
                    import network_analyzer
                    top = network_analyzer.top_mentions(username, db_path)
                    if not top:
                        st.warning("Немає @-згадок для аналізу. Перевірте доказову базу.")
                    else:
                        prompt = network_analyzer.build_prompt(username, top)
                        llm_result = _ask_ollama(prompt, timeout=config.OLLAMA_TIMEOUT_TAGS)
                        st.session_state["_net_analysis"] = {
                            "top": top,
                            "report": llm_result,
                        }
                except Exception as exc:
                    st.error(f"Помилка мережевого аналізу: {exc}")

        net_res = st.session_state.get("_net_analysis")
        if net_res:
            st.markdown("#### 👥 Топ-5 @-контактів")
            top_df = pd.DataFrame(net_res["top"], columns=["Нікнейм", "Кількість згадок"])
            st.dataframe(top_df, use_container_width=True, hide_index=True)
            st.markdown("---")
            st.markdown("#### 🧠 LLM-класифікація контактів")
            st.markdown(net_res["report"])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 7: Пошук на зовнішніх платформах (cross_platform_linker.py)
    # ─────────────────────────────────────────────────────────────────────────
    with tab7:
        st.subheader("🌐 Пошук цифрового сліду на платформах")
        st.caption(
            "Перевіряє наявність нікнейму на Instagram, YouTube, Reddit, X та ін. "
            "Також зберігає знайдені профілі у БД."
        )

        t7_col1, t7_col2 = st.columns([2, 1])
        with t7_col1:
            search_handle = st.text_input(
                "Нікнейм для пошуку",
                value=username,
                key="t7_handle",
                help="За замовчуванням — активний профіль. Можна ввести інший.",
            )
        with t7_col2:
            st.markdown("<br>", unsafe_allow_html=True)
            run_search = st.button(
                "🔍 Шукати на платформах",
                use_container_width=True,
                type="primary",
            )

        if run_search and search_handle:
            with st.spinner(f"Перевіряємо @{search_handle} на зовнішніх платформах..."):
                try:
                    import cross_platform_linker
                    found_dict = cross_platform_linker.search_linked_profiles(search_handle)
                    st.session_state["_platform_results"] = found_dict
                    if found_dict:
                        try:
                            cross_platform_linker.save_to_db(db_path, found_dict)
                        except Exception:
                            pass
                except Exception as exc:
                    st.error(f"Помилка пошуку: {exc}")

        plat_res = st.session_state.get("_platform_results")
        if plat_res is not None:
            if plat_res:
                st.markdown(f"### ✅ Знайдено на {len(plat_res)} платформах")
                for platform, url in plat_res.items():
                    st.success(f"**{platform}**: [{url}]({url})")
                plat_df = pd.DataFrame(
                    [{"Платформа": p, "URL": u} for p, u in plat_res.items()]
                )
                st.download_button(
                    "⬇️ Скачати CSV",
                    data=plat_df.to_csv(index=False).encode(),
                    file_name=f"platforms_{search_handle}.csv",
                    mime="text/csv",
                )
            else:
                st.info("Нікнейм не виявлено на жодній зовнішній платформі.")

elif not username:
    st.info("👆 Введіть нікнейм та запустіть аналіз, або завантажте архівну справу через sidebar.")