// Zero-dependency dashboard server.
//   GET  /              dashboard page
//   GET  /api/snapshot  latest merged snapshot + short CPU history
//   POST /api/ingest    JSON from collector/probe.py ({host:{...}}) and/or the cluster
//                       scheduler ({cluster:{...}, jobs:[...]}); parts are merged and
//                       each part keeps its own timestamp. Loopback only.
//                       {tasks:[...]} = progress of running work (feeder in emma-avatar-v3 tools/status).
//   POST /api/refresh   run the feeder now (loopback only)
// The server itself runs feeder.mjs every FEED_MIN minutes (default 5) and once at start, so the dashboard keeps
// refreshing whenever this process is alive: Windows Task Scheduler skipped runs on battery and failed after sleep /
// re-logon (0x80070520), which left the data hours old. The scheduled task is now only a watchdog that restarts this
// server (the feeder starts it when it is down).
const http = require("http");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const PORT = Number(process.env.PORT || 8787);
const DATA = path.join(__dirname, "data", "snapshot.json");
const PUBLIC = path.join(__dirname, "public");
const HISTORY_MAX = 180;

let snap = {};
try { snap = JSON.parse(fs.readFileSync(DATA, "utf8")); } catch { /* start empty */ }
let history = []; // {ts, cpu, gpu}

function pushHistory() {
  const h = snap.host;
  if (!h || !h.cores || !h.cores.length) return;
  const cpu = h.cores.reduce((s, c) => s + c.busy, 0) / h.cores.length;
  const g = h.gpu && h.gpu.ok && h.gpu.devices.length ? h.gpu.devices[0].util : null;
  history.push({ ts: h.ts, cpu: Math.round(cpu * 10) / 10, gpu: g });
  history = history.slice(-HISTORY_MAX);
}
pushHistory();

// ------------------------------------------------------------------ feeder loop
const FEED_MS = Number(process.env.FEED_MIN || 5) * 60_000, FEED_TIMEOUT_MS = 4.5 * 60_000;
const feed = { running: false, last_start: null, last_end: null, last_code: null, last_reason: null, runs: 0, next_due: null };
function runFeed(reason) {
  if (feed.running) return false;
  feed.running = true; feed.last_start = Date.now() / 1000; feed.last_reason = reason; feed.runs++;
  const p = spawn(process.execPath, [path.join(__dirname, "feeder.mjs")], { cwd: __dirname, windowsHide: true, stdio: "ignore",
    env: { ...process.env, FEED_FROM_SERVER: "1" } });
  const kill = setTimeout(() => { try { p.kill(); } catch { /* gone */ } }, FEED_TIMEOUT_MS);
  const done = (code) => { clearTimeout(kill); feed.running = false; feed.last_end = Date.now() / 1000; feed.last_code = code; };
  p.on("exit", done); p.on("error", () => done(-1));
  return true;
}
function schedule() {                       // a timer chain (not setInterval) so a long sleep cannot queue up missed runs
  feed.next_due = Date.now() / 1000 + FEED_MS / 1000;
  setTimeout(() => { runFeed("timer"); schedule(); }, FEED_MS);
}
setTimeout(() => runFeed("start"), 5_000);
schedule();

function ingest(body) {
  for (const key of ["host", "cluster", "jobs", "tasks"]) {
    if (body[key] === undefined) continue;
    snap[key] = body[key];
    snap[`${key}_ts`] = key === "host" && body.host.ts ? body.host.ts : Date.now() / 1000;
  }
  if (body.host) pushHistory();
  fs.mkdirSync(path.dirname(DATA), { recursive: true });
  fs.writeFileSync(DATA, JSON.stringify(snap));
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".json": "application/json" };

http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (req.method === "GET" && url.pathname === "/api/snapshot") {
    res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    return res.end(JSON.stringify({ now: Date.now() / 1000, snap, history, feed }));
  }
  if (req.method === "POST" && url.pathname === "/api/refresh") {
    const ip = req.socket.remoteAddress || "";
    if (!["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(ip)) { res.writeHead(403); return res.end("loopback only"); }
    const started = runFeed("manual");
    res.writeHead(202, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ started, feed }));
  }
  if (req.method === "POST" && url.pathname === "/api/ingest") {
    const ip = req.socket.remoteAddress || "";
    if (!["127.0.0.1", "::1", "::ffff:127.0.0.1"].includes(ip)) { res.writeHead(403); return res.end("loopback only"); }
    let buf = "";
    req.on("data", (c) => { buf += c; if (buf.length > 5e6) req.destroy(); });
    req.on("end", () => {
      try { ingest(JSON.parse(buf)); res.writeHead(204); res.end(); }
      catch (e) { res.writeHead(400); res.end(String(e.message)); }
    });
    return;
  }
  if (req.method === "GET") {
    const file = path.join(PUBLIC, url.pathname === "/" ? "index.html" : path.normalize(url.pathname));
    if (!file.startsWith(PUBLIC)) { res.writeHead(403); return res.end(); }
    return fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); return res.end("not found"); }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
      res.end(data);
    });
  }
  res.writeHead(405); res.end();
}).listen(PORT, "127.0.0.1", () => console.log(`ASTRUM dashboard on http://127.0.0.1:${PORT}`));
