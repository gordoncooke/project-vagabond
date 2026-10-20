#!/usr/bin/env python3
"""Minimal INA219 driver for the LAFVIN UPS Module 3S.

The voltage monitor uses the INA219 bus voltage and shunt voltage registers.
Current and power values are informational only and are not used to decide
whether the Raspberry Pi should shut down.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

try:
    from smbus2 import SMBus  # type: ignore
except ImportError:
    try:
        from smbus import SMBus  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on target OS
        raise RuntimeError(
            "Neither 'smbus' nor 'smbus2' is installed. "
            "Install python3-smbus on Raspberry Pi OS."
        ) from exc


_REG_CONFIG = 0x00
_REG_SHUNT_VOLTAGE = 0x01
_REG_BUS_VOLTAGE = 0x02
_REG_POWER = 0x03
_REG_CURRENT = 0x04
_REG_CALIBRATION = 0x05

# Configuration retained from the repository's original PowerWatch.py:
# 16 V bus range, +/-80 mV shunt range, 12-bit ADC with 32-sample averaging,
# continuous shunt and bus conversion.
_CONFIG_16V_5A = 0x0EEF
_DEFAULT_CALIBRATION = 26868
_DEFAULT_CURRENT_LSB_MA = 0.1524
_DEFAULT_POWER_LSB_W = 0.003048


@dataclass(frozen=True)
class INA219Reading:
    bus_voltage_v: float
    shunt_voltage_mv: float
    supply_voltage_v: float
    current_ma: float
    power_w: float
    conversion_ready: bool
    math_overflow: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class INA219:
    """Small INA219 driver using Linux SMBus/I2C."""

    def __init__(
        self,
        i2c_bus: int = 1,
        address: int = 0x41,
        calibration: int = _DEFAULT_CALIBRATION,
        current_lsb_ma: float = _DEFAULT_CURRENT_LSB_MA,
        power_lsb_w: float = _DEFAULT_POWER_LSB_W,
    ) -> None:
        if not 0x03 <= address <= 0x77:
            raise ValueError(f"Invalid 7-bit I2C address: 0x{address:02X}")

        self.i2c_bus = i2c_bus
        self.address = address
        self.calibration = calibration
        self.current_lsb_ma = current_lsb_ma
        self.power_lsb_w = power_lsb_w
        self._bus = SMBus(i2c_bus)
        self.configure()

    @staticmethod
    def _to_signed_16(value: int) -> int:
        return value - 65536 if value & 0x8000 else value

    def _read_u16_be(self, register: int) -> int:
        data = self._bus.read_i2c_block_data(self.address, register, 2)
        if len(data) != 2:
            raise OSError(f"INA219 returned {len(data)} bytes, expected 2")
        return (data[0] << 8) | data[1]

    def _write_u16_be(self, register: int, value: int) -> None:
        payload = [(value >> 8) & 0xFF, value & 0xFF]
        self._bus.write_i2c_block_data(self.address, register, payload)

    def configure(self) -> None:
        self._write_u16_be(_REG_CALIBRATION, self.calibration)
        self._write_u16_be(_REG_CONFIG, _CONFIG_16V_5A)

    def read(self) -> INA219Reading:
        # Some INA219 variants can lose calibration after a reset. Rewriting it
        # before current/power reads is cheap and matches the original example.
        self._write_u16_be(_REG_CALIBRATION, self.calibration)

        raw_bus = self._read_u16_be(_REG_BUS_VOLTAGE)
        raw_shunt = self._read_u16_be(_REG_SHUNT_VOLTAGE)
        raw_current = self._read_u16_be(_REG_CURRENT)
        raw_power = self._read_u16_be(_REG_POWER)

        bus_voltage_v = (raw_bus >> 3) * 0.004
        shunt_voltage_mv = self._to_signed_16(raw_shunt) * 0.01

        # V+ = V- + Vshunt. The original repository also describes this as
        # PSU voltage = bus voltage + shunt voltage.
        supply_voltage_v = bus_voltage_v + (shunt_voltage_mv / 1000.0)

        return INA219Reading(
            bus_voltage_v=bus_voltage_v,
            shunt_voltage_mv=shunt_voltage_mv,
            supply_voltage_v=supply_voltage_v,
            current_ma=self._to_signed_16(raw_current) * self.current_lsb_ma,
            power_w=raw_power * self.power_lsb_w,
            conversion_ready=bool(raw_bus & 0x0002),
            math_overflow=bool(raw_bus & 0x0001),
        )

    def close(self) -> None:
        close = getattr(self._bus, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "INA219":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
