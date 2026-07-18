# fw-pwrctl

Quiet your Framework Laptop's fan by intelligently managing CPU power limits.

## What it does

fw-pwrctl is a daemon that reduces fan noise on Framework Laptop 13 by controlling
the CPU power limit (RAPL PL1) instead of fighting the EC for direct fan control.

How it works:
1. Reads CPU temperature (coretemp or PECI — Platform Environmental Control
   Interface — sensor via cros_ec)
2. A PI (proportional-integral) controller adjusts PL1 (5–28W) to hold temp
   at setpoint (default 75°C)
3. Lower CPU power → cooler board sensors → EC runs fan slower

Additional features:
- **SEN5 guard**: Aggressively cuts PL1 if the board VRM sensor reaches
  `sen5GuardTemp` (default 75°C)
- **Idle mode**: Caps PL1 at `idleCeilingW` (default 15W) and sets CPU EPP to
  "power" when cool (below `idleTempC`, default 65°C; releases above
  `idleReleaseTempC`, default 68°C)
- **EC DDR override**: Raises the DDR sensor's fan_off threshold from 40→50°C,
  reducing idle fan speed by ~2,500 RPM

## Supported hardware

- Framework Laptop 13 — 12th Gen Intel (i5-1240P / Alder Lake)
- Ubuntu 22.04+ or comparable Linux distribution
- Kernel 6.x+ (requires intel_rapl, cros_ec; recommended: coretemp, int340x_thermal)
- Python 3.10+
- Root privileges (the daemon writes to RAPL sysfs and communicates with the EC)
- Recommended: `lm-sensors` (for hardware sensor data in logs; `sudo apt install lm-sensors`)

Other 12th Gen Framework 13 models (i7-1260P, i7-1280P) likely work but are
untested. 13th Gen and later are rejected by the startup CPU check — supporting
them would require code changes. Framework 16 is not supported.

## Try it first

You can run the daemon directly from the checkout without installing anything
(except ectool, which is needed to talk to the EC):

    # Install ectool (one-time, puts binary in /usr/local/bin)
    # Builds from source if you have cmake/clang/ninja; falls back to vendored binary
    bash install-ectool.sh          # or: bash install-ectool.sh --yes  (skip prompt)

    # Dry run — shows what the daemon would do, without touching hardware
    # (sudo needed: preflight checks and sensor/EC access require root)
    sudo python3 fw_pwrctl.py --dry-run --debug

This runs all preflight checks, discovers sensors, and prints the controller's
decisions every 2 seconds (thanks to `--debug`) — but skips all hardware writes
(RAPL, EC, EPP). The debug output shows current temperatures, the controller's
error/integral terms, and the PL1 it would set (see CLI flags for field details).
Press Ctrl-C to stop. If you like what you see, proceed to the full install.

To run for real from the checkout (e.g. while iterating on the code), stop
the installed service first to avoid two instances fighting over PL1:

    sudo systemctl stop fw-pwrctl
    sudo python3 fw_pwrctl.py --debug

## Quick start

    # 1. Install ectool (skip if you already did this above)
    bash install-ectool.sh

    # 2. Install the daemon
    bash install.sh

    # 3. Enable and start (starts now + auto-starts on boot)
    sudo systemctl enable --now fw-pwrctl

    # 4. Verify
    sudo bash check.sh

## Configuration

Config file: `/etc/fw-pwrctl/config.json` (installed by `install.sh`).
After editing, restart the service: `sudo systemctl restart fw-pwrctl`.
JSON does not support comments — use the table below as a reference while editing.

Defaults below are from the shipped `config.json`. Keys marked with \* have
different code fallbacks if omitted — see the Description column for details.

**Note:** The Range column shows recommended values. The validator accepts wider
bounds (e.g. `setpoint` allows 30–95, `pl1MinW` allows >= 1, gains must be > 0).
The validator caps `pl1MaxW` at 64W (the PL2 firmware limit). The RAPL kernel
interface silently clamps to the firmware-allowed range (28W on 12th Gen).

