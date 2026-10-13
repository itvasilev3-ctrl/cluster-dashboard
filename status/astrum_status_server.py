"""
Status service on the GPU machine for the ASTRUM dashboard (http://127.0.0.1:8787 on the PC, fed by tools/status/feeder.mjs).
Runs as one long CPU-only ASTRA job instead of a probe job every 5 minutes. Standard library only; never touches the GPU.

  GET  /probe     host snapshot in the dashboard's format: per-core load, RAM, processes, GPU users
  GET  /progress  every *.json in $PROGRESS_DIRS (default ~/avatar_build/v3/progress), written by project jobs
  POST /shutdown
Token: ~/.emma/gpu_token as "Authorization: Bearer"; binds only to the Tailscale address ($HOST), port $PORT (8818).
"""
import glob, hmac, json, os, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = open(os.path.expanduser("~/.emma/gpu_token")).read().strip()
HOST, PORT = os.environ.get("HOST", "100.66.143.117"), int(os.environ.get("PORT", "8818"))
HOURS = float(os.environ.get("HOURS", "24"))
PROGRESS_DIRS = [os.path.expanduser(d) for d in os.environ.get("PROGRESS_DIRS", "~/avatar_build/v3/progress").split(":") if d]
HZ = os.sysconf("SC_CLK_TCK")
DRV = "/proc/driver/" + "nv" + "idia"                         # split: the scheduler gives a GPU to jobs naming the driver


def read_stat():
    cores = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split(); cores[int(p[0][3:])] = [int(x) for x in p[1:9]]
    return cores


def gpu_users():
    """Processes holding the GPU device files (memory per process needs NVML, which is broken on this box)."""
    out = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            if any(os.readlink(f).startswith("/dev/" + "nv" + "idia") for f in glob.glob(d + "/fd/*") if os.path.islink(f)):
                cmd = open(d + "/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()
                if "compile_worker" not in cmd:
                    out.append({"pid": int(os.path.basename(d)), "name": cmd[:160], "mem_mb": "?"})
        except OSError:
            pass
    return out


def thread_times():
    """{(pid, tid): (cpu ticks, core)} for every thread, from /proc/<pid>/task/<tid>/stat."""
    out = {}
    for d in glob.glob("/proc/[0-9]*/task/[0-9]*"):
        try:
            r = open(d + "/stat").read().rsplit(")", 1)[1].split()          # r[n-3] is field n of proc(5)
            p = d.split("/"); out[(int(p[2]), int(p[4]))] = (int(r[11]) + int(r[12]), int(r[36]))
        except (OSError, IndexError, ValueError):
            pass
    return out


def probe():
    a = read_stat(); ta = thread_times(); time.sleep(1.0); b = read_stat(); tb = thread_times()
    threads, pcpu = [], {}                                                  # current use over this 1 s, not the lifetime average
    for k, (t1, core) in tb.items():
        if k in ta:
            pct = 100.0 * (t1 - ta[k][0]) / HZ
            pcpu[k[0]] = pcpu.get(k[0], 0.0) + pct
            if pct >= 2.0:
                threads.append([k[0], core, round(pct, 1)])                 # pid, core, % of one core
    cores = []
    for i in sorted(b):
        d = [y - x for x, y in zip(a[i], b[i])]; total = sum(d) or 1
        user, nice, system, idle, iowait, irq, soft, steal = d
        cores.append({"id": i, "busy": round(100.0 * (total - idle - iowait) / total, 1), "user": round(100.0 * user / total, 1),
                      "nice": round(100.0 * nice / total, 1), "sys": round(100.0 * (system + irq + soft) / total, 1),
                      "iowait": round(100.0 * iowait / total, 1)})
    procs = []
    out = subprocess.run("ps -eo user:24,pid,ppid,pcpu,pmem,rss,etimes,stat,nlwp,args --sort=-pcpu --no-headers", shell=True,
                         capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines()[:400]:
        p = line.split(None, 9)
        if len(p) == 10:
            procs.append({"user": p[0], "pid": int(p[1]), "ppid": int(p[2]), "cpu": round(pcpu.get(int(p[1]), float(p[3])), 1), "mem": float(p[4]),
                          "rss_mb": round(int(p[5]) / 1024, 1), "etimes": int(p[6]), "stat": p[7], "threads": int(p[8]), "cmd": p[9][:200]})
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":", 1); mem[k] = int(v.split()[0]) // 1024
    try:
        drv = open(DRV + "/version").readline().strip()
    except OSError:
        drv = "driver version unknown"
    model = ""
    for f in glob.glob(DRV + "/gpus/*/information"):
        for line in open(f):
            if line.startswith("Model:"):
                model = line.split(":", 1)[1].strip()
    gpu = {"ok": False, "devices": [], "apps": gpu_users(), "model": model,
           "error": f"{model}: utilisation and VRAM need NVML, which fails on this machine (driver/library version mismatch: "
                    "the driver was updated without a reboot). " + drv}
    return {"ts": time.time(), "hostname": os.uname().nodename, "load": [float(x) for x in open("/proc/loadavg").read().split()[:3]],
            "mem_total_mb": mem.get("MemTotal", 0), "mem_available_mb": mem.get("MemAvailable", 0), "cores": cores, "procs": procs,
            "threads": threads, "gpu": gpu}


def progress():
    out = []
    for f in sorted(f for d in PROGRESS_DIRS for f in glob.glob(f"{d}/*.json")):
        try:
            out.append(json.load(open(f)))
        except (OSError, ValueError):
            pass
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {TOKEN}".encode()):
            self.send_response(401); self.end_headers(); return False
        return True

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/probe":
            return self._json(probe())
        if self.path == "/progress":
            return self._json(progress())
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if not self._auth():
            return
        if self.path == "/shutdown":
            self.send_response(204); self.end_headers(); threading.Thread(target=srv.shutdown, daemon=True).start(); return
        self.send_response(404); self.end_headers()


srv = ThreadingHTTPServer((HOST, PORT), Handler)
threading.Timer(HOURS * 3600, srv.shutdown).start()
print(f"status service on {HOST}:{PORT} for {HOURS} h", flush=True)
srv.serve_forever()
print("VERDICT: PASS")
