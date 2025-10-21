// ==== Utilities to handle Pacific Time and formatting ====
const PT = 'America/Los_Angeles';
let lastRows = [];
let selectedDay = null; // YYYYMMDD in PT
let activeMode = 'today'; // 'yesterday' | 'today' | 'tomorrow' | 'custom'
let lastPoints = []; // {x,y,valueCents,date}
let tooltipIndex = null; // index into lastPoints

// === Cookie helpers ===
function setCookie(name, value, days) {
  const d = new Date();
  d.setTime(d.getTime() + (days*24*60*60*1000));
  document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}; expires=${d.toUTCString()}; path=/`;
}
function getCookie(name) {
  const key = encodeURIComponent(name) + '=';
  const ca = document.cookie.split(';');
  for (let c of ca) {
    c = c.trim();
    if (c.indexOf(key) === 0) return decodeURIComponent(c.substring(key.length));
  }
  return '';
}

function formatParts(date, timeZone) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).formatToParts(date);
  const map = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return map;
}

function yyyymmddInTZ(date, timeZone) {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date);
  const map = Object.fromEntries(parts.map(p => [p.type, p.value]));
  return `${map.year}${map.month}${map.day}`;
}

function dashedFromYYYYMMDD(s) {
  return `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;
}

function YYYYMMDDFromDashed(s) {
  return (s || '').replaceAll('-', '');
}

function ptDateWithOffset(offsetDays = 0) {
  const ptNow = new Date(new Date().toLocaleString('en-US', { timeZone: PT }));
  const d = new Date(ptNow);
  d.setDate(ptNow.getDate() + offsetDays);
  return d;
}

function todayInPT() {
  return yyyymmddInTZ(ptDateWithOffset(0), PT);
}

function normalizeIso(ts) {
  return ts.replace(/([+-][0-9]{2})([0-9]{2})$/, '$1:$2');
}

function setStatus(kind, msg) {
  const dot = document.getElementById('dot');
  const status = document.getElementById('statusText');
  dot.className = 'dot ' + (kind || '');
  status.textContent = msg;
}

function setTZPill() {
  const me = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const pill = document.getElementById('tz-pill');
  pill.textContent = `Your TZ: ${me}`;
}

// ==== Core fetch + render logic ====
async function fetchPricing({ utility, market, program, startdate, enddate, ratename, representativeCircuitId, cca, env }) {
  const base = env === 'stage' ? 'https://pge-pe-api.gridx.com/stage/v1/getPricing' : 'https://pge-pe-api.gridx.com/v1/getPricing';
  const params = new URLSearchParams({ utility, market, program, startdate, enddate, ratename, representativeCircuitId });
  if (cca) params.set('cca', cca);
  const url = `${base}?${params.toString()}`;
  const res = await fetch(url);
  if (res.status === 204) return { meta: { code: 204 }, data: [] };
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return res.json();
}

function extractRows(data) {
  const rows = [];
  for (const day of data || []) for (const d of (day.priceDetails || [])) rows.push(d);
  rows.sort((a,b) => new Date(normalizeIso(a.startIntervalTimeStamp)) - new Date(normalizeIso(b.startIntervalTimeStamp)));
  return rows;
}