### PI controller settings

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `setpoint` | 75 | 60–85 °C | Target CPU temperature |
| `Kp` | 0.25 | 0.1–1.0 | Proportional gain (W/°C) |
| `Ki` | 0.021 | 0.005–0.1 | Integral gain (W/°C·s) |
| `pl1MinW` | 5 | 1–15 W | Minimum PL1 (floor) |
| `pl1MaxW` | 28 | 15–28 W | Maximum PL1 (ceiling — 28W is firmware max) |
| `updateInterval` | 2 | 1–10 s | Control loop period (integer) |
| `integralMaxW` | 250 | 50–500 W | Anti-windup integral clamp (limits I-term contribution) |
| `rampUpRateLimitW` | 3 | 1–10 W/cycle | Max PL1 increase per update cycle (0 disables) |
| `rampDownRateLimitW` | 3 | 1–10 W/cycle | Max PL1 decrease per update cycle (0 disables) |
| `sensorSmoothing` | 5 | 1–20 | Median filter window in samples (integer) |

### SEN5 board sensor guard

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `sen5GuardTemp` | 75 | 70–80 °C | SEN5 temp that activates guard |
| `sen5CriticalTemp` | 78 | 75–85 °C | SEN5 temp that forces PL1 to minimum |
| `sen5ReleaseTemp` | 73 | 65–75 °C | SEN5 temp that releases guard |
| `sen5CutRateW` | 2 | 1–5 W/cycle | PL1 cut per update cycle when guard active |

### Idle mode

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `idleCeilingW` | 15\* | 5–20 W | PL1 cap during idle (code fallback: pl1MaxW — idle effectively off) |
| `idleTempC` | 65\* | 55–70 °C | CPU temp below which idle mode activates (code fallback: 0 — disabled) |
| `idleReleaseTempC` | 68\* | 60–75 °C | CPU temp above which idle mode releases (code fallback: 0) |
| `idleEPP` | "power"\* | see below | CPU EPP during idle (code fallback: none — EPP unmanaged) |
| `normalEPP` | "balance_performance"\* | see below | CPU EPP during normal operation (code fallback: none) |

EPP values: `performance`, `balance_performance`, `balance_power`, `power`

### Logging

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `logging.enabled` | true\* | true/false | Enable JSONL sensor logging (code fallback: false) |
| `logging.path` | /var/log/fw-pwrctl/sensor-log.json | — | Log file path |
| `logging.maxSizeMB` | 50 | 10–500 MB | Max log file size before rotation |
| `logging.maxLogFiles` | 100 | 1–1000 | Max rotated log files to keep (excludes active) |
| `logging.flushIntervalSeconds` | 120 | 10–600 s | Buffer flush interval |

### Prometheus metrics

| Key | Default | Description |
|-----|---------|-------------|
| `metrics.enabled` | false | Enable Prometheus textfile export |
| `metrics.path` | /var/log/fw-pwrctl/textfile/fw_pwrctl.prom | `.prom` file for the node_exporter textfile collector (absolute, must end in `.prom`) |

