#!/usr/bin/env node
// Cluster dashboard feeder: one update per run. The Windows scheduled task "Cluster dashboard feeder" runs it every 5 minutes
// through run-feeder.vbs (no console window), so the dashboard keeps updating without any Claude session.
// `node feeder.mjs --loop` repeats every 5 minutes in this process instead (for testing).
//
//   1. the dashboard server (server.js, http://127.0.0.1:8787) is started detached if it is not running
//   2. scheduler state for ALL clients (capacity and the latest jobs) through the local ASTRA client
//   3. host snapshot and project progress files from the status service on the GPU machine (status/astrum_status_server.py,
//      one long CPU-only ASTRA job, project "astrum-status"); resubmitted when it is not running
//   4. tasks with % done and the CPU/GPU each one uses, posted to the dashboard (POST /api/ingest, loopback only)
// Settings: config.json; the status-service token in .env (STATUS_TOKEN, never in git).
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATE = path.join(HERE, "state");
const CFG = JSON.parse(fs.readFileSync(path.join(HERE, "config.json"), "utf8"));
const DASH = `http://127.0.0.1:${CFG.dashboard_port || 8787}`;
const ASTRA_ROOT = process.env.ASTRA_ROOT || CFG.astra_root || "C:\\Dev\\ASTRA";
const ASTRA_PY = path.join(ASTRA_ROOT, "venv", process.platform === "win32" ? "Scripts\\python.exe" : "bin/python");
const SS = CFG.status_service;
const STATUS = `http://${SS.host}:${SS.port}`;
const CLAUDE_PROJECTS = path.join(os.homedir(), ".claude", "projects");
fs.mkdirSync(STATE, { recursive: true });

const envText = fs.existsSync(path.join(HERE, ".env")) ? fs.readFileSync(path.join(HERE, ".env"), "utf8") : "";
const TOKEN = /^STATUS_TOKEN=(.*)$/m.exec(envText)?.[1]?.trim().replace(/^"|"$/g, "");

function log(...a) {
  const f = path.join(STATE, "feeder.log");
  try { if (fs.statSync(f).size > 1e6) fs.renameSync(f, f + ".1"); } catch { /* first run */ }
  fs.appendFileSync(f, `${new Date().toISOString()} ${a.join(" ")}\n`);
}

