// The ten stages, in the order the docs build them, and a renderer for each.
'use strict';
window.RD = window.RD || {};
(function (RD) {
  const { esc, pill, check, stat, fmtDur, fmtClock, when, money, gib, shortNode, shortPod, jobPill, spark, toMs } = RD;

  const STAGES = [
    {
      id: 'cluster', nav: 'Cluster', family: 'plat', kicker: 'Build the platform',
      title: 'An AKS cluster with an enforcing dataplane',
      doc: 'Quickstart: Run a Ray workload on a single GPU · Prerequisites',
      command: 'az aks create --resource-group $RG --name $AKS `\n  --node-vm-size Standard_D4s_v5 --node-count 1 `\n  --network-plugin azure `\n  --network-plugin-mode overlay `\n  --network-dataplane cilium',
      look: ['Every node reports Ready', 'Each pool shows its node OS: the container host', 'Cilium enforces the network policies used later'],
    },
    {
      id: 'gpu', nav: 'GPU node', family: 'gpu', kicker: 'Build the platform',
      title: "A managed T4 node: Ready isn't schedulable",
      doc: 'Quickstart · Add a GPU node pool',
      command: 'az aks nodepool add --resource-group $RG `\n  --cluster-name $AKS --name raygpu --mode User `\n  --node-vm-size Standard_NC4as_T4_v3 `\n  --node-count 1 --node-taints sku=gpu:NoSchedule `\n  --enable-managed-gpu=true `\n  --gpu-driver-mode DevicePlugin\n\nkubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu\n\n# Both steps: ./scripts/Add-RayGpuNodePool.ps1',
      look: ['Ready and allocatable nvidia.com/gpu are separate signals', 'AKS-managed pools report GPU health conditions', 'The taint keeps everything but GPU work off the node'],
    },
    {
      id: 'kuberay', nav: 'KubeRay', family: 'work', kicker: 'KubeRay and Ray',
      title: 'KubeRay makes a Ray cluster a Kubernetes resource',
      doc: 'Quickstart · Install KubeRay',
      command: 'helm repo add kuberay `\n  https://ray-project.github.io/kuberay-helm/\nhelm install kuberay-operator `\n  kuberay/kuberay-operator --version 1.7.0 `\n  --namespace kuberay-system --create-namespace\nkubectl -n kuberay-system get pods\n\n# Or: ./scripts/Install-KubeRay.ps1',
      look: ['The operator is ready, and its version is pinned', 'Ray clusters and jobs become ray.io/v1 resources', 'Each RayJob gets a Kubernetes submitter Job'],
    },
    {
      id: 'raycluster', nav: 'RayCluster', family: 'work', kicker: 'KubeRay and Ray',
      title: 'A head on a CPU node, a worker on the GPU',
      doc: 'Quickstart · Create the Ray cluster',
      command: 'kubectl apply -f `\n  samples/quickstart/raycluster-t4.yaml\nkubectl -n ray-learn get rayclusters\nkubectl -n ray-learn get pods `\n  -l ray.io/is-ray-node=yes\n\n# Or: ./scripts/Deploy-RayQuickstart.ps1',
      look: ['The GPU is declared twice: Kubernetes allocates it, Ray schedules it', 'The head advertises zero CPUs, so tasks run on workers', "Ray's view comes from metrics; the pods come from Kubernetes"],
    },
    {
      id: 'gpucheck', nav: 'GPU check', family: 'gpu', kicker: 'Run it',
      title: 'Run a GPU task as a RayJob',
      doc: 'Quickstart · Run the GPU check',
      command: './scripts/Invoke-RayQuickstartJobs.ps1 `\n  -Secured -NameSuffix live\n\n# Or, before the isolation in stage 7:\nkubectl apply -f `\n  samples/quickstart/rayjob-gpu-check.yaml\nkubectl -n ray-learn get rayjob -w',
      look: ['Status: blank, PENDING, RUNNING, SUCCEEDED', 'The result names the GPU and the CUDA runtime', 'tensor_sum must be 10.0'],
    },
    {
      id: 'inference', nav: 'Inference', family: 'gpu', kicker: 'Run it',
      title: 'Batch inference: a GPU actor keeps the model loaded',
      doc: 'Quickstart · Run batch inference',
      command: '# The script submits it right after the GPU check.\n# Or, before the isolation in stage 7:\nkubectl apply -f `\n  samples/quickstart/rayjob-inference.yaml',
      look: ['Four records, one pinned model revision', "Highlighted words weren't in the input", 'SUCCEEDED can still be wrong: read DLV-002'],
    },
    {
      id: 'security', nav: 'Security', family: 'plat', kicker: 'Operate',
      title: 'Deny by default, then allow what Ray needs',
      doc: 'Secure Ray clusters on AKS',
      command: './scripts/Protect-RayCluster.ps1 `\n  -WithProxy -WithTokenAuth\n\n# Then rerun both jobs through the isolation:\n./scripts/Invoke-RayQuickstartJobs.ps1 `\n  -Secured -NameSuffix secured',
      look: ['Default deny for ingress and egress', 'Without the token, the Jobs API answers 401', "Probes target the head pod IP, so DNS can't fake isolation"],
    },
    {
      id: 'monitoring', nav: 'Metrics', family: 'num', kicker: 'Operate',
      title: 'Ray metrics, GPU metrics, and job logs',
      doc: 'Monitor Ray workloads on AKS · Scrape Ray metrics',
      command: 'kubectl apply -f `\n  samples/monitoring/ray-t4-podmonitor.yaml\n\n# DCGM series in managed Prometheus also need\n# dcgmexporter = true in the ConfigMap\n# ama-metrics-settings-configmap',
      look: ['Every Ray pod is an up=1 scrape target', 'Charts lag about a minute; the live GPU panel, at most 30 s', 'Short jobs can finish between 30-second samples'],
    },
    {
      id: 'autoscaling', nav: 'Autoscaling', family: 'plat', kicker: 'Operate',
      title: 'Three layers scale GPU workers 1 → 4 → 1',
      doc: 'Autoscale Ray workloads on AKS · Submit and observe a burst',
      command: 'kubectl -n ray-scale exec $HEAD -c ray-head -- `\n  ray job submit --address=http://127.0.0.1:8265 `\n  --no-wait -- `\n  python /home/ray/autoscaling/burst.py `\n  --tasks 4 --duration-seconds 1800 `\n  --timeout-seconds 5400',
      look: ['Ray adds worker pods for queued GPU tasks', 'Pending pods make AKS add GPU nodes', 'Idle workers leave after 180 s; empty nodes after about 10 min'],
    },
    {
      id: 'cost', nav: 'Cost', family: 'num', kicker: 'Operate',
      title: 'GPUs bill while they wait',
      doc: 'Quickstart · Clean up',
      command: './scripts/Remove-RayQuickstart.ps1 -RemoveGpuPool `\n  -ResourceGroup $RG -ClusterName $AKS',
      look: ['A GPU node bills whether or not Ray uses it', "Deleting Ray objects doesn't stop the VM", "Delete or scale the pool when you're done"],
    },
  ];
  STAGES.forEach((s, i) => { s.num = i + 1; });

  // ── Shared pieces ───────────────────────────────────────────────────────
  const gpuNodes = (s) => s.nodes.filter((n) => n.gpuCapacity > 0 || n.accelerator);
  const liveFor = (s, node) => (s.liveGpus || []).find((g) => g.node === node);
  const empty = (text) => `<div class="card"><p class="muted">${esc(text)}</p></div>`;

  function osBadge(families) {
    return (families || []).map((f) => pill(f, f === 'Azure Linux' ? 'blue' : '')).join(' ');
  }

  function liveGpuCard(g, markers) {
    if (!g) return '';
    const stale = RD.isStale(g);
    const util = g.util == null ? '—' : `${Math.round(g.util)}%`;
    const power = g.powerW == null ? '—' : `${Math.round(g.powerW)} W`;
    const staleNote = stale ? ` · <b class="stale-note">no reading for ${esc(fmtDur(g.ageSeconds))}; showing the last one</b>` : '';
    return `<div class="card tint-gpu"><div class="label">Live GPU · ${esc(shortNode(g.node))} · DCGM exporter</div>`
      + `<div class="livegpu${stale ? ' stale' : ''}"><div class="big">${esc(util)}</div><div class="meta">${esc(g.model || 'GPU')} · memory ${esc(gib(g.fbUsedMiB))} of ${esc(gib(g.fbTotalMiB))} · ${esc(power)} · ${esc(g.tempC ?? '—')} °C${staleNote}</div></div>`
      + `${spark(g.history, g.fbTotalMiB, markers)}</div>`;
  }

  function jobMarkers(j) {
    const out = [];
    const start = toMs(j && (j.startTime || j.created));
    if (start) out.push({ t: start / 1000, label: 'start' });
    const end = toMs(j && j.endTime);
    if (end) out.push({ t: end / 1000, label: 'end' });
    return out;
  }

  function timeline(j) {
    const tl = j.timeline || [];
    if (!tl.length) return '';
    const t0 = toMs(j.created) || toMs(tl[0].at);
    // jobStatus and jobDeploymentStatus change separately. Show the job status the docs list,
    // blank first, once each.
    const steps = [];
    for (const e of tl) {
      const label = e.jobStatus || '(blank)';
      if (steps.length && steps[steps.length - 1].label === label) continue;
      steps.push({ label, at: e.at });
    }
    return `<div class="timeline">${steps.map((e) => `<div class="step"><span class="s">${esc(e.label)}</span>`
      + `<span class="at">+${esc(fmtDur((toMs(e.at) - t0) / 1000))}</span></div>`).join('<span class="arrow">→</span>')}</div>`;
  }

  function jobBanner(j) {
    const cls = j.running ? '' : j.jobStatus === 'SUCCEEDED' ? 'done' : 'bad';
    const since = j.startTime || j.created;
    const timer = j.running
      ? `<span class="timer tick" data-since="${esc(since)}">${esc(fmtClock(j.durationSeconds))}</span>`
      : j.durationSeconds == null ? '' : `<span class="timer">${esc(fmtDur(j.durationSeconds))}</span>`;
    const started = j.running ? '' : `<span class="dim">started ${esc(when(since))}</span>`;
    return `<div class="banner ${cls}">${j.running ? '<span class="livebadge">LIVE</span>' : ''}`
      + `<span class="name">${esc(j.name)}</span>${jobPill(j)}`
      + `${j.deploymentStatus ? pill(j.deploymentStatus) : ''}${started}${timeline(j)}${timer}</div>`;
  }

  const SOURCES = { 'pod log': "the submitter pod's log", 'Container insights': 'Container insights' };

  function sourceLine(j) {
    if (!j.resultSource) return '';
    return `<div class="source">Output read from ${esc(SOURCES[j.resultSource] || j.resultSource)} · ${esc(when(j.resultAt))}`
      + `${j.resultNote ? ' · ' + esc(j.resultNote) : ''}</div>`;
  }

  function noResult(j) {
    if (j.running) return '<div class="card"><p class="muted">Waiting for the job to print its result…</p></div>';
    return '<div class="card"><p class="muted">No output found. The submitter pod is gone, and Container insights has no record of it.</p></div>';
  }

  const fmtSum = (v) => (typeof v === 'number' ? v.toFixed(1) : v);

  function resultSummary(j) {
    const r = j.result;
    if (!r) return '<span class="dim">—</span>';
    const ok = r.verified && r.verified.ok;
    const mark = ok ? '<span class="pill ok">✓</span>' : '<span class="pill bad">✗</span>';
    if (j.kind === 'gpu-check' && r.gpu) return `${esc(r.gpu.gpu)} · tensor_sum ${esc(fmtSum(r.gpu.tensor_sum))} ${mark}`;
    if (j.kind === 'inference') return `${(r.records || []).length} records ${mark}`;
    return '';
  }

  function jobTable(jobs) {
    if (!jobs.length) return '';
    const rows = jobs.map((j) => `<tr><td class="mono">${esc(j.name)}</td><td>${jobPill(j)}</td>`
      + `<td>${esc(when(j.startTime || j.created))}</td><td class="num">${esc(fmtDur(j.durationSeconds))}</td>`
      + `<td>${resultSummary(j)}</td><td class="dim">${esc(j.resultSource || '')}</td></tr>`).join('');
    return `<div class="card"><div class="label">Every run on this cluster</div><table><thead><tr><th>RayJob</th><th>Status</th>`
      + `<th>Started</th><th class="num">Took</th><th>Result</th><th>Source</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ── 1. Cluster ──────────────────────────────────────────────────────────
  function cluster(s) {
    const c = s.cluster;
    const facts = `<div class="grid4">${stat(c.kubernetesVersion || '—', 'Kubernetes', { small: true })}`
      + `${stat(`${c.readyNodes} of ${c.nodes}`, 'nodes Ready', { small: true })}`
      + `${stat(c.dataplane || 'unknown', 'network dataplane', { small: true })}`
      + `${stat(c.region || '—', 'region', { small: true })}</div>`;
    const rows = s.pools.map((p) => `<tr><td><b>${esc(p.name)}</b> ${p.gpu ? pill('GPU', 'ok') : ''}</td>`
      + `<td class="mono">${esc(p.vmSize)}</td><td class="num">${p.ready} / ${p.nodes}</td>`
      + `<td>${osBadge(p.osFamilies)} <span class="dim">${esc(p.osImages.join(', '))}</span></td>`
      + `<td class="mono dim">${esc(p.nodeImages.join(', '))}</td>`
      + `<td class="num">${p.gpu ? `${p.gpuAllocated} of ${p.gpuAllocatable}` : '—'}</td></tr>`).join('');
    const table = `<div class="card"><div class="label">Node pools</div><table><thead><tr><th>Pool</th><th>VM size</th>`
      + `<th class="num">Ready</th><th>Node OS (container host)</th><th>Node image</th><th class="num">GPUs in use</th></tr></thead>`
      + `<tbody>${rows}</tbody></table></div>`;
    const families = c.osFamilies || [];
    const callout = families.includes('Azure Linux')
      ? `<div class="callout blue"><span class="ic">i</span><div><b>Azure Linux is running here.</b> Pools with the Azure Linux badge use Microsoft's container host; the same Ray images run on every pool.</div></div>`
      : `<div class="callout blue"><span class="ic">i</span><div><b>Every pool here runs ${esc(families.join(' and ') || 'Linux')}.</b> For Azure Linux, create a pool with <code>--os-sku AzureLinux</code>. The Ray images bring their own userspace, so they run unchanged; check that your GPU driver option supports it first.</div></div>`;
    const nodeRows = s.nodes.map((n) => `<tr><td class="mono">${esc(n.short)}</td><td>${n.ready ? pill('Ready', 'ok') : pill('NotReady', 'bad')}</td>`
      + `<td>${esc(n.osImage || '')}</td><td class="mono dim">${esc(n.kernel || '')}</td>`
      + `<td class="mono dim">${esc((n.containerRuntime || '').replace('containerd://', 'containerd '))}</td>`
      + `<td class="dim">${esc(when(n.created))}</td></tr>`).join('');
    const nodes = `<div class="card"><div class="label">Nodes · what the container host reports</div><table><thead><tr><th>Node</th><th>Status</th>`
      + `<th>OS image</th><th>Kernel</th><th>Runtime</th><th>Created</th></tr></thead><tbody>${nodeRows}</tbody></table></div>`;
    return facts + table + callout + nodes;
  }

  // ── 2. GPU node ─────────────────────────────────────────────────────────
  function gpu(s) {
    const nodes = gpuNodes(s);
    if (!nodes.length) return empty('No GPU nodes yet. Add a GPU node pool to continue.');
    const cards = nodes.map((n) => {
      const holders = n.gpuHolders.length
        ? n.gpuHolders.map((h) => `${esc(h.namespace)}/${esc(shortPod(h.pod))}`).join(', ')
        : '<span class="dim">nobody: the GPU is free, and still billing</span>';
      const conds = n.gpuConditions.map((c) => check(c.status === 'False', c.type, c.status)).join('');
      return `<div class="card"><h3>${esc(shortNode(n.name))} <span class="dim mono">${esc(n.vmSize)}</span>`
        + ` ${n.managedGpu ? pill('AKS-managed GPU', 'ok') : pill('driver only + device plugin', 'pend')}</h3>`
        + `<div class="grid2">${check(n.ready, 'Node Ready', n.ready ? 'True' : 'False')}`
        + `${check(n.gpuAllocatable > 0, 'Allocatable nvidia.com/gpu', String(n.gpuAllocatable))}</div>`
        + `<dl class="kv"><dt>Held by</dt><dd>${holders}</dd><dt>Taints</dt><dd class="mono">${esc(n.taints.join(', ') || 'none')}</dd>`
        + `<dt>Node OS</dt><dd>${esc(n.osImage || '')} · kernel ${esc(n.kernel || '')}</dd></dl>`
        + (conds ? `<div class="label">GPU health conditions</div><div class="checks compact">${conds}</div>` : '')
        + '</div>';
    }).join('');
    const live = nodes.map((n) => liveGpuCard(liveFor(s, n.name))).join('');
    const lesson = '<div class="callout"><span class="ic">!</span><div><b>Ready isn\'t schedulable.</b> A node can report Ready before the device plugin registers <code>nvidia.com/gpu</code>. Work submitted in that gap fails, so wait for the allocatable GPU.</div></div>';
    return lesson + `<div class="grid2">${cards}</div>` + live;
  }

  // ── 3. KubeRay ──────────────────────────────────────────────────────────
  function kuberay(s) {
    const k = s.kuberay;
    if (!k.found) return empty('The KubeRay operator isn\'t installed in this cluster.');
    const used = { RayCluster: k.rayClusters, RayJob: k.rayJobs };
    const kinds = (k.kinds.length ? k.kinds : ['RayCluster', 'RayCronJob', 'RayJob', 'RayService']).map((kind) =>
      `<div class="card ${used[kind] ? 'tint-work' : ''}"><div class="stat"><span class="v sm mono">${esc(kind)}</span>`
      + `<span class="l">${used[kind] != null ? `${used[kind]} on this cluster` : 'not used here'}</span></div></div>`).join('');
    const op = `<div class="grid4">${stat(k.version || '—', 'operator version')}${stat(`${k.ready} / ${k.desired}`, 'replicas ready')}`
      + `${stat(String(k.restarts), 'restarts')}${stat(shortNode(k.node || '') || '—', 'runs on', { small: true })}</div>`;
    const clusters = s.rayClusters.map((c) => `<tr><td class="mono">${esc(c.namespace)}/${esc(c.name)}</td><td>${pill(c.state, c.state === 'ready' ? 'ok' : 'pend')}</td>`
      + `<td class="num">${c.readyWorkers}${c.desiredWorkers != null ? ' / ' + esc(c.desiredWorkers) : ''}</td><td>${esc(c.authMode)}</td>`
      + `<td>${c.autoscaling ? pill('autoscaling', 'blue') : '<span class="dim">fixed</span>'}</td><td>${esc(c.rayVersion || '')}</td></tr>`).join('');
    return op + `<div class="label">Custom resources · ray.io/v1</div><div class="grid4">${kinds}</div>`
      + `<div class="card"><div class="label">Ray clusters the operator manages</div><table><thead><tr><th>RayCluster</th><th>State</th>`
      + `<th class="num">Workers</th><th>Auth</th><th>Scaling</th><th>Ray</th></tr></thead><tbody>${clusters}</tbody></table>`
      + `<p class="source">Image <span class="mono">${esc(k.image || '')}</span> · namespace ${esc(k.namespace || '')}</p></div>`;
  }

  // ── 4. RayCluster ───────────────────────────────────────────────────────
  function raycluster(s) {
    const c = s.primaryCluster;
    if (!c) return empty('The quickstart RayCluster doesn\'t exist yet.');
    const byNode = {};
    for (const p of [...c.head.map((h) => ({ ...h, role: 'head' })), ...c.workers.map((w) => ({ ...w, role: 'worker' }))]) {
      (byNode[p.node || 'unscheduled'] = byNode[p.node || 'unscheduled'] || []).push(p);
    }
    const nodeBoxes = Object.entries(byNode).map(([name, pods]) => {
      const node = s.nodes.find((n) => n.name === name);
      const isGpu = node && (node.gpuCapacity > 0 || node.accelerator);
      const podHtml = pods.map((p) => `<div class="pod ${p.phase === 'Pending' ? 'pending' : ''}"><div class="pn">Ray ${p.role} pod · ${esc(p.phase)}${p.ready ? ' ✓' : ''}</div>`
        + `<div class="pm">${esc(p.name)}</div></div>`).join('');
      return `<div class="node ${isGpu ? 'gpu' : ''}"><div class="nh"><b>${isGpu ? 'GPU node' : 'CPU node'}</b>`
        + `<span class="mono dim">${esc(shortNode(name))} · ${esc(node ? node.vmSize : '')}</span></div>${podHtml}`
        + (isGpu ? `<div class="gpuchip">NVIDIA GPU allocated to the worker pod</div>` : '') + '</div>';
    }).join('');
    const g = c.workerGroups[0] || {};
    const rp = g.rayStartParams || {};
    const twice = `<div class="card"><div class="label">The GPU, declared twice</div><div class="grid2">`
      + `<div><b class="mono" style="color:var(--plat)">Kubernetes</b><dl class="kv"><dt>nvidia.com/gpu</dt><dd>${esc(g.gpuLimit)}</dd>`
      + `<dt>memory</dt><dd>${esc(g.memoryLimit || '—')}</dd><dt>cpu</dt><dd>${esc(g.cpuLimit || '—')}</dd><dt>pool</dt><dd>${esc(g.pool || '—')}</dd></dl></div>`
      + `<div><b class="mono" style="color:var(--work)">Ray</b><dl class="kv"><dt>num-gpus</dt><dd>${esc(rp['num-gpus'] ?? '—')}</dd>`
      + `<dt>num-cpus</dt><dd>${esc(rp['num-cpus'] ?? '—')}</dd><dt>object store</dt><dd>${esc(rp['object-store-memory'] ? (rp['object-store-memory'] / 1073741824).toFixed(0) + ' GiB' : '—')}</dd>`
      + `<dt>head num-cpus</dt><dd>${esc(c.headParams['num-cpus'] ?? '—')}</dd></dl></div></div></div>`;
    const rr = (s.prometheus && s.prometheus.rayResources || []).filter((r) => r.ray_io_cluster === c.name);
    const val = (name, state) => (rr.find((r) => r.name === name && r.state === state) || {}).value;
    const res = (name) => {
      const used = val(name, 'used'), avail = val(name, 'available');
      return used == null && avail == null ? '—' : `${used ?? 0} of ${(used ?? 0) + (avail ?? 0)}`;
    };
    const delay = s.sources.prometheus && s.sources.prometheus.delaySeconds;
    const rayView = `<div class="card"><div class="label">Ray's view · from metrics${delay != null ? `, about ${esc(delay)} s behind` : ''}</div>`
      + `<div class="grid2">${stat(res('gpu'), 'logical GPUs in use', { small: true })}${stat(res('cpu'), 'logical CPUs in use', { small: true })}</div></div>`;
    const head = `<div class="grid4">${stat(c.state, 'RayCluster state', { small: true, color: c.state === 'ready' ? 'var(--gpu)' : 'var(--num)' })}`
      + `${stat(c.rayVersion || '—', 'Ray version', { small: true })}${stat(c.authMode, 'authentication', { small: true })}`
      + `${stat(c.autoscaling ? 'on' : 'off', 'in-tree autoscaling', { small: true })}</div>`;
    return head + `<div class="nodes">${nodeBoxes}</div><div class="grid2">${twice}${rayView}</div>`;
  }

  // ── 5. GPU check ────────────────────────────────────────────────────────
  function gpucheck(s) {
    const jobs = s.rayJobs.filter((j) => j.kind === 'gpu-check');
    const j = jobs[0];
    if (!j) return empty('No GPU check has run yet. Run the command on the right.');
    const r = j.result && j.result.gpu;
    let result = noResult(j);
    if (r) {
      const v = j.result.verified || { checks: [] };
      result = `<div class="result-hero">${stat(r.gpu, 'the GPU the task saw', { color: 'var(--gpu)', small: true })}`
        + `${stat(r.cuda_runtime, 'CUDA runtime', { small: true })}${stat(r.torch_version, 'PyTorch', { small: true })}`
        + `${stat(fmtSum(r.tensor_sum), 'tensor_sum (expect 10.0)', { small: true, color: v.ok ? 'var(--gpu)' : 'var(--red)' })}</div>`
        + `<div class="checks">${v.checks.map((c) => check(c.ok, c.label)).join('')}</div>${sourceLine(j)}`;
    }
    const g = (s.liveGpus || []).find((x) => s.primaryCluster && s.primaryCluster.workers.some((w) => w.node === x.node));
    return jobBanner(j) + result + liveGpuCard(g, jobMarkers(j)) + jobTable(jobs);
  }

  // ── 6. Inference ────────────────────────────────────────────────────────
  function markNovel(text, novel) {
    return String(text || '').split(/([A-Za-z0-9:']+)/)
      .map((part, i) => (i % 2 === 1 && novel.has(part) ? `<mark>${esc(part)}</mark>` : esc(part))).join('');
  }

  function inference(s) {
    const jobs = s.rayJobs.filter((j) => j.kind === 'inference');
    const j = jobs[0];
    if (!j) return empty('No inference job has run yet. The script runs it after the GPU check.');
    let body = noResult(j);
    const records = j.result && j.result.records;
    if (records && records.length) {
      const cards = records.map((r) => {
        const novel = new Set(r.novelWords || []);
        return `<div class="record ${r.reviewNote ? 'flag' : ''}"><span class="id">${esc(r.id)}</span>`
          + `<div class="in">${esc(r.event)}</div><div class="out">${markNovel(r.summary, novel)}</div>`
          + (r.reviewNote ? `<div class="note">${esc(r.reviewNote)}</div>` : '') + '</div>';
      }).join('');
      const first = records[0];
      const v = j.result.verified || { checks: [] };
      body = `<div class="records">${cards}</div>`
        + `<div class="grid2"><div class="checks">${v.checks.map((c) => check(c.ok, c.label)).join('')}</div>`
        + `<dl class="kv card"><dt>Model</dt><dd class="mono">${esc(first.model || '')}</dd><dt>Revision</dt>`
        + `<dd class="mono">${esc((first.modelRevision || '').slice(0, 12))}…</dd><dt>GPU</dt><dd>${esc(first.gpu || '')}</dd>`
        + `<dt>Marked words</dt><dd class="muted">Not in the input. A reading aid, not a verdict.</dd></dl></div>${sourceLine(j)}`;
    }
    const g = (s.liveGpus || []).find((x) => s.primaryCluster && s.primaryCluster.workers.some((w) => w.node === x.node));
    return jobBanner(j) + body + liveGpuCard(g, jobMarkers(j)) + jobTable(jobs);
  }

  // ── 7. Security ─────────────────────────────────────────────────────────
  function security(s) {
    const sec = s.security;
    const checks = `<div class="card"><div class="label">Controls on ${esc(sec.namespace)}, read live</div><div class="checks">`
      + sec.checks.map((c) => check(c.ok, c.label, c.detail)).join('') + '</div></div>';
    const policies = sec.policies.length
      ? `<div class="card"><div class="label">${sec.policies.length} network policies</div><table><thead><tr><th>Policy</th><th>Selects</th><th>Types</th></tr></thead><tbody>`
        + sec.policies.map((p) => `<tr><td class="mono">${esc(p.name)} ${p.defaultDeny ? pill('default deny', 'bad') : ''}</td>`
          + `<td class="mono dim">${esc(Object.entries(p.selector).map(([k, v]) => `${k}=${v}`).join(', ') || 'every pod')}</td>`
          + `<td>${esc(p.types.join(', '))}</td></tr>`).join('') + '</tbody></table></div>'
      : empty('No network policies in this namespace yet.');
    const probes = (sec.probes || []).length
      ? `<div class="card"><div class="label">Probe results · ${esc(sec.probes[0].source || '')} · ${esc(when(sec.probes[0].time))}</div><table><thead><tr><th>Probe pod</th><th>Check</th><th>Result</th><th></th></tr></thead><tbody>`
        + sec.probes.flatMap((p) => p.checks.filter((c) => c.check !== 'dns').map((c) => `<tr><td>${esc(p.title)}</td><td>${esc(c.label)}</td>`
          + `<td class="mono">${esc(c.status)}</td><td>${c.ok === true ? pill('as expected', 'ok') : c.ok === false ? pill('unexpected', 'bad') : ''}</td></tr>`)).join('')
        + '</tbody></table></div>'
      : '<div class="card"><p class="muted">No probe results yet. The security article\'s probes print one JSON line per check.</p></div>';
    const note = sec.tokenSecretNote ? `<div class="callout blue"><span class="ic">i</span><div>${esc(sec.tokenSecretNote)}</div></div>` : '';
    return `<div class="grid2">${checks}${policies}</div>${probes}${note}`;
  }

  // ── 8. Metrics ──────────────────────────────────────────────────────────
  function windowButtons(s, ui) {
    const btns = ['15m', '1h', '6h', '24h'].map((w) => `<button type="button" data-window="${w}" class="${ui.window === w ? 'on' : ''}">${w}</button>`).join('');
    const replay = s.replay && s.replay.available
      ? `<button type="button" data-window="replay" class="replay ${ui.window === 'replay' ? 'on' : ''}">Replay: ${esc(s.replay.label)}</button>` : '';
    return `<div class="windows"><span class="dim">Charts</span>${btns}${replay}</div>`;
  }

  const CHART_TITLES = {
    gpuUtil: 'GPU utilization', gpuMemory: 'GPU memory used', rayGpus: 'GPUs Ray has handed out',
    rayTasks: 'Ray tasks running (sample code)', gpuNodes: 'GPU nodes', readyWorkers: 'Ready Ray workers',
  };

  function charts(ids, ui, opts = {}) {
    const m = ui.metrics;
    if (!m || m.window !== ui.window) return ids.map((id) => RD.lineChart(null, 0, 1, { title: CHART_TITLES[id] })).join('');
    if (m.configured === false) return '';
    if (m.error) return `<div class="callout red"><span class="ic">!</span><div>${esc(m.error)}</div></div>`;
    return ids.map((id) => RD.lineChart(m.charts && m.charts[id], m.start, m.end, { step: m.step, title: CHART_TITLES[id], empty: opts.empty && opts.empty[id] })).join('');
  }

  function promMissing(s) {
    return s.sources.prometheus && s.sources.prometheus.configured ? ''
      : '<div class="callout"><span class="ic">i</span><div><b>Charts are off:</b> no managed Prometheus endpoint or identity is configured. Live GPU data and everything from Kubernetes still work. See dashboard/README.md.</div></div>';
  }

  function monitoring(s, ui) {
    const mon = s.monitoring;
    const targets = (s.prometheus && s.prometheus.targets) || [];
    const up = targets.filter((t) => t.value === 1).length;
    const delay = s.sources.prometheus && s.sources.prometheus.delaySeconds;
    const facts = `<div class="grid4">${stat(String(mon.podMonitors.length), 'PodMonitors')}`
      + `${stat(targets.length ? `${up} of ${targets.length}` : '—', 'Ray scrape targets up')}`
      + `${stat(String(mon.dcgmNodes.length), 'nodes with a DCGM exporter')}`
      + `${stat(delay != null ? `${delay} s` : '—', 'Prometheus data delay')}</div>`;
    const rows = targets.map((t) => `<tr><td class="mono">${esc(t.namespace)}/${esc(shortPod(t.pod))}</td><td>${esc(t.ray_node_type || '')}</td>`
      + `<td>${t.value === 1 ? pill('up', 'ok') : pill('down', 'bad')}</td></tr>`).join('');
    const table = targets.length ? `<div class="card"><div class="label">Scrape targets · up{namespace=~"…"}</div><table><tbody>${rows}</tbody></table></div>` : '';
    return promMissing(s) + facts
      + `<div class="grid2">${charts(['gpuUtil', 'gpuMemory'], ui)}</div>`
      + `<div class="grid2">${charts(['rayGpus', 'rayTasks'], ui, { empty: { rayTasks: 'No Ray tasks ran in this window' } })}</div>`
      + table
      + '<p class="source">Charts: Azure Monitor managed Prometheus (30-second scrapes, then ingestion). The live GPU panels read the DCGM exporter on the node directly, so they lead the charts; the exporter itself samples every 30 seconds by default.</p>';
  }

  // ── 9. Autoscaling ──────────────────────────────────────────────────────
  function autoscaling(s, ui) {
    const a = s.autoscaling;
    if (!a.found) return empty('The autoscaling RayCluster isn\'t deployed on this cluster.');
    const o = a.cluster.autoscaler || {};
    const config = `<div class="windows">${pill(`Ray autoscaler ${o.version || ''}`, 'purple')}${pill(o.upscalingMode || 'Default')}`
      + `${pill(`idle timeout ${o.idleTimeoutSeconds ?? '—'} s`)}${pill(`workers ${a.minWorkers}–${a.maxWorkers}`)}${pill(`node pool ${a.pool || '—'}`, 'blue')}</div>`;
    const facts = `<div class="grid4">${stat(`${a.readyWorkers} of ${a.maxWorkers}`, 'Ray workers ready', { color: 'var(--work)' })}`
      + `${stat(String(a.pendingWorkers), 'worker pods waiting for a GPU', { color: a.pendingWorkers ? 'var(--num)' : undefined })}`
      + `${stat(`${a.poolReadyNodes} of ${a.poolNodes}`, `${a.pool} nodes Ready`, { color: 'var(--plat)' })}`
      + `${stat(String(a.poolAllocatableGpus), 'GPUs allocatable in the pool', { color: 'var(--gpu)' })}</div>`;
    const replayNote = ui.window === 'replay'
      ? `<div class="callout"><span class="ic">▶</span><div><b>Replaying ${esc(s.replay.label)}</b> from managed Prometheus: four 30-minute GPU tasks scaled the pool from one node to four and back.</div></div>` : '';
    const events = a.events.length
      ? `<div class="card"><div class="label">Recent events in ${esc(a.cluster.namespace)}</div><table><tbody>`
        + a.events.slice(0, 6).map((e) => `<tr><td class="dim">${esc(when(e.time))}</td><td>${pill(e.reason, e.type === 'Warning' ? 'pend' : '')}</td>`
          + `<td class="mono dim">${esc(e.object)}</td><td>${esc(e.message)}</td></tr>`).join('') + '</tbody></table></div>' : '';
    return config + facts + replayNote + promMissing(s)
      + `<div class="grid2">${charts(['gpuNodes', 'readyWorkers'], ui)}</div>`
      + `<div class="grid2">${charts(['rayGpus', 'gpuUtil'], ui)}</div>` + events;
  }

  // ── 10. Cost ────────────────────────────────────────────────────────────
  function cost(s) {
    const c = s.cost;
    const facts = `<div class="grid4">${stat(money(c.perHour) + '/h', 'compute right now', { color: 'var(--num)' })}`
      + `${stat(money(c.perDay), 'per day at this size')}${stat(money(c.perMonth, 0), 'per month (730 h)')}`
      + `${stat(money(c.gpuPerHour) + '/h', 'of that is GPU', { color: 'var(--gpu)' })}</div>`;
    const rows = s.pools.map((p) => `<tr><td><b>${esc(p.name)}</b></td><td class="mono">${esc(p.vmSize)}</td><td class="num">${p.nodes}</td>`
      + `<td class="num">${esc(money(p.pricePerHour, 3))}</td><td class="num"><b>${esc(money(p.costPerHour, 3))}</b></td></tr>`).join('');
    const promUtil = (s.prometheus && s.prometheus.gpuUtil) || [];
    const priceOf = (n) => (s.pools.find((p) => p.name === n.pool) || {}).pricePerHour || 0;
    const idle = [];
    const gpuRows = gpuNodes(s).map((n) => {
      const live = liveFor(s, n.name);
      const util = live && live.util != null ? live.util : (promUtil.find((u) => u.kubernetes_node === n.name) || {}).value;
      const held = n.gpuAllocated > 0;
      const busy = util != null && util > 5;
      if (!held || (util != null && !busy)) idle.push(n);
      const tag = !held ? pill('free, still billing', 'bad') : busy ? pill('busy', 'ok') : pill('held, idle', 'pend');
      return `<tr><td>${esc(shortNode(n.name))}</td><td>${n.gpuHolders.map((h) => esc(shortPod(h.pod))).join(', ') || '<span class="dim">no pod</span>'}</td>`
        + `<td class="num">${util == null ? '—' : esc(Math.round(util) + '%')}</td><td>${tag}</td><td class="dim">${live ? 'DCGM, live' : 'Prometheus'}</td></tr>`;
    }).join('');
    const idleCost = idle.reduce((a, n) => a + priceOf(n), 0);
    const callout = idle.length
      ? `<div class="callout red"><span class="ic">$</span><div><b>${idle.length} of ${gpuNodes(s).length} GPU${gpuNodes(s).length === 1 ? ' is' : 's are'} idle right now: `
        + `${esc(money(idleCost))}/h, ${esc(money(idleCost * 24))} a day.</b> Deleting the RayCluster doesn't stop the VM; `
        + 'delete the GPU pool, or scale it down, when the demo is over.</div></div>'
      : '';
    return facts + callout
      + `<div class="grid2"><div class="card"><div class="label">By node pool · list price</div><table><thead><tr><th>Pool</th><th>VM size</th>`
      + `<th class="num">Nodes</th><th class="num">$/h each</th><th class="num">$/h</th></tr></thead><tbody>${rows}</tbody></table></div>`
      + `<div class="card"><div class="label">Each GPU: held versus busy</div><table><thead><tr><th>Node</th><th>Held by</th>`
      + `<th class="num">Util.</th><th></th><th>Source</th></tr></thead><tbody>${gpuRows}</tbody></table></div></div>`
      + `<p class="source">${esc(c.note)}</p>`;
  }

  // ── Frame ───────────────────────────────────────────────────────────────
  const RENDER = { cluster, gpu, kuberay, raycluster, gpucheck, inference, security, monitoring, autoscaling, cost };

  function renderStage(meta, s, ui) {
    const right = meta.id === 'monitoring' || meta.id === 'autoscaling' ? windowButtons(s, ui) : '';
    return `<div class="stage" style="--fam:${RD.FAM[meta.family]}">`
      + `<div class="stage-head"><div><div class="kicker">Stage ${meta.num} <span class="dim">· ${esc(meta.kicker)}</span></div>`
      + `<h2>${esc(meta.title)}</h2><div class="docref">${esc(meta.doc)}</div></div>${right}</div>`
      + `<div class="stage-body"><div class="stage-main">${RENDER[meta.id](s, ui)}</div>`
      + `<aside class="stage-side"><div><div class="label">From the docs</div>${RD.codeBlock(meta.command)}</div>`
      + `<div class="card"><div class="label">What to look for</div><ul class="look">${meta.look.map((l) => `<li>${esc(l)}</li>`).join('')}</ul></div>`
      + '</aside></div></div>';
  }

  Object.assign(RD, { STAGES, renderStage });
})(window.RD);
