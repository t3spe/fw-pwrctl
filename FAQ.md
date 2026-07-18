# FAQ

## Is this safe?

### Can fw-pwrctl damage my laptop?

No. The daemon controls CPU power limits (RAPL PL1), not the fan directly.
The EC (Embedded Controller) always owns the fan and enforces its own thermal
safety limits independently. fw-pwrctl cannot override the EC's critical
shutoffs.

The only EC modification is raising the DDR sensor's `fan_off` threshold
from 40°C to 50°C. This makes the fan *less* aggressive at idle — it never
lowers any safety threshold. The override is guarded by a sensor name check
(`F75303_DDR`) so it won't apply on a board revision with different sensors.

PL1 is written via the kernel's RAPL sysfs interface, which enforces the
firmware-allowed range. Even if you configure a value outside the hardware
limits, the kernel silently clamps it. The default config uses 5-28W; the
firmware maximum is 28W. The worst case of any PL1 value is reduced
performance, never hardware damage.

### What happens if the daemon crashes?

| Scenario | Fan | CPU power limit | Recovery |
|----------|-----|-----------------|----------|
| Normal stop (SIGTERM) | Restored to EC auto | Restored to original | Automatic |
| Crash / SIGKILL / OOM | Restored to EC auto | Restored to 28W | Automatic (ExecStopPost) |
| Power loss / hard reboot | Firmware defaults | Firmware defaults | Automatic |

On any service exit (including crashes and SIGKILL), systemd runs
`ectool fanduty 1` then `ectool autofanctrl` via `ExecStopPost` to re-enable
automatic fan control. The `fanduty 1` step works around an EC firmware bug
where `autofanctrl` alone cannot re-enable the PWM hardware. This is the
safety net — the fan always recovers.

PL1 is also restored by `ExecStopPost`: systemd writes 28W (firmware max) to
RAPL after any service exit, including SIGKILL and OOM. This prevents the CPU
from getting stuck at a low power limit if the daemon is killed mid-operation.

If the daemon crashes repeatedly, systemd restarts it up to 5 times per
minute, then stops trying. The fan is always restored between attempts.

### What if the CPU gets dangerously hot?

The daemon has two independent 95°C CPU critical overrides. First, the main
loop forces PL1 to minimum if 3 consecutive raw readings reach 95°C. Second,
the PI controller forces PL1 to minimum when the median-filtered temperature
reaches 95°C. These are last-resort backstops — under
normal operation the PI controller holds CPU temp near 75°C, and the EC has
its own independent thermal throttle at 103°C.

Additionally, the SEN5 board sensor guard monitors the VRM area independently
of CPU temperature. If the board reaches 78°C (configurable), PL1 is forced
to minimum immediately. Between 75°C and 78°C, PL1 is progressively cut.

### Does the DDR thermal override persist after shutdown?

The DDR `fan_off` override (40°C to 50°C) is written to EC RAM and persists
until the next reboot. It is intentionally not restored on daemon shutdown
because:

1. It only *raises* a threshold (more permissive, never more dangerous)
2. Restoring it would make the fan spin up louder the moment you stop the daemon
3. EC RAM is volatile — the override resets on every reboot

### Does this void my Framework warranty?

Framework has an unusually mod-friendly warranty policy. Using software tools
to adjust power limits and read EC sensors is well within what the community
does. That said, this project is not affiliated with Framework — check their
current warranty terms if you're concerned.

---

## Is this for me?

### I use my Framework as a normal laptop (not docked). Should I use this?

Yes, but with different expectations. The daemon was developed on a docked
desktop (lid closed, no keyboard/screen airflow). With the lid open, board
sensors run a few degrees cooler (1-3°C measured) thanks to better airflow
through the keyboard deck. You'll see lower baseline temperatures and the
daemon will be less active — which means less benefit but also less downside.

The code has no lid or dock detection. It works identically in either
configuration.

### How much performance am I giving up?

It depends on the workload and how you tune it.

At idle, the CPU draws ~4W regardless of PL1 cap, so there's **no perceptible
performance difference** — but the fan noise reduction is dramatic. Measured
idle fan speed drops from ~4500-5900 RPM (stock, lid closed) to ~1500 RPM
with the daemon's default settings (PL1 idle ceiling + DDR threshold raise +
EPP tuning — all active by default).

Under sustained heavy load, performance is reduced because PL1 is capped
at whatever the PI controller decides. With moderate loads (4-8 threads),
PL1 settles around 10-12W to hold 75°C. With all-core stress (16 threads),
PL1 may settle higher (~19W) as the controller balances temperature. This
means longer compile times but a much quieter system.

