#!/usr/bin/env python3

import json
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from lafvin_epd import epd2in13_V4


# ============================================================
# Configuration
# ============================================================

WIDTH = 250
HEIGHT = 122

UPS_STATUS = Path("/run/lafvin-ups/status.json")

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WIFI_INTERFACE = "wlan0"

# Poll urgent items this often.
# This does NOT refresh the screen unless something changes.
URGENT_POLL_SECONDS = 1

# Normal status refresh / slower health checks.
NORMAL_REFRESH_SECONDS = 300

# After five fast updates, next update is full.
FAST_REFRESH_LIMIT = 5

# Confirmed experimentally on this UPS:
# positive = charging
# negative = discharging
# External power detection.
# On battery we measured about -120 mA.
# On wall power, including when full, current is near 0 or positive.
EXTERNAL_POWER_THRESHOLD_MA = -50

STORAGE_WARNING_GB = 5.0
CPU_WARNING_C = 80.0

STOP_EVENT = threading.Event()

# Special 100% display hysteresis.
# It latches ON at >=99% while external power is present
# and stays ON until <=97% or wall power is removed.
FULL_LATCHED = False


# ============================================================
# Basic helpers
# ============================================================

def command(args):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def http_ok(url):
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "offline-library-eink/1.0"},
        )

        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= response.status < 400

    except Exception:
        return False


# ============================================================
# UPS / battery
# ============================================================

def read_ups():
    try:
        data = json.loads(UPS_STATUS.read_text())

        voltage = data.get("filtered_voltage_v")

        if voltage is None:
            voltage = data.get("voltage_v")

        return {
            "available": True,
            "voltage": voltage,
            "percent": data.get("battery_percent_estimate"),
            "current_ma": data.get("current_ma"),
            "state": data.get("state", "unknown"),
            "math_overflow": data.get("math_overflow", False),
        }

    except Exception:
        return {
            "available": False,
            "voltage": None,
            "percent": None,
            "current_ma": None,
            "state": "sensor_unavailable",
            "math_overflow": False,
        }


def ups_low_warning_active(ups):
    state = str(ups.get("state", "")).lower()

    return state in {
        "warning_pending",
        "warning",
        "low",
        "shutdown_pending",
        "shutdown",
        "shutdown_threshold",
        "emergency_shutdown",
        "emergency",
    }


def external_power_present(ups):
    current = ups.get("current_ma")

    if current is None:
        return False

    try:
        return float(current) > EXTERNAL_POWER_THRESHOLD_MA
    except Exception:
        return False


def battery_full_display(ups):
    global FULL_LATCHED

    # Never show 100% while running from battery.
    if not external_power_present(ups):
        FULL_LATCHED = False
        return False

    percent = ups.get("percent")

    if percent is None:
        return FULL_LATCHED

    try:
        percent = float(percent)
    except Exception:
        return FULL_LATCHED

    if percent >= 99.0:
        FULL_LATCHED = True

    elif percent <= 97.0:
        FULL_LATCHED = False

    return FULL_LATCHED


def battery_bars(ups):
    # Empty battery icon means the UPS has reached
    # its low-voltage warning state.
    if ups_low_warning_active(ups):
        return 0

    percent = ups.get("percent")

    if percent is not None:
        try:
            percent = float(percent)

            if percent >= 67:
                return 3
            if percent >= 34:
                return 2
            if percent >= 10:
                return 1

            return 0

        except Exception:
            pass

    voltage = ups.get("voltage")

    if voltage is None:
        return 0

    if voltage >= 11.8:
        return 3
    if voltage >= 11.3:
        return 2
    if voltage > 10.8:
        return 1

    return 0


def power_flow_state(ups):
    if not ups.get("available"):
        return "UPS_ERROR"

    if external_power_present(ups):
        return "EXTERNAL"

    return "DISCHARGING"


# ============================================================
# Network
# ============================================================

