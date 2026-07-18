# Changelog

## v1.1.0 — 2026-07-18

Additive, opt-in release. With the `metrics` section absent or disabled the
daemon behaves exactly as v1.0.0 did.

- Prometheus textfile export: set `metrics.enabled` and the daemon rewrites
  `metrics.path` every control tick for node_exporter's textfile collector
  (`--collector.textfile.directory`). No HTTP server, no new dependencies,
  stdlib only
- `logging` and `metrics` are independent sinks over the same sensor snapshot,
  each with its own `enabled` flag — run either, both, or neither. Prometheus
  without the JSONL log is a supported configuration
- Exports PL1 (readback + controller target), setpoint, error, integral, idle
  and guard state, board sensors SEN2–SEN5, and cumulative CPU throttle
  counters, plus `fw_pwrctl_up` / `fw_pwrctl_build_info` /
  `fw_pwrctl_last_write_timestamp_seconds` for staleness alerting. CPU and
  memory are not exported — node_exporter already covers them
- Writes are atomic (temp file + `os.replace`, mode 0644) so a scraper running
  as another uid never reads a partial file; unavailable values are `NaN`, not
  a fabricated `0`
- Write failures are contained: reported to stderr (rate-limited), never
  propagated into the control loop
- 149 new unit tests (523 total). `prometheus_client` and `promtool` are used
  as extra format oracles when installed, but neither is required to run the
  suite

## v1.0.0 — 2026-03-13

Initial open-source release.

- PI controller for RAPL PL1 thermal management
- SEN5 board sensor guard with hysteresis
- Idle mode with EPP management
- EC DDR fan_off threshold override (40→50°C) via merge-not-overwrite
- JSONL sensor logging with rotation
- 5-panel sensor-plot.sh visualization
- install.sh with atomic install/uninstall/upgrade support
- Comprehensive safety: bounds validation on EC writes, RAPL constraint
  verification, dt cap after suspend, sensor failure safe mode,
  ExecStopPost fallback, systemd hardening
- 363 unit tests (plus additional live-hardware tests on Framework hardware)
