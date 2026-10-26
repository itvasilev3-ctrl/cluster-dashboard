"""
Status service on the GPU machine for the ASTRUM dashboard (http://127.0.0.1:8787 on the PC, fed by tools/status/feeder.mjs).
Runs as one long CPU-only ASTRA job instead of a probe job every 5 minutes. Standard library only; it never starts GPU work
(it only queries the driver with nvidia-smi, and falls back to listing the device-file holders when NVML is broken).

  GET  /probe     host snapshot in the dashboard's format: per-core load, RAM, processes, GPU utilisation / VRAM / users
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
SMI = "nv" + "idia-smi"                                       # same reason; it only queries the driver, it never starts GPU work


def read_stat():
    cores = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split(); cores[int(p[0][3:])] = [int(x) for x in p[1:9]]
    return cores


def gpu_users():
    """Processes holding the GPU device files (works even when NVML does not)."""
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


def num(s):
    try:
        return float(s)
    except ValueError:
        return None                                                         # "[N/A]", "[Not Supported]"


def smi_query(what, fields):
    """Rows of `nvidia-smi --query-<what>=<fields>` as lists of strings; RuntimeError carries the first line of its complaint."""
    r = subprocess.run([SMI, f"--query-{what}={fields}", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
    if r.returncode:
        raise RuntimeError(((r.stdout + r.stderr).strip().splitlines() or [f"exit {r.returncode}"])[0])
    return [[c.strip() for c in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]


SMI_EXTRA = [("fan.speed", "fan_pct"), ("pstate", "pstate"), ("clocks.current.graphics", "clock_gr_mhz"), ("clocks.max.graphics", "clock_gr_max_mhz"),
             ("clocks.current.memory", "clock_mem_mhz"), ("clocks.max.memory", "clock_mem_max_mhz"), ("power.max_limit", "power_max_w"),
             ("pcie.link.gen.current", "pcie_gen"), ("pcie.link.gen.max", "pcie_gen_max"), ("pcie.link.width.current", "pcie_width"),
             ("pcie.link.width.max", "pcie_width_max"), ("utilization.encoder", "enc_util"), ("utilization.decoder", "dec_util"),
             ("memory.reserved", "mem_reserved_mb"), ("clocks_throttle_reasons.active", "throttle")]
THROTTLE = {0x2: "application clocks", 0x4: "software power cap", 0x8: "hardware slowdown", 0x20: "software thermal limit",
            0x40: "hardware thermal limit", 0x80: "power brake", 0x100: "display clocks"}


def throttle_names(val):
    """Why the clocks are lowered, from the driver's bit mask; the idle bit (0x1) is normal and reported as 'idle'."""
    try:
        m = int(val, 16)
    except ValueError:
        return []
    return (["idle"] if m & 0x1 else []) + [n for bit, n in THROTTLE.items() if m & bit]


def cmdline(pid):
    try:
        return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace").strip()[:160]
    except OSError:
        return ""


def smi_pmon():
    """Per-process rows of `nvidia-smi pmon -c 1 -s um` (compute and graphics processes): type, SM / memory / encoder / decoder %, VRAM MB."""
    r = subprocess.run([SMI, "pmon", "-c", "1", "-s", "um"], capture_output=True, text=True, timeout=8)
    if r.returncode:
        return []
    cols, rows = None, []
    for line in r.stdout.splitlines():
        if line.startswith("#"):
            if cols is None:                                                # the first comment line names the columns
                cols = line.lstrip("# ").split()
            continue
        if cols is None or not line.strip():
            continue
        p = line.split(None, len(cols) - 1)
        if len(p) < len(cols) or not p[cols.index("pid")].isdigit():
            continue
        row = dict(zip(cols, p))
        val = lambda k: num(row[k]) if k in row and row[k] != "-" else None
        rows.append({"pid": int(row["pid"]), "name": row.get("command", ""), "type": row.get("type"), "sm": val("sm"), "mem_pct": val("mem"),
                     "enc": val("enc"), "dec": val("dec"), "mem_mb": val("fb")})
    return rows