Single-threaded burst performance is mostly preserved: the idle mode releases
instantly when load arrives, and PL1 ramps up at 1.5W/s.

### What does "PL1 5W floor" mean in practice?

At 5W, the CPU's clock speed is significantly reduced but the system remains
responsive for desktop tasks (browsing, terminals, editors). You'd typically
hit this floor when the SEN5 critical guard fires (board sensor above 78°C)
or when the PI controller's integral term accumulates under sustained heavy
load. Both are rare during normal use.

### Can I tune it for more performance / more quiet?

Yes. Edit `/etc/fw-pwrctl/config.json`:

**Quieter** (lower performance): `"setpoint": 70`, `"pl1MaxW": 20`, `"idleCeilingW": 10`

**More performance** (louder): `"setpoint": 80`, `"pl1MaxW": 28`, `"idleCeilingW": 20`

The `setpoint` is the target CPU temperature. Lower = quieter and cooler but
more aggressive PL1 capping. The sweet spot for lid-closed docked use is
75°C (default).

---

## Compatibility

### How does this compare to fw-fanctrl?

See [doc/vs-fw-fanctrl.md](doc/vs-fw-fanctrl.md) for a detailed comparison.
The short version: fw-fanctrl overrides the EC and sets fan speed directly;
fw-pwrctl adjusts the CPU power limit and lets the EC manage the fan.
fw-fanctrl supports more hardware (AMD, Framework 16) and has a larger
community. fw-pwrctl has stronger safety guarantees and richer observability.

### Which Framework laptops are supported?

**Tested and supported:**
- Framework Laptop 13 — 12th Gen Intel (i5-1240P, Alder Lake)

**Likely works (untested):**
- Other 12th Gen Framework 13 models (i7-1260P, i7-1280P)

**Will not work without code changes:**
- 13th Gen Intel (Raptor Lake) — rejected by the CPU generation check
- AMD Ryzen models — rejected by CPU check; uses a different power management
  architecture (PPT, not RAPL PL1)
- Framework Laptop 16 — uses AMD or Intel 13th Gen+, rejected by CPU check;
  also has a different board layout and sensor configuration

The daemon checks for `board_vendor == "Framework"` and that `/proc/cpuinfo`
contains `"12th Gen Intel"` at startup. Any system that fails either check
is rejected. This is a safety measure to prevent applying wrong thermal
assumptions to untested hardware.

### Do I need to disable thermald / TLP / power-profiles-daemon?

**thermald**: If active, it may also write to RAPL PL1, causing the two
daemons to fight over the power limit. Check with
`systemctl is-active thermald`. If it's running, either disable it
(`sudo systemctl disable --now thermald`) or set its RAPL management
to passive.

**TLP / power-profiles-daemon**: These manage EPP (energy performance
preference). If fw-pwrctl's idle mode is enabled (it is by default), both
tools will write to the same sysfs files. Either disable EPP management
in fw-pwrctl (`idleEPP` and `normalEPP` — remove both keys from config)
or disable it in TLP/power-profiles-daemon.

**auto-cpufreq**: Manages CPU frequency scaling only, not PL1. Generally
safe to run alongside fw-pwrctl, but the EPP conflict applies here too.

The daemon does not detect or warn about conflicting services. If the fan
behavior seems erratic, check for competing power management daemons.

### What kernel modules are required?

| Module | Purpose | If missing |
|--------|---------|-----------|
| `intel_rapl` | RAPL PL1 read/write | Daemon exits at startup |
| `coretemp` | CPU package temperature | Falls back to PECI sensor |
| `cros_ec` | EC communication (ectool) | ectool check fails, daemon exits |
| `int340x_thermal` | Board sensors (SEN2-SEN5) | SEN5 guard disabled, logging degraded |

The daemon discovers sensors at runtime via sysfs rather than checking for
modules by name. If a sensor path isn't found, it either falls back
(coretemp to PECI) or degrades gracefully (SEN5 guard disabled).

---

## ectool

### What is ectool and where does it come from?

ectool is a command-line tool for communicating with Framework's Embedded
Controller (Chrome EC). It reads fan RPM, board sensor temperatures, and
thermal configuration.