def get_network():
    info = command([
        "iw", "dev", WIFI_INTERFACE, "info"
    ])

    link = command([
        "iw", "dev", WIFI_INTERFACE, "link"
    ])

    interface_type = ""

    match = re.search(
        r"^\s*type\s+(\S+)",
        info,
        re.MULTILINE,
    )

    if match:
        interface_type = match.group(1).upper()

    ssid = ""

    if interface_type == "AP":
        match = re.search(
            r"^\s*ssid\s+(.+)$",
            info,
            re.MULTILINE,
        )

        if match:
            ssid = match.group(1).strip()

    else:
        match = re.search(
            r"^\s*SSID:\s*(.+)$",
            link,
            re.MULTILINE,
        )

        if match:
            ssid = match.group(1).strip()

    # NetworkManager fallback
    if not ssid:
        connection = command([
            "nmcli",
            "-g",
            "GENERAL.CONNECTION",
            "device",
            "show",
            WIFI_INTERFACE,
        ])

        if connection and connection != "--":
            candidate = command([
                "nmcli",
                "-g",
                "802-11-wireless.ssid",
                "connection",
                "show",
                connection,
            ])

            if candidate:
                ssid = candidate.splitlines()[0].strip()

    ip_output = command([
        "ip",
        "-4",
        "-o",
        "addr",
        "show",
        "dev",
        WIFI_INTERFACE,
        "scope",
        "global",
    ])

    ip_address = ""

    match = re.search(
        r"\binet\s+([0-9.]+)/",
        ip_output,
    )

    if match:
        ip_address = match.group(1)

    if interface_type == "AP":
        mode = "HOTSPOT"

    elif ssid and ip_address:
        mode = "HOME"

    else:
        mode = "OFFLINE"

    return {
        "mode": mode,
        "ssid": ssid or "Not connected",
        "ip": ip_address or "No IP",
    }


# ============================================================
# System status
# ============================================================

def get_uptime():
    try:
        seconds = int(
            float(
                Path("/proc/uptime")
                .read_text()
                .split()[0]
            )
        )

        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60

        if days:
            return f"{days}d {hours:02d}h"

        return f"{hours:02d}h {minutes:02d}m"

    except Exception:
        return "?"


def get_cpu_temperature():
    try:
        raw = Path(
            "/sys/class/thermal/thermal_zone0/temp"
        ).read_text().strip()

        return int(raw) / 1000.0

    except Exception:
        return None


def get_storage_free_gb():
    path = (
        "/srv/library"
        if Path("/srv/library").exists()
        else "/"
    )

    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)

    except Exception:
        return 9999.0


# ============================================================
# Service readiness
# ============================================================

def get_failed_services():
    failed = []

    if not http_ok("http://127.0.0.1/"):
        return ["NGINX"]

    # Use the same user-facing nginx path that actually works.
    if not http_ok("http://127.0.0.1/wiki/"):
        failed.append("KIWIX")

    if Path("/etc/vuio").exists():
        if not http_ok("http://127.0.0.1:8090/"):
            failed.append("MEDIA")

    if Path("/opt/flatnotes").exists():
        if not http_ok("http://127.0.0.1/notes/"):
            failed.append("NOTES")

    return failed


def gather_status(ups=None, network=None):
    if ups is None:
        ups = read_ups()

    if network is None:
        network = get_network()

    return {
        "ups": ups,
        "network": network,
        "temperature": get_cpu_temperature(),
        "free_gb": get_storage_free_gb(),
        "uptime": get_uptime(),
        "failed_services": get_failed_services(),
    }


# ============================================================
# Warning logic
# ============================================================

def get_warning(status):
    ups = status["ups"]
    network = status["network"]

    if not ups["available"]:
        return "UPS SENSOR ERROR"

    if ups.get("math_overflow"):
        return "UPS DATA ERROR"

    state = str(
        ups.get("state", "")
    ).lower()

    if state in {
        "emergency_shutdown",
        "emergency",
    }:
        return "EMERGENCY LOW BATTERY"

    if state in {
        "shutdown_pending",
        "shutdown",
        "shutdown_threshold",
    }:
        return "LOW BATTERY - SHUTDOWN"

    if state in {
        "warning_pending",
        "warning",
        "low",
    }:
        return "LOW BATTERY"

    temperature = status["temperature"]

    if (
        temperature is not None
        and temperature >= CPU_WARNING_C
    ):
        return f"CPU HOT {temperature:.0f}C"

    free_gb = status["free_gb"]

    if free_gb < STORAGE_WARNING_GB:
        return f"STORAGE LOW {free_gb:.1f}GB"

    if network["mode"] == "OFFLINE":
        return "NO NETWORK"

    failed = status["failed_services"]

    if failed:
        if len(failed) == 1:
            return f"{failed[0]} NOT READY"

        names = "/".join(failed[:2])

        if len(failed) > 2:
            names += "+"

        return f"{names} NOT READY"

    return None


# ============================================================
# Drawing
# ============================================================

