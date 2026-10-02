"""
app.py — Streamlit OSINT Dashboard v2.0

New features (refactoring):
  - Instant switching between cases via Sidebar (like ChatGPT chat)
  - AI parameters (temperature, focus) are passed in ALL requests to Ollama
  - Full DB context in RAG-chat: description + OCR + audio + raw_result
  - TAB 2: Toggle "Connections / Interest Graph" (Semantic Interest Graph)
  - TAB 6: Network analysis (network_analyzer.py) — LLM-classification of contacts
  - TAB 7: Search on other platforms (cross_platform_linker.py)
  - check_same_thread=False on all sqlite3.connect() — thread safety
"""

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(funcName)s | %(message)s",
    handlers=[
        logging.FileHandler("streamlit_app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

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
# PAGE
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="TikTok OSINT Profiler",
    page_icon="🕵️‍♂️",
    layout="wide",
)
st.title("🕵️‍♂️ TikTok OSINT Profiler")
st.markdown(
    "Full digital investigation platform: parsing → analysis → dossier → social graph."
)

_project_dir = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# HELPER: SQLite connection
# ══════════════════════════════════════════════════════════════════════════════
def _db(db_path: str) -> sqlite3.Connection:
    """Opens an SQLite connection. check_same_thread=False for Streamlit threads."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# MODULE 1: Case Archive (Sidebar) — instant switching
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("📂 Case Archive")

found_dbs = sorted(glob.glob(os.path.join(_project_dir, "databases", "osint_*.db")))
available_profiles = [
    os.path.splitext(os.path.basename(p))[0].replace("osint_", "")
    for p in found_dbs
]

# Initialize default active profile
if "active_username" not in st.session_state and available_profiles:
    st.session_state["active_username"] = available_profiles[0]
    st.session_state["profile_loaded"] = True

if available_profiles:
    # Synchronize selected index with session_state so selectbox does not reset
    _current = st.session_state.get("active_username", available_profiles[0])
    _cur_idx = available_profiles.index(_current) if _current in available_profiles else 0

    archive_profile = st.sidebar.selectbox(
        "Choose profile",
        options=available_profiles,
        index=_cur_idx,
        help="All local OSINT databases in the project directory",
    )
    if st.sidebar.button("📂 Load archived case", use_container_width=True):
        # Instant switching: save and rerun
        st.session_state["active_username"] = archive_profile
        st.session_state["profile_loaded"] = True
        st.session_state["messages"] = []  # reset chat when switching case
        st.rerun()
else:
    st.sidebar.info("No databases found. Run analysis for the first profile.")

st.sidebar.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 2: Data Export (Sidebar)
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("📥 Data Export")

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
                label="⬇️ Download reposts.csv",
                data=csv_bytes,
                file_name=f"reposts_{_active}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        except Exception:
            st.sidebar.info("Data unavailable for export.")
    else:
        st.sidebar.info("Select or load a profile to export.")
else:
    st.sidebar.info("Run analysis or select an archived case.")

st.sidebar.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# MODULE 3: AI Settings (Sidebar) — applied to all requests
# ══════════════════════════════════════════════════════════════════════════════
st.sidebar.header("⚙️ AI Analyst Settings")

temperature = st.sidebar.slider(
    "Temperature (creativity)",
    min_value=0.1,
    max_value=0.9,
    value=st.session_state.get("temperature", 0.3),
    step=0.1,
    help="Affects the variability of Qwen2.5 responses in all requests",
)

focus_options = [
    "Basic psychological profile",
    "Vulnerabilities and triggers analysis",
    "Social status assessment",
]
focus_option = st.sidebar.selectbox(
    "Analysis focus",
    options=focus_options,
    index=focus_options.index(st.session_state.get("focus_option", focus_options[0]))
    if st.session_state.get("focus_option") in focus_options else 0,
    help="Determines the system prompt in RAG-chat and profiler",
)

# Save in session_state — persists across tab switches
st.session_state["temperature"] = temperature
st.session_state["focus_option"] = focus_option

_FOCUS_PROMPTS: dict[str, str] = {
    "Basic psychological profile": (
        "You are an OSINT analyst. Provide balanced psychological insights "
        "based on TikTok data. Answer briefly and specifically."
    ),
    "Vulnerabilities and triggers analysis": (
        "You are a social engineering profiler. Focus on psychological vulnerabilities, "
        "trust triggers, and rejection factors of the subject based on their TikTok behavior."
    ),
    "Social status assessment": (
        "You are a sociologist and OSINT analyst. Evaluate social status, career vector, "
        "network of contacts, and subject's influence, based on TikTok data."
    ),
}
system_prompt = _FOCUS_PROMPTS.get(focus_option, _FOCUS_PROMPTS[focus_options[0]])

# Single options dictionary for all requests to Ollama
_ollama_options: dict = {"temperature": temperature}

st.sidebar.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# HELPER: Ollama request with correct settings
# ══════════════════════════════════════════════════════════════════════════════
def _ask_ollama(prompt: str, timeout: int = 120, model: str | None = None) -> str:
    """Sends request to Ollama. Uses global _ollama_options."""
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
        return f"Ollama Error: HTTP {res.status_code}"
    except requests.exceptions.Timeout:
        return f"⏱️ Ollama did not respond within {timeout}s. Check server."
    except Exception as exc:
        return f"Error: {exc}"


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PANEL: New Analysis Settings
# ══════════════════════════════════════════════════════════════════════════════
with st.container():
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        username_input = st.text_input(
            "TikTok Username (without @)",
            placeholder="Example: username123",
        )
    with col2:
        limit = st.slider("Video limit:", min_value=10, max_value=300, value=100, step=10)
    with col3:
        workers = st.slider("AI threads:", min_value=1, max_value=10, value=3)

with st.container():
    st.markdown("### ⚙️ Operating mode")
    only_profiler = st.checkbox(
        "⏩ Skip data collection (generate dossier only)",
        help="If database is already collected — just regenerates TXT/PDF.",
    )

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE LAUNCH
# ══════════════════════════════════════════════════════════════════════════════
if st.button("🚀 Run analysis", type="primary", use_container_width=True):
    if not username_input:
        st.warning("⚠️ Enter target username.")
    else:
        try:
            safe_user = sanitize_username(username_input)
        except ValueError:
            st.error("❌ Invalid username. Use only letters, digits, _ and -")
            st.stop()

        progress_bar = st.progress(0)
        status_text = st.empty()

        with st.expander("🛠 Live terminal logs", expanded=True):
            log_container = st.empty()

        status_text.info(f"🚀 Launching pipeline for @{safe_user}...")

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

                step_m = re.search(r"\[STEP (\d+)/(\d+)\]", line_clean)
                if step_m:
                    progress_bar.progress(int(step_m.group(1)) / int(step_m.group(2)))
                    status_text.info(f"🔄 {line_clean.split(']')[-1].strip()}")

                vid_m = re.search(r"\[(\d+)/(\d+)\]", line_clean)
                if vid_m and "STEP" not in line_clean:
                    c, t = int(vid_m.group(1)), int(vid_m.group(2))
                    if t > 0:
                        status_text.info(f"⏳ Processing video: {c} of {t}")

            process.wait()

            if process.returncode == 0:
                progress_bar.progress(1.0)
                status_text.success(f"✅ Work on @{safe_user} completed successfully!")
                st.session_state["active_username"] = safe_user
                st.session_state["profile_loaded"] = True
                st.session_state["messages"] = []
                st.rerun()
            else:
                status_text.error("❌ Pipeline execution error. Check logs.")

        except Exception as exc:
            st.error(f"Critical launch error: {exc}")

# ══════════════════════════════════════════════════════════════════════════════
# RESULTS TABS
# ══════════════════════════════════════════════════════════════════════════════
username = st.session_state.get("active_username", "")

if username and st.session_state.get("profile_loaded"):
    db_path = db_path_for(username)

    if not os.path.exists(db_path):
        st.warning(f"Database for @{username} not found. Run analysis.")
        st.stop()

    # ── Metrics ──────────────────────────────────────────────────────────────
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
        m1.metric("📹 Reposts in DB", total_reposts)
        m2.metric("🧠 Analyzed", total_analyzed)
        m3.metric("🔗 With @-mentions", total_mentions)
        m4.metric("👤 Active profile", f"@{username}")
    except Exception:
        pass

    st.markdown("---")

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📄 Dossier",
        "🕸️ Social Graph",
        "💬 AI Investigator",
        "🖼️ Evidence Base",
        "🔗 DB Overlaps",
        "📡 Network Analysis",
        "🌐 Platform Search",
    ])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 1: Dossier
    # ─────────────────────────────────────────────────────────────────────────
    with tab1:
        txt_file = os.path.join(_project_dir, "reports", f"profile_{username}.txt")
        pdf_file = os.path.join(_project_dir, "reports", f"profile_{username}.pdf")

        res_col1, res_col2 = st.columns([3, 1])
        with res_col1:
            st.subheader("📄 Psychological Dossier")
            if os.path.exists(txt_file):
                st.markdown(open(txt_file, encoding="utf-8").read())
            else:
                st.info("Text report is not generated yet. Run the pipeline.")

        with res_col2:
            st.subheader("📥 Export")
            if os.path.exists(pdf_file):
                with open(pdf_file, "rb") as fh:
                    st.download_button(
                        label="💾 Download PDF",
                        data=fh,
                        file_name=f"profile_{username}.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
            else:
                st.warning("PDF not created.")

            st.markdown("---")
            st.markdown("**♻️ Regenerate dossier**")
            st.caption(
                f"Focus: **{focus_option}**  \nTemperature: **{temperature}**"
            )
            if st.button("🔄 Regenerate", use_container_width=True):
                with st.spinner("Generating dossier via Qwen2.5..."):
                    try:
                        import profiler_cdp
                        profiler_cdp.main(
                            db_path=db_path,
                            output_file=txt_file,
                            temperature=temperature,
                        )
                        st.success("Dossier updated!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error: {e}")


    # ─────────────────────────────────────────────────────────────────────────
    # TAB 2: Social Graph + Interest Graph
    # ─────────────────────────────────────────────────────────────────────────
    with tab2:
        # Graph mode switch
        graph_mode = st.radio(
            "Graph mode",
            options=["🔗 Connections (Mentions)", "🧠 Interest Graph"],
            horizontal=True,
            key="graph_mode_radio",
        )

        st.markdown("---")

        # ── Mode 1: Mentions Graph ─────────────────────────────────────────────
        if graph_mode == "🔗 Connections (Mentions)":
            st.subheader("🕸️ Social Graph of Mentions")

            g_col1, g_col2, g_col3 = st.columns([1, 1, 1])
            with g_col1:
                graph_physics = st.toggle("⚛️ Node physics", value=True)
            with g_col2:
                min_edge_weight = st.slider(
                    "Min. number of mentions", min_value=1, max_value=10, value=1,
                )
            with g_col3:
                show_isolated = st.checkbox("Show isolated nodes", value=False)

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
                    mg1.metric("🔵 Graph vertices", G.number_of_nodes())
                    mg2.metric("🔗 Connections", G.number_of_edges())
                    mg3.metric(
                        "👑 Most mentioned",
                        top_mentioned[0][0] if top_mentioned else "—",
                        f"{top_mentioned[0][1]} mentions" if top_mentioned else "",
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
                            title=f"@{node}\nReceived: {deg_in}\nMentions: {deg_out}",
                            size=size, color=color, shape=shape,
                        )

                    max_w = max((d.get("weight", 1) for _, _, d in G.edges(data=True)), default=1)
                    for u, v, data in G.edges(data=True):
                        w = data.get("weight", 1)
                        net.add_edge(u, v, width=1 + (w / max_w) * 5, title=f"{w} mentions")

                    _cross = st.session_state.get("_cross_result")
                    if _cross is not None and _cross.overlaps:
                        for (u_a, u_b), overlap_count in _cross.overlaps.items():
                            for nd in (u_a, u_b):
                                if nd not in G.nodes():
                                    net.add_node(
                                        nd, label=f"📁 @{nd}",
                                        title=f"@{nd}\n(other OSINT profile)",
                                        size=20,
                                        color={"background": "#9b5de5", "border": "#6a0dad",
                                               "highlight": {"background": "#c77dff", "border": "#7b2d8b"}},
                                        shape="database",
                                    )
                            net.add_edge(
                                u_a, u_b,
                                width=2 + min(overlap_count, 8),
                                title=f"Shared content: {overlap_count} videos",
                                color="#9b5de5", dashes=True,
                            )

                    if not graph_physics:
                        net.toggle_physics(False)

                    st.components.v1.html(net.generate_html(), height=670)

                    st.markdown("""
                    <div style="display:flex; gap:24px; padding:8px 0; flex-wrap:wrap; font-size:13px;">
                      <span>⭐ <b style="color:#e63946">Target</b></span>
                      <span>🟠 <b style="color:#f4a261">Popular</b> (≥3 mentions)</span>
                      <span>🔵 <b style="color:#457b9d">Active</b> (only mentions)</span>
                      <span>🟢 <b style="color:#2a9d8f">Regular</b></span>
                      <span>🟣 <b style="color:#9b5de5">Dashed</b> = Shared content between profiles</span>
                    </div>
                    """, unsafe_allow_html=True)

                    with st.expander("📊 Node statistics", expanded=False):
                        tc1, tc2 = st.columns(2)
                        with tc1:
                            st.markdown("**👑 Top-10 mentioned**")
                            st.dataframe(
                                pd.DataFrame(
                                    sorted(in_deg.items(), key=lambda x: x[1], reverse=True)[:10],
                                    columns=["Username", "Received mentions"],
                                ),
                                use_container_width=True, hide_index=True,
                            )
                        with tc2:
                            st.markdown("**🗣️ Top-10 active**")
                            st.dataframe(
                                pd.DataFrame(
                                    sorted(out_deg.items(), key=lambda x: x[1], reverse=True)[:10],
                                    columns=["Username", "Mentions others"],
                                ),
                                use_container_width=True, hide_index=True,
                            )
                else:
                    st.info("No @-mentions found. Decrease the minimum threshold.")
            except Exception as exc:
                st.error(f"Error building graph: {exc}")

        # ── Mode 2: Interest Graph ──────────────────────────────────────────
        else:
            st.subheader("🧠 Interest Graph")
            st.caption(
                "Central node — analysis object. "
                "Surrounding nodes — topics/interests extracted from video analysis. "
                "Edge thickness — frequency of topic mention."
            )

            int_col1, int_col2 = st.columns([1, 3])
            with int_col1:
                min_freq = st.number_input(
                    "Min. topic frequency", min_value=1, max_value=20, value=2,
                    help="Show only topics occurring ≥N times",
                )
                int_physics = st.toggle("⚛️ Physics", value=True, key="int_phys")

            with int_col2:
                try:
                    _ic = _db(db_path)
                    # Extract all interests, hobbies, music_taste and hashtags
                    interest_rows = _ic.execute(
                        """
                        SELECT a.interests, a.hobbies, a.music_taste, r.hashtags
                        FROM reposts r
                        LEFT JOIN analysis a ON r.video_id = a.video_id
                        WHERE a.interests IS NOT NULL OR a.hobbies IS NOT NULL
                        """
                    ).fetchall()
                    _ic.close()

                    # Count topic frequencies
                    topic_counter: Counter = Counter()
                    for row in interest_rows:
                        for field in row:
                            if not field:
                                continue
                            # Parse: can be JSON-list or comma-separated string
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

                    # Filter by minimum frequency
                    filtered_topics = {
                        k: v for k, v in topic_counter.items() if v >= min_freq
                    }

                    if not filtered_topics:
                        st.info(
                            "Not enough data for the interest graph. "
                            "Decrease min. frequency or run full pipeline (steps 4–5)."
                        )
                    else:
                        # Build NetworkX graph
                        IG = nx.Graph()
                        IG.add_node(username, node_type="user")
                        for topic, freq in filtered_topics.items():
                            IG.add_node(topic, node_type="topic", freq=freq)
                            IG.add_edge(username, topic, weight=freq)

                        # Build PyVis
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

                        # Central node (user)
                        inet.add_node(
                            username,
                            label=f"★ @{username}",
                            title=f"Analysis Object\nTopics in DB: {len(filtered_topics)}",
                            size=50,
                            color={"background": "#e63946", "border": "#ff6b6b",
                                   "highlight": {"background": "#ff4757", "border": "#ff6b81"}},
                            shape="star",
                        )

                        # Interest nodes (color by frequency: blue to orange)
                        for topic, freq in sorted(filtered_topics.items(), key=lambda x: x[1], reverse=True):
                            ratio = freq / max_freq
                            # Color interpolation: blue → yellow → orange
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
                                title=f"Topic: {topic}\nOccurrences: {freq} times",
                                size=size,
                                color={"background": bg_color, "border": "#264653",
                                       "highlight": {"background": "#ffb347", "border": "#e76f51"}},
                                shape="ellipse",
                            )
                            inet.add_edge(
                                username, topic,
                                width=1 + ratio * 6,
                                title=f"{freq} mentions",
                            )

                        if not int_physics:
                            inet.toggle_physics(False)

                        st.components.v1.html(inet.generate_html(), height=670)

                        # Top topics table
                        with st.expander("📊 Top-20 interests/topics", expanded=False):
                            top_topics = sorted(
                                filtered_topics.items(), key=lambda x: x[1], reverse=True
                            )[:20]
                            st.dataframe(
                                pd.DataFrame(top_topics, columns=["Topic / Interest", "Frequency"]),
                                use_container_width=True, hide_index=True,
                            )

                except Exception as exc:
                    st.error(f"Error building interest graph: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 3: AI-Investigator (RAG-chat) — full DB context
    # ─────────────────────────────────────────────────────────────────────────
    with tab3:
        st.subheader("💬 AI Investigator")
        st.caption(f"Focus: **{focus_option}** | Temperature: **{temperature}**")

        if "messages" not in st.session_state:
            st.session_state.messages = []

        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        if st.button("🗑️ Clear chat history", key="clear_chat"):
            st.session_state.messages = []
            st.rerun()

        user_prompt = st.chat_input("Enter request to dossier database...")
        if user_prompt:
            st.session_state.messages.append({"role": "user", "content": user_prompt})

            # ── Max context from DB (STEP 2 of task) ─────────────────
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
                    if r[1]: parts.append(f"description:{r[1]}")
                    if r[2] and r[2] != "[]": parts.append(f"tags:{r[2]}")
                    if r[3]: parts.append(f"interests:{r[3]}")
                    if r[4]: parts.append(f"hobbies:{r[4]}")
                    if r[5]: parts.append(f"relationships:{r[5]}")
                    if r[6]: parts.append(f"music:{r[6]}")
                    if r[7] and r[7] not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                        parts.append(f"OCR:{r[7]}")
                    if r[8] and r[8].strip(): parts.append(f"audio:{r[8]}")
                    if r[9]: parts.append(f"analysis:{r[9][:200]}")
                    context_parts.append(" | ".join(parts))

                context = "\n".join(context_parts)
            except Exception as e:
                context = f"Context unavailable: {e}"

            full_prompt = (
                f"{system_prompt}\n\n"
                f"Context (analysis of TikTok activity @{username}):\n"
                f"{context[:25000]}\n\n"
                f"Investigator request: {user_prompt}"
            )

            with st.chat_message("assistant"):
                with st.spinner("Qwen2.5 is analyzing..."):
                    answer = _ask_ollama(full_prompt, timeout=120)
                    st.write(answer)
                    st.session_state.messages.append({"role": "assistant", "content": answer})


    # ─────────────────────────────────────────────────────────────────────────
    # TAB 4: Evidence Base
    # ─────────────────────────────────────────────────────────────────────────
    with tab4:
        st.subheader("🖼️ Evidence Base")

        ev_col1, ev_col2 = st.columns([2, 1])
        with ev_col1:
            ev_limit = st.slider("Videos per page", 6, 60, 30, 6, key="ev_limit")
        with ev_col2:
            ev_filter = st.selectbox(
                "Filter",
                ["All", "With OCR text", "With audio transcription", "With visual description"],
                key="ev_filter",
            )

        try:
            _ec = _db(db_path)
            _where = {
                "All": "a.visual_context IS NOT NULL OR a.video_text IS NOT NULL OR a.audio_text IS NOT NULL",
                "With OCR text": "a.video_text IS NOT NULL AND a.video_text NOT IN ('', 'ERROR_NO_FILE', 'ERROR_CANT_OPEN')",
                "With audio transcription": "a.audio_text IS NOT NULL AND a.audio_text != ''",
                "With visual description": "a.visual_context IS NOT NULL AND a.visual_context NOT IN ('', 'ERROR_NO_FILE', 'ERROR_CANT_OPEN')",
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
            st.error(f"DB read error: {exc}")
            evidence_rows = []

        if not evidence_rows:
            st.info("Evidence base is empty. Run the full pipeline (steps 2–3).")
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
                            f"[🎬 Open in TikTok]({url})" if url else f"`{v_id}`"
                        )

                    with st.expander(f"@{author} — {v_id}", expanded=False):
                        if visual and visual not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                            st.markdown(f"**🎥 Visual description:**\n{visual}")
                        if ocr_text and ocr_text not in ("ERROR_NO_FILE", "ERROR_CANT_OPEN"):
                            st.markdown(f"**📝 OCR text:**\n`{ocr_text}`")
                        if audio_text and audio_text.strip():
                            st.markdown(f"**🎙️ Transcription:**\n{audio_text}")

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 5: DB Overlaps
    # ─────────────────────────────────────────────────────────────────────────
    with tab5:
        st.subheader("🔗 Overlaps between databases")
        st.caption(
            "Finds videos reposted simultaneously by two or more "
            "investigated targets, as well as semantic intersections of their interests."
        )

        tab_exact, tab_semantic = st.tabs(["Shared videos (Exact matches)", "Shared interests (Semantics)"])

        # ── Table Helper (AgGrid / fallback) ───────────────────────────────
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
                    st.caption(f"Page {st.session_state[page_key]+1}/{total_pages} ({len(df)} rows)")
                with pg_c3:
                    if st.button("▶", key=f"next_{key}", disabled=st.session_state[page_key] >= total_pages - 1):
                        st.session_state[page_key] += 1
                start = st.session_state[page_key] * page_size
                st.dataframe(df.iloc[start:start + page_size], use_container_width=True, hide_index=True)

        with tab_exact:
            ca_c1, ca_c2 = st.columns([1, 1])
            with ca_c1:
                min_users_filter = st.number_input("Min. profiles per video", min_value=2, max_value=10, value=2)
            with ca_c2:
                aggrid_page = st.number_input("Rows per page", min_value=10, max_value=500, value=50, step=10)

            if st.button("🔍 Find shared reposts across all cases", use_container_width=True, type="primary"):
                with st.spinner("🔎 Scanning all databases..."):
                    _cr = find_shared_reposts(project_dir=_project_dir, min_users=int(min_users_filter))
                st.session_state["_cross_result"] = _cr
                for _k in list(st.session_state.keys()):
                    if _k.startswith("_page_"):
                        del st.session_state[_k]

            _cr = st.session_state.get("_cross_result")
            if _cr is None:
                st.info("👆 Click the button above to run exact match cross-analysis.")
            elif not _cr.shared:
                st.warning("No shared reposts found.")
            else:
                st.success(f"✅ Found {_cr.stats.get('shared_videos', 0)} shared videos.")
                _pairs_df = _cr.to_pairs_df()
                if not _pairs_df.empty:
                    _render_table(_pairs_df, key="tbl_pairs", page_size=int(aggrid_page))

                # Export buttons
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
                        "📋 Full CSV",
                        data=_cr.to_pairs_csv_bytes(),
                        file_name="osint_shared_reposts.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )

        with tab_semantic:
            st.markdown(
                "This section uses **LLM Qwen** to find hidden "
                "semantic overlaps between users based on their interests and video tags."
            )
            if st.button("🧠 Analyze semantic overlaps", use_container_width=True, type="primary"):
                with st.spinner("Querying local LLM Qwen... (~1–3 min)..."):
                    from cross_analyzer import find_shared_interests
                    semantic_res = find_shared_interests(project_dir=_project_dir)
                st.session_state["_semantic_result"] = semantic_res

            sem_res = st.session_state.get("_semantic_result")
            if sem_res:
                if "error" in sem_res:
                    st.error(sem_res["error"])
                else:
                    st.success(f"Analyzed: {', '.join(['@' + u for u in sem_res.get('users', [])])}")
                    st.markdown("### Semantic Overlaps Report")
                    st.markdown(sem_res.get("report", "No results."))

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 6: Network Analysis (network_analyzer.py)
    # ─────────────────────────────────────────────────────────────────────────
    with tab6:
        st.subheader("📡 Network Contacts Analysis")
        st.caption(
            "Identifies the target's top 5 @-contacts and classifies them via Qwen2.5 "
            "(friends / colleagues / significant persons / media accounts)."
        )

        if st.button("🔬 Run network analysis", use_container_width=True, type="primary"):
            with st.spinner("Qwen2.5 is analyzing contact network..."):
                try:
                    import network_analyzer
                    top = network_analyzer.top_mentions(username, db_path)
                    if not top:
                        st.warning("No @-mentions to analyze. Check the evidence base.")
                    else:
                        prompt = network_analyzer.build_prompt(username, top)
                        llm_result = _ask_ollama(prompt, timeout=config.OLLAMA_TIMEOUT_TAGS)
                        st.session_state["_net_analysis"] = {
                            "top": top,
                            "report": llm_result,
                        }
                except Exception as exc:
                    st.error(f"Network analysis error: {exc}")

        net_res = st.session_state.get("_net_analysis")
        if net_res:
            st.markdown("#### 👥 Top-5 @-contacts")
            top_df = pd.DataFrame(net_res["top"], columns=["Username", "Number of mentions"])
            st.dataframe(top_df, use_container_width=True, hide_index=True)
            st.markdown("---")
            st.markdown("#### 🧠 LLM classification of contacts")
            st.markdown(net_res["report"])

    # ─────────────────────────────────────────────────────────────────────────
    # TAB 7: Search on external platforms (cross_platform_linker.py)
    # ─────────────────────────────────────────────────────────────────────────
    with tab7:
        st.subheader("🌐 Platform Digital Footprint Search")
        st.caption(
            "Checks for username presence on Instagram, YouTube, Reddit, X, etc. "
            "Also saves found profiles in DB."
        )

        t7_col1, t7_col2 = st.columns([2, 1])
        with t7_col1:
            search_handle = st.text_input(
                "Username to search",
                value=username,
                key="t7_handle",
                help="Default is active profile. Can enter another one.",
            )
        with t7_col2:
            st.markdown("<br>", unsafe_allow_html=True)
            run_search = st.button(
                "🔍 Search on platforms",
                use_container_width=True,
                type="primary",
            )

        if run_search and search_handle:
            with st.spinner(f"Checking @{search_handle} on external platforms..."):
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
                    st.error(f"Search error: {exc}")

        plat_res = st.session_state.get("_platform_results")
        if plat_res is not None:
            if plat_res:
                st.markdown(f"### ✅ Found on {len(plat_res)} platforms")
                for platform, url in plat_res.items():
                    st.success(f"**{platform}**: [{url}]({url})")
                plat_df = pd.DataFrame(
                    [{"Platform": p, "URL": u} for p, u in plat_res.items()]
                )
                st.download_button(
                    "⬇️ Download CSV",
                    data=plat_df.to_csv(index=False).encode(),
                    file_name=f"platforms_{search_handle}.csv",
                    mime="text/csv",
                )
            else:
                st.info("Username not found on any external platform.")

elif not username:
    st.info("👆 Enter username and run analysis, or load archived case via sidebar.")