function renderRows(rows) {
  const tbody = document.getElementById('tbody');
  tbody.innerHTML = '';
  if (!rows || rows.length === 0) {
    const tr = document.createElement('tr');
    const td = document.createElement('td'); td.colSpan = 5; td.className = 'help'; td.textContent = 'No price data returned for the requested day.'; tr.appendChild(td); tbody.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const d = new Date(normalizeIso(r.startIntervalTimeStamp));
    const ptParts = formatParts(d, PT);
    const meParts = formatParts(d, Intl.DateTimeFormat().resolvedOptions().timeZone);
    const priceUSD = Number(r.intervalPrice);
    const priceCents = priceUSD * 100;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">${ptParts.year}-${ptParts.month}-${ptParts.day} ${ptParts.hour}:${ptParts.minute} PT</td>
      <td class="mono">${meParts.year}-${meParts.month}-${meParts.day} ${meParts.hour}:${meParts.minute}</td>
      <td class="mono">${priceUSD.toFixed(6)}</td>
      <td class="mono">${priceCents.toFixed(3)}</td>
      <td class="mono">${r.priceStatus || ''}</td>
    `;
    tbody.appendChild(tr);
  }
}

function drawNowLine(ctx, pad, cssW, cssH, rows) {
  if (activeMode !== 'today' || !rows || rows.length === 0) return;
  const nowPT = new Date(new Date().toLocaleString('en-US', { timeZone: PT }));
  const ymdNowPT = yyyymmddInTZ(nowPT, PT);
  if (ymdNowPT !== selectedDay) return;
  const n = rows.length;
  const t0 = new Date(normalizeIso(rows[0].startIntervalTimeStamp)).getTime();
  const t1 = new Date(normalizeIso(rows[n-1].startIntervalTimeStamp)).getTime();
  const step = n > 1 ? (new Date(normalizeIso(rows[1].startIntervalTimeStamp)).getTime() - t0) : 60*60*1000;
  const tEnd = t1 + step;
  const tNow = nowPT.getTime();
  const clamp = (x,min,max) => Math.max(min, Math.min(max, x));
  const frac = clamp((tNow - t0) / (tEnd - t0), 0, 1);
  const x = pad.left + frac * (cssW - pad.left - pad.right);
  ctx.save();
  ctx.setLineDash([5,4]);
  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--warn') || '#f59e0b';
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(x, pad.top);
  ctx.lineTo(x, cssH - pad.bottom);
  ctx.stroke();
  ctx.restore();
}

function renderChart(rows) {
  const canvas = document.getElementById('chart');
  const tooltipEl = document.getElementById('chartTooltip');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  canvas.width = Math.max(1, Math.floor(cssW * dpr));
  canvas.height = Math.max(1, Math.floor(cssH * dpr));
  ctx.setTransform(1,0,0,1,0,0);
  ctx.scale(dpr, dpr);

  ctx.clearRect(0, 0, cssW, cssH);
  ctx.font = '12px system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial';
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--text') || '#e2e8f0';
  ctx.strokeStyle = 'rgba(255,255,255,0.5)';

  const pad = { left: 48, right: 12, top: 12, bottom: 28 };
  const plotW = cssW - pad.left - pad.right;
  const plotH = cssH - pad.top - pad.bottom;

  lastPoints = [];

  if (!rows || rows.length === 0) {
    tooltipIndex = null;
    tooltipEl.style.display = 'none';
    ctx.fillStyle = 'rgba(226,232,240,0.8)';
    ctx.fillText('No data to chart', pad.left + 6, pad.top + 16);
    return;
  }

  const values = rows.map(r => Number(r.intervalPrice) * 100);
  let minV = 0;
  const maxData = Math.max(...values);
  let maxV = Math.max(7.0, isFinite(maxData) ? maxData : 0);
  if (maxV <= 0) maxV = 7.0;

  const ticks = 5;
  const step = (maxV - minV) / (ticks - 1);
  const tickVals = Array.from({length: ticks}, (_,i) => minV + i*step);

  ctx.strokeStyle = 'rgba(255,255,255,0.15)';
  ctx.fillStyle = 'rgba(148,163,184,0.9)';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (const tv of tickVals) {
    const y = pad.top + (maxV - tv) * (plotH / (maxV - minV));
    ctx.beginPath();
    ctx.moveTo(pad.left, y);
    ctx.lineTo(cssW - pad.right, y);
    ctx.stroke();
    ctx.fillText(tv.toFixed(1), pad.left - 6, y);
  }

  const n = rows.length;
  const idxs = Array.from(new Set([0, Math.floor(n*0.25), Math.floor(n*0.5), Math.floor(n*0.75), n-1]));
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (const i of idxs) {
    const x = pad.left + (n === 1 ? plotW/2 : (i * (plotW / (n - 1))));
    const d = new Date(normalizeIso(rows[i].startIntervalTimeStamp));
    const pt = formatParts(d, PT);
    ctx.fillText(`${pt.hour}:00`, x, cssH - pad.bottom + 6);
  }

  ctx.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue('--accent') || '#60a5fa';
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const v = values[i];
    const x = pad.left + (n === 1 ? plotW/2 : (i * (plotW / (n - 1))));
    const y = pad.top + (maxV - v) * (plotH / (maxV - minV));
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.stroke();

  ctx.fillStyle = 'rgba(96,165,250,0.9)';
  for (let i = 0; i < n; i++) {
    const v = values[i];
    const x = pad.left + (n === 1 ? plotW/2 : (i * (plotW / (n - 1))));
    const y = pad.top + (maxV - v) * (plotH / (maxV - minV));
    lastPoints.push({ x, y, valueCents: v, date: new Date(normalizeIso(rows[i].startIntervalTimeStamp)) });
    ctx.beginPath(); ctx.arc(x, y, 2.5, 0, Math.PI*2); ctx.fill();
  }

  drawNowLine(ctx, pad, cssW, cssH, rows);

  if (tooltipIndex != null && lastPoints[tooltipIndex]) {
    const p = lastPoints[tooltipIndex];
    const pt = formatParts(p.date, PT);
    const txt = `${pt.hour}:00 — ${p.valueCents.toFixed(1)} ¢/kWh`;
    const tx = Math.min(cssW - 6, Math.max(6, p.x));
    const ty = Math.max(6, p.y);
    const tt = document.getElementById('chartTooltip');
    tt.textContent = txt;
    tt.style.left = `${tx}px`;
    tt.style.top = `${ty}px`;
    tt.style.display = 'block';
  } else {
    tooltipEl.style.display = 'none';
  }
}

// ==== Hit testing helpers ====
function canvasPointFromEvent(evt, canvas) {
  const rect = canvas.getBoundingClientRect();
  const x = (evt.touches ? evt.touches[0].clientX : evt.clientX) - rect.left;
  const y = (evt.touches ? evt.touches[0].clientY : evt.clientY) - rect.top;
  return { x, y };
}
function dist2PointToSegment(px, py, x1, y1, x2, y2) {
  const vx = x2 - x1, vy = y2 - y1;
  const wx = px - x1, wy = py - y1;
  const len2 = vx*vx + vy*vy || 1;
  let t = (wx*vx + wy*vy) / len2;
  t = Math.max(0, Math.min(1, t));
  const projx = x1 + t*vx, projy = y1 + t*vy;
  const dx = px - projx, dy = py - projy;
  return { dist2: dx*dx + dy*dy, t };
}
function nearestSegment(pt, points) {
  let best = { idx: -1, dist2: Infinity, t: 0 };
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i+1];
    const d = dist2PointToSegment(pt.x, pt.y, a.x, a.y, b.x, b.y);
    if (d.dist2 < best.dist2) best = { idx: i, dist2: d.dist2, t: d.t };
  }
  return best;
}

// ==== Chart interactivity (click/tap) ====
function handleCanvasClick(evt) {
  const canvas = document.getElementById('chart');
  const pt = canvasPointFromEvent(evt, canvas);

  if (!lastPoints || lastPoints.length === 0) {
    tooltipIndex = null;
    renderChart(lastRows);
    return;
  }

  // Hit-test points
  let bestPoint = { idx: -1, dist2: Infinity };
  for (let i = 0; i < lastPoints.length; i++) {
    const p = lastPoints[i];
    const dx = p.x - pt.x, dy = p.y - pt.y;
    const d2 = dx*dx + dy*dy;
    if (d2 < bestPoint.dist2) bestPoint = { idx: i, dist2: d2 };
  }
  const pointRadius = 12; // px threshold

  // Hit-test polyline
  const seg = nearestSegment(pt, lastPoints);
  const lineThreshold = 8; // px threshold

  const pointHit = bestPoint.idx !== -1 && bestPoint.dist2 <= pointRadius*pointRadius;
  const lineHit = seg.idx !== -1 && seg.dist2 <= lineThreshold*lineThreshold;

  if (pointHit) {
    tooltipIndex = bestPoint.idx;
  } else if (lineHit) {
    tooltipIndex = seg.t < 0.5 ? seg.idx : (seg.idx + 1);
  } else {
    tooltipIndex = null; // click/tap away from the line -> hide
  }

  renderChart(lastRows);
}

// ==== Day selector logic ====
function setActiveToggle(which) {
  for (const id of ['btnYesterday','btnToday','btnTomorrow','btnCustom']) {
    document.getElementById(id).classList.remove('active');
  }
  document.getElementById(which).classList.add('active');
  const picker = document.getElementById('customDay');
  picker.style.display = (which === 'btnCustom') ? 'inline-block' : 'none';
}

function selectDay(offset, whichId, mode) {
  selectedDay = yyyymmddInTZ(ptDateWithOffset(offset), PT);
  activeMode = mode;
  setActiveToggle(whichId);
  loadPrices();
}

function selectCustom() {
  activeMode = 'custom';
  setActiveToggle('btnCustom');
  const picker = document.getElementById('customDay');
  if (!picker.value) picker.value = dashedFromYYYYMMDD(todayInPT());
  const ymd = YYYYMMDDFromDashed(picker.value);
  if (/^[0-9]{8}$/.test(ymd)) {
    selectedDay = ymd;
    loadPrices();
  } else {
    setStatus('err', 'Custom date must be YYYY-MM-DD.');
  }
}

async function loadPrices() {
  setStatus('', 'Loading…');
  const circuitEl = document.getElementById('circuit');
  const rateEl = document.getElementById('ratename');
  const ccaEl = document.getElementById('cca');
  const utility = (document.getElementById('utility').value || 'PGE').toUpperCase();
  const program = document.getElementById('program').value || 'CalFUSE';
  const market = 'DAM';
  const ratename = rateEl.value || 'EV2A';
  const representativeCircuitId = (circuitEl.value || '').padStart(9, '0');
  const cca = ccaEl.value || '';
  const env = document.getElementById('env').value || 'prod';

  // Persist preferences
  setCookie('gl_circuit', representativeCircuitId, 180);
  setCookie('gl_ratename', ratename, 180);
  setCookie('gl_cca', cca, 180);

  const day = selectedDay || todayInPT();
  try {
    const json = await fetchPricing({ utility, market, program, startdate: day, enddate: day, ratename, representativeCircuitId, cca, env });
    if (json.meta?.code === 204) {
      setStatus('warn', 'No data (HTTP 204). Try again after ~4:30–6:30pm PT.');
      lastRows = [];
      renderRows(lastRows);
      renderChart(lastRows);
      return;
    }
    lastRows = extractRows(json.data);
    tooltipIndex = null;
    renderRows(lastRows);
    renderChart(lastRows);
    const days = (json.data || []).length;
    const intervals = lastRows.length;
    setStatus('ok', `Loaded ${intervals} intervals across ${days} day(s).`);
  } catch (err) {
    console.error(err);
    setStatus('err', err.message);
  }
}

// ==== Wire up UI ====
document.getElementById('loadBtn').addEventListener('click', loadPrices);
document.getElementById('btnToday').addEventListener('click', () => selectDay(0, 'btnToday', 'today'));
document.getElementById('btnYesterday').addEventListener('click', () => selectDay(-1, 'btnYesterday', 'yesterday'));
document.getElementById('btnTomorrow').addEventListener('click', () => selectDay(1, 'btnTomorrow', 'tomorrow'));
document.getElementById('btnCustom').addEventListener('click', selectCustom);
document.getElementById('customDay').addEventListener('change', () => {
  const ymd = YYYYMMDDFromDashed(document.getElementById('customDay').value);
  if (/^[0-9]{8}$/.test(ymd)) { selectedDay = ymd; activeMode = 'custom'; loadPrices(); }
});
setTZPill();

// Persisted preferences: load cookies
(function loadPrefs() {
  const circuit = getCookie('gl_circuit');
  const rate = getCookie('gl_ratename');
  const cca = getCookie('gl_cca');
  if (circuit) document.getElementById('circuit').value = circuit;
  if (rate) document.getElementById('ratename').value = rate;
  if (cca) document.getElementById('cca').value = cca;
  document.getElementById('circuit').addEventListener('change', (e) => setCookie('gl_circuit', (e.target.value||'').padStart(9,'0'), 180));
  document.getElementById('ratename').addEventListener('change', (e) => setCookie('gl_ratename', e.target.value||'', 180));
  document.getElementById('cca').addEventListener('change', (e) => setCookie('gl_cca', e.target.value||'', 180));
})();

(function init() {
  selectedDay = todayInPT();
  activeMode = 'today';
  setActiveToggle('btnToday');
  loadPrices();
})();

window.addEventListener('resize', () => renderChart(lastRows));

const canvasEl = document.getElementById('chart');
canvasEl.addEventListener('click', handleCanvasClick);
canvasEl.addEventListener('touchstart', (e) => { handleCanvasClick(e); }, { passive: true });
