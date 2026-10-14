// Page controller: polls the server, keeps the UI state, and renders the chrome around the current stage.
'use strict';
(function (RD) {
  const { esc, STAGES } = RD;
  const $ = (id) => document.getElementById(id);
  const WINDOWS = ['15m', '1h', '6h', '24h'];
  const CHART_STAGES = new Set(['monitoring', 'autoscaling']);
  const JOB_STAGE = { 'gpu-check': 'gpucheck', inference: 'inference' };
  const stageIndex = (id) => STAGES.findIndex((st) => st.id === id);
  const ui = {
    stage: 0, follow: true, window: '1h', metrics: null, metricsAt: 0, metricsBusy: false,
    state: null, failures: 0, jobs: null, build: null, mainHtml: '', teleHtml: '', resetScroll: false, toastTimer: 0,
  };

  // ── Data ────────────────────────────────────────────────────────────────
  async function getJson(url) {
    const res = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  }

  async function poll() {
    try {
      const s = await getJson('/api/state');
      ui.failures = 0;
      if (s.loading) { renderLoading(s); return; }
      if (ui.build && s.build && s.build !== ui.build) { location.reload(); return; }
      ui.build = s.build;
      ui.state = s;
      followJobs(s);
      render();
      maybeMetrics();
    } catch (err) {
      ui.failures += 1;
      if (ui.failures >= 3) renderMain();
    } finally {
      setTimeout(poll, 2000);
    }
  }

  async function maybeMetrics(force) {
    if (!ui.state || ui.metricsBusy || !CHART_STAGES.has(STAGES[ui.stage].id)) return;
    const m = ui.metrics;
    const fresh = m && m.window === ui.window && (ui.window === 'replay' || Date.now() - ui.metricsAt < 25000);
    if (fresh && !force) return;
    const want = ui.window;
    ui.metricsBusy = true;
    try {
      const data = await getJson(`/api/metrics?window=${encodeURIComponent(want)}`);
      ui.metrics = { ...data, window: data.window || want };
    } catch (err) {
      ui.metrics = { window: want, configured: true, error: `Couldn't load the charts: ${err.message}` };
    } finally {
      ui.metricsAt = Date.now();
      ui.metricsBusy = false;
      renderMain();
      if (want !== ui.window) maybeMetrics();
    }
  }

  // Jump to a RayJob's stage when it appears or changes status.
  function followJobs(s) {
    const jobs = s.rayJobs.filter((j) => JOB_STAGE[j.kind]);
    const seen = new Map(jobs.map((j) => [j.uid, `${j.jobStatus}|${j.deploymentStatus}`]));
    if (ui.jobs) {
      const j = jobs.find((x) => ui.jobs.get(x.uid) !== seen.get(x.uid));
      if (j) {
        toast(ui.jobs.has(j.uid) ? `RayJob ${j.name}: ${j.jobStatus || j.deploymentStatus}` : `RayJob ${j.name} submitted`);
        if (ui.follow) go(stageIndex(JOB_STAGE[j.kind]));
      }
    }
    ui.jobs = seen;
  }

  // ── Navigation ──────────────────────────────────────────────────────────
  function go(index) {
    if (index < 0 || index >= STAGES.length) return;
    if (index !== ui.stage) ui.resetScroll = true;
    ui.stage = index;
    const hash = '#' + STAGES[index].id;
    if (location.hash !== hash) history.replaceState(null, '', hash);
    render();
    maybeMetrics();
  }

  function setWindow(w) {
    if (w === ui.window) return;
    ui.window = w;
    renderMain();
    maybeMetrics(true);
  }

  function cycleWindow() {
    setWindow(WINDOWS[(WINDOWS.indexOf(ui.window) + 1) % WINDOWS.length]);
    if (!CHART_STAGES.has(STAGES[ui.stage].id)) toast(`Chart window: ${ui.window}`);
  }

  function toggleReplay() {
    const r = ui.state && ui.state.replay;
    if (!r || !r.available) { toast('No recorded run is configured'); return; }
    if (ui.window === 'replay') { setWindow('1h'); return; }
    ui.window = 'replay';
    go(stageIndex('autoscaling'));
  }

  function setFollow(on) {
    ui.follow = on;
    try { localStorage.setItem('rd.follow', on ? '1' : '0'); } catch (e) { /* storage can be blocked */ }
    renderFollow();
    toast(on ? 'Follow live jobs: on' : 'Follow live jobs: off');
  }

  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.remove('show');
    void t.offsetWidth;
    t.classList.add('show');
    clearTimeout(ui.toastTimer);
    ui.toastTimer = setTimeout(() => t.classList.remove('show'), 4000);
  }

  // ── Rendering ───────────────────────────────────────────────────────────
  function render() {
    renderTop();
    renderRail();
    renderMain();
    renderTele();
  }

  function sourceChip(label, src, okText) {
    let cls = 'off';
    let title = 'Not configured';
    if (src && src.configured !== false) {
      if (src.ok === true && src.error) { cls = 'warn'; title = `Partly failing: ${src.error}`; }
      else if (src.ok === true) { cls = 'ok'; title = okText || 'Connected'; }
      else if (src.ok === false) { cls = 'bad'; title = src.error || 'Error'; }
      else { cls = ''; title = 'Waiting for the first response'; }
    }
    return `<span class="src ${cls}" title="${esc(title)}"><i></i>${esc(label)}</span>`;
  }

  // Before the server has a first good snapshot, say why the page is still waiting.
  function renderLoading(s) {
    if (ui.state) return;
    const k = s.sources && s.sources.kubernetes;
    const msg = k && k.ok === false ? `Can't read the Kubernetes API yet: ${k.error || 'unknown error'}` : 'Connecting to the cluster…';
    const html = `<div class="empty"><div class="spinner" aria-hidden="true"></div><p>${esc(msg)}</p></div>`;
    if (ui.mainHtml !== html) { $('main').innerHTML = html; ui.mainHtml = html; }
  }

  function renderTop() {
    const s = ui.state;
    if (!s) return;
    const c = s.cluster;
    const src = s.sources || {};
    const parts = [c.region, c.kubernetesVersion && `Kubernetes ${c.kubernetesVersion}`, `${c.readyNodes} of ${c.nodes} nodes Ready`].filter(Boolean);
    const cluster = `<b>${esc(c.name || 'AKS cluster')}</b> · ${esc(parts.join(' · '))}`;
    if ($('cluster').innerHTML !== cluster) $('cluster').innerHTML = cluster;
    const dcgm = src.dcgm && src.dcgm.nodes === 0 ? { configured: false } : src.dcgm;
    const p = src.prometheus || {};
    const html = sourceChip('Kubernetes', src.kubernetes, src.kubernetes && `Polled every few seconds · ${src.kubernetes.latencyMs} ms`)
      + sourceChip('GPU live (DCGM)', dcgm, 'Read from the GPU node every 2 seconds')
      + sourceChip('Prometheus', p, p.delaySeconds != null ? `Newest sample is ${p.delaySeconds} s old` : 'Connected')
      + sourceChip('Container insights', src.logAnalytics, 'Job output after the pods are gone');
    if ($('sources').innerHTML !== html) $('sources').innerHTML = html;
  }

  function renderFollow() {
    const b = $('follow');
    b.classList.toggle('on', ui.follow);
    b.textContent = ui.follow ? '● Following live jobs' : '○ Follow live jobs';
    b.setAttribute('aria-pressed', String(ui.follow));
  }

  // The rail is built once and updated in place, so the pulsing live icon keeps its rhythm.
  function renderRail() {
    const rail = $('rail');
    if (!rail.children.length) {
      rail.innerHTML = STAGES.map((st, i) => `<button type="button" class="chip" data-go="${i}" style="--fam:${RD.FAM[st.family]}">`
        + `<span class="row">${RD.statusIcon('todo')}<span class="n">${st.num}</span><span class="t">${esc(st.nav)}</span></span>`
        + '<span class="c"></span></button>').join('');
    }
    const info = Object.fromEntries(((ui.state && ui.state.stages) || []).map((x) => [x.id, x]));
    STAGES.forEach((st, i) => {
      const chip = rail.children[i];
      const x = info[st.id] || { status: 'todo', caption: '' };
      chip.classList.toggle('active', i === ui.stage);
      if (i === ui.stage) chip.setAttribute('aria-current', 'step');
      else chip.removeAttribute('aria-current');
      if (chip.dataset.status !== x.status) {
        chip.dataset.status = x.status;
        chip.querySelector('.si').outerHTML = RD.statusIcon(x.status);
      }
      const c = chip.querySelector('.c');
      if (c.textContent !== x.caption) { c.textContent = x.caption; chip.title = `${st.nav}: ${x.caption}`; }
    });
  }

  function errorBar() {
    const msgs = [];
    if (ui.failures >= 3) msgs.push('Lost contact with the dashboard server. Is the port-forward still running? Retrying…');
    const s = ui.state;
    const k = s && s.sources && s.sources.kubernetes;
    if (k && k.ok === false) msgs.push(`Kubernetes API: ${k.error || 'error'}. Showing the last good data.`);
    const age = s ? (Date.now() - RD.toMs(s.generatedAt)) / 1000 : 0;
    if (age > 30) msgs.push(`The cluster data is ${RD.fmtDur(age)} old.`);
    return msgs.length ? `<div class="errorbar">${msgs.map(esc).join('<br>')}</div>` : '';
  }

  function renderMain() {
    const main = $('main');
    if (!ui.state) {
      if (ui.failures >= 3) main.innerHTML = `<div class="empty">${errorBar()}<p>Waiting for the dashboard server…</p></div>`;
      return;
    }
    let body;
    try {
      body = RD.renderStage(STAGES[ui.stage], ui.state, ui);
    } catch (err) {
      console.error(err);
      body = `<div class="callout red"><span class="ic">!</span><div>This stage couldn't render: ${esc(err.message)}</div></div>`;
    }
    const html = errorBar() + body;
    if (html === ui.mainHtml) return;
    const top = ui.resetScroll ? 0 : main.scrollTop;
    main.innerHTML = html;
    main.scrollTop = top;
    ui.resetScroll = false;
    ui.mainHtml = html;
    tick();
  }

  function pickGpu(s) {
    const gpus = s.liveGpus || [];
    const workers = new Set(((s.primaryCluster && s.primaryCluster.workers) || []).map((w) => w.node));
    return gpus.find((g) => workers.has(g.node)) || gpus.slice().sort((a, b) => (b.util || 0) - (a.util || 0))[0];
  }

  const tile = (label, value, sub) => `<div class="tile"><span class="l">${esc(label)}</span><span class="v">${value}</span><span class="s">${sub}</span></div>`;

  function renderTele() {
    const s = ui.state;
    if (!s) return;
    const gpuNodes = s.nodes.filter((n) => n.gpuCapacity > 0 || n.accelerator);
    const alloc = gpuNodes.reduce((a, n) => a + n.gpuAllocatable, 0);
    const held = gpuNodes.reduce((a, n) => a + n.gpuAllocated, 0);
    const pools = new Set(gpuNodes.map((n) => n.pool)).size;
    const g = pickGpu(s);
    const j = s.rayJobs[0];
    const timer = j && (j.running
      ? `<span class="tick" data-since="${esc(j.startTime || j.created)}">${esc(RD.fmtClock(j.durationSeconds))}</span>`
      : esc(RD.fmtDur(j.durationSeconds)));
    const html = [
      tile('GPU nodes', esc(`${gpuNodes.filter((n) => n.ready).length} Ready`), esc(`${gpuNodes.length} in ${pools} pool${pools === 1 ? '' : 's'}`)),
      tile('GPUs held by pods', esc(`${held} of ${alloc}`), 'allocatable nvidia.com/gpu'),
      g ? `<div class="tile split"><span class="l">Live GPU · ${esc(g.short || RD.shortNode(g.node))} · DCGM</span>`
        + `<span class="v" style="color:${RD.isStale(g) ? 'var(--dim)' : 'var(--gpu)'}">${esc(g.util == null ? '—' : `${Math.round(g.util)}%`)}</span>`
        + `${RD.spark(g.history, g.fbTotalMiB, [], { height: 34, legend: false, seconds: 180 })}`
        + `<span class="s">${RD.isStale(g) ? `<b style="color:var(--num)">no reading for ${esc(RD.fmtDur(g.ageSeconds))}</b>`
          : esc(`memory ${RD.gib(g.fbUsedMiB)} of ${RD.gib(g.fbTotalMiB)} · ${g.powerW == null ? '—' : `${Math.round(g.powerW)} W`}`)}</span></div>`
        : tile('Live GPU · DCGM', '—', 'No DCGM exporter reachable'),
      j ? tile('Latest RayJob', esc(j.name), `${RD.jobPill(j)} ${timer}`) : tile('Latest RayJob', '—', 'None yet'),
      tile('Compute now', esc(`${RD.money(s.cost.perHour)}/h`), esc(`GPU ${RD.money(s.cost.gpuPerHour)}/h · list price`)),
    ].join('');
    if (html !== ui.teleHtml) { $('tele').innerHTML = html; ui.teleHtml = html; tick(); }
  }

  function tick() {
    const now = Date.now();
    document.querySelectorAll('.tick[data-since]').forEach((el) => {
      const since = RD.toMs(el.dataset.since);
      if (since) el.textContent = RD.fmtClock((now - since) / 1000);
    });
    $('clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  // ── Input ───────────────────────────────────────────────────────────────
  function onKey(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '')) return;
    const help = $('help');
    const k = e.key;
    if (k === 'Escape') { help.hidden = true; return; }
    if (k === '?') { help.hidden = !help.hidden; e.preventDefault(); return; }
    if (k === 'ArrowRight' || k === 'PageDown') { go(ui.stage + 1); e.preventDefault(); }
    else if (k === 'ArrowLeft' || k === 'PageUp') { go(ui.stage - 1); e.preventDefault(); }
    else if (k === 'Home') { go(0); e.preventDefault(); }
    else if (k === 'End') { go(STAGES.length - 1); e.preventDefault(); }
    else if (/^[0-9]$/.test(k)) go(k === '0' ? 9 : Number(k) - 1);
    else if (k === 'f' || k === 'F') setFollow(!ui.follow);
    else if (k === 't' || k === 'T') cycleWindow();
    else if (k === 'r' || k === 'R') toggleReplay();
  }

  function onClick(e) {
    const el = e.target.closest('[data-go],[data-window],#follow,#helpbtn,#help');
    if (!el) return;
    if (el.id === 'follow') setFollow(!ui.follow);
    else if (el.id === 'helpbtn') $('help').hidden = !$('help').hidden;
    else if (el.id === 'help') { if (e.target === el) el.hidden = true; }
    else if (el.dataset.go != null) go(Number(el.dataset.go));
    else if (el.dataset.window) setWindow(el.dataset.window);
  }

  function init() {
    const dots = ['dev', 'work', 'plat', 'gpu'].map((f) => `<i style="background:var(--${f})"></i>`).join('');
    $('top').innerHTML = `<div class="brand"><div class="dots">${dots}</div><div><div class="kick">Live demo</div><h1>Ray on AKS</h1></div></div>`
      + '<div class="cluster" id="cluster"></div><div class="sources" id="sources"></div>'
      + '<button type="button" class="toggle" id="follow" title="F"></button>'
      + '<button type="button" class="toggle" id="helpbtn" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">?</button>'
      + '<div class="clock" id="clock"></div>';
    try { ui.follow = localStorage.getItem('rd.follow') !== '0'; } catch (e) { /* storage can be blocked */ }
    // ?window=replay (or 15m, 1h, 6h, 24h) opens with that chart window, for a bookmarked starting view.
    const w = new URLSearchParams(location.search).get('window');
    if (w && (WINDOWS.includes(w) || w === 'replay')) ui.window = w;
    const fromHash = stageIndex(location.hash.slice(1));
    if (fromHash >= 0) ui.stage = fromHash;
    renderFollow();
    renderRail();
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', onClick);
    window.addEventListener('hashchange', () => {
      const i = stageIndex(location.hash.slice(1));
      if (i >= 0 && i !== ui.stage) go(i);
    });
    setInterval(tick, 1000);
    tick();
    poll();
  }

  init();
})(window.RD);