def draw_battery(draw, x, y, bars, show_full=False):
    draw.rounded_rectangle(
        (x, y, x + 32, y + 16),
        radius=2,
        outline=0,
        width=2,
    )

    # Battery terminal
    draw.rectangle(
        (x + 33, y + 5, x + 36, y + 11),
        fill=0,
    )

    if show_full:
        font = ImageFont.truetype(
            FONT_BOLD,
            8,
        )

        text = "100%"

        box = draw.textbbox(
            (0, 0),
            text,
            font=font,
        )

        tw = box[2] - box[0]
        th = box[3] - box[1]

        tx = x + (32 - tw) // 2
        ty = y + (16 - th) // 2 - 1

        draw.text(
            (tx, ty),
            text,
            font=font,
            fill=0,
        )

    else:
        for i in range(bars):
            bx = x + 4 + (i * 9)

            draw.rectangle(
                (bx, y + 4, bx + 6, y + 12),
                fill=0,
            )


def draw_plug(draw, x, y):
    # Two prongs
    draw.line(
        (x + 4, y, x + 4, y + 5),
        fill=0,
        width=2,
    )

    draw.line(
        (x + 10, y, x + 10, y + 5),
        fill=0,
        width=2,
    )

    # Plug body
    draw.rounded_rectangle(
        (x + 2, y + 5, x + 12, y + 11),
        radius=1,
        outline=0,
        width=2,
    )

    # Cord
    draw.line(
        (x + 7, y + 11, x + 7, y + 16),
        fill=0,
        width=2,
    )


def draw_house(draw, x, y):
    draw.polygon(
        [
            (x + 8, y),
            (x, y + 7),
            (x + 3, y + 7),
            (x + 3, y + 16),
            (x + 13, y + 16),
            (x + 13, y + 7),
            (x + 16, y + 7),
        ],
        fill=0,
    )

    draw.rectangle(
        (x + 7, y + 10, x + 10, y + 16),
        fill=255,
    )


def draw_wifi(draw, x, y):
    draw.arc(
        (x, y, x + 18, y + 18),
        210,
        330,
        fill=0,
        width=2,
    )

    draw.arc(
        (x + 4, y + 4, x + 14, y + 14),
        210,
        330,
        fill=0,
        width=2,
    )

    draw.ellipse(
        (x + 8, y + 13, x + 10, y + 15),
        fill=0,
    )


def draw_warning(draw, x, y):
    draw.polygon(
        [
            (x + 8, y),
            (x, y + 16),
            (x + 16, y + 16),
        ],
        outline=0,
    )

    draw.line(
        (x + 8, y + 5, x + 8, y + 10),
        fill=0,
        width=2,
    )

    draw.ellipse(
        (x + 7, y + 12, x + 9, y + 14),
        fill=0,
    )


def fit_text(draw, text, font, max_width):
    if (
        draw.textbbox(
            (0, 0),
            text,
            font=font,
        )[2]
        <= max_width
    ):
        return text

    shortened = text

    while shortened:
        candidate = shortened + "..."

        if (
            draw.textbbox(
                (0, 0),
                candidate,
                font=font,
            )[2]
            <= max_width
        ):
            return candidate

        shortened = shortened[:-1]

    return ""


# ============================================================
# Render screen
# ============================================================

def render_screen(status):
    ups = status["ups"]
    network = status["network"]

    warning = get_warning(status)

    image = Image.new(
        "1",
        (WIDTH, HEIGHT),
        255,
    )

    draw = ImageDraw.Draw(image)

    font_title = ImageFont.truetype(
        FONT_BOLD,
        15,
    )

    font_main = ImageFont.truetype(
        FONT_REG,
        13,
    )

    font_main_bold = ImageFont.truetype(
        FONT_BOLD,
        13,
    )

    font_small = ImageFont.truetype(
        FONT_REG,
        11,
    )

    # Header
    draw.text(
        (2, 4),
        "OFFLINE LIBRARY",
        font=font_title,
        fill=0,
    )

    plugged_in = external_power_present(ups)
    show_full = battery_full_display(ups)

    if plugged_in:
        draw_plug(
            draw,
            191,
            5,
        )

    draw_battery(
        draw,
        210,
        5,
        battery_bars(ups),
        show_full=show_full,
    )

    # Network
    if network["mode"] == "HOME":
        draw_house(draw, 3, 29)
        prefix = "HOME"

    elif network["mode"] == "HOTSPOT":
        draw_wifi(draw, 2, 28)
        prefix = "HOTSPOT"

    else:
        draw_warning(draw, 3, 28)
        prefix = "OFFLINE"

    network_text = fit_text(
        draw,
        f"{prefix}: {network['ssid']}",
        font_main_bold,
        220,
    )

    draw.text(
        (25, 28),
        network_text,
        font=font_main_bold,
        fill=0,
    )

    # IP
    ip_text = fit_text(
        draw,
        f"IP {network['ip']}",
        font_main,
        150,
    )

    draw.text(
        (3, 50),
        ip_text,
        font=font_main,
        fill=0,
    )

    # Battery voltage
    voltage = ups.get("voltage")

    if voltage is None:
        voltage_text = "--.--V"
    else:
        voltage_text = f"{float(voltage):.2f}V"

    voltage_width = draw.textbbox(
        (0, 0),
        voltage_text,
        font=font_main_bold,
    )[2]

    draw.text(
        (247 - voltage_width, 50),
        voltage_text,
        font=font_main_bold,
        fill=0,
    )

    # Uptime
    draw.text(
        (3, 70),
        f"UP {status['uptime']}",
        font=font_small,
        fill=0,
    )

    # Status line
    draw.line(
        (2, 91, 247, 91),
        fill=0,
        width=1,
    )

    if warning:
        draw_warning(
            draw,
            4,
            100,
        )

        warning_text = fit_text(
            draw,
            warning,
            font_main_bold,
            220,
        )

        draw.text(
            (25, 100),
            warning_text,
            font=font_main_bold,
            fill=0,
        )

    else:
        draw.text(
            (4, 100),
            "SERVICES OK",
            font=font_main_bold,
            fill=0,
        )

    return image


