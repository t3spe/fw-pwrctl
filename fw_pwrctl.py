#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 t3spe
"""PL1 control daemon for Framework laptops (Python 3.10+).

PI controller adjusting RAPL PL1 to target a temperature setpoint.
The EC manages the fan autonomously.
"""

import argparse
import datetime
import glob
import gzip
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

__version__ = "1.1.0"

ECTOOL = "/usr/local/bin/ectool"
ECTOOL_CMD = (ECTOOL, "--interface=dev")
CRITICAL_TEMP = 95  # °C — force PL1 to minimum when exceeded
CRITICAL_COUNT = 3  # consecutive readings above CRITICAL_TEMP before override fires
SANE_TEMP_MIN = 5
SANE_TEMP_MAX = 110
SENSOR_RESCAN_AFTER = 10  # re-scan for sensor after this many consecutive read failures
EC_OVERRIDE_RECHECK = 60  # re-check EC thermal overrides every N ticks (~60s)
RAPL_PL1_PATH = "/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"

# EC thermal overrides applied on startup.
# DDR sensor (id=2): raise fan_off from firmware default 313K (40°C) to
# 323K (50°C). Reduces idle fan noise by ~2,500 RPM. Without this override,
# the DDR sensor's low fan_off threshold (40°C) forces the fan on at low
# board temps.
# "name" is a safety guard — override is skipped if the sensor name doesn't
# match, preventing wrong thresholds on different board revisions.
# Only the fields to change are listed — apply_ec_overrides() merges them
# into the current EC config, preserving firmware values for other fields.
# NOTE: These overrides are intentionally NOT restored on daemon shutdown.
# They persist in EC RAM until reboot. This is safe because the overrides
# only raise thresholds (more permissive), never lower them.
EC_THERMAL_OVERRIDES = {
    2: {"name": "F75303_DDR", "fan_off": 323},
}