`logging` and `metrics` are independent sinks over the same sensor snapshot —
enable either, both, or neither. See
[Prometheus metrics](#prometheus-metrics-node_exporter-textfile-collector).

### Tuning examples

**Quieter (lower performance):** Set `"setpoint": 70`, `"pl1MaxW": 20`, `"idleCeilingW": 10`

**More performance (louder):** Set `"setpoint": 80`, `"pl1MaxW": 28`, `"idleCeilingW": 20`

## CLI flags

    fw_pwrctl.py [--config PATH] [--mode MODE] [--debug] [--dry-run] [--version]

- `--config` — config file path (default: config.json in same directory as fw_pwrctl.py)
- `--mode` — `full` (default), `monitor` (logging only), `control` (PI loop only).
  All modes run the same preflight checks and require ectool — this ensures the
  environment is valid if you later switch modes (e.g. capture a baseline with
  `monitor`, then compare under `control`).
- `--debug` — print controller state every update cycle:
  `temp=72.5  SEN5=68.3  med=72.5  err=-2.5  I=45.2  PL1=18.5W`
  Fields: `temp` = raw CPU °C, `SEN5` = board sensor °C, `med` = median-filtered
  CPU °C, `err` = med − setpoint (positive = above target), `I` = integral term,
  `PL1` = power limit set. Optional suffixes: `GUARD` (SEN5 guard active),
  `IDLE` (idle ceiling active)
- `--dry-run` — skip all hardware writes (ectool, RAPL, EPP)
- `--version` — print version and exit

To get debug output from the running service, stop it and run the daemon
manually with `--debug`, or use `systemctl edit fw-pwrctl` to override
ExecStart (add an empty `ExecStart=` line to clear the original, then a new
`ExecStart=...` line with `--debug` appended).

## Monitoring

### sensor-plot.sh

Generates a 5-panel PNG chart from sensor logs:

    sudo bash sensor-plot.sh --hours 6 --rolling 5m

Panels: Fan RPM, CPU temperature, PL1 power, board sensors, system load.

<img src="doc/sensor-plot-example.png" alt="sensor-plot example — 12h with 5m rolling average" width="700">

Options:

    -o, --output FILE    Save to FILE (default: /tmp/sensor-plot-{hours}h.png)
    --hours N            Time window in hours (default: 3)
    --rolling DURATION   Overlay rolling average (e.g. 30s, 5m, 1h)
    --log-dir DIR        Log directory (default: /var/log/fw-pwrctl)

When no `-o` flag is specified and a display is available, the chart opens
automatically in the default image viewer. Over SSH or headless, the file is
saved silently to the default path.

Requires: `sudo apt install python3-matplotlib` (or `pip3 install matplotlib`)

### check.sh

Verifies installation health — files present, service running, sensors readable:

    sudo bash check.sh

### Sensor log format

The daemon writes JSONL (one JSON object per line) to the configured log path
(default: `/var/log/fw-pwrctl/sensor-log.json`). Each line is a JSON object
with the following top-level keys (all optional except `timestamp`):

```json
{
  "timestamp": "2026-03-07T14:35:12.345678+02:00",
  "sensors": { ... },
  "cpu": {
    "user": 5.2, "nice": 0.0, "system": 2.1, "idle": 90.3,
    "iowait": 0.1, "irq": 0.0, "softirq": 0.0,
    "load_1m": 1.23, "load_5m": 0.98, "load_15m": 0.87
  },
  "memory": {
    "total_mb": 16384, "used_mb": 8192, "free_mb": 4096, "available_mb": 12000,
    "buffers_mb": 256, "cached_mb": 3840,
    "swap_total_mb": 8192, "swap_used_mb": 0, "swap_free_mb": 8192
  },
  "board_temps": { "sen2_c": 48.2, "sen3_c": 52.1, "sen4_c": 55.0, "sen5_c": 68.3 },
  "rapl_pl1_w": 18.5,
  "throttle": { "package_throttle_count": 0, "core_throttle_count": 0 },
  "ec_thermal": [ ... ],
  "controller": {
    "pl1_w": 18.5, "raw_temp_c": 73.0, "median_c": 72.5,
    "error": -2.5, "integral": 45.2, "setpoint_c": 75,
    "pl1_min_w": 5, "pl1_max_w": 28,
    "idle_active": false, "idle_ceiling_w": 15,
    "guard_active": false, "sen5_c": 68.3,
    "epp_active": false
  }
}
```

`sensors` contains the full output of `sensors -j` (lm-sensors). Fan RPM is
at `sensors.cros_ec-isa-0000.fan1.fan1_input` and CPU package temperature at
`sensors.coretemp-isa-0000["Package id 0"].temp1_input`. sensor-plot.sh
parses these paths automatically.

The `controller` key is present only in `full` mode. In `monitor` mode, it
is omitted (no PI loop runs). In `control` mode, sensor logging is disabled
entirely (only PI control runs, no log file).
`board_temps` appears only when int340x thermal zones are discovered at startup.
Each section has independent error handling — if any source fails, the
remaining sections are still logged.

### Logging behavior

- Entries are buffered in memory and flushed every 120 seconds (configurable
  via `logging.flushIntervalSeconds`; typically ~60 entries per flush, hard cap
  at 1000 entries as a safety limit)
- When the log file exceeds `logging.maxSizeMB` (default 50MB), it is rotated
  to `sensor-log.YYYYMMDD_HHMMSS_ffffff.json.gz` (gzip compressed, ~98%
  reduction). A metadata index (`sensor-log-meta.json`) tracks the time range
  of each rotated file for efficient plotting. Files beyond `logging.maxLogFiles`
  (default 100, not counting the active log file) are automatically deleted
  oldest-first. Retention depends on entry size and update interval — typically
  several weeks at default settings (~1MB compressed per file, ~8h per file)
- The metadata index is safe to delete — it is recreated automatically on the
  next rotation. `sensor-plot.sh` falls back to loading all files if missing
- If 3 consecutive flush attempts fail (e.g. disk full), sensor logging is
  automatically disabled for the remainder of the session to avoid repeated
  error noise

### Prometheus metrics (node_exporter textfile collector)

Optional. Switch it on and the daemon rewrites a `.prom` file every control
tick (~2 s) with current values:

```json
"metrics": {
  "enabled": true,
  "path": "/var/log/fw-pwrctl/textfile/fw_pwrctl.prom"
}
```

`path` is optional and defaults to the value above.

This is a separate sink from `logging`, with its own `enabled` flag, so you can
run any combination:

| `logging.enabled` | `metrics.enabled` | Result |
|---|---|---|
| true | false | JSONL only (default — unchanged from v1.0.0) |
| false | true | Prometheus only, no log files on disk |
| true | true | Both, from the same snapshot |
| false | false | Neither; the PI controller still runs |

Then point node_exporter at the *directory*:

    node_exporter --collector.textfile.directory=/var/log/fw-pwrctl/textfile

node_exporter reads every `*.prom` in that directory on each scrape and merges
the contents into its own `/metrics`. Nothing listens on a port and no extra
process runs — fw-pwrctl only writes a file. Use a dedicated `textfile/`
subdirectory so node_exporter doesn't also see `sensor-log.json`.

Keep the path under `/var/log/fw-pwrctl/`: the systemd unit already grants
`ReadWritePaths=/var/log/fw-pwrctl`, so no unit change is needed. Anywhere else
requires adding a `ReadWritePaths=` line to the unit. The file is written
atomically (temp file + `rename`) at mode 0644, so a scraper running as another
uid — in a container, on a read-only bind mount — never sees a partial file.

What it exports:

| Metric | Type | Notes |
|---|---|---|
| `fw_pwrctl_rapl_pl1_watts` | gauge | Current PL1 readback |
| `fw_pwrctl_controller_pl1_target_watts` | gauge | PL1 the controller asked for |
| `fw_pwrctl_pl1_min_watts`, `fw_pwrctl_pl1_max_watts` | gauge | Configured bounds |
| `fw_pwrctl_controller_setpoint_celsius` | gauge | Target temperature |
| `fw_pwrctl_controller_temp_celsius{kind="raw\|median"}` | gauge | Controller input |
| `fw_pwrctl_controller_error_celsius` | gauge | temp − setpoint |
| `fw_pwrctl_controller_integral_watts` | gauge | PI integral term |
| `fw_pwrctl_idle_ceiling_watts` | gauge | PL1 ceiling while idle |
| `fw_pwrctl_idle_active`, `fw_pwrctl_guard_active`, `fw_pwrctl_epp_active` | gauge | 1/0 |
| `fw_pwrctl_cpu_throttle_total{scope="package\|core"}` | counter | Thermal throttle events |
| `fw_pwrctl_board_temp_celsius{sensor="sen2".."sen5"}` | gauge | Board sensors |
| `fw_pwrctl_build_info{version="…"}`, `fw_pwrctl_up` | gauge | Always 1 |
| `fw_pwrctl_last_write_timestamp_seconds` | gauge | Unix time of the last write |

CPU and memory stats are deliberately **not** exported — node_exporter's own
collectors already provide them as `node_cpu_*` / `node_memory_*`, and the
`hwmon` collector covers the EC fan and temperature chips.

The highest-value alert is throttling, because these counters only ever rise:

```yaml
- alert: FwPwrctlThrottling
  expr: rate(fw_pwrctl_cpu_throttle_total[5m]) > 0
- alert: FwPwrctlStale          # writer died but the file lingers
  expr: time() - fw_pwrctl_last_write_timestamp_seconds > 60
```

Values are always current-only (no history) and at most one tick stale. A
metric unavailable this tick is exported as `NaN`, never as a fabricated `0` —
a real 0 W is meaningful, absence is not. Requires `--mode full` or
`--mode monitor`; `--mode control` collects no sensor data to export.

If the write fails (disk full, path unwritable), the daemon logs one line to
the journal and keeps controlling PL1 — the `.prom` just goes stale, which
`FwPwrctlStale` above catches.

## Uninstall

    bash install.sh --uninstall

This stops and removes the service and daemon. Config files in `/etc/fw-pwrctl/`
and sensor logs in `/var/log/fw-pwrctl/` are preserved — remove manually if
desired. ectool (`/usr/local/bin/ectool`) is also left in place — remove it
manually if you no longer need EC access.

## How it works (detailed)

The Framework EC (Embedded Controller) owns the fan exclusively. There is no
kernel fan cooling device — you cannot set fan speed via standard Linux thermal
interfaces. The EC runs a linear interpolation fan algorithm driven by its board
sensors — up to 6 sensor slots, of which 4 (SEN2–SEN5) are active on
the 12th Gen mainboard:

    fan_pct = MAX across all sensors of: (temp - fan_off) / (fan_max - fan_off)    # temps in Kelvin

The key insight: **PL1 is the real control lever.** Reducing CPU power limits
cools the board sensors, which causes the EC to run the fan slower. This is more
effective and safer than overriding fan duty directly.

fw-pwrctl reads the CPU temperature, runs a PI (proportional-integral) controller,
and writes the resulting PL1 value to the RAPL sysfs interface. The EC continues
to manage the fan autonomously based on its own sensor readings.

### Safety layers

The daemon has three independent safety mechanisms, listed from most to least
common:

**SEN5 board sensor guard** — A 3-threshold state machine with hysteresis that
monitors the VRM-area board sensor (SEN5). When SEN5 rises above `sen5GuardTemp`
(default 75°C), the guard activates and cuts PL1 by `sen5CutRateW` (2W) per
tick. If SEN5 reaches `sen5CriticalTemp` (78°C), PL1 is forced to minimum
immediately. The guard releases when SEN5 drops below `sen5ReleaseTemp` (73°C).
This prevents the board from overheating even if the CPU temperature is within
target.

**CPU critical override** — Two independent mechanisms protect against
dangerously high CPU temperature. First, the main loop tracks **raw**
(unfiltered) temperature readings: if 3 consecutive readings reach 95°C, PL1
is forced to minimum immediately. Second, the PI controller forces PL1 to
minimum when the **median-filtered** temperature reaches 95°C. Using raw
readings for the main-loop check ensures spikes aren't masked by the filter.
These are last-resort backstops that should never trigger under normal
operation (the PI controller keeps CPU temp near 75°C, and the EC has its own
103°C thermal throttle).

**Sensor failure safe mode** — If the temperature sensor becomes unreadable,
PL1 is immediately set to minimum. After 10 consecutive failures, the daemon
re-scans sysfs to find the sensor (and SEN5) at new hwmon paths (common after
kernel updates). If the re-scan finds nothing, the daemon keeps the old path
and retries — re-scanning again after another 10 failures.

### PI controller details

The controller uses anti-windup to prevent integral term accumulation when
the output is saturated. When PL1 hits the floor (`pl1MinW`) while temperature
is above setpoint, or hits the ceiling (`pl1MaxW`) while below setpoint, the
integral term freezes. This prevents sluggish recovery when conditions change.

PL1 changes are rate-limited to `rampUpRateLimitW` and `rampDownRateLimitW`
(default 3W per update cycle — with `updateInterval: 2`, that's 1.5 W/s)
to avoid abrupt power swings. Temperature readings pass
through a median filter (`sensorSmoothing` samples, default 5) to reject
transient spikes.

### EC DDR override

On startup, the daemon raises the DDR sensor's `fan_off` threshold from
40°C (313K) to 50°C (323K) via `ectool thermalset`, merging only the changed
field into the current EC config (all other thermal fields are preserved). This
is a write to EC RAM (not flash) that persists until reboot. It reduces idle
fan speed by ~2,500 RPM because the DDR sensor's low stock threshold forces the
fan on even when the board is cool. The override is guarded by a sensor name check (`F75303_DDR`) —
if the sensor name doesn't match (e.g. a different board revision), the override
is silently skipped. In `full` and `control` modes, the daemon re-applies the
override every ~60 seconds to survive suspend/resume cycles (EC resets to
firmware defaults on resume).
The override is intentionally **not** restored on daemon shutdown — it only
raises a threshold (more permissive), and reverting it would cause the fan to
spin up the moment the daemon stops. See FAQ for manual restore commands.

## How is this different from...

| Tool | Difference |
|------|------------|
| **thermald** | Targets CPU temp via passive cooling; doesn't understand Framework EC board sensors |
| **auto-cpufreq** | CPU frequency scaling only; no PL1 power limit control |
| **ectool fanduty** | Manual fan duty override; no closed-loop control, EC can't resume properly after 0% |
| **TLP / power-profiles-daemon** | Power management focused on battery life, not thermal/acoustic optimization |
| **[fw-fanctrl](https://github.com/TamtamHero/fw-fanctrl)** | Direct fan duty override with temp→speed curves; wider hardware support (AMD, FW16). See [detailed comparison](doc/vs-fw-fanctrl.md). |

## FAQ

See [FAQ.md](FAQ.md) — covers safety, compatibility, performance tradeoffs,
ectool trust, kernel/BIOS update resilience, and monitoring.

## License

This project is licensed under the GNU General Public License v3.0 — see
[LICENSE](LICENSE) for details.

If you'd like to use this code under different licensing terms (e.g., for
integration into a proprietary project), please open an issue or reach out.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
