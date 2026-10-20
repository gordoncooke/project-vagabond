#!/usr/bin/env python3
"""LAFVIN UPS Module 3S monitor and safe-shutdown service."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Sequence

from ina219 import INA219, INA219Reading

VERSION = "1.0.0"
DEFAULT_CONFIG = "/etc/lafvin-ups.conf"
LOG = logging.getLogger("lafvin-ups")


@dataclass(frozen=True)
class Settings:
    i2c_bus: int
    i2c_address: int
    sensor_retry_seconds: float
    sample_interval_seconds: float
    filter_window: int
    startup_grace_seconds: float
    min_valid_voltage: float
    max_valid_voltage: float
    warning_voltage: float
    warning_duration_seconds: float
    recovery_voltage: float
    shutdown_voltage: float
    shutdown_duration_seconds: float
    emergency_voltage: float
    emergency_duration_seconds: float
    battery_empty_voltage: float
    battery_full_voltage: float
    log_every_seconds: float
    shutdown_enabled: bool
    shutdown_command: str
    shutdown_retry_seconds: float
    wall_enabled: bool
    warning_repeat_seconds: float
    status_file: str
    log_level: str


def _get_int_auto(section: configparser.SectionProxy, key: str) -> int:
    raw = section.get(key, fallback="").strip()
    try:
        return int(raw, 0)
    except ValueError as exc:
        raise ValueError(f"Invalid integer for {section.name}.{key}: {raw!r}") from exc


def load_settings(path: str) -> Settings:
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise FileNotFoundError(f"Configuration file not found: {path}")

    hardware = parser["hardware"]
    monitor = parser["monitor"]
    actions = parser["actions"]
    logging_section = parser["logging"]

    settings = Settings(
        i2c_bus=hardware.getint("i2c_bus"),
        i2c_address=_get_int_auto(hardware, "i2c_address"),
        sensor_retry_seconds=hardware.getfloat("sensor_retry_seconds"),
        sample_interval_seconds=monitor.getfloat("sample_interval_seconds"),
        filter_window=monitor.getint("filter_window"),
        startup_grace_seconds=monitor.getfloat("startup_grace_seconds"),
        min_valid_voltage=monitor.getfloat("min_valid_voltage"),
        max_valid_voltage=monitor.getfloat("max_valid_voltage"),
        warning_voltage=monitor.getfloat("warning_voltage"),
        warning_duration_seconds=monitor.getfloat("warning_duration_seconds"),
        recovery_voltage=monitor.getfloat("recovery_voltage"),
        shutdown_voltage=monitor.getfloat("shutdown_voltage"),
        shutdown_duration_seconds=monitor.getfloat("shutdown_duration_seconds"),
        emergency_voltage=monitor.getfloat("emergency_voltage"),
        emergency_duration_seconds=monitor.getfloat("emergency_duration_seconds"),
        battery_empty_voltage=monitor.getfloat("battery_empty_voltage"),
        battery_full_voltage=monitor.getfloat("battery_full_voltage"),
        log_every_seconds=monitor.getfloat("log_every_seconds"),
        shutdown_enabled=actions.getboolean("shutdown_enabled"),
        shutdown_command=actions.get("shutdown_command").strip(),
        shutdown_retry_seconds=actions.getfloat("shutdown_retry_seconds"),
        wall_enabled=actions.getboolean("wall_enabled"),
        warning_repeat_seconds=actions.getfloat("warning_repeat_seconds"),
        status_file=actions.get("status_file").strip(),
        log_level=logging_section.get("level").strip().upper(),
    )
    validate_settings(settings)
    return settings


def validate_settings(s: Settings) -> None:
    if s.i2c_bus < 0:
        raise ValueError("hardware.i2c_bus must be >= 0")
    if not 0x03 <= s.i2c_address <= 0x77:
        raise ValueError("hardware.i2c_address must be a valid 7-bit I2C address")
    if s.sample_interval_seconds < 0.2:
        raise ValueError("monitor.sample_interval_seconds must be >= 0.2")
    if s.sensor_retry_seconds < 1:
        raise ValueError("hardware.sensor_retry_seconds must be >= 1")
    if s.filter_window < 1:
        raise ValueError("monitor.filter_window must be >= 1")
    if s.min_valid_voltage >= s.max_valid_voltage:
        raise ValueError("min_valid_voltage must be below max_valid_voltage")
    if not (
        s.emergency_voltage
        < s.shutdown_voltage
        < s.warning_voltage
        <= s.recovery_voltage
    ):
        raise ValueError(
            "Voltage thresholds must satisfy: emergency < shutdown < warning <= recovery"
        )
    if s.battery_empty_voltage >= s.battery_full_voltage:
        raise ValueError("battery_empty_voltage must be below battery_full_voltage")
    for name in (
        "startup_grace_seconds",
        "warning_duration_seconds",
        "shutdown_duration_seconds",
        "emergency_duration_seconds",
        "log_every_seconds",
        "shutdown_retry_seconds",
        "warning_repeat_seconds",
    ):
        if getattr(s, name) < 0:
            raise ValueError(f"{name} must be >= 0")
    if not s.shutdown_command:
        raise ValueError("actions.shutdown_command must not be empty")
    if s.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"Unsupported logging level: {s.log_level}")


def estimated_battery_percent(voltage: float, empty: float, full: float) -> float:
    """Linear display estimate only; never used for the shutdown decision."""
    value = (voltage - empty) / (full - empty) * 100.0
    return max(0.0, min(100.0, value))


def duration_since(start: Optional[float], now: float) -> float:
    return 0.0 if start is None else max(0.0, now - start)


class UPSMonitor:
    def __init__(self, settings: Settings, dry_run: bool = False) -> None:
        self.settings = settings
        self.dry_run = dry_run
        self.samples: Deque[float] = deque(maxlen=settings.filter_window)
        self.stop_event = threading.Event()
        self.sensor: Optional[INA219] = None
        self.started_monotonic = time.monotonic()
        self.last_log_monotonic = 0.0
        self.last_warning_monotonic: Optional[float] = None
        self.last_shutdown_attempt_monotonic: Optional[float] = None
        self.warning_since: Optional[float] = None
        self.shutdown_since: Optional[float] = None
        self.emergency_since: Optional[float] = None
        self.warning_active = False
        self.last_state = "starting"

    def request_stop(self, signum: int, _frame: object) -> None:
        LOG.info("Received signal %s; stopping monitor", signum)
        self.stop_event.set()

    def close_sensor(self) -> None:
        if self.sensor is not None:
            try:
                self.sensor.close()
            except Exception:
                LOG.debug("Failed to close INA219 cleanly", exc_info=True)
            self.sensor = None

    def wait(self, seconds: float) -> bool:
        return self.stop_event.wait(max(0.0, seconds))

    def connect_sensor(self) -> bool:
        try:
            self.sensor = INA219(
                i2c_bus=self.settings.i2c_bus,
                address=self.settings.i2c_address,
            )
            reading = self.sensor.read()
            self._validate_reading(reading)
            LOG.info(
                "INA219 connected on /dev/i2c-%d at 0x%02X; voltage %.3f V",
                self.settings.i2c_bus,
                self.settings.i2c_address,
                reading.supply_voltage_v,
            )
            return True
        except Exception as exc:
            self.close_sensor()
            LOG.error(
                "Cannot access INA219 on /dev/i2c-%d at 0x%02X: %s; retrying in %.1f s",
                self.settings.i2c_bus,
                self.settings.i2c_address,
                exc,
                self.settings.sensor_retry_seconds,
            )
            self._write_status(
                {
                    "state": "sensor_unavailable",
                    "error": str(exc),
                    "timestamp": time.time(),
                }
            )
            return False

    def _validate_reading(self, reading: INA219Reading) -> None:
        voltage = reading.supply_voltage_v
        if reading.math_overflow:
            LOG.warning("INA219 math overflow flag is set")
        if not self.settings.min_valid_voltage <= voltage <= self.settings.max_valid_voltage:
            raise ValueError(
                f"Voltage {voltage:.3f} V outside valid range "
                f"{self.settings.min_valid_voltage:.3f}-{self.settings.max_valid_voltage:.3f} V"
            )

    def _reset_low_voltage_timers(self) -> None:
        self.warning_since = None
        self.shutdown_since = None
        self.emergency_since = None

    @staticmethod
    def _update_timer(
        start: Optional[float], condition: bool, now: float
    ) -> Optional[float]:
        if not condition:
            return None
        return now if start is None else start

    def _notify_wall(self, message: str) -> None:
        if not self.settings.wall_enabled:
            return
        wall = shutil.which("wall")
        if wall is None:
            LOG.warning("wall_enabled is true, but the 'wall' command was not found")
            return
        try:
            subprocess.run(
                [wall, message],
                check=False,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Unable to send wall warning: %s", exc)

    def _warning_due(self, now: float) -> bool:
        if self.last_warning_monotonic is None:
            return True
        return (
            now - self.last_warning_monotonic
            >= self.settings.warning_repeat_seconds
        )

    def _send_low_battery_warning(self, voltage: float, now: float) -> None:
        if not self._warning_due(now):
            return
        message = (
            f"LAFVIN UPS battery is low: {voltage:.2f} V. "
            "Save your work; automatic shutdown may occur."
        )
        LOG.warning(message)
        self._notify_wall(message)
        self.last_warning_monotonic = now

    def _shutdown_attempt_due(self, now: float) -> bool:
        if self.last_shutdown_attempt_monotonic is None:
            return True
        return (
            now - self.last_shutdown_attempt_monotonic
            >= self.settings.shutdown_retry_seconds
        )

    def _request_shutdown(self, voltage: float, reason: str, now: float) -> None:
        if not self._shutdown_attempt_due(now):
            return
        self.last_shutdown_attempt_monotonic = now

        message = (
            f"LAFVIN UPS requesting safe shutdown: {reason}; "
            f"filtered battery voltage {voltage:.2f} V."
        )

        if self.dry_run or not self.settings.shutdown_enabled:
            LOG.critical(
                "%s Shutdown is disabled%s.",
                message,
                " by --dry-run" if self.dry_run else " in the configuration",
            )
            self._notify_wall(message + " Shutdown is currently disabled.")
            return

        command: Sequence[str] = shlex.split(self.settings.shutdown_command)
        if not command:
            LOG.error("Shutdown command parsed to an empty command")
            return

        LOG.critical(message)
        self._notify_wall(message)
        try:
            result = subprocess.run(
                list(command),
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.exception("Failed to execute shutdown command: %s", exc)
            return

        if result.returncode != 0:
            LOG.error(
                "Shutdown command failed with exit code %d: %s",
                result.returncode,
                result.stdout.strip(),
            )
        else:
            LOG.critical("Shutdown command accepted by systemd")

    def _evaluate_voltage(self, voltage: float, now: float) -> Dict[str, Any]:
        uptime = now - self.started_monotonic
        grace_remaining = max(0.0, self.settings.startup_grace_seconds - uptime)

        if grace_remaining > 0:
            self._reset_low_voltage_timers()
            self.warning_active = False
            state = "startup_grace"
        else:
            self.warning_since = self._update_timer(
                self.warning_since,
                voltage <= self.settings.warning_voltage,
                now,
            )
            self.shutdown_since = self._update_timer(
                self.shutdown_since,
                voltage <= self.settings.shutdown_voltage,
                now,
            )
            self.emergency_since = self._update_timer(
                self.emergency_since,
                voltage <= self.settings.emergency_voltage,
                now,
            )

            warning_elapsed = duration_since(self.warning_since, now)
            shutdown_elapsed = duration_since(self.shutdown_since, now)
            emergency_elapsed = duration_since(self.emergency_since, now)

            if voltage >= self.settings.recovery_voltage:
                self.warning_active = False
                self.last_warning_monotonic = None
                state = "normal"
            elif (
                self.emergency_since is not None
                and emergency_elapsed >= self.settings.emergency_duration_seconds
            ):
                self.warning_active = True
                state = "emergency_shutdown"
                self._send_low_battery_warning(voltage, now)
                self._request_shutdown(voltage, "emergency low voltage", now)
            elif (
                self.shutdown_since is not None
                and shutdown_elapsed >= self.settings.shutdown_duration_seconds
            ):
                self.warning_active = True
                state = "shutdown_threshold"
                self._send_low_battery_warning(voltage, now)
                self._request_shutdown(voltage, "sustained low voltage", now)
            elif (
                self.warning_since is not None
                and warning_elapsed >= self.settings.warning_duration_seconds
            ):
                self.warning_active = True
                state = "warning"
                self._send_low_battery_warning(voltage, now)
            elif self.warning_active:
                # Hysteresis: remain in warning until recovery_voltage is met.
                state = "warning"
                self._send_low_battery_warning(voltage, now)
            elif voltage <= self.settings.shutdown_voltage:
                state = "shutdown_pending"
            elif voltage <= self.settings.warning_voltage:
                state = "warning_pending"
            else:
                state = "normal"

        return {
            "state": state,
            "startup_grace_remaining_seconds": round(grace_remaining, 1),
            "warning_low_for_seconds": round(duration_since(self.warning_since, now), 1),
            "shutdown_low_for_seconds": round(duration_since(self.shutdown_since, now), 1),
            "emergency_low_for_seconds": round(duration_since(self.emergency_since, now), 1),
        }

    def _write_status(self, data: Dict[str, Any]) -> None:
        if not self.settings.status_file:
            return
        target = Path(self.settings.status_file)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(data)
            payload.setdefault("hostname", socket.gethostname())
            payload.setdefault("version", VERSION)
            payload.setdefault("shutdown_enabled", self.settings.shutdown_enabled and not self.dry_run)

            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", dir=str(target.parent), text=True
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(temporary_name, target)
                os.chmod(target, 0o644)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            LOG.debug("Unable to write status file %s: %s", target, exc)

    def _log_measurement(
        self,
        reading: INA219Reading,
        filtered_voltage: float,
        percent: float,
        state: str,
        now: float,
    ) -> None:
        state_changed = state != self.last_state
        periodic_due = (
            self.last_log_monotonic == 0
            or now - self.last_log_monotonic >= self.settings.log_every_seconds
        )
        if state_changed or periodic_due:
            LOG.info(
                "state=%s voltage=%.3fV filtered=%.3fV battery_est=%.1f%% "
                "current=%.1fmA power=%.3fW",
                state,
                reading.supply_voltage_v,
                filtered_voltage,
                percent,
                reading.current_ma,
                reading.power_w,
            )
            self.last_log_monotonic = now
            self.last_state = state

    def run(self) -> int:
        LOG.info(
            "Starting LAFVIN UPS monitor v%s; shutdown_enabled=%s%s",
            VERSION,
            self.settings.shutdown_enabled,
            " (forced dry-run)" if self.dry_run else "",
        )

        while not self.stop_event.is_set():
            if self.sensor is None and not self.connect_sensor():
                if self.wait(self.settings.sensor_retry_seconds):
                    break
                continue

            cycle_started = time.monotonic()
            try:
                assert self.sensor is not None
                reading = self.sensor.read()
                self._validate_reading(reading)
            except Exception as exc:
                LOG.error("INA219 read failed: %s", exc)
                self.samples.clear()
                self._reset_low_voltage_timers()
                self.warning_active = False
                self.close_sensor()
                self._write_status(
                    {
                        "state": "sensor_error",
                        "error": str(exc),
                        "timestamp": time.time(),
                    }
                )
                if self.wait(self.settings.sensor_retry_seconds):
                    break
                continue

            self.samples.append(reading.supply_voltage_v)
            filtered_voltage = float(statistics.median(self.samples))
            percent = estimated_battery_percent(
                filtered_voltage,
                self.settings.battery_empty_voltage,
                self.settings.battery_full_voltage,
            )
            now = time.monotonic()
            evaluation = self._evaluate_voltage(filtered_voltage, now)

            status: Dict[str, Any] = {
                "timestamp": time.time(),
                "state": evaluation["state"],
                "voltage_v": round(reading.supply_voltage_v, 4),
                "filtered_voltage_v": round(filtered_voltage, 4),
                "bus_voltage_v": round(reading.bus_voltage_v, 4),
                "shunt_voltage_mv": round(reading.shunt_voltage_mv, 3),
                "current_ma": round(reading.current_ma, 3),
                "power_w": round(reading.power_w, 4),
                "battery_percent_estimate": round(percent, 1),
                "math_overflow": reading.math_overflow,
                **evaluation,
            }
            self._write_status(status)
            self._log_measurement(
                reading,
                filtered_voltage,
                percent,
                str(evaluation["state"]),
                now,
            )

            elapsed = time.monotonic() - cycle_started
            if self.wait(max(0.0, self.settings.sample_interval_seconds - elapsed)):
                break

        self.close_sensor()
        LOG.info("LAFVIN UPS monitor stopped")
        return 0


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def read_once(settings: Settings) -> int:
    configure_logging(settings.log_level)
    try:
        with INA219(settings.i2c_bus, settings.i2c_address) as sensor:
            reading = sensor.read()
    except Exception as exc:
        LOG.error("Unable to read INA219: %s", exc)
        return 1

    if not settings.min_valid_voltage <= reading.supply_voltage_v <= settings.max_valid_voltage:
        LOG.error("Voltage %.3f V is outside configured valid range", reading.supply_voltage_v)
        return 2

    payload = reading.to_dict()
    payload["battery_percent_estimate"] = estimated_battery_percent(
        reading.supply_voltage_v,
        settings.battery_empty_voltage,
        settings.battery_full_voltage,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="INI configuration path")
    parser.add_argument("--once", action="store_true", help="Read and print one measurement")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all monitoring logic but never execute the shutdown command",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, KeyError, configparser.Error) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.once:
        return read_once(settings)

    configure_logging(settings.log_level)
    if settings.shutdown_enabled and not args.dry_run and os.geteuid() != 0:
        LOG.warning("Automatic shutdown is enabled, but the monitor is not running as root")

    monitor = UPSMonitor(settings, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, monitor.request_stop)
    signal.signal(signal.SIGINT, monitor.request_stop)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
