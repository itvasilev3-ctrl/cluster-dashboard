// Zero-dependency dashboard server.
//   GET  /              dashboard page
//   GET  /api/snapshot  latest merged snapshot + short CPU history
//   POST /api/ingest    JSON from collector/probe.py ({host:{...}}) and/or the cluster
//                       scheduler ({cluster:{...}, jobs:[...]}); parts are merged and
//                       each part keeps its own timestamp. Loopback only.
//                       {tasks:[...]} = progress of running work (feeder in emma-avatar-v3 tools/status).
const http = require("http");
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
    return res.end(JSON.stringify({ now: Date.now() / 1000, snap, history }));
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
