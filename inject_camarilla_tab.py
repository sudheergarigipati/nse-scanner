#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  inject_camarilla_tab.py
#  Run once on VM to add Camarilla tab to NSE Volume Scanner HTML
#  Usage: python3 inject_camarilla_tab.py
# ═══════════════════════════════════════════════════════════════

import os
import shutil
from datetime import datetime

BASE_DIR  = os.path.expanduser('~/nse-scanner')
HTML_FILE = os.path.join(BASE_DIR, 'NSE_Volume_Scanner_Ver1.0.html')
BACKUP    = os.path.join(BASE_DIR, f'NSE_Volume_Scanner_Ver1.0_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.html')

# ── Read original ──────────────────────────────────────────────
with open(HTML_FILE, 'r') as f:
    html = f.read()

print(f"Loaded: {len(html)} chars")

# Backup first
shutil.copy(HTML_FILE, BACKUP)
print(f"Backup: {BACKUP}")

# ══════════════════════════════════════════════════════════════
#  1. CSS — inject before </style>
# ══════════════════════════════════════════════════════════════
CAM_CSS = """
/* ── Camarilla Tab Styles ── */
.tab.cam-tab.active{color:#a78bfa;border-bottom-color:#a78bfa}
.cam-tab .tab-count{background:#1a0a2e;color:#a78bfa}
.cam-stats{display:grid;gap:12px;padding:14px 24px;grid-template-columns:repeat(5,1fr)}
.cam-stat{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:11px 14px}
.cam-stat .val{font-size:22px;font-weight:700}
.cam-stat .lbl{font-size:10px;color:#64748b;margin-top:2px;text-transform:uppercase}
.v-purple{color:#a78bfa}.v-green{color:#22c55e}.v-red{color:#ef4444}
.v-amber{color:#f59e0b}.v-blue{color:#38bdf8}
.cam-week{font-size:11px;color:#475569;padding:4px 24px 8px}
.cam-section{padding:0 24px 24px}
.cam-section-title{font-size:12px;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;margin:14px 0 8px;display:flex;align-items:center;
  gap:8px;color:#94a3b8}
.cam-section-title.bull{color:#22c55e}
.cam-section-title.bear{color:#ef4444}
.cam-section-title.trig{color:#f59e0b}
.dist-bar{height:4px;background:#1e293b;border-radius:2px;width:60px;
  overflow:hidden;display:inline-block;vertical-align:middle;margin-left:4px}
.dist-fill-bull{height:100%;background:#22c55e;border-radius:2px}
.dist-fill-bear{height:100%;background:#ef4444;border-radius:2px}
.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;
  margin-right:4px;vertical-align:middle}
.dot-watch{background:#f59e0b}
.dot-triggered{background:#22c55e;box-shadow:0 0 4px #22c55e}
.dot-expired{background:#475569}
.tag-l3{background:#052e16;color:#22c55e;border:1px solid #166534;
  display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.tag-h3{background:#1a0808;color:#ef4444;border:1px solid #991b1b;
  display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.tag-triggered{background:#1a2e05;color:#86efac;border:1px solid #166534;
  display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.tag-expired{background:#1e293b;color:#64748b;border:1px solid #334155;
  display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:700}
.price-at-level{color:#f59e0b;font-weight:700;animation:blink 1s infinite}
.cam-empty{text-align:center;padding:48px 24px;color:#475569}
.trig-card{background:#0f1f0a;border:1px solid #166534;border-radius:8px;
  padding:14px 18px;margin-bottom:10px;display:flex;
  justify-content:space-between;align-items:flex-start}
.trig-card.bear{background:#1a0808;border-color:#991b1b}
.trig-sym{font-size:16px;font-weight:700;color:#f1f5f9}
.trig-levels{display:grid;grid-template-columns:repeat(4,1fr);
  gap:8px;margin-top:10px}
.trig-level{background:#0a0f1a;border-radius:6px;padding:8px 10px;text-align:center}
.trig-level .lv{font-size:10px;color:#64748b;text-transform:uppercase}
.trig-level .lv-val{font-size:14px;font-weight:700;margin-top:2px}
.lv-entry{color:#38bdf8}.lv-sl{color:#ef4444}
.lv-t1{color:#22c55e}.lv-t2{color:#a78bfa}
.rr-badge{background:#1e293b;color:#a78bfa;border-radius:4px;
  padding:2px 8px;font-size:11px;font-weight:700}
.no-signals{background:#0f172a;border:1px solid #1e293b;border-radius:8px;
  padding:24px;text-align:center;color:#475569;font-size:13px}
"""