class Hardware:
    """All system I/O. Subclass with MockHardware for testing."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._prev_cpu_stats = None  # for delta-based CPU usage calculation

    # --- Sensor discovery ---

    def find_peci_sensor(self):
        """Find PECI temp sensor via cros_ec hwmon.

        Only looks for the cros_ec PECI sensor. The caller (run()) handles
        the coretemp fallback via find_coretemp_sensor().
        """
        hwmon = Path("/sys/class/hwmon")
        if not hwmon.is_dir():
            return None
        for dev in sorted(hwmon.iterdir()):
            name_file = dev / "name"
            if not name_file.exists():
                continue
            if name_file.read_text().strip() != "cros_ec":
                continue
            for label in sorted(dev.glob("temp*_label")):
                if label.read_text().strip() == "PECI":
                    idx = label.name.replace("temp", "").replace("_label", "")
                    input_file = dev / f"temp{idx}_input"
                    if input_file.exists():
                        return str(input_file)
        return None

    def find_coretemp_sensor(self):
        """Find coretemp package sensor (temp1 = Package id 0).

        Returns sysfs path or None. Used by PI controller for smoother readings
        vs PECI which reports the hottest core at each instant.
        """
        hwmon = Path("/sys/class/hwmon")
        if not hwmon.is_dir():
            return None
        for dev in sorted(hwmon.iterdir()):
            name_file = dev / "name"
            if not name_file.exists():
                continue
            if name_file.read_text().strip() == "coretemp":
                input_file = dev / "temp1_input"
                if input_file.exists():
                    return str(input_file)
        return None

    def find_sen5_sensor(self):
        """Find SEN5 board temp sensor via DPTF int340x thermal zones.

        SEN5 is near the VRM and is the EC's hot-trip trigger at 75 C.
        Returns sysfs temp path (millidegrees) or None.
        """
        thermal = Path("/sys/class/thermal")
        if not thermal.is_dir():
            return None
        for tz in sorted(thermal.glob("thermal_zone*")):
            type_file = tz / "type"
            if type_file.exists() and type_file.read_text().strip() == "SEN5":
                temp_file = tz / "temp"
                if temp_file.exists():
                    return str(temp_file)
        return None

    def discover_board_sensors(self):
        """Find SEN2-SEN5 thermal zone paths."""
        sensors = {}
        thermal = Path("/sys/class/thermal")
        if not thermal.is_dir():
            return sensors
        for tz in sorted(thermal.glob("thermal_zone*")):
            try:
                type_file = tz / "type"
                if not type_file.exists():
                    continue
                tz_type = type_file.read_text().strip()
                if tz_type in ("SEN2", "SEN3", "SEN4", "SEN5"):
                    temp_file = tz / "temp"
                    if temp_file.exists():
                        sensors[tz_type] = str(temp_file)
            except Exception:
                continue  # skip unreadable zones (permissions, TOCTOU)
        return sensors

    # --- Reads (may raise) ---

    def read_temp(self, sensor_path, retries=3, retry_delay=0.05):
        """Read temperature in °C from sysfs (millidegrees).

        Retries on transient sysfs errors (e.g. 'No data available') before
        raising. This absorbs brief glitches without immediately going to 100% fan.
        """
        last_err = None
        for attempt in range(retries):
            try:
                with open(sensor_path) as f:
                    raw = f.read().strip()
                temp = int(raw) / 1000.0
                if not (SANE_TEMP_MIN <= temp <= SANE_TEMP_MAX):
                    raise ValueError(f"temp {temp}°C outside sane range [{SANE_TEMP_MIN}, {SANE_TEMP_MAX}]")
                return temp
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(retry_delay)
        raise last_err

    def read_rapl_pl1(self):
        """Read current RAPL PL1 in microwatts."""
        with open(RAPL_PL1_PATH) as f:
            return int(f.read().strip())

    # --- Writes (respect self.dry_run) ---

    def write_rapl_pl1(self, uw):
        """Write RAPL PL1 in microwatts. Returns True on success."""
        if self.dry_run:
            return True
        try:
            with open(RAPL_PL1_PATH, "w") as f:
                f.write(str(int(uw)))
            return True
        except (OSError, ValueError) as e:
            print(f"RAPL PL1 write failed: {e}", file=sys.stderr, flush=True)
            return False

    def write_epp(self, value):
        """Write EPP to all CPUs. Returns True if all writes succeed."""
        if self.dry_run:
            return True
        success = True
        for path in sorted(glob.glob(
                "/sys/devices/system/cpu/cpu*/cpufreq/"
                "energy_performance_preference")):
            try:
                with open(path, "w") as f:
                    f.write(value)
            except OSError as e:
                print(f"EPP write failed ({path}): {e}",
                      file=sys.stderr, flush=True)
                success = False
        return success

    def set_fan(self, speed_pct):
        """Set fan duty cycle via ectool. Returns True on success.

        Not used by the daemon (EC manages fan autonomously via board sensors).
        Provided for manual testing and external tooling.

        Minimum duty is 1% (never 0%) — ectool fanduty 0 disables the EC's
        PWM hardware, and autofanctrl cannot re-enable it (Chrome EC firmware
        bug in set_thermal_control_enabled). 1% keeps hardware alive.
        """
        pct = max(1, min(100, int(round(speed_pct))))
        if self.dry_run:
            return True
        try:
            subprocess.run(
                [*ECTOOL_CMD, "fanduty", str(pct)],
                check=True, capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, OSError) as e:
            print(f"ectool fanduty {pct} failed: {e}", file=sys.stderr, flush=True)
            return False

    def restore_ec(self):
        """Restore EC automatic fan control.

        Sends fanduty 1 before autofanctrl as a workaround for Chrome EC
        firmware bug: after fanduty 0, the fan PWM hardware is disabled and
        autofanctrl alone cannot re-enable it.
        """
        if self.dry_run:
            return
        try:
            subprocess.run(
                [*ECTOOL_CMD, "fanduty", "1"],
                check=True, capture_output=True, timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            pass  # best-effort, autofanctrl below is the real goal
        try:
            subprocess.run(
                [*ECTOOL_CMD, "autofanctrl"],
                check=True, capture_output=True, timeout=5,
            )
        except (subprocess.SubprocessError, OSError) as e:
            print(f"WARNING: failed to restore EC fan control: {e}",
                  file=sys.stderr, flush=True)

    def read_fan_rpm(self):
        """Read current fan RPM via ectool. Returns int or None on failure."""
        try:
            result = subprocess.run(
                [*ECTOOL_CMD, "pwmgetfanrpm"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "RPM" in line:
                    # "Fan 0 RPM: 7580"
                    return int(line.split(":")[-1].strip())
        except Exception:
            pass
        return None

    def read_thermal_config(self):
        """Read EC thermal config via ectool thermalget.

        Returns list of dicts: sensor_id, warn, high, halt,
        fan_off, fan_max, name. Temps in Kelvin. Returns [] on failure.
        """
        if self.dry_run:
            return []
        try:
            result = subprocess.run(
                [*ECTOOL_CMD, "thermalget"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
        except (subprocess.SubprocessError, OSError):
            return []
        sensors = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("sensor"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                entry = {
                    "sensor_id": int(parts[0]),
                    "warn": int(parts[1]),
                    "high": int(parts[2]),
                    "halt": int(parts[3]),
                    "fan_off": int(parts[4]),
                    "fan_max": int(parts[5]),
                }
                if len(parts) >= 7:
                    entry["name"] = parts[6]
                sensors.append(entry)
            except (ValueError, IndexError):
                continue
        return sensors

    def write_thermal_config(self, sensor_id, warn, high, halt, fan_off, fan_max):
        """Write EC thermal config for one sensor via ectool thermalset.

        All temps in Kelvin. Returns True on success.
        Validates bounds before writing to prevent obviously wrong values
        from reaching EC RAM.
        """
        # Bounds check: reject values that would be physically nonsensical
        # or dangerous. 0 means "disabled" for warn/high, so only check
        # non-zero thresholds. fan_off and fan_max must always be sane.
        for name, val in [("fan_off", fan_off), ("fan_max", fan_max)]:
            if val <= 273 or val > 373:
                print(f"write_thermal_config: {name}={val}K out of range "
                      f"(must be 274-373K / 1-100°C)", file=sys.stderr, flush=True)
                return False
        for name, val in [("halt", halt), ("high", high), ("warn", warn)]:
            if val != 0 and (val <= 273 or val > 423):
                print(f"write_thermal_config: {name}={val}K out of range "
                      f"(must be 0 or 274-423K)", file=sys.stderr, flush=True)
                return False
        if fan_off >= fan_max:
            print(f"write_thermal_config: fan_off ({fan_off}K) must be < "
                  f"fan_max ({fan_max}K)", file=sys.stderr, flush=True)
            return False
        if self.dry_run:
            return True
        try:
            subprocess.run(
                [*ECTOOL_CMD, "thermalset", str(sensor_id),
                 str(warn), str(high), str(halt), str(fan_off), str(fan_max)],
                check=True, capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, OSError) as e:
            print(f"ectool thermalset {sensor_id} failed: {e}",
                  file=sys.stderr, flush=True)
            return False

    def check_ectool(self):
        """Verify ectool is working. Returns True on success."""
        if self.dry_run:
            return True
        try:
            subprocess.run(
                [*ECTOOL_CMD, "version"],
                check=True, capture_output=True, timeout=5,
            )
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    # --- System snapshot for sensor logging ---

    def read_system_snapshot(self, board_sensor_paths):
        """Collect system data for sensor logging.

        Returns dict with optional keys: sensors, cpu, memory,
        throttle, board_temps, rapl_pl1_w.
        Each section has its own try/except — partial data is fine.
        """
        entry = {}

        # lm-sensors via subprocess
        try:
            result = subprocess.run(
                ["sensors", "-j"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                entry["sensors"] = json.loads(result.stdout)
        except Exception:
            pass

        # CPU stats from /proc/stat (delta-based — shows recent usage, not boot average)
        try:
            with open("/proc/stat") as f:
                stat_line = f.readline()
            fields = stat_line.split()
            vals = list(map(int, fields[1:8]))
            names = ["user", "nice", "system", "idle", "iowait", "irq", "softirq"]
            if self._prev_cpu_stats is not None:
                delta = [c - p for c, p in zip(vals, self._prev_cpu_stats)]
                delta_total = sum(delta)
                if delta_total > 0:
                    cpu = {n: round(d / delta_total * 100, 1)
                           for n, d in zip(names, delta)}
                else:
                    cpu = {n: 0.0 for n in names}
            else:
                # First read — use cumulative stats as baseline, then switch
                # to delta for subsequent reads
                total = sum(vals)
                cpu = {n: round(v / total * 100, 1) for n, v in zip(names, vals)}
            self._prev_cpu_stats = vals
            with open("/proc/loadavg") as f:
                la = f.read().split()
            cpu["load_1m"] = float(la[0])
            cpu["load_5m"] = float(la[1])
            cpu["load_15m"] = float(la[2])
            entry["cpu"] = cpu
        except Exception:
            pass

        # Memory from /proc/meminfo
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    mem[parts[0].rstrip(":")] = int(parts[1])
            entry["memory"] = {
                "total_mb": round(mem["MemTotal"] / 1024),
                "used_mb": round((mem["MemTotal"] - mem["MemAvailable"]) / 1024),
                "free_mb": round(mem["MemFree"] / 1024),
                "available_mb": round(mem["MemAvailable"] / 1024),
                "buffers_mb": round(mem["Buffers"] / 1024),
                "cached_mb": round(mem["Cached"] / 1024),
                "swap_total_mb": round(mem["SwapTotal"] / 1024),
                "swap_used_mb": round((mem["SwapTotal"] - mem["SwapFree"]) / 1024),
                "swap_free_mb": round(mem["SwapFree"] / 1024),
            }
        except Exception:
            pass

        # Throttle counts
        try:
            throttle = {}
            pkg = Path("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")
            core = Path("/sys/devices/system/cpu/cpu0/thermal_throttle/core_throttle_count")
            if pkg.exists():
                throttle["package_throttle_count"] = int(pkg.read_text().strip())
            if core.exists():
                throttle["core_throttle_count"] = int(core.read_text().strip())
            if throttle:
                entry["throttle"] = throttle
        except Exception:
            pass

        # Board sensors SEN2-SEN5
        board = {}
        for name, path in sorted(board_sensor_paths.items()):
            try:
                with open(path) as f:
                    board[name.lower() + "_c"] = round(int(f.read().strip()) / 1000, 1)
            except Exception:
                pass
        if board:
            entry["board_temps"] = board

        # RAPL PL1
        try:
            with open(RAPL_PL1_PATH) as f:
                entry["rapl_pl1_w"] = round(int(f.read().strip()) / 1_000_000, 1)
        except Exception:
            pass

        # EC thermal config
        try:
            thermal = self.read_thermal_config()
            if thermal:
                entry["ec_thermal"] = thermal
        except Exception:
            pass

        return entry

    # --- Preflight checks ---

    def check_platform(self):
        """Check if running on Linux. Returns (ok, message)."""
        if sys.platform != "linux":
            return False, f"This tool only runs on Linux (detected: {sys.platform})"
        return True, ""

    def check_framework_laptop(self):
        """Check if this is a Framework laptop. Returns (ok, message)."""
        try:
            vendor = Path("/sys/class/dmi/id/board_vendor").read_text().strip()
            if vendor != "Framework":
                return False, f"Not a Framework laptop (board_vendor: {vendor})"
        except Exception as e:
            return False, f"Cannot read board vendor: {e}"
        return True, ""

    def check_alder_lake(self):
        """Check for 12th Gen Intel (Alder Lake) processor. Returns (ok, message)."""
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        if "12th Gen Intel" in line:
                            return True, ""
                        cpu = line.split(":", 1)[1].strip()
                        return False, f"Not an Alder Lake processor (found: {cpu})"
            return False, "Cannot find model name in /proc/cpuinfo"
        except Exception as e:
            return False, f"Cannot read /proc/cpuinfo: {e}"

    def check_python_version(self):
        """Check if Python is 3.10+. Returns (ok, message)."""
        if sys.version_info < (3, 10):
            v = f"{sys.version_info.major}.{sys.version_info.minor}"
            return False, f"Python 3.10+ required (found {v})"
        return True, ""

    def check_root(self):
        """Check if running as root. Returns (ok, message)."""
        if os.geteuid() != 0:
            return False, "Must run as root (sudo)"
        return True, ""

    def check_ectool_installed(self):
        """Check if ectool binary is installed and executable. Returns (ok, message)."""
        if not os.path.isfile(ECTOOL):
            return False, (
                f"ectool not found at {ECTOOL}\n"
                "  Run install-ectool.sh to install it."
            )
        if not os.access(ECTOOL, os.X_OK):
            return False, f"ectool at {ECTOOL} is not executable (chmod +x?)"
        return True, ""

    # --- Time (override for fast tests) ---

    def sleep(self, seconds):
        """Sleep. Override in tests for instant execution."""
        time.sleep(seconds)


class PIPL1Controller:
    """PI controller that adjusts RAPL PL1 to target a temperature setpoint.

    Lets the EC manage the fan autonomously. Uses coretemp as the primary
    sensor (fast: tau=5-12s) and SEN5 as a guard to prevent the EC's hot
    trip at 75 C (slow: tau=43-55s).

    PI output is watts of PL1 *reduction* from pl1_max:
      error = temp - setpoint  (positive when too hot)
      reduction = Kp * error + Ki * integral
      PL1 = pl1_max - reduction
    """

    def __init__(self, config):
        self.setpoint = config.get("setpoint", 75)
        self.kp = config["Kp"]
        self.ki = config["Ki"]
        self.pl1_min = config.get("pl1MinW", 5)
        self.pl1_max = config.get("pl1MaxW", 28)
        self.integral_max = config.get("integralMaxW", 250)
        self.ramp_up_rate = config.get("rampUpRateLimitW", 3)
        self.ramp_down_rate = config.get("rampDownRateLimitW", 3)
        self.sen5_guard = config.get("sen5GuardTemp", 75)
        self.sen5_critical = config.get("sen5CriticalTemp", 78)
        self.sen5_release = config.get("sen5ReleaseTemp", 73)
        self.sen5_cut_rate = config.get("sen5CutRateW", 2)
        self.idle_ceiling = config.get("idleCeilingW", self.pl1_max)
        self.idle_temp = config.get("idleTempC", 0)
        self.idle_release_temp = config.get("idleReleaseTempC", 0)
        smoothing = config.get("sensorSmoothing", 5)

        self._samples = deque(maxlen=smoothing)
        self._integral = 0.0
        self._last_pl1 = float(self.pl1_max)
        self._sen5_guard_active = False
        self._idle_active = False

    def update(self, raw_temp, sen5_temp, dt):
        """Compute new PL1 in watts.

        Args:
            raw_temp: coretemp reading (C)
            sen5_temp: SEN5 board temp (C), or None if unavailable
            dt: seconds since last update

        Returns:
            PL1 in watts, clamped to [pl1_min, pl1_max].
        """
        self._samples.append(raw_temp)

        # Median filter on coretemp (rejects turbo spikes)
        sorted_samples = sorted(self._samples)
        temp = sorted_samples[len(sorted_samples) // 2]

        # Emergency: coretemp critical -> minimum PL1
        if temp >= CRITICAL_TEMP:
            self._integral = self.integral_max  # remember we were hot
            self._last_pl1 = float(self.pl1_min)
            return self.pl1_min

        # SEN5 guard logic (overrides PI when board is too hot)
        if sen5_temp is not None:
            if sen5_temp >= self.sen5_critical:
                self._integral = self.integral_max  # remember we were hot
                self._last_pl1 = float(self.pl1_min)
                self._sen5_guard_active = True
                return self.pl1_min

            if sen5_temp >= self.sen5_guard:
                self._sen5_guard_active = True
                pl1 = self._last_pl1 - self.sen5_cut_rate
                pl1 = max(self.pl1_min, pl1)
                self._last_pl1 = pl1
                return pl1

            if self._sen5_guard_active and sen5_temp < self.sen5_release:
                self._sen5_guard_active = False
                self._integral = 0.0  # fresh start for PI after guard release
        elif self._sen5_guard_active:
            # SEN5 unavailable while guard active: release guard to allow
            # PI control. Keeping guard locked would permanently reduce PL1.
            self._sen5_guard_active = False
            self._integral = 0.0  # fresh start for PI after guard release

        # Idle mode transition — checked before PI so integral reset takes
        # effect immediately in this cycle (not one cycle late).
        if temp < self.idle_temp:
            self._idle_active = True
        elif temp > self.idle_release_temp:
            if self._idle_active:
                # Reset integral on idle→active transition to prevent sluggish
                # response. During idle, PL1 is clamped to idleCeilingW but
                # the integral keeps accumulating negative error (temp below
                # setpoint). Anti-windup limits positive integral but not
                # negative. Clearing it lets the controller respond
                # immediately to rising load.
                self._integral = 0.0
            self._idle_active = False

        # PI computation
        error = temp - self.setpoint

        p_term = self.kp * error

        # Anti-windup: stop integrating when output is saturated
        proposed_pl1 = self.pl1_max - (p_term + self.ki * self._integral)
        saturated_low = proposed_pl1 <= self.pl1_min and error > 0
        saturated_high = proposed_pl1 >= self.pl1_max and error < 0

        if not (saturated_low or saturated_high):
            self._integral += error * dt
            self._integral = max(-self.integral_max,
                                 min(self.integral_max, self._integral))

        i_term = self.ki * self._integral
        pl1 = self.pl1_max - (p_term + i_term)

        # Clamp
        pl1 = max(self.pl1_min, min(self.pl1_max, pl1))

        # Idle ceiling: reduce PL1 when system is cool to keep EC fan quiet
        if self._idle_active:
            pl1 = min(pl1, self.idle_ceiling)

        # Asymmetric rate limiting (per cycle, not per second)
        delta = pl1 - self._last_pl1
        if delta > 0 and self.ramp_up_rate > 0:
            pl1 = min(pl1, self._last_pl1 + self.ramp_up_rate)
        elif delta < 0 and self.ramp_down_rate > 0:
            pl1 = max(pl1, self._last_pl1 - self.ramp_down_rate)

        self._last_pl1 = pl1
        return pl1

    def notify_external_pl1(self, pl1_w):
        """Sync internal state when PL1 is set externally (e.g., main loop CRITICAL)."""
        self._last_pl1 = float(pl1_w)
        self._integral = self.integral_max  # remember we were hot

    def debug_state(self):
        """Return debug string with internal state."""
        if not self._samples:
            return "no samples"
        sorted_samples = sorted(self._samples)
        median = sorted_samples[len(sorted_samples) // 2]
        error = median - self.setpoint
        guard = "  GUARD" if self._sen5_guard_active else ""
        idle = "  IDLE" if self._idle_active else ""
        return (f"med={median:.1f}  err={error:+.1f}  "
                f"I={self._integral:.1f}  PL1={self._last_pl1:.1f}W"
                f"{guard}{idle}")

    def log_state(self, raw_temp, sen5_temp):
        """Return dict of all controller parameters for logging."""
        if not self._samples:
            return {"raw_temp_c": round(raw_temp, 1)}
        sorted_samples = sorted(self._samples)
        median = sorted_samples[len(sorted_samples) // 2]
        state = {
            "pl1_w": round(self._last_pl1, 1),
            "raw_temp_c": round(raw_temp, 1),
            "median_c": round(median, 1),
            "error": round(median - self.setpoint, 1),
            "integral": round(self._integral, 1),
            "setpoint_c": self.setpoint,
            "pl1_min_w": self.pl1_min,
            "pl1_max_w": self.pl1_max,
            "idle_active": self._idle_active,
            "idle_ceiling_w": self.idle_ceiling,
            "guard_active": self._sen5_guard_active,
        }
        if sen5_temp is not None:
            state["sen5_c"] = round(sen5_temp, 1)
        return state


def _prom_escape_label(value):
    """Escape a label value per the text exposition format."""
    return (str(value).replace("\\", "\\\\")
                      .replace('"', '\\"')
                      .replace("\n", "\\n"))


def _prom_escape_help(text):
    """Escape HELP text. Only backslash and newline are special there."""
    return str(text).replace("\\", "\\\\").replace("\n", "\\n")


def _prom_labels(labels):
    """Render a label set as {k="v",...}, or "" when there are no labels."""
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_prom_escape_label(v)}"'
                          for k, v in labels.items()) + "}"


def _prom_value(value):
    """Format a sample value.

    Missing or non-numeric values become NaN, never a fabricated 0 — a real
    0 W or 0 °C is meaningful, absence is not. Booleans become 1/0.
    """
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "NaN"
    if num != num:
        return "NaN"  # Python prints 'nan'; Prometheus requires 'NaN'
    if num == float("inf"):
        return "+Inf"
    if num == float("-inf"):
        return "-Inf"
    return repr(num)


class PrometheusTextfileWriter:
    """Write current telemetry to a Prometheus textfile-collector .prom file.

    node_exporter's textfile collector reads every *.prom in a directory on
    each scrape and merges the contents into its own /metrics output. This
    writer drops one such file and keeps it current: the whole file is
    rewritten every control tick, so scraped values are at most one tick
    (~updateInterval) stale. It holds no history — only current values.

    The write is atomic (temp file in the same directory, then os.replace)
    because node_exporter reads on its own schedule and must never observe a
    half-written file. The temp deliberately does not end in .prom, or
    node_exporter would try to parse it too.

    Never raises: a metrics failure must not disturb the control loop. On
    failure the file simply goes stale, which is detectable downstream via
    fw_pwrctl_last_write_timestamp_seconds.
    """

    FILE_MODE = 0o644  # node_exporter reads this as a different uid
    DIR_MODE = 0o755   # ...and has to be able to traverse into the directory
    BOARD_SENSORS = ("sen2", "sen3", "sen4", "sen5")
    # Repeated failures are reported at most this often. At one write per
    # ~2s, an unwritable path would otherwise flood the journal with tens of
    # thousands of identical lines per day.
    FAILURE_REPORT_INTERVAL = 300

    def __init__(self, path):
        self.path = path
        # The temp must share a filesystem with the destination for
        # os.replace to be atomic — keep it in the same directory. The pid
        # keeps two instances from clobbering each other's temp.
        self._tmp_path = f"{path}.{os.getpid()}.tmp"
        self._need_mkdir = True
        self._failures = 0
        self._last_report = 0.0
        try:
            self._ensure_dir()
        except Exception as e:
            # Non-fatal, and deliberately not just OSError: nothing about
            # metrics setup may stop the daemon from starting. Retried on
            # every write attempt.
            print(f"Prometheus textfile directory not ready: {e}",
                  file=sys.stderr, flush=True)

    def write(self, entry, now=None):
        """Render `entry` and atomically replace the .prom file.

        Never raises — metrics must not crash the control loop.
        """
        try:
            self._write(entry, now)
            self._failures = 0
        except Exception as e:
            self._report_failure(e)

    def _write(self, entry, now=None):
        """Inner implementation of write() — may raise."""
        text = self.render(entry, now)
        if self._need_mkdir:
            self._ensure_dir()
        try:
            with open(self._tmp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            # open() masks the mode with the umask, so set it explicitly:
            # node_exporter runs as another uid and must be able to read the
            # file. os.replace carries the mode over to the destination.
            os.chmod(self._tmp_path, self.FILE_MODE)
            os.replace(self._tmp_path, self.path)
        except Exception:
            self._remove_tmp()  # never leave a stray temp behind
            raise

    def _ensure_dir(self):
        """Create the containing directory. May raise OSError."""
        directory = os.path.dirname(self.path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
            # makedirs' mode is masked by the umask, so a restrictive umask
            # would leave a 0700 directory and node_exporter could not
            # traverse in — which would make FILE_MODE pointless. Only
            # applied to a directory we just created; one the operator
            # already set up is theirs to permission.
            try:
                os.chmod(directory, self.DIR_MODE)
            except OSError:
                pass
        self._need_mkdir = False

    def _remove_tmp(self):
        """Best-effort temp cleanup."""
        try:
            os.remove(self._tmp_path)
        except OSError:
            pass

    def _report_failure(self, exc):
        """Print a rate-limited failure line and arm a directory retry."""
        self._failures += 1
        self._need_mkdir = True  # the directory may have gone away
        now = time.monotonic()
        if (self._failures == 1
                or now - self._last_report >= self.FAILURE_REPORT_INTERVAL):
            print(f"Prometheus textfile write failed ({self._failures}x): {exc}",
                  file=sys.stderr, flush=True)
            self._last_report = now

    def render(self, entry, now=None):
        """Render a sensor `entry` in the Prometheus text exposition format.

        Every family is emitted on every tick, with NaN where a value is
        unavailable, so the exported series set stays constant between
        scrapes.

        entry["cpu"] and entry["memory"] are deliberately not exported —
        node_exporter already provides those as node_cpu_*/node_memory_*.
        """
        controller = entry.get("controller") or {}
        throttle = entry.get("throttle") or {}
        board = entry.get("board_temps") or {}

        # (name, type, help, [(labels, value), ...]) — one HELP/TYPE pair per
        # family, so a name can never be declared twice.
        families = [
            ("fw_pwrctl_build_info", "gauge",
             "fw-pwrctl version, always 1.",
             [({"version": __version__}, 1)]),
            ("fw_pwrctl_up", "gauge",
             "1 if fw-pwrctl wrote this file.",
             [({}, 1)]),
            ("fw_pwrctl_last_write_timestamp_seconds", "gauge",
             "Unix time this file was written.",
             [({}, time.time() if now is None else now)]),
            ("fw_pwrctl_rapl_pl1_watts", "gauge",
             "Current RAPL PL1 power limit (readback).",
             [({}, entry.get("rapl_pl1_w"))]),
            ("fw_pwrctl_controller_pl1_target_watts", "gauge",
             "PL1 the PI controller last asked for.",
             [({}, controller.get("pl1_w"))]),
            ("fw_pwrctl_pl1_min_watts", "gauge",
             "Configured PL1 lower bound.",
             [({}, controller.get("pl1_min_w"))]),
            ("fw_pwrctl_pl1_max_watts", "gauge",
             "Configured PL1 upper bound.",
             [({}, controller.get("pl1_max_w"))]),
            ("fw_pwrctl_controller_setpoint_celsius", "gauge",
             "PI controller temperature setpoint.",
             [({}, controller.get("setpoint_c"))]),
            ("fw_pwrctl_controller_temp_celsius", "gauge",
             "Controller input temperature.",
             [({"kind": "raw"}, controller.get("raw_temp_c")),
              ({"kind": "median"}, controller.get("median_c"))]),
            ("fw_pwrctl_controller_error_celsius", "gauge",
             "temp - setpoint (positive = too hot).",
             [({}, controller.get("error"))]),
            ("fw_pwrctl_controller_integral_watts", "gauge",
             "PI integral term.",
             [({}, controller.get("integral"))]),
            ("fw_pwrctl_idle_ceiling_watts", "gauge",
             "PL1 ceiling applied while idle mode is engaged.",
             [({}, controller.get("idle_ceiling_w"))]),
            ("fw_pwrctl_idle_active", "gauge",
             "1 when idle mode is engaged.",
             [({}, controller.get("idle_active"))]),
            ("fw_pwrctl_guard_active", "gauge",
             "1 when the SEN5 board-sensor guard is engaged.",
             [({}, controller.get("guard_active"))]),
            ("fw_pwrctl_epp_active", "gauge",
             "1 when the idle energy-performance preference is applied.",
             [({}, controller.get("epp_active"))]),
            ("fw_pwrctl_cpu_throttle_total", "counter",
             "Cumulative CPU thermal-throttle events.",
             [({"scope": "package"}, throttle.get("package_throttle_count")),
              ({"scope": "core"}, throttle.get("core_throttle_count"))]),
            ("fw_pwrctl_board_temp_celsius", "gauge",
             "Board sensor temperatures.",
             [({"sensor": s}, board.get(f"{s}_c")) for s in self.BOARD_SENSORS]),
        ]

        lines = []
        for name, metric_type, help_text, samples in families:
            lines.append(f"# HELP {name} {_prom_escape_help(help_text)}")
            lines.append(f"# TYPE {name} {metric_type}")
            for labels, value in samples:
                lines.append(f"{name}{_prom_labels(labels)} {_prom_value(value)}")
        return "\n".join(lines) + "\n"


class SensorLogger:
    """Append JSON-lines sensor snapshots to a log file."""

    MAX_BUFFER_ENTRIES = 1000  # ~2000s at 2s intervals, ~1MB

    def __init__(self, config, hw=None, metrics_config=None):
        self.enabled = config.get("enabled", False)
        self.path = config.get("path", "/var/log/fw-pwrctl/sensor-log.json")
        self.max_size = config.get("maxSizeMB", 50) * 1024 * 1024
        self.max_log_files = config.get("maxLogFiles", 100)
        self.flush_interval = config.get("flushIntervalSeconds", 120)
        self._hw = hw
        self._board_sensor_paths = hw.discover_board_sensors() if hw else {}
        self._buffer = []
        self._last_flush = time.monotonic()
        self._flush_failures = 0
        self._meta_path = os.path.join(os.path.dirname(self.path),
                                       "sensor-log-meta.json")
        self._meta_lock = threading.Lock()
        # Second output sink, configured exactly like the JSONL log above and
        # switched on independently: either, both, or neither.
        metrics_config = metrics_config or {}
        self.metrics_enabled = metrics_config.get("enabled", False)
        self.metrics_path = metrics_config.get(
            "path", "/var/log/fw-pwrctl/textfile/fw_pwrctl.prom")
        self._metrics = (PrometheusTextfileWriter(self.metrics_path)
                         if self.metrics_enabled else None)

    def log(self, controller_state=None):
        """Collect all sensor data and append one JSON line.

        Never raises — logging must not crash the control loop.
        """
        # Either sink is reason enough to collect the snapshot — they are
        # enabled independently.
        if not (self.enabled or self._metrics):
            return
        try:
            self._collect_and_buffer(controller_state)
        except Exception as e:
            print(f"Sensor log collection failed: {e}", file=sys.stderr, flush=True)

    def _collect_and_buffer(self, controller_state):
        """Inner implementation of log() — may raise."""
        entry = {"timestamp": datetime.datetime.now().astimezone().isoformat()}

        if self._hw:
            entry.update(self._hw.read_system_snapshot(self._board_sensor_paths))

        # Controller internal state
        if controller_state is not None:
            entry["controller"] = controller_state

        # Prometheus textfile export — immediate and unbuffered, so scraped
        # values are never more than one tick stale. Its own try/except: a
        # metrics failure must not abort JSONL buffering below (write() is
        # already fail-safe; this is the belt to its braces).
        if self._metrics is not None:
            try:
                self._metrics.write(entry)
            except Exception as e:
                print(f"Prometheus textfile write failed: {e}",
                      file=sys.stderr, flush=True)

        if not self.enabled:
            return  # metrics-only: nothing to buffer or flush

        # Buffer in memory (cap size to prevent runaway growth)
        self._buffer.append(json.dumps(entry))
        if len(self._buffer) > self.MAX_BUFFER_ENTRIES:
            self._buffer = self._buffer[-self.MAX_BUFFER_ENTRIES:]

        # Flush to disk periodically
        if time.monotonic() - self._last_flush >= self.flush_interval:
            self.flush()

    def flush(self):
        """Write buffered entries to disk."""
        if not self._buffer:
            return
        self._rotate_if_needed()
        try:
            log_dir = os.path.dirname(self.path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            with open(self.path, "a") as f:
                f.write("\n".join(self._buffer) + "\n")
            self._buffer.clear()
            self._last_flush = time.monotonic()
            self._flush_failures = 0
        except Exception as e:
            self._flush_failures += 1
            # Keep buffer on failure — MAX_BUFFER_ENTRIES cap prevents
            # unbounded growth, and data will be written on next flush
            self._last_flush = time.monotonic()  # space out retries
            print(f"Sensor log write failed ({self._flush_failures}x): {e}",
                  file=sys.stderr, flush=True)
            if self._flush_failures >= 3:
                self.enabled = False
                self._buffer.clear()  # give up — drop remaining data
                print("Sensor logging disabled after 3 consecutive flush failures",
                      file=sys.stderr, flush=True)

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds max size.

        After rotation: compress to .json.gz in a background thread,
        update metadata index, and delete oldest files if count exceeds
        max_log_files.
        """
        try:
            if not os.path.exists(self.path) or os.path.getsize(self.path) <= self.max_size:
                return
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base, ext = os.path.splitext(self.path)
            dest = f"{base}.{ts}{ext}"
            if os.path.exists(dest):
                return  # extremely unlikely; skip rotation this cycle
            os.rename(self.path, dest)

            # Read first/last line for metadata timestamps
            start_ts, end_ts = self._read_boundary_timestamps(dest)

            # Compress in background to avoid blocking the control loop
            threading.Thread(
                target=self._compress_and_finalize,
                args=(dest, start_ts, end_ts),
                daemon=True,
                name="fw-pwrctl-compress",
            ).start()

        except Exception as e:
            print(f"Sensor log rotation failed: {e}", file=sys.stderr, flush=True)

    def _read_boundary_timestamps(self, filepath):
        """Read first and last timestamps from a JSONL file."""
        start_ts = end_ts = None
        try:
            with open(filepath, "rb") as f:
                first_line = f.readline().decode().strip()
                if first_line:
                    start_ts = json.loads(first_line).get("timestamp")
                f.seek(0, 2)
                end_pos = f.tell()
                if end_pos > 0:
                    # Skip trailing newline(s)
                    pos = end_pos - 1
                    while pos > 0:
                        f.seek(pos)
                        if f.read(1) not in (b"\n", b"\r"):
                            break
                        pos -= 1
                    # Find start of last line
                    while pos > 0:
                        f.seek(pos - 1)
                        if f.read(1) == b"\n":
                            break
                        pos -= 1
                    f.seek(pos)
                    last_line = f.readline().decode().strip()
                    if last_line:
                        end_ts = json.loads(last_line).get("timestamp")
        except Exception:
            pass  # metadata will be incomplete but rotation still works
        return start_ts, end_ts

    def _compress_and_finalize(self, dest, start_ts, end_ts):
        """Compress a rotated log file and update metadata (runs in background)."""
        gz_tmp = dest + ".gz.tmp"
        gz_dest = dest + ".gz"
        try:
            with open(dest, "rb") as f_in, gzip.open(gz_tmp, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.rename(gz_tmp, gz_dest)  # atomic — no partial .gz on SIGTERM
            os.remove(dest)
            dest = gz_dest
        except Exception as e:
            print(f"Sensor log compression failed: {e}",
                  file=sys.stderr, flush=True)
            # Clean up partial temp file
            try:
                os.remove(gz_tmp)
            except OSError:
                pass
            # Keep uncompressed file — still a valid rotation

        self._update_metadata(os.path.basename(dest), start_ts, end_ts)
        self._prune_old_logs()

    def _update_metadata(self, filename, start_ts, end_ts):
        """Append an entry to the metadata index (atomic write)."""
        with self._meta_lock:
            try:
                meta = []
                if os.path.exists(self._meta_path):
                    try:
                        with open(self._meta_path) as f:
                            meta = json.load(f)
                    except (json.JSONDecodeError, ValueError):
                        meta = []  # corrupted — start fresh
                meta.append({"file": filename, "start": start_ts, "end": end_ts})
                tmp = self._meta_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(meta, f)
                os.rename(tmp, self._meta_path)
            except Exception as e:
                print(f"Sensor log metadata update failed: {e}",
                      file=sys.stderr, flush=True)

    def _prune_old_logs(self):
        """Delete oldest rotated log files if count exceeds max_log_files."""
        try:
            log_dir = os.path.dirname(self.path)
            rotated = sorted(glob.glob(os.path.join(log_dir, "sensor-log.*.json.gz")))
            # Also count uncompressed rotated files (pre-compression leftovers)
            rotated += sorted(glob.glob(os.path.join(log_dir, "sensor-log.*.json")))
            rotated.sort()  # chronological by timestamp in filename
            if len(rotated) <= self.max_log_files:
                return
            to_delete = rotated[:len(rotated) - self.max_log_files]
            deleted_names = set()
            for fpath in to_delete:
                try:
                    os.remove(fpath)
                    deleted_names.add(os.path.basename(fpath))
                except OSError:
                    pass
            # Prune metadata
            if deleted_names:
                with self._meta_lock:
                    try:
                        if os.path.exists(self._meta_path):
                            with open(self._meta_path) as f:
                                meta = json.load(f)
                            meta = [e for e in meta if e["file"] not in deleted_names]
                            tmp = self._meta_path + ".tmp"
                            with open(tmp, "w") as f:
                                json.dump(meta, f)
                            os.rename(tmp, self._meta_path)
                    except Exception:
                        pass
        except Exception as e:
            print(f"Sensor log pruning failed: {e}",
                  file=sys.stderr, flush=True)


def _validate_settings(config):
    """Validate PI controller settings."""
    for key in ("setpoint", "Kp", "Ki"):
        if key not in config:
            raise ValueError(f"missing '{key}'")
    if not isinstance(config["setpoint"], (int, float)) or not 30 <= config["setpoint"] <= 95:
        raise ValueError("setpoint must be 30-95")
    if not isinstance(config["Kp"], (int, float)) or config["Kp"] <= 0:
        raise ValueError("Kp must be > 0")
    if not isinstance(config["Ki"], (int, float)) or config["Ki"] <= 0:
        raise ValueError("Ki must be > 0")
    pl1_min = config.get("pl1MinW", 5)
    pl1_max = config.get("pl1MaxW", 28)
    if not isinstance(pl1_min, (int, float)) or pl1_min < 1:
        raise ValueError("pl1MinW must be >= 1")
    if not isinstance(pl1_max, (int, float)) or pl1_max <= pl1_min:
        raise ValueError("pl1MaxW must be > pl1MinW")
    if pl1_max > 64:
        raise ValueError("pl1MaxW must be <= 64 (PL2 firmware limit)")
    # Idle ceiling validation (optional params — only validate if present)
    if "idleCeilingW" in config:
        ceiling = config["idleCeilingW"]
        if not isinstance(ceiling, (int, float)) or ceiling < pl1_min or ceiling > pl1_max:
            raise ValueError("idleCeilingW must be between pl1MinW and pl1MaxW")
    if ("idleTempC" in config) != ("idleReleaseTempC" in config):
        raise ValueError("idleTempC and idleReleaseTempC must both be present or both absent")
    if "idleTempC" in config:
        idle_temp = config["idleTempC"]
        idle_release = config["idleReleaseTempC"]
        if not isinstance(idle_temp, (int, float)):
            raise ValueError("idleTempC must be numeric")
        if not isinstance(idle_release, (int, float)):
            raise ValueError("idleReleaseTempC must be numeric")
        if idle_temp >= idle_release:
            raise ValueError("idleTempC must be < idleReleaseTempC")
        setpoint = config.get("setpoint", 75)
        if idle_release >= setpoint:
            raise ValueError("idleReleaseTempC must be < setpoint")
    # updateInterval (used by run() for control loop timing)
    if "updateInterval" in config:
        ui = config["updateInterval"]
        if not isinstance(ui, int) or ui < 1:
            raise ValueError("updateInterval must be integer >= 1")
    # sensorSmoothing (deque maxlen for median filter — 0 would crash)
    if "sensorSmoothing" in config:
        ss = config["sensorSmoothing"]
        if not isinstance(ss, int) or ss < 1:
            raise ValueError("sensorSmoothing must be integer >= 1")
    # integralMaxW (anti-windup bound — 0 disables I-term entirely)
    if "integralMaxW" in config:
        imax = config["integralMaxW"]
        if not isinstance(imax, (int, float)) or imax <= 0:
            raise ValueError("integralMaxW must be > 0")
    # Ramp rate limits (0 disables limiting; negatives are rejected)
    if "rampUpRateLimitW" in config:
        rur = config["rampUpRateLimitW"]
        if not isinstance(rur, (int, float)) or rur < 0:
            raise ValueError("rampUpRateLimitW must be >= 0")
    if "rampDownRateLimitW" in config:
        rdr = config["rampDownRateLimitW"]
        if not isinstance(rdr, (int, float)) or rdr < 0:
            raise ValueError("rampDownRateLimitW must be >= 0")
    # SEN5 guard temperature ordering
    if any(k in config for k in ("sen5GuardTemp", "sen5CriticalTemp", "sen5ReleaseTemp")):
        sen5_release = config.get("sen5ReleaseTemp", 73)
        sen5_guard = config.get("sen5GuardTemp", 75)
        sen5_critical = config.get("sen5CriticalTemp", 78)
        if not all(isinstance(t, (int, float)) for t in (sen5_release, sen5_guard, sen5_critical)):
            raise ValueError("SEN5 temps must be numeric")
        if sen5_release >= sen5_guard:
            raise ValueError("sen5ReleaseTemp must be < sen5GuardTemp")
        if sen5_guard > sen5_critical:
            raise ValueError("sen5GuardTemp must be <= sen5CriticalTemp")
    # sen5CutRateW (0 makes guard ineffective, negative increases PL1)
    if "sen5CutRateW" in config:
        scr = config["sen5CutRateW"]
        if not isinstance(scr, (int, float)) or scr <= 0:
            raise ValueError("sen5CutRateW must be > 0")
    # EPP management (optional — both must be present or both absent)
    if ("idleEPP" in config) != ("normalEPP" in config):
        raise ValueError("idleEPP and normalEPP must both be present or both absent")
    valid_epp = {"performance", "balance_performance", "balance_power", "power"}
    if "idleEPP" in config:
        if config["idleEPP"] not in valid_epp:
            raise ValueError(f"idleEPP must be one of: {', '.join(sorted(valid_epp))}")
        if config["normalEPP"] not in valid_epp:
            raise ValueError(f"normalEPP must be one of: {', '.join(sorted(valid_epp))}")
    # Warn about idle-related keys that have no effect without idleTempC
    if "idleTempC" not in config:
        idle_only = [k for k in ("idleCeilingW", "idleEPP", "normalEPP") if k in config]
        if idle_only:
            print(f"WARNING: {', '.join(idle_only)} has no effect without "
                  f"idleTempC/idleReleaseTempC", file=sys.stderr, flush=True)


_KNOWN_KEYS = {
    "setpoint", "Kp", "Ki", "pl1MinW", "pl1MaxW", "integralMaxW",
    "rampUpRateLimitW", "rampDownRateLimitW", "sensorSmoothing",
    "updateInterval", "idleCeilingW", "idleTempC", "idleReleaseTempC",
    "idleEPP", "normalEPP",
    "sen5GuardTemp", "sen5CriticalTemp", "sen5ReleaseTemp", "sen5CutRateW",
    "logging", "metrics",
}


def validate_config(config):
    """Validate config structure on startup. Raises ValueError on problems."""
    unknown = set(config.keys()) - _KNOWN_KEYS
    if unknown:
        print(f"WARNING: unknown config key(s): {', '.join(sorted(unknown))}",
              file=sys.stderr, flush=True)
    _validate_settings(config)
    if "logging" in config:
        log = config["logging"]
        if not isinstance(log, dict):
            raise ValueError("logging must be an object")
        if not isinstance(log.get("enabled", False), bool):
            raise ValueError("logging.enabled must be a boolean")
        if "path" in log and not isinstance(log["path"], str):
            raise ValueError("logging.path must be a string")
        if "maxSizeMB" in log:
            ms = log["maxSizeMB"]
            if not isinstance(ms, (int, float)) or ms <= 0:
                raise ValueError("logging.maxSizeMB must be > 0")
        if "flushIntervalSeconds" in log:
            fi = log["flushIntervalSeconds"]
            if not isinstance(fi, (int, float)) or fi <= 0:
                raise ValueError("logging.flushIntervalSeconds must be > 0")
        if "maxLogFiles" in log:
            ml = log["maxLogFiles"]
            if not isinstance(ml, int) or ml <= 0:
                raise ValueError("logging.maxLogFiles must be a positive integer")
    # Prometheus textfile export — same shape as logging, enabled separately
    if "metrics" in config:
        met = config["metrics"]
        if not isinstance(met, dict):
            raise ValueError("metrics must be an object")
        if not isinstance(met.get("enabled", False), bool):
            raise ValueError("metrics.enabled must be a boolean")
        if "path" in met:
            if not isinstance(met["path"], str):
                raise ValueError("metrics.path must be a string")
            if not met["path"].endswith(".prom"):
                # node_exporter's textfile collector only reads *.prom, so
                # any other suffix would silently export nothing.
                raise ValueError("metrics.path must end in .prom")
            if not os.path.isabs(met["path"]):
                # The daemon's cwd is / under systemd — relative paths trap.
                raise ValueError("metrics.path must be an absolute path")


def preflight_checks(hw):
    """Run all hardware/environment checks before starting. Exits on failure."""
    checks = [
        ("Platform", hw.check_platform()),
        ("Python version", hw.check_python_version()),
        ("Root privileges", hw.check_root()),
        ("Framework laptop", hw.check_framework_laptop()),
        ("Alder Lake CPU", hw.check_alder_lake()),
        ("ectool", hw.check_ectool_installed()),
    ]
    failed = [(name, msg) for name, (ok, msg) in checks if not ok]
    if failed:
        print("Preflight checks failed:", file=sys.stderr)
        for name, msg in failed:
            print(f"  {name}: {msg}", file=sys.stderr)
        sys.exit(1)


def run(config, hw, debug=False, max_ticks=0, mode="full"):
    """Main daemon logic. Testable with mock hardware.

    max_ticks: exit after N iterations (0 = run forever, for production).
    mode: 'full' (default), 'monitor' (logging only), 'control' (PI loop only).
    """
    if mode not in ("full", "monitor", "control"):
        raise ValueError(f"invalid mode: {mode!r} (must be 'full', 'monitor', or 'control')")
    do_control = mode in ("full", "control")
    do_monitor = mode in ("full", "monitor")

    # Sensor logging (skip hw wiring when not monitoring — avoids discover_board_sensors())
    # Both sinks read the same snapshot, so both belong to the monitor path:
    # control-only mode collects nothing to write.
    log_config = config.get("logging", {})
    metrics_config = config.get("metrics", {}) if do_monitor else {}
    sensor_logger = SensorLogger(log_config, hw if do_monitor else None,
                                 metrics_config=metrics_config)
    if not do_monitor:
        sensor_logger.enabled = False

    pl1_controller = PIPL1Controller(config)
    update_freq = config.get("updateInterval", 2)
    sensor = hw.find_coretemp_sensor()
    if sensor is None:
        sensor = hw.find_peci_sensor()
        print("WARNING: coretemp not found, falling back to PECI",
              file=sys.stderr, flush=True)
    sen5_sensor = hw.find_sen5_sensor()
    if sen5_sensor is None and do_control:
        print("WARNING: SEN5 not found, running without board temp guard",
              file=sys.stderr, flush=True)

    if sensor is None:
        print("No temperature sensor found!", file=sys.stderr)
        sys.exit(1)

    # Verify sensor is readable before entering main loop
    try:
        initial_temp = hw.read_temp(sensor)
    except Exception as e:
        print(f"Cannot read sensor {sensor}: {e}", file=sys.stderr)
        sys.exit(1)

    # Verify ectool works before entering main loop.
    # All modes check ectool so the environment is validated upfront —
    # a user can capture a baseline with monitor, then switch to control
    # without discovering ectool is broken.
    if not hw.check_ectool():
        print("ectool not working", file=sys.stderr)
        sys.exit(1)

    # Verify RAPL constraint_0 is actually PL1 (long_term), not PL2 or
    # something else. Some kernels or firmware could reorder constraints.
    rapl_name_path = RAPL_PL1_PATH.replace("power_limit_uw", "name")
    try:
        with open(rapl_name_path) as f:
            constraint_name = f.read().strip()
        if constraint_name != "long_term":
            print(f"RAPL constraint_0 is '{constraint_name}', expected 'long_term' (PL1)",
                  file=sys.stderr)
            sys.exit(1)
    except FileNotFoundError:
        pass  # Some kernels don't expose constraint_0_name — allow it

    # Save original PL1 and ensure EC auto fan
    original_pl1_uw = None
    if do_control:
        try:
            original_pl1_uw = hw.read_rapl_pl1()
        except Exception as e:
            print(f"Cannot read RAPL PL1: {e}", file=sys.stderr)
            sys.exit(1)
        hw.restore_ec()  # ensure EC auto fan on startup

    # Apply EC thermal overrides (e.g. DDR fan_off 40→50°C).
    # Called at startup and periodically to handle suspend/resume (EC resets
    # to firmware defaults on resume, losing the override).
    def apply_ec_overrides(verbose=True):
        """Re-apply EC thermal overrides by merging into current config.

        Only the fields listed in EC_THERMAL_OVERRIDES are changed — all other
        thermal fields (warn, high, halt, fan_max) are preserved from the
        current EC config. Returns True if any overrides were applied.
        """
        current_thermal = hw.read_thermal_config()
        applied = False
        for sensor_id, desired in EC_THERMAL_OVERRIDES.items():
            actual = next((s for s in current_thermal if s["sensor_id"] == sensor_id), None)
            if actual is None:
                continue
            expected_name = desired.get("name")
            if expected_name and actual.get("name") != expected_name:
                if verbose:
                    print(f"WARNING: EC sensor {sensor_id} is '{actual.get('name')}', "
                          f"expected '{expected_name}' — skipping thermal override",
                          file=sys.stderr, flush=True)
                continue
            override_fields = {k: v for k, v in desired.items() if k != "name"}
            if any(actual.get(k) != v for k, v in override_fields.items()):
                # Merge: start with current EC values, overlay our overrides
                merged = {k: actual[k] for k in ("warn", "high", "halt", "fan_off", "fan_max")}
                merged.update(override_fields)
                changes = ", ".join(f"{k} {actual.get(k)}→{v}K"
                                    for k, v in override_fields.items())
                print(f"Correcting EC thermal sensor {sensor_id} ({changes})",
                      flush=True)
                if hw.write_thermal_config(sensor_id, **merged):
                    applied = True
                elif verbose:
                    print(f"WARNING: failed to write thermal override for sensor {sensor_id}",
                          file=sys.stderr, flush=True)
        return applied

    if do_control:
        if hw.dry_run:
            print("DRY RUN — skipping EC thermal overrides", flush=True)
        else:
            apply_ec_overrides()

    # EPP management
    idle_epp = config.get("idleEPP", None)
    normal_epp = config.get("normalEPP", None)
    epp_managed = bool(idle_epp and normal_epp)
    epp_active = False

    if epp_managed and do_control:
        print(f"EPP management enabled: idle={idle_epp}, normal={normal_epp}",
              flush=True)
        hw.write_epp(normal_epp)  # clean slate (handles restart-while-idle)

    print(f"fw-pwrctl {__version__}", flush=True)
    print(f"Mode: {mode}", flush=True)
    print(f"Sensor: {sensor} ({initial_temp:.1f}°C)", flush=True)
    sen5_str = f", SEN5={sen5_sensor}" if sen5_sensor else ", no SEN5"
    print(f"Controller: setpoint={config.get('setpoint', 75)}°C, "
          f"Kp={config['Kp']}, Ki={config['Ki']}, "
          f"PL1={pl1_controller.pl1_min}-{pl1_controller.pl1_max}W, "
          f"update every {update_freq}s{sen5_str}", flush=True)
    if do_control:
        print(f"Original PL1: {original_pl1_uw / 1_000_000:.1f}W", flush=True)
    if sensor_logger.enabled:
        print(f"Sensor logging: {sensor_logger.path} "
              f"(every {update_freq}s, flush every {sensor_logger.flush_interval}s, "
              f"max {log_config.get('maxSizeMB', 50)}MB)", flush=True)
    if sensor_logger.metrics_enabled:
        print(f"Prometheus metrics: {sensor_logger.metrics_path} "
              f"(rewritten every {update_freq}s)", flush=True)
    if hw.dry_run:
        print("DRY RUN — not calling ectool/RAPL", flush=True)

    need_fan_update = True  # ensures first successful reading triggers an update
    critical_temp_count = 0  # consecutive readings above CRITICAL_TEMP (unused in monitor mode)
    temp_read_failures = 0
    running = True

    def shutdown(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Set dt baseline after all startup work (EC overrides, EPP, printing)
    # so the first PI update gets an accurate dt (~2s), not one inflated
    # by startup overhead (which could be 3-5s).
    last_update_time = time.monotonic()

    tick = 0
    try:
        while running and (max_ticks == 0 or tick < max_ticks):
            # Read temperature
            try:
                temp = hw.read_temp(sensor)
                temp_read_failures = 0
            except Exception as e:
                temp_read_failures += 1
                if do_control:
                    print(f"Temp read failed ({temp_read_failures}x): {e} — PL1 to min",
                          file=sys.stderr, flush=True)
                    if not hw.write_rapl_pl1(pl1_controller.pl1_min * 1_000_000):
                        print("WARNING: RAPL PL1 write failed", file=sys.stderr, flush=True)
                    pl1_controller.notify_external_pl1(pl1_controller.pl1_min)
                else:
                    print(f"Temp read failed ({temp_read_failures}x): {e}",
                          file=sys.stderr, flush=True)
                # Re-scan sensor after persistent failures (hwmon renumbering)
                if temp_read_failures >= SENSOR_RESCAN_AFTER:
                    new_sensor = hw.find_coretemp_sensor() or hw.find_peci_sensor()
                    if new_sensor and new_sensor != sensor:
                        print(f"Sensor changed: {sensor} -> {new_sensor}", flush=True)
                        sensor = new_sensor
                        need_fan_update = True
                    elif new_sensor is None:
                        print("Sensor re-scan found nothing, keeping old path", flush=True)
                    # Also re-scan SEN5 (thermal zone numbering can change too)
                    new_sen5 = hw.find_sen5_sensor()
                    if new_sen5 != sen5_sensor:
                        old_str = sen5_sensor or "none"
                        new_str = new_sen5 or "none"
                        print(f"SEN5 changed: {old_str} -> {new_str}", flush=True)
                        sen5_sensor = new_sen5
                    temp_read_failures = 0
                last_update_time = time.monotonic()
                hw.sleep(1)
                tick += 1
                continue

            tick += 1

            # Critical temp override — require CRITICAL_COUNT consecutive
            # readings above CRITICAL_TEMP to force PL1 to minimum
            if do_control:
                if temp >= CRITICAL_TEMP:
                    critical_temp_count += 1
                    if critical_temp_count >= CRITICAL_COUNT:
                        if not hw.write_rapl_pl1(pl1_controller.pl1_min * 1_000_000):
                            print("WARNING: RAPL PL1 write failed at critical temp",
                                  file=sys.stderr, flush=True)
                        pl1_controller.notify_external_pl1(pl1_controller.pl1_min)
                        if debug:
                            print(f"temp={temp:.1f}  CRITICAL ({critical_temp_count}x)  "
                                  f"PL1->{pl1_controller.pl1_min}W",
                                  flush=True)
                        # Feed critical temps into PI median buffer so it
                        # stays current. Without this, post-critical PI uses
                        # stale pre-critical median for ~5 ticks.
                        pl1_controller._samples.append(temp)
                        # Log the critical event (don't skip logging on the
                        # most important data point for post-mortem analysis)
                        if do_monitor:
                            crit_sen5 = None
                            if sen5_sensor:
                                try:
                                    crit_sen5 = hw.read_temp(sen5_sensor)
                                except Exception:
                                    pass
                            crit_state = pl1_controller.log_state(temp, crit_sen5)
                            crit_state["epp_active"] = epp_active
                            sensor_logger.log(controller_state=crit_state)
                        last_update_time = time.monotonic()
                        hw.sleep(1)
                        continue
                else:
                    critical_temp_count = 0

            # Update PL1 on first reading, then every update_freq seconds
            if need_fan_update or tick % update_freq == 0:
                sen5_temp = None
                if sen5_sensor:
                    try:
                        sen5_temp = hw.read_temp(sen5_sensor)
                    except Exception:
                        pass  # SEN5 failure is non-fatal

                if do_control:
                    now = time.monotonic()
                    actual_dt = now - last_update_time
                    # Cap dt to prevent integral windup after suspend/resume.
                    # time.monotonic() includes suspend time, so a 30-minute
                    # suspend would produce dt=1800s and a huge integral spike.
                    actual_dt = min(actual_dt, update_freq * 3)
                    last_update_time = now
                    new_pl1 = pl1_controller.update(
                        temp, sen5_temp, actual_dt)
                    if not hw.write_rapl_pl1(int(round(new_pl1 * 1_000_000))):
                        print(f"WARNING: RAPL PL1 write failed ({new_pl1:.1f}W)",
                              file=sys.stderr, flush=True)

                    if epp_managed:
                        if pl1_controller._idle_active and not epp_active:
                            hw.write_epp(idle_epp)
                            epp_active = True
                            if debug:
                                print(f"  EPP -> {idle_epp}", flush=True)
                        elif not pl1_controller._idle_active and epp_active:
                            hw.write_epp(normal_epp)
                            epp_active = False
                            if debug:
                                print(f"  EPP -> {normal_epp}", flush=True)

                    if debug:
                        sen5_str = (f"  SEN5={sen5_temp:.1f}"
                                    if sen5_temp is not None else "")
                        print(f"temp={temp:.1f}{sen5_str}  "
                              f"{pl1_controller.debug_state()}",
                              flush=True)
                elif debug:
                    sen5_str = (f"  SEN5={sen5_temp:.1f}"
                                if sen5_temp is not None else "")
                    print(f"temp={temp:.1f}{sen5_str}", flush=True)

                if do_monitor:
                    if do_control:
                        controller_state = pl1_controller.log_state(
                            temp, sen5_temp)
                        controller_state["epp_active"] = epp_active
                    else:
                        controller_state = None
                    sensor_logger.log(
                        controller_state=controller_state)

                need_fan_update = False

            # Periodically re-apply EC thermal overrides. The EC resets to
            # firmware defaults on suspend/resume, losing the DDR fan_off
            # override. This re-checks and re-applies if needed.
            if do_control and tick > 0 and tick % EC_OVERRIDE_RECHECK == 0:
                apply_ec_overrides(verbose=False)

            hw.sleep(1)
    finally:
        sensor_logger.flush()
        if do_control:
            if epp_managed and epp_active:
                # NOTE: We restore normalEPP, not the pre-daemon EPP. The daemon
                # does not save the original EPP because the "right" value depends
                # on the user's power profile, which may change during runtime.
                print(f"Restoring EPP to {normal_epp}...", flush=True)
                hw.write_epp(normal_epp)
            print(f"Restoring PL1 to {original_pl1_uw / 1_000_000:.1f}W...",
                  flush=True)
            hw.write_rapl_pl1(original_pl1_uw)
            print("Restoring EC fan control...", flush=True)
            hw.restore_ec()


def main():
    """Entry point: parse CLI args, create hardware, run daemon."""
    parser = argparse.ArgumentParser(description="PL1 thermal control daemon for Framework laptops")
    parser.add_argument(
        "--config", type=str,
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        help="Path to config.json",
    )
    parser.add_argument("--debug", action="store_true", help="Print state every update")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip all hardware writes (ectool, RAPL, EPP)")
    parser.add_argument(
        "--mode", choices=["full", "monitor", "control"], default="full",
        help="full: control + logging (default), monitor: logging only, control: PI loop only",
    )
    parser.add_argument("--version", action="version", version=f"fw-pwrctl {__version__}")
    args = parser.parse_args()

    # Load and validate config
    try:
        with open(args.config) as f:
            config = json.load(f)
    except FileNotFoundError:
        print(f"Config file not found: {args.config}", file=sys.stderr)
        print("  Run install.sh or use --config to specify the path.",
              file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {args.config}: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        validate_config(config)
    except ValueError as e:
        print(f"Invalid config: {e}", file=sys.stderr)
        sys.exit(1)

    hw = Hardware(dry_run=args.dry_run)
    preflight_checks(hw)
    run(config, hw, debug=args.debug, mode=args.mode)


if __name__ == "__main__":
    main()