function astra(req, timeoutMs = 60_000) {
  const r = spawnSync(ASTRA_PY, [path.join(ASTRA_ROOT, "astra_tool.py")], { input: JSON.stringify(req), cwd: ASTRA_ROOT, encoding: "utf8",
    timeout: timeoutMs, windowsHide: true, maxBuffer: 1 << 28, env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" } });
  if (r.error) throw r.error;
  const out = (r.stdout || "").trim();
  const o = JSON.parse(out.slice(out.indexOf("{")));
  return typeof o.result === "string" ? JSON.parse(o.result) : o;
}

async function get(url, timeoutMs = 8000, headers = {}) {
  const r = await fetch(url, { headers, signal: AbortSignal.timeout(timeoutMs) });
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

async function ensureDashboard() {
  try { await get(`${DASH}/api/snapshot`, 3000); return; } catch { /* not running */ }
  const p = spawn(process.execPath, ["server.js"], { cwd: HERE, detached: true, stdio: "ignore", windowsHide: true,
    env: { ...process.env, PORT: String(CFG.dashboard_port || 8787) } });
  p.unref();
  log("dashboard server was not running; started pid", p.pid);
  await new Promise((r) => setTimeout(r, 1500));
}

// the status service: read it, or (re)submit its job when no such job is queued or running
async function statusService(jobs) {
  if (!TOKEN) return { host: null, progress: [], note: "no STATUS_TOKEN in .env" };
  const H = { Authorization: `Bearer ${TOKEN}` };
  try {
    const [host, progress] = await Promise.all([get(`${STATUS}/probe`, 12000, H), get(`${STATUS}/progress`, 12000, H)]);
    return { host, progress };
  } catch (e) {
    const mine = jobs.filter((j) => j.project === SS.project && ["queued", "running"].includes(j.status));
    if (!mine.length) {
      const pre = `import os\nos.environ.setdefault("HOST", ${JSON.stringify(SS.host)})\nos.environ.setdefault("PORT", "${SS.port}")\n` +
        `os.environ.setdefault("HOURS", "${SS.hours || 24}")\nos.environ.setdefault("PROGRESS_DIRS", ${JSON.stringify((SS.progress_dirs || []).join(":"))})\n`;
      const body = Buffer.from(pre + fs.readFileSync(path.join(HERE, "status", "astrum_status_server.py"), "utf8")).toString("base64");
      // base64 body: the scheduler gives a GPU slot to any job whose code names the GPU libraries or driver
      const code = `import base64\nexec(compile(base64.b64decode("${body}").decode(), "astrum_status_server", "exec"))\n`;
      const r = astra({ action: "cluster_submit", code, project: SS.project, priority: 0, cpu_slots: 1, gpu_slots: 0, memory_mb: 0,
        max_seconds: Math.round((SS.hours || 24) * 3600 + 600), threads_per_process: 0 });
      log("status service unreachable (", e.message, "); submitted", r.job_id || JSON.stringify(r).slice(0, 200));
      return { host: null, progress: [], note: "status service starting (submitted)" };
    }
    return { host: null, progress: [], note: `status service ${mine[0].status}, not answering yet` };
  }
}

// ---------------------------------------------------------------- Claude workflows (agents finished / expected)
function findJournal(run) {
  if (!fs.existsSync(CLAUDE_PROJECTS)) return null;
  for (const proj of fs.readdirSync(CLAUDE_PROJECTS)) {
    let sessions = [];
    try { sessions = fs.readdirSync(path.join(CLAUDE_PROJECTS, proj)); } catch { continue; }
    for (const s of sessions) {
      const f = path.join(CLAUDE_PROJECTS, proj, s, "subagents", "workflows", run, "journal.jsonl");
      if (fs.existsSync(f)) return f;
    }
  }
  return null;
}
function workflowProgress(run, expected) {
  const f = findJournal(run);
  if (!f) return { frac: 0, status: "not found", detail: "journal not found" };
  let started = 0, done = 0;
  for (const l of fs.readFileSync(f, "utf8").split("\n")) {
    if (!l) continue;
    try { const o = JSON.parse(l); if (o.type === "started") started++; else if (o.type === "result") done++; } catch { /* partial line */ }
  }
  const idle = (Date.now() - fs.statSync(f).mtimeMs) / 1000;
  return { frac: Math.min(1, done / Math.max(1, expected)), status: done >= expected ? "done" : idle > 3 * 3600 ? "stalled?" : "running",
    detail: `${done} of ${expected} agents finished, ${Math.max(0, started - done)} running`, idle };
}

const progressFrac = (p) => (p && p.total ? Math.min(1, p.done / p.total) : null);
const progressEta = (p) => (p && p.done && p.started && p.done < p.total ? ((p.ts - p.started) / p.done) * (p.total - p.done) : null);

// ---------------------------------------------------------------- what each job uses: its process tree on the host
function resourcesFor(host) {
  if (!host) return () => null;
  const kids = new Map();
  for (const p of host.procs || []) { if (!kids.has(p.ppid)) kids.set(p.ppid, []); kids.get(p.ppid).push(p); }
  const byPid = new Map((host.procs || []).map((p) => [p.pid, p]));
  const gpuPids = new Set(((host.gpu && host.gpu.apps) || []).map((a) => a.pid));
  const model = (host.gpu && host.gpu.model) || "GPU";
  return (rootPid) => {
    const tree = new Set(), stack = [rootPid];
    while (stack.length) { const pid = stack.pop(); if (tree.has(pid)) continue; tree.add(pid); for (const c of kids.get(pid) || []) stack.push(c.pid); }
    let cpu = 0, rss = 0;
    for (const pid of tree) { const p = byPid.get(pid); if (p) { cpu += p.cpu; rss += p.rss_mb; } }
    const perCore = {};
    for (const [pid, core, pct] of host.threads || []) if (tree.has(pid)) perCore[core] = (perCore[core] || 0) + pct;
    const cores = Object.entries(perCore).filter(([, v]) => v >= 10).sort((a, b) => b[1] - a[1]).map(([c]) => c);
    const gpu = [...tree].filter((pid) => gpuPids.has(pid));
    return { cpu_cores: cpu / 100, cores, rss_gb: rss / 1024, gpu: gpu.length ? `${model} (pid ${gpu.join(", ")})` : null, tree };
  };
}
function usesText(j, r) {
  const cpu = `CPU: ${j.cpu_slots} slot${j.cpu_slots === 1 ? "" : "s"}` + (r ? ` · ${r.cpu_cores.toFixed(1)} cores busy` +
    (r.cores.length ? ` (core ${r.cores.slice(0, 8).join(", ")}${r.cores.length > 8 ? ` +${r.cores.length - 8}` : ""})` : "") + ` · RAM ${r.rss_gb.toFixed(1)} GB` : "");
  const gpu = j.gpu_slots ? `GPU: ${j.gpu_slots} slot · ${r ? (r.gpu ? `in use: ${r.gpu}` : "slot held, no GPU process seen") : "?"}`
    : r && r.gpu ? `GPU: no slot, but uses ${r.gpu}` : "GPU: none";
  return `${cpu} | ${gpu}`;
}

// ---------------------------------------------------------------- tasks
function projectTasks(file, byTask, used) {
  let M;
  try { M = JSON.parse(fs.readFileSync(file, "utf8")); } catch (e) { log("project file", file, e.message); return []; }
  const tasks = [];
  const wfExpected = new Map((M.workflows || []).map((w) => [w.run, w.expected_agents]));
  const partState = (src) => {
    const [kind, arg] = src.split(/:(.*)/s);
    if (kind === "workflow") return workflowProgress(arg, wfExpected.get(arg) || 1);
    if (kind === "progress") {
      used.add(arg); const p = byTask.get(arg);
      return p ? { frac: progressFrac(p) ?? 0, status: p.failed ? "failed" : p.done >= p.total ? "done" : "running", detail: `${p.done}/${p.total} ${p.unit || ""} ${p.note || ""}`.trim(), eta: progressEta(p), job_id: p.job_id }
        : { frac: 0, status: "not started", detail: "" };
    }
    if (kind === "manual") return { frac: Number(arg) || 0, status: Number(arg) >= 1 ? "done" : "running", detail: "" };
    return { frac: 0, status: "?", detail: src };
  };
  const rows = [];
  let doneH = 0;
  for (const m of M.milestones) {
    let frac = m.status === "done" ? 1 : 0;
    const parts = [];
    if (m.status === "active" && m.parts) {
      frac = 0;
      for (const p of m.parts) { const r = partState(p.source); frac += p.weight * r.frac; parts.push({ p, r }); }
    }
    doneH += m.gpu_h * frac;
    if (m.status === "active") {
      rows.push({ name: `${m.id} ${m.name}`, kind: "milestone", status: "in progress", pct: 100 * frac, level: 1, detail: `${m.gpu_h} GPU-h in the plan` });
      for (const { p, r } of parts) rows.push({ name: p.name, kind: p.source.split(":")[0], status: r.status, pct: 100 * r.frac, level: 2,
        detail: r.detail, eta_s: r.eta ?? null, job_id: r.job_id, uses: p.source.startsWith("workflow:") ? "Claude agents (cloud): no cluster CPU/GPU" : null });
    }
  }
  const pct = (100 * doneH) / M.total_gpu_h, retryH = M.total_with_retries_gpu_h || M.total_gpu_h;
  const next = M.milestones.find((m) => m.status !== "done");
  tasks.push({ name: `${M.project} (whole project)`, kind: "project", status: next ? `at ${next.id}` : "done", pct, level: 0,
    detail: `≈${((pct / 100) * retryH).toFixed(1)} of ~${retryH} GPU-h (plan ${M.total_gpu_h} GPU-h + retries); ` +
      `${M.milestones.filter((m) => m.status === "done").length} of ${M.milestones.length} milestones done` });
  tasks.push(...rows);
  const attached = new Set(M.milestones.flatMap((m) => (m.parts || []).map((p) => p.source)));
  for (const w of M.workflows || []) {
    if (attached.has(`workflow:${w.run}`)) continue;
    const r = workflowProgress(w.run, w.expected_agents);
    if (r.status === "done" && r.idle > 3600) continue;
    tasks.push({ name: w.name, kind: "workflow", status: r.status, pct: 100 * r.frac, level: 0, detail: r.detail, uses: "Claude agents (cloud): no cluster CPU/GPU" });
  }
  return tasks;
}

function buildTasks(jobs, progress, host, now) {
  const byTask = new Map(progress.map((p) => [p.task, p]));
  const used = new Set();
  const res = resourcesFor(host);
  const jobRes = new Map(jobs.filter((j) => j.status === "running" && j.pid).map((j) => [j.job_id, { j, r: res(j.pid) }]));
  const tasks = [];
  for (const proj of CFG.projects || []) tasks.push(...projectTasks(proj.milestones, byTask, used));
  for (const t of tasks) if (t.job_id && jobRes.has(t.job_id)) { const { j, r } = jobRes.get(t.job_id); t.uses = usesText(j, r); }
  for (const p of progress) {                                     // other recent progress files
    if (used.has(p.task) || now - (p.ts || 0) > 6 * 3600 || (p.done >= p.total && now - p.ts > 1800)) continue;
    const jr = p.job_id && jobRes.get(p.job_id);
    tasks.push({ name: p.label || p.task, kind: "project job", status: p.failed ? "failed" : p.done >= p.total ? "done" : "running", pct: 100 * (progressFrac(p) ?? 0),
      detail: `${p.done}/${p.total} ${p.unit || ""} ${p.note || ""}`.trim(), eta_s: progressEta(p), uses: jr ? usesText(jr.j, jr.r) : null });
  }
  // every queued or running job on the cluster, any client
  const progByJob = new Map(progress.filter((p) => p.job_id).map((p) => [p.job_id, p]));
  const claimed = new Set();
  for (const j of jobs.filter((x) => ["queued", "running"].includes(x.status))) {
    const p = progByJob.get(j.job_id), jr = jobRes.get(j.job_id);
    if (jr) for (const pid of jr.r?.tree || []) claimed.add(pid);
    const el = j.started_ts ? now - j.started_ts : 0, lim = j.timeout_seconds || 0;
    tasks.push({ name: `${j.client_id} · ${j.project}`, kind: p ? "cluster job" : "cluster job (time used)", status: j.status,
      pct: j.status === "queued" ? 0 : p ? 100 * (progressFrac(p) ?? 0) : lim ? Math.min(100, (100 * el) / lim) : null,
      detail: `${j.job_id} · running ${Math.round(el / 60)} min` + (lim ? ` of max ${(lim / 3600).toFixed(1)} h` : "") +
        (!p && j.status === "running" ? " · no progress report, so % = time used of its limit" : p ? ` · ${p.done}/${p.total} ${p.unit || ""}` : ""),
      eta_s: p ? progressEta(p) : lim && j.status === "running" ? Math.max(0, lim - el) : null,
      uses: j.status === "queued" ? `waiting for CPU ${j.cpu_slots} / GPU ${j.gpu_slots}` : usesText(j, jr?.r || null) });
  }
  // GPU held by a process outside every scheduler job (e.g. an orphaned worker)
  for (const a of (host && host.gpu && host.gpu.apps) || []) {
    if (claimed.has(a.pid)) continue;
    tasks.push({ name: "GPU held outside the scheduler", kind: "warning", status: "running", pct: null, detail: `pid ${a.pid}: ${a.name}`,
      uses: `GPU: ${(host.gpu && host.gpu.model) || "GPU"} (no scheduler job owns this process)` });
  }
  return tasks;
}

async function cycle() {
  const now = Date.now() / 1000;
  await ensureDashboard();
  let cluster = null, jobs = [];
  try {
    cluster = astra({ action: "cluster_capacity" });
    jobs = astra({ action: "cluster_job", job_id: "", client_filter: "", limit: 200 }).jobs || [];   // 200 = the maximum; long jobs fall out of shorter windows
  } catch (e) { log("scheduler:", e.message); }
  const st = await statusService(jobs);
  const tasks = buildTasks(jobs, st.progress || [], st.host, now);
  const body = { tasks };
  if (cluster) { body.cluster = cluster; body.jobs = jobs; }
  if (st.host) body.host = st.host;
  const r = await fetch(`${DASH}/api/ingest`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    signal: AbortSignal.timeout(5000) });
  fs.writeFileSync(path.join(STATE, "last-feed.json"), JSON.stringify({ at: new Date().toISOString(), ingest: r.status, note: st.note || null,
    host: !!st.host, jobs: jobs.length, tasks }, null, 1));
  log(`fed: ingest ${r.status}, ${jobs.length} jobs, ${tasks.length} tasks, host ${st.host ? "ok" : st.note || "missing"}`);
}

const loop = process.argv.includes("--loop");
do {
  try { await cycle(); } catch (e) { log("cycle failed:", e.stack || e.message); }
  if (loop) await new Promise((r) => setTimeout(r, (CFG.refresh_minutes || 5) * 60 * 1000));
} while (loop);