if CAM_CSS.strip() not in html:
    html = html.replace('</style>', CAM_CSS + '\n</style>', 1)
    print("✅ CSS injected")
else:
    print("⚠️  CSS already present — skipping")

# ══════════════════════════════════════════════════════════════
#  2. TAB BUTTON — inject before </div> closing the tabs div
#     We find tabWatch and add Camarilla tab before it
# ══════════════════════════════════════════════════════════════
CAM_TAB = """  <div class="tab cam-tab" id="tabCam" onclick="switchTab('cam')">
    &#x1F3AF; Camarilla <span class="tab-count" id="camTabCount">&#8212;</span>
  </div>
"""

TAB_ANCHOR = 'id="tabWatch"'
if 'tabCam' not in html:
    html = html.replace(TAB_ANCHOR, CAM_TAB + '  <div class="tab" id="tabWatch"', 1)
    print("✅ Tab button injected")
else:
    print("⚠️  Tab button already present — skipping")

# ══════════════════════════════════════════════════════════════
#  3. PANEL HTML — inject after panelWatch closing </div>
# ══════════════════════════════════════════════════════════════
CAM_PANEL = """
<div id="panelCam" class="panel">

  <div class="cam-stats">
    <div class="cam-stat">
      <div class="val v-purple" id="camTotal">—</div>
      <div class="lbl">Total Watching</div>
    </div>
    <div class="cam-stat">
      <div class="val v-green" id="camBullCount">—</div>
      <div class="lbl">Bullish Setups</div>
    </div>
    <div class="cam-stat">
      <div class="val v-red" id="camBearCount">—</div>
      <div class="lbl">Bearish Setups</div>
    </div>
    <div class="cam-stat">
      <div class="val v-amber" id="camTrigCount">—</div>
      <div class="lbl">Triggered Today</div>
    </div>
    <div class="cam-stat">
      <div class="val v-blue" id="camWeekVal">—</div>
      <div class="lbl">Week Start</div>
    </div>
  </div>

  <div class="cam-week" id="camTimestamp"></div>

  <div class="cam-section">
    <div class="cam-section-title trig">&#x26A1; Today's Entry Triggers</div>
    <div id="camTriggers">
      <div class="no-signals">No triggers fired today yet. Monitoring every 15 min during market hours.</div>
    </div>
  </div>

  <div class="cam-section">
    <div class="cam-section-title bull">&#x1F7E2; Bullish Watchlist — L3 Bounce Setups</div>
    <div id="camBullTable"><div class="cam-empty">Loading...</div></div>
  </div>

  <div class="cam-section">
    <div class="cam-section-title bear">&#x1F534; Bearish Watchlist — H3 Fade Setups</div>
    <div id="camBearTable"><div class="cam-empty">Loading...</div></div>
  </div>

</div>
"""

PANEL_ANCHOR = 'id="panelWatch"'
if 'panelCam' not in html:
    # Find the closing </div> of panelWatch
    pw_start = html.find(PANEL_ANCHOR)
    if pw_start != -1:
        # Find the next <div id="panel or <script after panelWatch
        search_from = pw_start
        next_script = html.find('<script', search_from)
        next_panel  = html.find('<div id="panel', search_from + 10)

        if next_panel != -1 and (next_panel < next_script or next_script == -1):
            insert_at = next_panel
        else:
            insert_at = next_script

        if insert_at != -1:
            html = html[:insert_at] + CAM_PANEL + '\n' + html[insert_at:]
            print("✅ Panel HTML injected")
        else:
            print("❌ Could not find insertion point for panel")
    else:
        print("❌ Could not find panelWatch anchor")