The `install-ectool.sh` script builds ectool from source using
[DHowett's ectool fork](https://gitlab.howett.net/DHowett/ectool) — a
well-known build in the Framework community. If the build fails (missing
deps, network issues), it falls back to a vendored pre-built binary
included in the repository, verified with a SHA256 checksum.

The binary is installed to `/usr/local/bin/ectool` and runs as root (required
for EC access).

### Is the ectool binary safe to run?

The trust model:
- Primary path: built from source on your machine (full transparency)
- Fallback: vendored binary from a known community CI build, SHA256-verified
- Source repo served over HTTPS
- No GPG signature or package manager integration
- The binary communicates with the EC over the LPC bus — standard for
  Chrome EC tools

The vendored binary's provenance (commit hash, CI job, compiler) is
documented in `vendor/README.md`.

---

## Resilience

### What happens on kernel updates?

If you reboot after a kernel update (the normal case), no action is needed —
sensor paths are re-discovered at startup.

**Sensor paths** (hwmon numbering) can change after a kernel update. The
daemon handles this:

1. If a sensor read fails, PL1 is immediately set to the minimum (safety first)
2. After 10 consecutive failures, the daemon re-scans for both the main sensor
   and SEN5 by name
3. If a sensor moved to a new hwmon path, the daemon picks it up and continues
4. If a sensor is not found, the daemon keeps the old path and re-scans again
   after another 10 failures

**RAPL path** is hardcoded (`/sys/class/powercap/intel-rapl:0/constraint_0_...`).
This path is stable across kernel versions for the same hardware. If it
somehow changes, the daemon will exit at startup (fail-safe).

**Thermal zone numbering** for SEN5 can also change, but is discovered by
type name (`SEN5`) at startup, not by number.

### What happens on BIOS / EC firmware updates?

The EC thermal override is protected by a sensor name check: it only applies
to sensor ID 2 if its name is `F75303_DDR`. If a firmware update changes
the sensor layout, the override is silently skipped and a warning is logged.

Other firmware changes (different PL1 range, different thermal behavior) may
require config tuning but won't cause unsafe behavior — the daemon respects
whatever limits the firmware enforces.

### What happens during suspend / resume?

When the system suspends, the daemon's process is frozen by the kernel. On
resume, it wakes up and continues normally — PL1 is re-read from RAPL sysfs
(firmware may have reset it) and the control loop resumes.

The EC resets to its firmware defaults on suspend/resume, so the DDR
fan_off override is lost. The daemon periodically re-checks EC thermal
config (every ~60 seconds) and re-applies the override when it detects a
reset. After resume, the override is restored within ~60 seconds.

If your system uses s2idle rather than S3 deep sleep, the daemon continues
running during the "suspended" state. This is harmless — PL1 control
continues as normal.

### The daemon is using 100% of a CPU core / seems stuck

The main loop ticks once per second (temperature read, critical temp check),
but the PI controller only recalculates PL1 every `updateInterval` seconds
(default: 2). If you see high CPU usage, something is wrong (likely an
ectool call hanging). Check:

    journalctl -u fw-pwrctl -n 50

If the daemon is truly stuck, restart it:

    sudo systemctl restart fw-pwrctl

---

## Troubleshooting

### What do the log messages mean?

Common messages in `journalctl -u fw-pwrctl`:

**Startup messages** (normal, in order):

    fw-pwrctl 1.1.0
    Mode: full
    Sensor: /sys/class/hwmon/hwmon4/temp1_input (65.2°C)
    Controller: setpoint=75°C, Kp=0.25, Ki=0.021, PL1=5-28W, update every 2s, SEN5=...
    Original PL1: 28.0W
    Sensor logging: /var/log/fw-pwrctl/sensor-log.json (every 2s, flush every 120s, max 50MB)

**Warnings** (informational, daemon continues):

    WARNING: coretemp not found, falling back to PECI

The coretemp hwmon path wasn't found. The daemon falls back to the cros_ec
PECI sensor. Functionally equivalent — no action needed.

    WARNING: SEN5 not found, running without board temp guard

The SEN5 thermal zone wasn't found. The daemon still controls PL1 via CPU
temperature but the board sensor guard is disabled. Check that `int340x_thermal`
modules are loaded.

    WARNING: EC sensor 2 is 'UNKNOWN', expected 'F75303_DDR' — skipping thermal override

The DDR sensor has a different name than expected (possibly a firmware update
changed it). The DDR fan_off threshold override is skipped. The daemon works
normally otherwise.

    Temp read failed (3x): ... — PL1 to min

The temperature sensor became unreadable. PL1 is set to minimum as a safety
measure. After 10 consecutive failures, the daemon re-scans for the sensor.

    Sensor changed: /sys/class/hwmon/hwmon4/temp1_input -> /sys/class/hwmon/hwmon5/temp1_input

The sensor moved to a new hwmon path (common after kernel updates). The daemon
found it automatically and resumed normal operation.

**Errors** (daemon exits):

    Not a Framework laptop (board_vendor: LENOVO)
    Not an Alder Lake processor (found: 13th Gen Intel...)
    ectool not found at /usr/local/bin/ectool

These are startup preflight failures. The daemon refuses to run on unsupported
hardware or without ectool installed.

---

## Monitoring

### How do I know it's working?

Run the health check:

    sudo bash check.sh

This verifies all files are installed, the service is running, sensors are
readable, and RAPL is writable.

For ongoing monitoring, the daemon logs sensor data to
`/var/log/fw-pwrctl/sensor-log.json` (JSONL format). Generate a chart:

    sudo bash sensor-plot.sh --hours 6

This produces a 5-panel PNG showing fan RPM, CPU temperature, PL1 power,
board sensors, and system load over time.

### How do I capture a baseline for comparison?

Use monitor mode to log sensor data without the daemon controlling anything:

    sudo python3 /usr/local/lib/fw-pwrctl/fw_pwrctl.py \
        --config /etc/fw-pwrctl/config.json --mode monitor

Or from a checkout (before installing): `sudo python3 fw_pwrctl.py --mode monitor`

Or via the systemd service (edit the unit to add `--mode monitor`, then
restart). Compare the sensor-plot output between monitor and full mode
to see the daemon's effect on fan speed and temperatures.

### Can I get this into Prometheus / Grafana?

Yes, without running an HTTP server. Enable the `metrics` sink in
`config.json`:

    "metrics": {
      "enabled": true,
      "path": "/var/log/fw-pwrctl/textfile/fw_pwrctl.prom"
    }

The daemon rewrites that file every control tick. Point an existing
node_exporter at the directory with
`--collector.textfile.directory=/var/log/fw-pwrctl/textfile` and it merges
fw-pwrctl's metrics into its own `/metrics` on each scrape.

Keep the path under `/var/log/fw-pwrctl/` — the systemd unit already allows
writes there. Elsewhere you must add a `ReadWritePaths=` line to the unit, or
the daemon logs `Prometheus textfile write failed: Read-only file system`.

It is off by default, adds no dependencies, and a write failure never affects
PL1 control — the file just stops updating. Alert on that with
`time() - fw_pwrctl_last_write_timestamp_seconds > 60`. See the README for the
full metric list.

### Can I use Prometheus without the JSONL sensor log?

Yes. `logging` and `metrics` are independent sinks with their own `enabled`
flags, both fed from the same sensor snapshot. For Prometheus only, with
nothing accumulating on disk:

    "logging": { "enabled": false },
    "metrics": { "enabled": true }

Enabling both is fine too — the snapshot is collected once per tick and handed
to each sink. Note that `sensor-plot.sh` needs the JSONL log, so turning
`logging` off means no plots.

Nothing is exported in `--mode control`, which collects no sensor data.

---

## Uninstall

### How do I completely remove fw-pwrctl?

    bash install.sh --uninstall

This stops the service, removes the daemon and systemd unit. ectool
(`/usr/local/bin/ectool`) is left in place — remove manually if desired.
Config files in `/etc/fw-pwrctl/` are preserved — remove them manually:

    sudo rm -rf /etc/fw-pwrctl

Sensor logs are in `/var/log/fw-pwrctl/` — remove if desired:

    sudo rm -rf /var/log/fw-pwrctl

The system returns to stock behavior immediately: EC manages the fan with
its default thermal tables, PL1 returns to firmware default (28W) on reboot.

### How do I manage sensor log disk usage?

The daemon rotates `sensor-log.json` when it exceeds `logging.maxSizeMB`
(default 50MB), creating gzip-compressed files like
`sensor-log.20260308_143012_654321.json.gz` (~98% compression).
A metadata index (`sensor-log-meta.json`) tracks the time range of each file.

Old files are automatically deleted when the count exceeds `logging.maxLogFiles`
(default 100, not counting the active log). Retention depends on entry size and
update interval — typically several weeks at default settings.

The metadata index (`sensor-log-meta.json`) is safe to delete — it is recreated
on the next rotation and `sensor-plot.sh` works without it.

To clean up manually:

    sudo rm -f /var/log/fw-pwrctl/sensor-log.2*.json.gz
    sudo rm -f /var/log/fw-pwrctl/sensor-log-meta.json

To automate with a cron job (keeps the last 7 days):

    sudo find /var/log/fw-pwrctl -name 'sensor-log.2*.json.gz' -mtime +7 -delete
    # The metadata index is recreated on the next rotation if missing.

To disable logging entirely, set `logging.enabled` to `false` in
`/etc/fw-pwrctl/config.json` and restart the service.

### I uninstalled but the fan is still quiet

The DDR thermal override persists in EC RAM until reboot. Reboot to restore
the original fan behavior, or run:

    # Stock 12th Gen defaults (verify yours with: sudo ectool thermalget)
    sudo ectool thermalset 2 0 360 370 313 342

to restore the original DDR `fan_off` threshold (40°C) without rebooting.