def gpu_info(model, drv):
    """GPU block of the snapshot: utilisation, VRAM, temperature and power when NVML works; otherwise only the device-file holders."""
    apps = gpu_users()                                                      # first: the query below holds the device files itself
    try:
        devices = [{"index": int(r[0]), "name": r[1], "util": num(r[2]) or 0.0, "mem_util": num(r[3]) or 0.0, "mem_used_mb": num(r[4]) or 0.0,
                    "mem_total_mb": num(r[5]) or 0.0, "temp_c": num(r[6]) or 0.0, "power_w": num(r[7]), "power_limit_w": num(r[8])}
                   for r in smi_query("gpu", "index,name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,power.limit")]
        if not devices:
            raise RuntimeError("no GPU listed")
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, IndexError) as e:
        return {"ok": False, "devices": [], "apps": apps, "model": model,
                "error": f"{model}: utilisation and VRAM are unavailable ({e}). {drv}"}
    # clocks, fan, power limit, PCIe link, encoder / decoder, throttle reasons: a separate query so an unsupported field cannot take the basics down
    for fields in (SMI_EXTRA, [f for f in SMI_EXTRA if "throttle" not in f]):
        try:
            rows = smi_query("gpu", ",".join(f for f, _ in fields))
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, IndexError):
            continue
        for dev, row in zip(devices, rows):
            for (f, key), val in zip(fields, row):
                if key == "throttle":
                    dev[key] = throttle_names(val)
                else:
                    dev[key] = val if key == "pstate" else num(val)
        break
    # processes: every pid that has the GPU open, graphics ones (the login screen) included, with VRAM and utilisation per process
    procs = {a["pid"]: a for a in apps}                                     # device-file holders we can read
    for p in smi_pmon():
        a = procs.setdefault(p["pid"], {"pid": p["pid"], "name": p["name"]})
        a.update({k: v for k, v in p.items() if k != "name" and v is not None})
        if a["name"] == p["name"] or a["name"].startswith("pid "):
            a["name"] = cmdline(p["pid"]) or p["name"]
    try:                                                                    # compute processes pmon did not list
        for r in smi_query("compute-apps", "pid,process_name,used_memory"):
            if r[0].isdigit():
                a = procs.setdefault(int(r[0]), {"pid": int(r[0]), "name": cmdline(int(r[0])) or r[1]})
                if a.get("mem_mb") is None:
                    a["mem_mb"] = num(r[2])
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, IndexError):
        pass
    for a in procs.values():
        a.setdefault("mem_mb", "?")
    return {"ok": True, "devices": devices, "apps": sorted(procs.values(), key=lambda a: -(a["mem_mb"] if isinstance(a["mem_mb"], (int, float)) else 0)),
            "model": model or devices[0]["name"], "error": None}


RAPL = "/sys/class/powercap/intel-rapl:0"                    # CPU package energy counter (AMD Zen uses the same name)


def rapl_read():
    """(energy in uJ, counter range in uJ, None) of the CPU package, or (None, None, reason) when it cannot be read."""
    try:
        uj = int(open(RAPL + "/energy_uj").read())
    except FileNotFoundError:
        return None, None, "no CPU package energy counter on this machine"
    except PermissionError:
        return None, None, "the CPU package energy counter (RAPL) is readable only by root on this machine"
    except (OSError, ValueError) as e:
        return None, None, f"RAPL: {e}"
    try:
        rng = int(open(RAPL + "/max_energy_range_uj").read())
    except (OSError, ValueError):
        rng = None
    return uj, rng, None


def cpu_power(r0, r1, seconds):
    """Average CPU package power in watts between two rapl_read() results."""
    if r0[2] or r1[2]:
        return {"watts": None, "reason": r0[2] or r1[2]}
    d = r1[0] - r0[0]
    if d < 0:
        if not r0[1]:
            return {"watts": None, "reason": "the energy counter wrapped during the sample"}
        d += r0[1]
    return {"watts": round(d / 1e6 / seconds, 1), "source": "RAPL package-0 energy counter"}


def cpu_temps():
    """CPU temperature from the kernel's hwmon sensors: AMD k10temp (Tctl = package, Tccd* = one per chiplet) or Intel coretemp."""
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            name = open(h + "/name").read().strip()
            if name not in ("k10temp", "coretemp"):
                continue
            sensors = []
            for f in sorted(glob.glob(h + "/temp*_input")):
                try:
                    label = open(f.replace("_input", "_label")).read().strip()
                except OSError:
                    label = os.path.basename(f)
                sensors.append({"label": label, "temp_c": round(int(open(f).read()) / 1000, 1)})
            pkg = next((s for s in sensors if s["label"] in ("Tctl", "Package id 0")), sensors[0] if sensors else None)
            if pkg:
                return {"temp_c": pkg["temp_c"], "label": pkg["label"], "sensors": sensors, "driver": name}
        except (OSError, ValueError):
            continue
    return None


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
    e0, t0 = rapl_read(), time.monotonic()
    a = read_stat(); ta = thread_times(); time.sleep(1.0); b = read_stat(); tb = thread_times()
    e1, t1 = rapl_read(), time.monotonic()
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
    gpu = gpu_info(model, drv)
    return {"ts": time.time(), "hostname": os.uname().nodename, "load": [float(x) for x in open("/proc/loadavg").read().split()[:3]],
            "mem_total_mb": mem.get("MemTotal", 0), "mem_available_mb": mem.get("MemAvailable", 0), "cores": cores, "procs": procs,
            "threads": threads, "gpu": gpu, "cpu_temp": cpu_temps(), "cpu_power": cpu_power(e0, e1, t1 - t0)}


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
limit = threading.Timer(HOURS * 3600, srv.shutdown)
limit.daemon = True                                          # a non-daemon timer kept the job alive for the full 24 h after /shutdown
limit.start()
print(f"status service on {HOST}:{PORT} for {HOURS} h", flush=True)
srv.serve_forever()
srv.server_close()                                           # free the port so the next service can bind it
print("VERDICT: PASS")