else:
    print("⚠️  Panel already present — skipping")

# ══════════════════════════════════════════════════════════════
#  4. SWITCHTAB EXTENSION — inject before last </script>
# ══════════════════════════════════════════════════════════════
CAM_JS = """
// ── Camarilla Tab Extension ───────────────────────────────────
var _prevSwitchCam = switchTab;
switchTab = function(tab) {
  _prevSwitchCam(tab);
  document.getElementById('panelCam').classList.toggle('active', tab === 'cam');
  if (tab === 'cam') {
    document.getElementById('tabCam').classList.add('active');
    loadCamarilla();
  } else {
    document.getElementById('tabCam').classList.remove('active');
  }
};

// ── Camarilla Data & Render ───────────────────────────────────
const CAM_API = 'http://140.245.221.168:8081/camarilla';

function loadCamarilla() {
  fetch(CAM_API)
    .then(r => r.json())
    .then(data => renderCamarilla(data))
    .catch(e => {
      document.getElementById('camBullTable').innerHTML =
        '<div class="cam-empty">Error: ' + e.message + '</div>';
    });
}

function renderCamarilla(data) {
  var counts = data.counts || {};
  document.getElementById('camTotal').textContent     = counts.watching       || 0;
  document.getElementById('camBullCount').textContent = counts.bullish        || 0;
  document.getElementById('camBearCount').textContent = counts.bearish        || 0;
  document.getElementById('camTrigCount').textContent = counts.triggers_today || 0;
  document.getElementById('camWeekVal').textContent   = data.week_start       || '—';
  document.getElementById('camTabCount').textContent  = counts.total          || 0;
  document.getElementById('camTimestamp').textContent = '⏰ ' + (data.timestamp || '');
  renderTriggers(data.triggers  || []);
  renderCamTable('camBullTable', data.bullish || [], 'BULLISH');
  renderCamTable('camBearTable', data.bearish || [], 'BEARISH');
}

function renderTriggers(triggers) {
  var el = document.getElementById('camTriggers');
  if (!triggers.length) {
    el.innerHTML = '<div class="no-signals">No triggers fired today yet. Monitoring every 15 min during market hours.</div>';
    return;
  }
  el.innerHTML = triggers.map(function(t) {
    var isBull = t.direction === 'BULLISH';
    return '<div class="trig-card ' + (isBull ? '' : 'bear') + '">' +
      '<div style="flex:1">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<span class="trig-sym">' + t.symbol + '</span>' +
          '<span class="' + (isBull ? 'tag-l3' : 'tag-h3') + '">' + t.trigger_type + '</span>' +
          '<span class="rr-badge">R:R 1:' + t.risk_reward + '</span>' +
          '<span style="font-size:11px;color:#64748b;margin-left:auto">' + t.trigger_time + '</span>' +
        '</div>' +
        '<div class="trig-levels">' +
          '<div class="trig-level"><div class="lv">Entry</div><div class="lv-val lv-entry">&#x20B9;' + t.entry_price + '</div></div>' +
          '<div class="trig-level"><div class="lv">Stop Loss</div><div class="lv-val lv-sl">&#x20B9;' + t.stop_loss + '</div></div>' +
          '<div class="trig-level"><div class="lv">Target 1</div><div class="lv-val lv-t1">&#x20B9;' + t.target1 + '</div></div>' +
          '<div class="trig-level"><div class="lv">Target 2</div><div class="lv-val lv-t2">&#x20B9;' + t.target2 + '</div></div>' +
        '</div>' +
      '</div>' +
      '<div style="margin-left:16px">' +
        '<a href="https://www.tradingview.com/chart/?symbol=NSE:' + t.symbol + '" target="_blank" class="tv-link">Chart &#x2197;</a>' +
      '</div>' +
    '</div>';
  }).join('');
}

function renderCamTable(elId, stocks, direction) {
  var el     = document.getElementById(elId);
  var isBull = direction === 'BULLISH';
  if (!stocks.length) {
    el.innerHTML = '<div class="cam-empty">No ' + direction.toLowerCase() +
      ' setups this week.<br><span style="font-size:11px">Scanner runs every morning at 8AM IST.</span></div>';
    return;
  }
  var rows = stocks.map(function(s) {
    var atLevel   = s.dist_pct <= 0.3;
    var statusTag = s.status === 'TRIGGERED'
      ? '<span class="tag-triggered">TRIGGERED</span>'
      : s.status === 'EXPIRED'
        ? '<span class="tag-expired">EXPIRED</span>'
        : '<span class="status-dot dot-watch"></span><span style="font-size:11px;color:#f59e0b">WATCHING</span>';
    var distColor  = s.dist_pct <= 0.3 ? '#f59e0b' : s.dist_pct <= 1.0 ? '#22c55e' : '#64748b';
    var closeColor = atLevel ? 'price-at-level' : (isBull ? 'c-green' : 'c-red');
    var distPct    = Math.min(s.dist_pct / 2 * 100, 100);
    return '<tr>' +
      '<td><span class="sym">' + s.symbol + '</span></td>' +
      '<td><span class="' + (isBull ? 'c-green' : 'c-red') + '" style="font-weight:700">&#x20B9;' + s.entry + '</span></td>' +
      '<td><span class="c-red">&#x20B9;' + s.stop_loss + '</span></td>' +
      '<td><span class="c-green">&#x20B9;' + s.target1 + '</span></td>' +
      '<td><span class="c-purple">&#x20B9;' + s.target2 + '</span></td>' +
      '<td><span class="' + closeColor + '" style="font-weight:600">&#x20B9;' + s.close + '</span></td>' +
      '<td><span style="color:' + distColor + ';font-size:12px">' + s.dist_pct + '%</span>' +
        '<div class="dist-bar"><div class="' + (isBull ? 'dist-fill-bull' : 'dist-fill-bear') +
        '" style="width:' + distPct + '%"></div></div></td>' +
      '<td><span class="c-muted">' + s.sl_pct + '%</span></td>' +
      '<td><span class="c-muted" style="font-size:11px">&#x20B9;' + s.ema20w + '</span></td>' +
      '<td>' + statusTag + '</td>' +
      '<td><a href="https://www.tradingview.com/chart/?symbol=NSE:' + s.symbol +
        '" target="_blank" class="tv-link">TV &#x2197;</a></td>' +
    '</tr>';
  }).join('');

  el.innerHTML = '<table><thead><tr>' +
    '<th>Symbol</th>' +
    '<th>' + (isBull ? 'L3 Entry' : 'H3 Entry') + '</th>' +
    '<th>Stop Loss</th><th>Target 1</th><th>Target 2</th>' +
    '<th>LTP</th><th>Distance</th><th>SL%</th>' +
    '<th>20w EMA</th><th>Status</th><th>Chart</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
}
"""

if 'loadCamarilla' not in html:
    last_script = html.rfind('</script>')
    if last_script != -1:
        html = html[:last_script] + CAM_JS + '\n</script>' + html[last_script+9:]
        print("✅ JavaScript injected")
    else:
        print("❌ Could not find </script> to inject JS")
else:
    print("⚠️  JS already present — skipping")

# ══════════════════════════════════════════════════════════════
#  5. Write output
# ══════════════════════════════════════════════════════════════
OUT_FILE = os.path.join(BASE_DIR, 'NSE_Volume_Scanner_Ver1.0.html')
with open(OUT_FILE, 'w') as f:
    f.write(html)

print(f"\n✅ Done! Updated file: {OUT_FILE}")
print(f"   Original backup: {BACKUP}")
print(f"   New size: {len(html)} chars")
print("\nNext steps:")
print("  1. Copy to www/: cp NSE_Volume_Scanner_Ver1.0.html www/index.html")
print("  2. Add /camarilla endpoint to price_api.py")
print("  3. Restart: ./stop.sh && ./start.sh")
