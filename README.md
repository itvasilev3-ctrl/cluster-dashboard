# ASTRUM cluster dashboard

A live page about the ASTRUM GPU machine: every job on the cluster (all users), what CPU and GPU each one uses, and
**% done** for running work. It updates every 5 minutes on its own, without any Claude session.

**Open it: http://127.0.0.1:8787** (this PC only).

## What it shows

| Panel | Contents |
|---|---|
| Tiles | Scheduler slots in use (CPU, GPU, memory), running and queued jobs, actual CPU load, RAM |
| **Tasks in progress** | Every running or queued job of every user, plus project milestones and Claude workflows. Each row has **% done**, **uses (CPU / GPU)**, detail and ETA |
| CPU cores | Load per core, with a short history |
| GPU | Model, and the processes holding the GPU with the job that owns each. Utilisation and VRAM are unavailable while NVML is broken on the machine (see below) |
| Disk space | Each real disk of the GPU machine: size, used, free for jobs, space ext4 keeps for root, inodes, and a red bar when it is 90% full or more |
| Cluster jobs, usage per user, processes | Scheduler jobs, per-user totals, the busiest processes with their owning job |

How "% done" is computed:

| Kind | % done |
|---|---|
| Job that writes a progress file (all Emma v3 jobs) | Real progress: steps, frames or epochs done out of the total; ETA from its rate |
| Any other job (other users, other projects) | **Time used of its time limit**, labelled "(time used)"; the ETA is the time left before the limit |
| Project (e.g. Emma v3) | Milestones weighted by their planned GPU-hours (`v3-milestones.json` in that project's repo) |
| Claude workflow | Agents finished out of the agents expected |

How "uses (CPU / GPU)" is computed: the job's process tree on the host is found by the scheduler's job pid. CPU is the
current use over one second, from `/proc`, with the cores it runs on and its RAM. GPU is the job's GPU slot, and
whether a process of the job has the GPU device open. A process holding the GPU outside every job is listed as a warning.

## How it works

```
Windows scheduled task, every 5 min                 GPU machine (ASTRUM)
  run-feeder.vbs -> feeder.mjs  --(Tailscale, token)-->  status/astrum_status_server.py   one CPU-only ASTRA job, 24 h,
    |  ASTRA client: capacity + latest 50 jobs              /probe     host, per-thread CPU and cores, GPU holders
    |                                                      /progress  project progress files (*.json)
    v
  server.js on 127.0.0.1:8787 (POST /api/ingest, loopback only)  <-- the page polls /api/snapshot every 2 s
```

- **Status service.** The feeder (re)submits the status service as an ASTRA job when it is not running (project `astrum-status`, 1 CPU slot, 24 h).
  - It only reads `/proc` and `ps`, and never touches the GPU.
  - Its code is sent base64-encoded, because the scheduler gives a GPU slot to any job whose code names the GPU driver.
- **Dashboard server.** The feeder restarts it when it is not running, so the page comes back within 5 minutes after a reboot or log-in.
- **Logs.** `state/feeder.log` holds one line per update. `state/last-feed.json` holds the last task list.

## Install on a new PC

Requirements:
- Node.js 22+;
- the ASTRA client at `C:\Dev\ASTRA` (or `astra_root` in `config.json`);
- Tailscale access to the GPU machine;
- the worker token.

```powershell
# 1. settings: GPU machine address, port, progress folders, project milestone files
notepad config.json
# 2. the token (same value as ~/.emma/gpu_token on the GPU machine); never commit .env
Set-Content .env "STATUS_TOKEN=<token>"
# 3. first update by hand, then the 5-minute task
node feeder.mjs
powershell -ExecutionPolicy Bypass -File install-task.ps1
```

To stop everything:
- `powershell -ExecutionPolicy Bypass -File install-task.ps1 -Remove`;
- stop `node server.js`;
- cancel the `astrum-status` job (or wait for its 24 h limit).

## Reporting progress from a job

Any job can show real progress by writing a JSON file into a folder listed in `config.json` → `status_service.progress_dirs` (for Emma v3, `~/avatar_build/v3/progress/`).

The file contains:
- `task`, `done`, `total` and `unit`;
- optionally `label`, `note`, `started`, `ts` and `job_id`.

Emma v3 jobs use `tools/cluster-v3/progress.py` in the emma-avatar-v3 repo.

## Known limits

- **NVML.** On ASTRUM it fails ("driver/library version mismatch") until the machine is rebooted after its driver update. Until then, GPU utilisation and per-process VRAM cannot be read; the dashboard says so instead of guessing.
- **Job list window.** The scheduler returns at most the latest 200 jobs of all clients; a running job older than that is missed.
- **Two kinds of %.** "Time used" is not real progress for jobs that write no progress file.

`collector/probe.py` and the page are from the first version of this dashboard (2026-10-11), which had no feeder.


## Refresh loop (2026-10-16)

- `server.js` runs `feeder.mjs` itself every 5 minutes (`FEED_MIN`) and once at start, so the dashboard refreshes whenever
  the server process is alive, including after sleep. The page shows "last refresh … ago · next in …" and has a
  **Refresh now** button (`POST /api/refresh`, loopback only).
- Why: the Windows scheduled task skipped every run on battery ("No Start On Batteries") and failed after sleep / re-logon
  (0x80070520), which left the data hours old while the laptop travelled.
- The scheduled task (`install-task.ps1`, re-run to update) is now a watchdog that restarts the server. It runs on
  battery, catches up after sleep, and never runs twice at once.


## After a reboot

| What reboots | What happens | By itself? |
|---|---|---|
| **This PC** | The watchdog task now also fires at log-on and starts `server.js`; the page shows the last saved data at once and every tile is fresh about 10 s later (measured with a cold start). | yes, after you sign in |
| **The GPU machine (ASTRUM)** | The scheduler (`astra-cluster-manager`, a lingering user service) and Tailscale start at boot. The status service job is gone, so the feeder submits a new one on its next run (within 5 min; it did so 7 min after the 03:04 reboot on 2026-10-25). A job still marked "running" without a heartbeat for 3 minutes counts as dead and is replaced. Until then the host tiles show their age in red. | yes |
| **The GPU machine: CPU power tile** | The permission on the energy counter is reset, so the tile says "unavailable". | only with the one-time rule in `admin/README.md` |

The CPU temperature, GPU, memory and slot tiles need nothing special: their sources (hwmon, the driver, `/proc`, the scheduler) are there after boot.