# ============================================================
# E-paper refresh
# ============================================================

def refresh_image(image, full):
    epd = epd2in13_V4.EPD()

    try:
        buffer = epd.getbuffer(image)

        if full:
            epd.init()
            epd.display(buffer)
            print("eink: full refresh", flush=True)

        else:
            epd.init_fast()
            epd.display_fast(buffer)
            print("eink: fast refresh", flush=True)

    finally:
        try:
            epd.sleep()
        except Exception:
            pass


def blank_display():
    print("eink: blanking screen before shutdown", flush=True)

    image = Image.new(
        "1",
        (WIDTH, HEIGHT),
        255,
    )

    # Always use a full refresh when blanking.
    refresh_image(
        image,
        full=True,
    )


# ============================================================
# Immediate-change monitoring
# ============================================================

def urgent_signature(ups, network):
    return (
        power_flow_state(ups),

        network.get("mode"),
        network.get("ssid"),
        network.get("ip"),

        bool(ups.get("available")),

        str(
            ups.get("state", "unknown")
        ).lower(),

        bool(
            ups.get("math_overflow", False)
        ),
    )


def handle_stop(signum, frame):
    STOP_EVENT.set()


# ============================================================
# Main service
# ============================================================

def main():
    # Manual command if ever needed:
    #
    # sudo python3 status_display.py --blank
    #
    if "--blank" in sys.argv:
        blank_display()
        return

    signal.signal(
        signal.SIGTERM,
        handle_stop,
    )

    signal.signal(
        signal.SIGINT,
        handle_stop,
    )

    # Initial boot/start display is always a full refresh.
    status = gather_status()

    refresh_image(
        render_screen(status),
        full=True,
    )

    last_urgent = urgent_signature(
        status["ups"],
        status["network"],
    )

    fast_refresh_count = 0

    next_normal_refresh = (
        time.monotonic()
        + NORMAL_REFRESH_SECONDS
    )

    while not STOP_EVENT.wait(
        URGENT_POLL_SECONDS
    ):
        ups = read_ups()
        network = get_network()

        current_urgent = urgent_signature(
            ups,
            network,
        )

        urgent_changed = (
            current_urgent != last_urgent
        )

        normal_due = (
            time.monotonic()
            >= next_normal_refresh
        )

        if not urgent_changed and not normal_due:
            continue

        # A real screen update is needed.
        # Gather slower items now as well so we do not need
        # another refresh immediately afterward.
        status = gather_status(
            ups=ups,
            network=network,
        )

        use_full_refresh = (
            fast_refresh_count
            >= FAST_REFRESH_LIMIT
        )

        try:
            refresh_image(
                render_screen(status),
                full=use_full_refresh,
            )

        except Exception as exc:
            print(
                f"eink: refresh failed: {exc}",
                flush=True,
            )
            continue

        if use_full_refresh:
            fast_refresh_count = 0
        else:
            fast_refresh_count += 1

        last_urgent = urgent_signature(
            status["ups"],
            status["network"],
        )

        # Because we just gathered the full status, restart
        # the normal five-minute interval from this update.
        next_normal_refresh = (
            time.monotonic()
            + NORMAL_REFRESH_SECONDS
        )

    # systemd sends SIGTERM for normal stop, reboot,
    # halt, poweroff, and UPS-triggered system shutdown.
    try:
        blank_display()

    except Exception as exc:
        print(
            f"eink: shutdown blank failed: {exc}",
            flush=True,
        )


if __name__ == "__main__":
    main()
