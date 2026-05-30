#!/usr/bin/env python3
"""NSE Trading Dashboard v2 — Clean & Focused"""

import streamlit as st
import sqlite3
import pandas as pd
import json
import os
from datetime import datetime, date, timedelta

BASE_DIR    = os.path.expanduser("~/nse-scanner")
DB_PATH     = os.path.join(BASE_DIR, "nse_data.db")
TRADES_FILE = os.path.join(BASE_DIR, "angel_trades.json")
CAUTIONARY  = os.path.join(BASE_DIR, "cautionary_stocks.json")
CAM_LOG     = os.path.join(BASE_DIR, "logs/index_options.log")

st.set_page_config(page_title="NSE Scanner", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url("https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;600;700;800&display=swap");
:root {
    --bg:#060910; --surf:#0d1117; --surf2:#161b22;
    --border:#21262d; --green:#3fb950; --red:#f85149;
    --yellow:#e3b341; --blue:#58a6ff; --purple:#bc8cff;
    --text:#e6edf3; --muted:#8b949e;
}

/* ── Base ── */
.stApp { background:var(--bg); color:var(--text); font-family:"Syne",sans-serif; }
.stApp header, [data-testid="stHeader"] { background:var(--bg) !important; }
#MainMenu, footer { visibility:hidden; }
[data-testid="stHeader"] { display:none; }
.block-container { padding-top:2rem; max-width:1400px; }
h1,h2,h3 { font-family:"Syne",sans-serif !important; color:var(--text) !important; }

/* ── KPI Row ── */
.kpi-row { display:flex; gap:12px; margin-bottom:20px; flex-wrap:wrap; }
.kpi { flex:1; min-width:120px; background:var(--surf); border:1px solid var(--border);
       border-radius:10px; padding:16px 20px; }
.kpi-val { font-size:1.8rem; font-weight:700; font-family:"JetBrains Mono",monospace; line-height:1; }
.kpi-lbl { font-size:0.7rem; color:var(--muted); text-transform:uppercase;
           letter-spacing:1.5px; margin-top:6px; }

/* ── Colors ── */
.green { color:var(--green); } .red { color:var(--red); }
.yellow{ color:var(--yellow); } .blue { color:var(--blue); }
.muted { color:var(--muted); }

/* ── Signal Card ── */
.signal-card {
    background:var(--surf); border:1px solid var(--border);
    border-left:3px solid var(--green); border-radius:8px;
    padding:16px 20px; margin-bottom:10px;
    display:flex; justify-content:space-between; align-items:center;
    gap:16px;
}
.sig-left { display:flex; flex-direction:column; gap:4px; min-width:0; }
.sig-sym  { font-size:1.1rem; font-weight:700; color:var(--green); }
.sig-age  { font-size:0.75rem; color:var(--muted); }
.sig-right { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; text-align:right; flex-shrink:0; }
.sig-item-val { font-size:0.95rem; font-weight:600; font-family:"JetBrains Mono",monospace; }
.sig-item-lbl { font-size:0.65rem; color:var(--muted); text-transform:uppercase; }
.chart-link { font-size:0.75rem; color:var(--blue); text-decoration:none; }

/* ── Index Card ── */
.index-card {
    background:var(--surf); border:1px solid var(--border);
    border-radius:10px; padding:20px; margin-bottom:12px;
}
.idx-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:8px; }
.idx-name   { font-size:1rem; font-weight:700; }
.idx-price  { font-size:1.6rem; font-weight:700; font-family:"JetBrains Mono",monospace; }
.cam-grid   { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
.cam-level  { background:var(--surf2); border-radius:6px; padding:10px; text-align:center; }
.cam-val    { font-size:0.9rem; font-weight:600; font-family:"JetBrains Mono",monospace; }
.cam-lbl    { font-size:0.65rem; color:var(--muted); text-transform:uppercase; margin-top:2px; }
.signal-box { margin-top:12px; padding:10px 14px; border-radius:6px; font-size:0.85rem; font-weight:600; }
.signal-buy  { background:#0d2818; border:1px solid var(--green); color:var(--green); }
.signal-sell { background:#2d0f0e; border:1px solid var(--red);   color:var(--red); }
.signal-wait { background:#1c1f26; border:1px solid var(--muted); color:var(--muted); }

/* ── Position Card ── */
.pos-card {
    background:var(--surf); border:1px solid var(--border);
    border-radius:8px; padding:16px 20px; margin-bottom:10px;
}
.pos-header { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.pos-sym    { font-size:1rem; font-weight:700; }
.pos-pnl    { font-size:1.1rem; font-weight:700; font-family:"JetBrains Mono",monospace; white-space:nowrap; }
.pos-bar-bg { background:var(--surf2); border-radius:4px; height:6px; margin-top:10px; }
.pos-bar-fg { height:6px; border-radius:4px; background:var(--green); }
.pos-details{ display:flex; gap:20px; margin-top:8px; font-size:0.78rem; color:var(--muted); flex-wrap:wrap; }

/* ── Watchlist ── */
.watch-row {
    background:var(--surf); border:1px solid var(--border);
    border-radius:6px; padding:12px 16px; margin-bottom:8px;
    display:flex; justify-content:space-between; align-items:center;
    gap:12px;
}
.watch-sym  { font-size:0.95rem; font-weight:700; }
.watch-info { font-size:0.75rem; color:var(--muted); margin-top:2px; }
.badge      { font-size:0.65rem; font-weight:700; padding:2px 8px; border-radius:20px; white-space:nowrap; }
.badge-green{ background:#0d2818; color:var(--green); border:1px solid var(--green); }
.badge-wait { background:#1c1f26; color:var(--muted);  border:1px solid var(--border); }

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background:var(--surf); border-radius:8px; padding:4px;
    border:1px solid var(--border); gap:4px; margin-bottom:16px;
}
.stTabs [data-baseweb="tab"] {
    color:var(--muted); border-radius:6px; font-size:0.85rem;
    font-family:"Syne",sans-serif; font-weight:600;
}
.stTabs [aria-selected="true"] {
    background:var(--surf2) !important; color:var(--text) !important;
}
.stDataFrame, .stDataFrame * { background:var(--surf) !important; color:var(--text) !important; }

/* ══════════════════════════════════════
   MOBILE RESPONSIVE — max-width: 768px
   ══════════════════════════════════════ */
@media (max-width: 768px) {

    /* Container padding */
    .block-container { padding:0.5rem !important; max-width:100% !important; }

    /* Header — stack vertically */
    [data-testid="column"]:first-child,
    [data-testid="column"]:last-child { min-width:100% !important; width:100% !important; }

    /* KPI cards — 2 per row on mobile */
    .kpi-row { gap:8px; }
    .kpi { min-width:calc(50% - 8px); padding:12px 14px; }
    .kpi-val { font-size:1.3rem; }
    .kpi-lbl { font-size:0.65rem; }

    /* Signal card — stack vertically */
    .signal-card { flex-direction:column; align-items:flex-start; gap:12px; padding:12px 14px; }
    .sig-right { grid-template-columns:repeat(2,1fr); width:100%; gap:8px; text-align:left; }
    .sig-item-val { font-size:0.85rem; }
    .sig-age { font-size:0.7rem; word-break:break-word; }

    /* Index options card */
    .index-card { padding:12px 14px; }
    .idx-header { flex-direction:column; align-items:flex-start; gap:6px; }
    .idx-price { font-size:1.3rem; }
    .cam-grid { grid-template-columns:repeat(2,1fr); gap:6px; }
    .cam-val { font-size:0.85rem; }
    .cam-lbl { font-size:0.6rem; }
    .signal-box { font-size:0.8rem; padding:8px 12px; }

    /* Position card */
    .pos-card { padding:12px 14px; }
    .pos-header { flex-wrap:wrap; }
    .pos-pnl { font-size:0.95rem; }
    .pos-details { gap:10px; font-size:0.72rem; }

    /* Watchlist row — stack on very small */
    .watch-row { flex-wrap:wrap; gap:10px; padding:10px 12px; }
    .watch-sym { font-size:0.9rem; }
    .watch-info { font-size:0.7rem; }

    /* Tabs — smaller text, fit all tabs */
    .stTabs [data-baseweb="tab-list"] { padding:3px; gap:2px; overflow-x:auto; }
    .stTabs [data-baseweb="tab"] { font-size:0.72rem; padding:6px 8px !important; white-space:nowrap; }

    /* Streamlit columns — force single column on mobile */
    [data-testid="stHorizontalBlock"] { flex-wrap:wrap !important; }
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        min-width:100% !important; width:100% !important; flex:1 1 100% !important;
    }

    /* Sliders — full width */
    [data-testid="stSlider"] { width:100% !important; }

    /* Dataframes — horizontal scroll, no overflow cutoff */
    [data-testid="stDataFrame"] { overflow-x:auto !important; -webkit-overflow-scrolling:touch; }
    [data-testid="stDataFrame"] > div { min-width:600px; }

    /* Code blocks (log lines) — wrap and scroll */
    .stCode, pre { overflow-x:auto !important; font-size:0.7rem !important;
                   white-space:pre-wrap !important; word-break:break-all; }

    /* Info/warning/success boxes */
    [data-testid="stAlert"] { font-size:0.82rem; padding:10px 12px; }

    /* Markdown headers */
    h3 { font-size:1rem !important; margin-top:1rem !important; }

    /* General text scaling */
    p, li, .stMarkdown { font-size:0.85rem; }
}

/* Extra small screens (iPhone SE etc) */
@media (max-width: 380px) {
    .kpi { min-width:calc(50% - 6px); padding:10px 10px; }
    .kpi-val { font-size:1.1rem; }
    .sig-right { grid-template-columns:repeat(2,1fr); }
    .cam-grid { grid-template-columns:repeat(2,1fr); }
    .stTabs [data-baseweb="tab"] { font-size:0.65rem; padding:5px 6px !important; }
}
</style>
""", unsafe_allow_html=True)

# ── Helpers ───────────────────────────────────────────────────
def get_ist():
    return datetime.now()

@st.cache_data(ttl=60)
def db(query, params=()):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df
    except:
        return pd.DataFrame()

def cautionary():
    try:
        with open(CAUTIONARY) as f:
            return set(json.load(f).get("stocks", []))
    except:
        return set()

def trades():
    try:
        with open(TRADES_FILE) as f:
            d = json.load(f)
        return d.get("trades", [])
    except:
        return []

def calc_cam(h, l, c):
    r = h - l
    return {
        "h4": round(c + r*1.1/2, 2),
        "h3": round(c + r*0.55/2, 2),
        "l3": round(c - r*0.55/2, 2),
        "l4": round(c - r*1.1/2, 2),
    }

def market_open():
    ist = get_ist()
    return ist.weekday() < 5 and 9 <= ist.hour < 16

# ── Header ────────────────────────────────────────────────────
ist = get_ist()
ist_str = ist.strftime("%d %b %Y %H:%M IST")
mkt = "🟢 MARKET OPEN" if market_open() else "🔴 MARKET CLOSED"
mkt_color = "#3fb950" if market_open() else "#f85149"
col_l, col_r = st.columns([2,1])
with col_l:
    st.markdown(
        "<h2 style='margin:0;padding:8px 0 4px;font-family:Syne,sans-serif'>"
        "📈 NSE <span style='color:#58a6ff'>Trading</span> Dashboard</h2>",
        unsafe_allow_html=True
    )
with col_r:
    st.markdown(
        f"<div style='text-align:right;padding-top:10px'>"
        f"<span style='color:{mkt_color};font-size:0.75rem;font-weight:600'>{mkt}</span><br>"
        f"<span style='color:#8b949e;font-size:0.72rem'>{ist_str}</span></div>",
        unsafe_allow_html=True
    )
st.markdown("<hr style='border-color:#21262d;margin:8px 0 16px'>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🎯 Buy Signals",
    "📊 Index Options",
    "💼 Positions",
    "👁 Watchlist",
    "📈 Performance",
])

# ═══════════════════════════════════════════════════════════════
# TAB 1 — BUY SIGNALS
# ═══════════════════════════════════════════════════════════════
with tab1:
    today    = date.today()
    min_date = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    max_date = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    skip     = cautionary()

    hv_df = db("""
        SELECT symbol, hv_date, hv_low, hv_high, hv_close,
               ROUND((hv_close-hv_low)/(hv_high-hv_low)*100,1) as candle_pos,
               ROUND((hv_high-hv_low)/hv_low*100,1) as upside_pct,
               julianday(?) - julianday(hv_date) as age
        FROM hv_summary
        WHERE hv_date BETWEEN ? AND ?
        AND (hv_close-hv_low)/(hv_high-hv_low) >= 0.5
        AND hv_low > 0 AND hv_high > hv_low * 1.05
        ORDER BY hv_date DESC
    """, params=(str(today), min_date, max_date))

    signals = []
    if not hv_df.empty:
        for _, s in hv_df.iterrows():
            sym = s["symbol"]
            if sym in skip:
                continue
            price_df = db("""SELECT date,open,high,low,close,volume
                FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 2
            """, params=(sym,))
            if len(price_df) < 2:
                continue
            tr = price_df.iloc[0]
            pr = price_df.iloc[1]
            cam = calc_cam(float(pr["high"]), float(pr["low"]), float(pr["close"]))
            l3_vs_hv = abs(cam["l3"] - s["hv_low"]) / s["hv_low"] * 100
            if l3_vs_hv > 1.0:
                continue
            if float(tr["low"]) > cam["l3"] * 1.005:
                continue
            if float(tr["close"]) <= float(tr["open"]):
                continue
            if (float(tr["close"]) - s["hv_low"]) / s["hv_low"] * 100 > 3.0:
                continue
            avg_v = db("SELECT AVG(volume) as a FROM (SELECT volume FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 20)", params=(sym,))
            avg_vol = float(avg_v.iloc[0]["a"]) if not avg_v.empty else 1
            vol_ratio = float(tr["volume"]) / avg_vol if avg_vol > 0 else 0
            if vol_ratio < 1.0:
                continue
            entry = cam["l3"]
            sl    = cam["l4"]
            tgt   = float(s["hv_high"])
            risk  = entry - sl
            rr    = round((tgt - entry) / risk, 1) if risk > 0 else 0
            signals.append({
                "sym": sym, "age": int(s["age"]),
                "candle": s["candle_pos"], "upside": s["upside_pct"],
                "entry": entry, "sl": sl, "target": tgt,
                "rr": rr, "vol": round(vol_ratio,1),
                "l3_vs_hv": round(l3_vs_hv, 2),
            })

    all_trades = trades()
    placed = len([t for t in all_trades if t.get("status") == "OPEN"])

    # KPIs
    st.markdown(f"""<div class="kpi-row">
        <div class="kpi"><div class="kpi-val green">{len(signals)}</div><div class="kpi-lbl">Live Signals</div></div>
        <div class="kpi"><div class="kpi-val yellow">{placed}</div><div class="kpi-lbl">Orders Placed</div></div>
        <div class="kpi"><div class="kpi-val">{len(hv_df) if not hv_df.empty else 0}</div><div class="kpi-lbl">HV Candidates</div></div>
        <div class="kpi"><div class="kpi-val blue">794</div><div class="kpi-lbl">Stocks Scanned</div></div>
    </div>""", unsafe_allow_html=True)

    if signals:
        for sig in sorted(signals, key=lambda x: x["rr"], reverse=True):
            pnl_color = "green" if sig["rr"] >= 5 else "yellow"
            st.markdown(f"""
            <div class="signal-card">
              <div class="sig-left">
                <div class="sig-sym">{sig["sym"]}</div>
                <div class="sig-age">HV {sig["age"]}d ago · Candle {sig["candle"]}% · Vol {sig["vol"]}x · L3 vs HV {sig["l3_vs_hv"]}%</div>
                <a class="chart-link" href="https://www.tradingview.com/chart/?symbol=NSE:{sig["sym"]}" target="_blank">📊 TradingView →</a>
              </div>
              <div class="sig-right">
                <div><div class="sig-item-val">₹{sig["entry"]:.2f}</div><div class="sig-item-lbl">Entry (L3)</div></div>
                <div><div class="sig-item-val red">₹{sig["sl"]:.2f}</div><div class="sig-item-lbl">SL (L4)</div></div>
                <div><div class="sig-item-val green">₹{sig["target"]:.2f}</div><div class="sig-item-lbl">Target</div></div>
                <div><div class="sig-item-val {pnl_color}">1:{sig["rr"]}</div><div class="sig-item-lbl">R:R</div></div>
              </div>
            </div>""", unsafe_allow_html=True)
    else:
        st.info("No signals today. Scanner checks every 15 min during market hours.")

    if all_trades:
        open_t = [t for t in all_trades if t.get("status") == "OPEN"]
        if open_t:
            st.markdown("#### Today\'s Auto-Trades")
            rows = []
            for t in open_t:
                rows.append({
                    "Symbol": t.get("symbol",""),
                    "Entry": f"₹{t.get('entry',0):.2f}",
                    "SL": f"₹{t.get('sl',0):.2f}",
                    "Target": f"₹{t.get('target',0):.2f}",
                    "Shares": t.get("shares",0),
                    "Value": f"₹{t.get('pos_val',0):,.0f}",
                    "Time": t.get("placed_at",""),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════════════
# TAB 2 — INDEX OPTIONS
# ═══════════════════════════════════════════════════════════════
with tab2:
    st.markdown("### BankNifty & Nifty — Camarilla Levels")

    for sym, label in [("BANKNIFTY", "Bank Nifty"), ("NIFTY50", "Nifty 50")]:
        price_df = db("""SELECT date,open,high,low,close FROM daily_prices
            WHERE symbol=? ORDER BY date DESC LIMIT 2""", params=(sym,))
        if price_df.empty or len(price_df) < 2:
            continue
        today_row = price_df.iloc[0]
        prev_row  = price_df.iloc[1]
        curr   = float(today_row["close"])
        prev_c = float(prev_row["close"])

        # Use WEEKLY Camarilla from DB
        weekly_df = db("""SELECT l3,l4,h3,h4,prev_close FROM weekly_camarilla
            WHERE symbol=? ORDER BY week_start DESC LIMIT 1""", params=(sym,))
        if not weekly_df.empty:
            cam = {
                "l3": float(weekly_df.iloc[0]["l3"]),
                "l4": float(weekly_df.iloc[0]["l4"]),
                "h3": float(weekly_df.iloc[0]["h3"]),
                "h4": float(weekly_df.iloc[0]["h4"]),
            }
        else:
            cam = calc_cam(float(prev_row["high"]), float(prev_row["low"]), float(prev_row["close"]))
        chg = curr - prev_c
        chg_pct = chg / prev_c * 100

        # Signal logic (Pivot Boss rules)
        if curr > cam["h4"]:
            signal = "🚀 ABOVE H4 — Breakout zone, NO TRADE"
            sig_cls = "signal-wait"
        elif curr < cam["l4"]:
            signal = "⚠️ BELOW L4 — Breakdown zone, NO TRADE"
            sig_cls = "signal-wait"
        elif curr >= cam["h3"] and curr <= cam["h4"]:
            signal = "🔴 H3-H4 Zone — Watch for 15min close below H3 for PUT"
            sig_cls = "signal-sell"
        elif curr <= cam["l3"] and curr >= cam["l4"]:
            signal = "🟢 L3-L4 Zone — Watch for 15min close above L3 for CALL"
            sig_cls = "signal-buy"
        elif curr >= cam["h3"] * 0.998:
            signal = "🔴 Near H3 — Watch for PUT on close below H3"
            sig_cls = "signal-sell"
        elif curr <= cam["l3"] * 1.002:
            signal = "🟢 Near L3 — Watch for CALL on close above L3"
            sig_cls = "signal-buy"
        else:
            signal = "⏳ NEUTRAL — Between L3 and H3, wait"
            sig_cls = "signal-wait"

        chg_col = "green" if chg >= 0 else "red"
        chg_sign = "+" if chg >= 0 else ""
        st.markdown(f"""
        <div class="index-card">
          <div class="idx-header">
            <div class="idx-name">{label}</div>
            <div>
              <span class="idx-price">₹{curr:,.2f}</span>
              <span class="{chg_col}" style="font-size:0.85rem;margin-left:10px;font-family:JetBrains Mono">
                {chg_sign}{chg:.2f} ({chg_sign}{chg_pct:.2f}%)
              </span>
            </div>
          </div>
          <div class="cam-grid">
            <div class="cam-level"><div class="cam-val red">₹{cam["l4"]:,.2f}</div><div class="cam-lbl">L4 — Strong SL</div></div>
            <div class="cam-level"><div class="cam-val yellow">₹{cam["l3"]:,.2f}</div><div class="cam-lbl">L3 — Buy CE here</div></div>
            <div class="cam-level"><div class="cam-val yellow">₹{cam["h3"]:,.2f}</div><div class="cam-lbl">H3 — Buy PE here</div></div>
            <div class="cam-level"><div class="cam-val red">₹{cam["h4"]:,.2f}</div><div class="cam-lbl">H4 — Strong Res</div></div>
          </div>
          <div class="signal-box {sig_cls}">{signal}</div>
        </div>
        """, unsafe_allow_html=True)

    # Last Telegram alert
    st.markdown("### Last Index Options Alert")
    try:
        with open(CAM_LOG) as f:
            lines = f.readlines()
        relevant = [l.strip() for l in lines if any(x in l for x in ["BANKNIFTY","NIFTY","Level","Signal","BUY","PUT","CALL","briefing"])]
        last = relevant[-10:] if relevant else ["No alerts yet"]
        for line in last:
            st.code(line, language=None)
    except:
        st.info("No index options log found.")

# ═══════════════════════════════════════════════════════════════
# TAB 3 — OPEN POSITIONS
# ═══════════════════════════════════════════════════════════════
with tab3:
    st.markdown("### Open Positions")

    all_t = trades()
    open_pos = [t for t in all_t if t.get("status") == "OPEN"]

    if open_pos:
        total_val  = sum(t.get("pos_val", 0) for t in open_pos)
        total_pnl  = 0

        for t in open_pos:
            sym    = t.get("symbol", "")
            entry  = t.get("entry", 0)
            sl     = t.get("sl", 0)
            target = t.get("target", 0)
            shares = t.get("shares", 0)
            placed = t.get("placed_at", "")

            curr_df = db("SELECT close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 1", params=(sym,))
            curr = float(curr_df.iloc[0]["close"]) if not curr_df.empty else entry

            pnl     = round((curr - entry) * shares, 0)
            pnl_pct = round((curr - entry) / entry * 100, 2) if entry > 0 else 0
            total_pnl += pnl
            progress = round((curr - entry) / (target - entry) * 100, 0) if target > entry else 0
            progress = max(0, min(100, progress))
            days_held = 0
            try:
                placed_dt = datetime.strptime(placed[:10], "%Y-%m-%d")
                days_held = (datetime.now() - placed_dt).days
            except:
                pass

            pnl_col   = "green" if pnl >= 0 else "red"
            pnl_sign  = "+" if pnl >= 0 else ""
            bar_color = "var(--green)" if pnl >= 0 else "var(--red)"

            st.markdown(f"""
            <div class="pos-card">
              <div class="pos-header">
                <div>
                  <div class="pos-sym">{sym}</div>
                  <div style="font-size:0.75rem;color:var(--muted);margin-top:2px">
                    {shares} shares · Bought {placed[:10]} · {days_held}d held
                  </div>
                </div>
                <div class="pos-pnl {pnl_col}">{pnl_sign}₹{abs(pnl):,.0f} ({pnl_sign}{pnl_pct:.2f}%)</div>
              </div>
              <div class="pos-bar-bg"><div class="pos-bar-fg" style="width:{progress}%;background:{bar_color}"></div></div>
              <div class="pos-details">
                <span>Entry ₹{entry:.2f}</span>
                <span>Current ₹{curr:.2f}</span>
                <span>SL ₹{sl:.2f}</span>
                <span>Target ₹{target:.2f}</span>
                <span>Progress {progress:.0f}%</span>
              </div>
            </div>
            """, unsafe_allow_html=True)

        pnl_col = "green" if total_pnl >= 0 else "red"
        pnl_sign = "+" if total_pnl >= 0 else ""
        st.markdown(f"""<div class="kpi-row" style="margin-top:12px">
            <div class="kpi"><div class="kpi-val">{len(open_pos)}</div><div class="kpi-lbl">Open Positions</div></div>
            <div class="kpi"><div class="kpi-val">₹{total_val:,.0f}</div><div class="kpi-lbl">Total Invested</div></div>
            <div class="kpi"><div class="kpi-val {pnl_col}">{pnl_sign}₹{abs(total_pnl):,.0f}</div><div class="kpi-lbl">Unrealized P&L</div></div>
        </div>""", unsafe_allow_html=True)
    else:
        st.info("No open positions. Signals will auto-trade when conditions are met.")

# ═══════════════════════════════════════════════════════════════
# TAB 4 — WATCHLIST
# ═══════════════════════════════════════════════════════════════
with tab4:
    st.markdown("### Stocks Near HV Low")

    col1, col2 = st.columns(2)
    with col1:
        hv_days = st.slider("HV within (days)", 7, 90, 90)
    with col2:
        max_pct = st.slider("Max % above HV Low", 1, 10, 5)

    today    = date.today()
    min_date = (today - timedelta(days=hv_days)).strftime("%Y-%m-%d")
    skip     = cautionary()

    watch = db("""
        SELECT symbol, hv_date, hv_low, hv_high, hv_close, latest_close,
               ROUND((hv_close-hv_low)/(hv_high-hv_low)*100,1) as candle_pos,
               ROUND((latest_close-hv_low)/hv_low*100,2) as pct_above,
               ROUND((hv_high-hv_low)/hv_low*100,1) as upside,
               julianday(?) - julianday(hv_date) as age
        FROM hv_summary
        WHERE hv_date >= ?
        AND (hv_close-hv_low)/(hv_high-hv_low) >= 0.5
        AND hv_low > 0
        AND latest_close >= hv_low * 0.97
        AND latest_close <= hv_low * ?
        ORDER BY pct_above ASC
    """, params=(str(today), min_date, 1 + max_pct/100))

    if not watch.empty:
        count = 0
        for _, r in watch.iterrows():
            sym = r["symbol"]
            if sym in skip:
                continue
            price_df = db("SELECT high,low,close FROM daily_prices WHERE symbol=? ORDER BY date DESC LIMIT 2", params=(sym,))
            if len(price_df) < 2:
                continue
            prev = price_df.iloc[1]
            cam = calc_cam(float(prev["high"]), float(prev["low"]), float(prev["close"]))
            l3_vs_hv = abs(cam["l3"] - r["hv_low"]) / r["hv_low"] * 100
            aligned = l3_vs_hv <= 1.0
            badge = """<span class="badge badge-green">✅ ALIGNED</span>""" if aligned else """<span class="badge badge-wait">Waiting</span>"""
            upside = round((r["hv_high"] - r["latest_close"]) / r["latest_close"] * 100, 1)
            st.markdown(f"""
            <div class="watch-row">
              <div>
                <div class="watch-sym">{sym}
                  <a href="https://www.tradingview.com/chart/?symbol=NSE:{sym}" target="_blank"
                     style="font-size:0.7rem;color:var(--blue);margin-left:8px">chart →</a>
                </div>
                <div class="watch-info">HV {int(r["age"])}d ago · Candle {r["candle_pos"]}% · L3 vs HV {l3_vs_hv:.2f}%</div>
              </div>
              <div style="text-align:right;display:flex;gap:16px;align-items:center">
                <div>
                  <div style="font-size:0.85rem;font-family:JetBrains Mono;font-weight:600">₹{r["latest_close"]:.2f}</div>
                  <div style="font-size:0.7rem;color:var(--muted)">{r["pct_above"]:.2f}% above HV Low</div>
                </div>
                <div>
                  <div style="font-size:0.85rem;color:var(--green);font-weight:600">+{upside}%</div>
                  <div style="font-size:0.7rem;color:var(--muted)">upside</div>
                </div>
                {badge}
              </div>
            </div>
            """, unsafe_allow_html=True)
            count += 1
        st.caption(f"Showing {count} stocks · ✅ ALIGNED = Camarilla L3 within 1% of HV Low")
    else:
        st.info("No stocks near HV Low with current filters.")

# ═══════════════════════════════════════════════════════════════
# TAB 5 — PERFORMANCE & JOURNAL
# ═══════════════════════════════════════════════════════════════
with tab5:
    st.markdown("### Trading Performance")

    all_t = trades()
    completed = [t for t in all_t if t.get('status') not in ['OPEN','CANCELLED','ERROR','BLOCKED']]
    open_pos  = [t for t in all_t if t.get('status') == 'OPEN']
    winners   = [t for t in completed if t.get('exit_pnl', 0) > 0]
    losers    = [t for t in completed if t.get('exit_pnl', 0) <= 0]

    total_pnl = sum(t.get('exit_pnl', 0) for t in completed)
    win_rate  = round(len(winners)/len(completed)*100) if completed else 0
    avg_win   = round(sum(t.get('exit_pnl',0) for t in winners)/len(winners),0) if winners else 0
    avg_loss  = round(sum(t.get('exit_pnl',0) for t in losers)/len(losers),0) if losers else 0

    # Phase tracker
    phase_trades = len(completed)
    phase_target = 15
    phase_pct    = min(100, round(phase_trades/phase_target*100))

    st.markdown(f"""<div class="kpi-row">
        <div class="kpi">
            <div class="kpi-val">{phase_trades}/{phase_target}</div>
            <div class="kpi-lbl">Phase 1 Trades</div>
            <div style="background:var(--border);border-radius:4px;height:4px;margin-top:8px">
                <div style="width:{phase_pct}%;height:4px;border-radius:4px;background:var(--blue)"></div>
            </div>
        </div>
        <div class="kpi"><div class="kpi-val {'green' if win_rate>=20 else 'red'}">{win_rate}%</div><div class="kpi-lbl">Win Rate</div></div>
        <div class="kpi"><div class="kpi-val {'green' if total_pnl>=0 else 'red'}">{"+" if total_pnl>=0 else ""}Rs {abs(total_pnl):,.0f}</div><div class="kpi-lbl">Total P&L</div></div>
        <div class="kpi"><div class="kpi-val yellow">{len(open_pos)}</div><div class="kpi-lbl">Open Positions</div></div>
    </div>""", unsafe_allow_html=True)

    # Stats row
    st.markdown(f"""<div class="kpi-row">
        <div class="kpi"><div class="kpi-val green">+Rs {avg_win:,.0f}</div><div class="kpi-lbl">Avg Win</div></div>
        <div class="kpi"><div class="kpi-val red">Rs {avg_loss:,.0f}</div><div class="kpi-lbl">Avg Loss</div></div>
        <div class="kpi"><div class="kpi-val">{len(winners)}</div><div class="kpi-lbl">Winners</div></div>
        <div class="kpi"><div class="kpi-val">{len(losers)}</div><div class="kpi-lbl">Losers</div></div>
    </div>""", unsafe_allow_html=True)

    # Phase 1 progress
    st.markdown("### Phase 1 Progress")
    if phase_trades < phase_target:
        st.info(f"Phase 1: {phase_trades}/{phase_target} trades completed. Need {phase_target-phase_trades} more before Phase 2 review.")
    else:
        wr_ok = win_rate >= 15
        ev_ok = total_pnl > 0
        if wr_ok and ev_ok:
            st.success(f"Phase 1 COMPLETE! Win rate {win_rate}% >= 15% and EV positive. Ready for Phase 2!")
        else:
            st.warning(f"Phase 1 trades done but: Win rate {win_rate}% (need 15%+), P&L Rs {total_pnl:+,.0f} (need positive). Review before Phase 2.")

    # Backtest reference
    st.markdown("### Strategy Backtest Reference (2021-2026)")
    bt = {
        'Metric'        : ['Trades','Win Rate','Avg Win','Avg Loss','EV/trade','R:R','Avg Hold'],
        'HV+Camarilla'  : ['291','25%','+16.67%','-0.91%','+3.56%','17:1','5 days'],
    }
    st.dataframe(pd.DataFrame(bt), use_container_width=True, hide_index=True)

    # Trade journal
    if completed:
        st.markdown("### Trade Journal")
        rows = []
        for t in sorted(completed, key=lambda x: x.get('placed_at',''), reverse=True):
            pnl = t.get('exit_pnl', 0)
            rows.append({
                'Date'    : t.get('placed_at','')[:10],
                'Symbol'  : t.get('symbol',''),
                'Entry'   : f"Rs {t.get('entry',0):.2f}",
                'Exit'    : f"Rs {t.get('exit_price',0):.2f}",
                'SL'      : f"Rs {t.get('sl',0):.2f}",
                'Target'  : f"Rs {t.get('target',0):.2f}",
                'Shares'  : t.get('shares',0),
                'P&L'     : f"{'+'if pnl>=0 else ''}Rs {pnl:,.0f}",
                'Result'  : '✅ WIN' if pnl > 0 else '❌ LOSS',
                'Source'  : t.get('signal_source',''),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No completed trades yet. Journal will appear here after first exit.")
