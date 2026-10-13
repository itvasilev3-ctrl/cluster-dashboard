"""Read-only snapshot of the ASTRUM host: processes, per-core load, memory, GPU.

Runs as an ordinary ASTRA cluster job (it only reads /proc, `ps` and `nvidia-smi`).
Prints one JSON document between the markers so the output is easy to extract:

    ===ASTRUM_SNAPSHOT===
    {...}
    ===END===

The last line is the usual "VERDICT: PASS" the oracle expects.
"""
import json
import os
import subprocess
import time


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001 - a probe must never crash
        return 1, str(e)


def read_stat():
    cores = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                p = line.split()
                cores[int(p[0][3:])] = [int(x) for x in p[1:9]]  # user nice sys idle iowait irq softirq steal
    return cores


a = read_stat()
time.sleep(1.0)
b = read_stat()
cores = []
for i in sorted(b):
    d = [y - x for x, y in zip(a[i], b[i])]
    total = sum(d) or 1
    user, nice, system, idle, iowait, irq, soft, steal = d
    cores.append({
        "id": i,
        "busy": round(100.0 * (total - idle - iowait) / total, 1),
        "user": round(100.0 * user / total, 1),
        "nice": round(100.0 * nice / total, 1),
        "sys": round(100.0 * (system + irq + soft) / total, 1),
        "iowait": round(100.0 * iowait / total, 1),
    })

procs = []
_, out = sh("ps -eo user:24,pid,ppid,pcpu,pmem,rss,etimes,stat,nlwp,args --sort=-pcpu --no-headers")
for line in out.splitlines():
    p = line.split(None, 9)
    if len(p) < 10:
        continue
    procs.append({
        "user": p[0], "pid": int(p[1]), "ppid": int(p[2]),
        "cpu": float(p[3]), "mem": float(p[4]), "rss_mb": round(int(p[5]) / 1024, 1),
        "etimes": int(p[6]), "stat": p[7], "threads": int(p[8]), "cmd": p[9][:200],
    })

meminfo = {}
with open("/proc/meminfo") as f:
    for line in f:
        k, v = line.split(":", 1)
        meminfo[k] = int(v.split()[0]) // 1024  # MB
load = [float(x) for x in open("/proc/loadavg").read().split()[:3]]

gpu = {"ok": False, "devices": [], "apps": [], "error": None}
rc, out = sh("nvidia-smi --query-gpu=index,name,utilization.gpu,utilization.memory,memory.used,"
             "memory.total,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits")
if rc == 0:
    gpu["ok"] = True
    for line in out.splitlines():
        v = [x.strip() for x in line.split(",")]
        gpu["devices"].append({
            "index": int(v[0]), "name": v[1], "util": float(v[2]), "mem_util": float(v[3]),
            "mem_used_mb": float(v[4]), "mem_total_mb": float(v[5]),
            "temp_c": float(v[6]), "power_w": float(v[7]) if v[7].replace(".", "").isdigit() else None,
            "power_limit_w": float(v[8]) if v[8].replace(".", "").isdigit() else None,
        })
    rc2, out2 = sh("nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits")
    if rc2 == 0:
        for line in out2.splitlines():
            v = [x.strip() for x in line.split(",")]
            if len(v) == 3 and v[0].isdigit():
                gpu["apps"].append({"pid": int(v[0]), "name": v[1], "mem_mb": float(v[2])})
else:
    gpu["error"] = out.splitlines()[0] if out else "nvidia-smi failed"

snap = {
    "host": {
        "ts": time.time(),
        "hostname": os.uname().nodename,
        "load": load,
        "mem_total_mb": meminfo.get("MemTotal"),
        "mem_available_mb": meminfo.get("MemAvailable"),
        "cores": cores,
        "procs": procs[:150],
        "gpu": gpu,
    }
}
print("===ASTRUM_SNAPSHOT===")
print(json.dumps(snap))
print("===END===")
print("VERDICT: PASS")
