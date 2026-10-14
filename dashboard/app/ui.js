// Shared helpers and components: formatting, small HTML builders, and SVG charts.
'use strict';
window.RD = window.RD || {};
(function (RD) {
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const PALETTE = ['#58A6FF', '#7EE787', '#FF9F43', '#B392F0', '#F2CC60', '#FF7B72', '#79C0FF', '#56D364', '#FFA657', '#D2A8FF'];
  const FAM = { plat: 'var(--plat)', work: 'var(--work)', gpu: 'var(--gpu)', dev: 'var(--dev)', num: 'var(--num)', red: 'var(--red)' };

  function toMs(v) {
    if (v == null || v === '') return null;
    if (typeof v === 'number') return v > 1e12 ? v : v * 1000;
    const t = Date.parse(v);
    return Number.isNaN(t) ? null : t;
  }
  function fmtDur(sec) {
    if (sec == null || !Number.isFinite(sec)) return '—';
    const s = Math.max(0, Math.round(sec));
    if (s < 120) return `${s} s`;
    const m = Math.floor(s / 60);
    if (m < 120) return `${m} min ${s % 60} s`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }
  function fmtClock(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }
  function fmtAgo(sec) {
    if (sec == null || !Number.isFinite(sec)) return '';
    if (sec < 60) return `${Math.max(0, Math.round(sec))} s ago`;
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
    return `${Math.round(sec / 86400)} d ago`;
  }
  function when(v) {
    const ms = toMs(v);
    if (ms == null) return '—';
    const d = new Date(ms);
    if (d.toDateString() === new Date().toDateString()) {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
    return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  const money = (v, digits = 2) => (v == null ? '—'
    : `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`);
  const gib = (mib) => (mib == null ? '—' : `${(mib / 1024).toFixed(1)} GiB`);
  function shortNode(name) {
    const m = /^aks-([a-z0-9]+)-\d+-vmss([0-9a-z]+)$/.exec(name || '');
    return m ? `${m[1]}-${m[2].replace(/^0+(?=.)/, '')}` : (name || '');
  }
  const shortPod = (name) => String(name || '').replace(/^(.*)-([a-z0-9]{5})$/, '$1·$2');
  // The server polls DCGM every 2 seconds; a reading much older than that means the scrape is failing.
  const isStale = (g) => !!g && g.ageSeconds != null && g.ageSeconds > 10;

  // ── Small components ────────────────────────────────────────────────────
  const pill = (text, cls = '') => `<span class="pill ${cls}">${esc(text)}</span>`;
  function statusIcon(status) {
    const map = { done: ['✓', 'ok'], live: ['●', 'live'], warn: ['!', 'warn'], todo: ['○', 'todo'], info: ['i', 'info'] };
    const [glyph, cls] = map[status] || map.todo;
    return `<span class="si si-${cls}" aria-hidden="true">${glyph}</span>`;
  }
  function check(ok, label, detail) {
    const cls = ok === true ? 'is-ok' : ok === false ? 'is-bad' : 'is-na';
    const mark = ok === true ? '✓' : ok === false ? '✗' : '–';
    return `<div class="check ${cls}"><span class="mark">${mark}</span><span>${esc(label)}</span>${detail ? `<span class="detail">${esc(detail)}</span>` : '<span></span>'}</div>`;
  }
  function stat(value, label, opts = {}) {
    const color = opts.color ? ` style="color:${opts.color}"` : '';
    return `<div class="card${opts.tint ? ' tint-' + opts.tint : ''}"><div class="stat"><span class="v${opts.small ? ' sm' : ''}"${color}>${esc(value)}</span><span class="l">${esc(label)}</span></div></div>`;
  }
  function codeBlock(text) {
    const lines = String(text || '').split('\n').map((line) =>
      (line.trimStart().startsWith('#') ? `<span class="c">${esc(line)}</span>` : esc(line)));
    return `<pre class="code">${lines.join('\n')}</pre>`;
  }
  function jobPill(j) {
    const label = j.jobStatus || j.deploymentStatus || 'created';
    let cls = '';
    if (j.jobStatus === 'SUCCEEDED') cls = 'ok';
    else if (j.jobStatus === 'FAILED' || j.deploymentStatus === 'Failed') cls = 'bad';
    else if (j.jobStatus === 'RUNNING') cls = 'live';
    else if (!j.terminal) cls = 'pend';
    return pill(label, cls);
  }

  // ── Charts ──────────────────────────────────────────────────────────────
  function niceMax(v) {
    if (!(v > 0)) return 1;
    if (v <= 1) return 1;
    if (v <= 5) return Math.ceil(v);
    const mag = 10 ** Math.floor(Math.log10(v));
    const n = v / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * mag;
  }
  function fmtValue(v, unit) {
    if (v == null || !Number.isFinite(v)) return '—';
    if (unit === '%') return `${Math.round(v)}%`;
    if (unit === 'MiB') return v >= 1024 ? `${(v / 1024).toFixed(1)} GiB` : `${Math.round(v)} MiB`;
    return Number.isInteger(v) ? String(v) : v.toFixed(1);
  }
  function fmtTimeAxis(t) {
    return new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  const seriesLabel = (name) => (/^aks-/.test(name) ? shortNode(name) : name);

  function lineChart(chart, start, end, opts = {}) {
    const title = chart?.title || opts.title || '';
    const header = `<h3><span>${esc(title)}</span><span class="u">${esc(chart?.unit || '')}</span></h3>`;
    if (!chart) return `<div class="chart">${header}<div class="nodata">Loading…</div></div>`;
    if (chart.error) return `<div class="chart">${header}<div class="nodata">Query failed: ${esc(chart.error)}</div></div>`;
    const series = (chart.series || []).filter((s) => s.points.length);
    if (!series.length) return `<div class="chart">${header}<div class="nodata">${esc(opts.empty || 'No data in this window')}</div></div>`;
    const W = 640, H = opts.height || 170, L = 46, R = 10, T = 10, B = 24, pw = W - L - R, ph = H - T - B;
    let maxV = 0;
    for (const s of series) for (const p of s.points) maxV = Math.max(maxV, p[1]);
    const top = chart.max || niceMax(maxV);
    const xs = (t) => L + ((t - start) / Math.max(1, end - start)) * pw;
    const ys = (v) => T + ph - (Math.max(0, Math.min(v, top)) / top) * ph;
    let svg = '';
    const yTicks = top <= 4 && chart.unit !== '%' ? Array.from({ length: top + 1 }, (_, i) => i) : [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
    for (const v of yTicks) {
      const y = ys(v).toFixed(1);
      svg += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text class="axis" x="${L - 6}" y="${(+y + 4).toFixed(1)}" text-anchor="end">${esc(fmtValue(v, chart.unit))}</text>`;
    }
    for (let i = 0; i <= 5; i++) {
      const t = start + (i / 5) * (end - start);
      const anchor = i === 0 ? 'start' : i === 5 ? 'end' : 'middle';
      svg += `<text class="axis" x="${xs(t).toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${esc(fmtTimeAxis(t))}</text>`;
    }
    const gap = (opts.step || 30) * 3.5;
    series.forEach((s, i) => {
      const color = PALETTE[i % PALETTE.length];
      let d = '';
      let prev = null;
      for (const [t, v] of s.points) {
        const X = xs(t).toFixed(1), Y = ys(v).toFixed(1);
        if (!prev || t - prev > gap) d += `M${X},${Y}`;
        else d += chart.step ? `H${X}V${Y}` : `L${X},${Y}`;
        prev = t;
      }
      svg += `<path d="${d}" fill="none" stroke="${color}" stroke-width="${chart.step ? 2.6 : 2}" stroke-linejoin="round"/>`;
    });
    const legend = series.map((s, i) => {
      const last = s.points[s.points.length - 1];
      return `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(seriesLabel(s.name))} <b>${esc(fmtValue(last && last[1], chart.unit))}</b></span>`;
    }).join('');
    return `<div class="chart">${header}<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${svg}</svg><div class="legend">${legend}</div></div>`;
  }

  // A sparkline for the live DCGM feed: utilization (green), memory used (blue), and power (purple).
  // Power is often the only trace of a short burst: the exporter samples every 30 s by default.
  function spark(history, totalMiB, markers = [], opts = {}) {
    const hist = (history || []).filter((p) => p && p[0]);
    if (hist.length < 2) return '<div class="dim">Collecting samples…</div>';
    const W = opts.width || 320, H = opts.height || 64;
    const t1 = hist[hist.length - 1][0];
    const t0 = Math.max(hist[0][0], t1 - (opts.seconds || 360));
    const pts = hist.filter((p) => p[0] >= t0);
    const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * W;
    const yU = (v) => H - 3 - (Math.min(100, Math.max(0, v || 0)) / 100) * (H - 6);
    const memMax = totalMiB || Math.max(1, ...pts.map((p) => p[2] || 0));
    const yM = (v) => H - 3 - (Math.min(memMax, Math.max(0, v || 0)) / memMax) * (H - 6);
    const powMax = Math.max(75, ...pts.map((p) => (p[3] || 0) * 1.15));
    const yP = (v) => H - 3 - (Math.min(powMax, Math.max(0, v || 0)) / powMax) * (H - 6);
    const path = (fy, idx) => pts.filter((p) => p[idx] != null)
      .map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${fy(p[idx]).toFixed(1)}`).join('');
    const util = path(yU, 1);
    const area = util ? `${util}L${x(t1).toFixed(1)},${H}L${x(pts[0][0]).toFixed(1)},${H}Z` : '';
    let marks = '';
    let marked = false;
    for (const m of markers) {
      if (!m || m.t < t0 || m.t > t1) continue;
      const mx = x(m.t).toFixed(1);
      marks += `<line x1="${mx}" x2="${mx}" y1="0" y2="${H}" stroke="#F2CC60" stroke-width="1.5" stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>`;
      marked = true;
    }
    const minutes = Math.max(1, Math.round((t1 - t0) / 60));
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="GPU utilization, memory, and power, last ${minutes} minutes">`
      + `<path d="${area}" fill="rgba(126,231,135,0.12)"/><path d="${path(yM, 2)}" fill="none" stroke="#58A6FF" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`
      + `<path d="${path(yP, 3)}" fill="none" stroke="#B392F0" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`
      + `<path d="${util}" fill="none" stroke="#7EE787" stroke-width="2" vector-effect="non-scaling-stroke"/>${marks}</svg>`
      + (opts.legend === false ? '' : `<div class="legend"><span><i style="background:#7EE787"></i>utilization</span><span><i style="background:#58A6FF"></i>memory used</span>`
        + `<span><i style="background:#B392F0"></i>power</span>${marked ? '<span><i style="background:#F2CC60"></i>job start and end</span>' : ''}`
        + `<span class="dim">last ${minutes} min · DCGM samples every 30 s by default, so a short burst can fall between samples</span></div>`);
  }

  Object.assign(RD, {
    esc, FAM, PALETTE, toMs, fmtDur, fmtClock, fmtAgo, when, money, gib, shortNode, shortPod, isStale,
    pill, statusIcon, check, stat, codeBlock, jobPill, lineChart, spark, fmtValue,
  });
})(window.RD);
