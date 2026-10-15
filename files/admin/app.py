from flask import Flask, render_template_string, request, jsonify
import json
import os
import socket
import subprocess
import shutil
import time
import configparser
import urllib.request
import urllib.error
import ssl

app = Flask(__name__)


def run_command(args):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=3,
            check=False
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "Unavailable"


def get_uptime():
    try:
        seconds = int(float(open("/proc/uptime").read().split()[0]))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60

        parts = []
        if days:
            parts.append(f"{days}d")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)
    except Exception:
        return "Unavailable"


def get_memory():
    try:
        values = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])

        total = values["MemTotal"]
        available = values["MemAvailable"]
        used = total - available

        return (
            f"{used / 1024:.0f} MB used / "
            f"{total / 1024:.0f} MB total"
        )
    except Exception:
        return "Unavailable"


def get_storage(path):
    try:
        usage = shutil.disk_usage(path)
        used = usage.used / (1024 ** 3)
        total = usage.total / (1024 ** 3)
        free = usage.free / (1024 ** 3)
        return f"{used:.1f} GB used / {total:.1f} GB total ({free:.1f} GB free)"
    except Exception:
        return "Unavailable"


def format_storage_size(value):
    value = max(0, int(value))

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit in ("GB", "TB"):
                return f"{size:.1f} {unit}"
            if unit == "MB":
                return f"{size:.0f} {unit}"
            return f"{size:.0f} {unit}"
        size /= 1024


def get_directory_size(path):
    try:
        result = subprocess.run(
            ["du", "-sb", path],
            capture_output=True,
            text=True,
            timeout=8,
            check=False
        )

        if result.returncode != 0:
            return 0

        return int(result.stdout.split()[0])

    except Exception:
        return 0


def get_storage_content():
    # Use df so these figures match what the administrator sees at the shell.
    try:
        result = subprocess.run(
            ["df", "-B1", "--output=size,used,avail,pcent", "/"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False
        )

        line = result.stdout.strip().splitlines()[-1].split()

        total = int(line[0])
        used = int(line[1])
        free = int(line[2])
        percent = line[3]

    except Exception:
        total = used = free = 0
        percent = "Unknown"

    categories = {
        "Wikipedia / Kiwix": get_directory_size("/srv/library/kiwix"),
        "Maps": get_directory_size("/srv/library/maps"),
        "Media": get_directory_size("/srv/library/media"),
        "Notes": get_directory_size("/srv/library/notes"),
        "Games": get_directory_size("/var/www/offline-library/games"),
        "Static tools": get_directory_size("/var/www/offline-library/tools"),
    }

    categorized = sum(categories.values())
    other = max(0, used - categorized)

    rows = [
        {
            "name": name,
            "size": format_storage_size(size)
        }
        for name, size in categories.items()
    ]

    rows.append({
        "name": "Other / system",
        "size": format_storage_size(other)
    })

    return {
        "total": format_storage_size(total),
        "used": format_storage_size(used),
        "free": format_storage_size(free),
        "percent": percent,
        "rows": rows,
    }


def get_temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = int(f.read().strip()) / 1000
        return f"{temp:.1f} °C"
    except Exception:
        return "Unavailable"


def get_ssid():
    output = run_command([
        "nmcli", "-t", "-f", "ACTIVE,SSID",
        "device", "wifi", "list", "ifname", "wlan0"
    ])

    if output == "Unavailable":
        return output

    for line in output.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1] or "Unknown"

    return "Not connected"


def get_network_mode(active_profile, wifi):
    """
    Report the network controller mode.

    AUTO is authoritative when the auto controller service is running.
    Otherwise infer HOME/HOTSPOT from the active NetworkManager profile.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "library-network-auto.service"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False
        )

        if result.stdout.strip() == "active":
            return "AUTO"
    except Exception:
        pass

    home_profile = wifi.get("home", {}).get(
        "profile",
        "netplan-wlan0-Cooke-home-wifi"
    )

    hotspot_profile = wifi.get("hotspot", {}).get(
        "profile",
        "OfflineLibrary-AP"
    )

    if active_profile == hotspot_profile:
        return "HOTSPOT"

    if active_profile == home_profile:
        return "HOME"

    return "Unknown"


def get_internet_status():
    """
    Use NetworkManager's cached connectivity state.
    This does not delay page loading with a fresh Internet test.
    """
    try:
        result = subprocess.run(
            ["nmcli", "networking", "connectivity"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False
        )

        state = result.stdout.strip().lower()

        names = {
            "full": "Available",
            "limited": "Limited",
            "portal": "Captive portal",
            "none": "Offline",
            "unknown": "Unknown",
        }

        return names.get(state, "Unknown")

    except Exception:
        return "Unknown"


def get_wifi_settings():
    try:
        result = subprocess.run(
            [
                "sudo",
                "/usr/local/sbin/library-admin-network-read"
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False
        )

        if result.returncode != 0:
            raise RuntimeError("network helper failed")

        return json.loads(result.stdout)

    except Exception:
        return {
            "home": {
                "ssid": "Unavailable",
                "password": None
            },
            "hotspot": {
                "ssid": "Unavailable",
                "password": ""
            }
        }


def get_ups_data():
    result = {
        "battery": "Unavailable",
        "voltage": "Unavailable",
        "current": "Unavailable",
        "power": "Unavailable",
        "state": "Unavailable",
        "charge_state": "Not calibrated",
        "monitor": "Unavailable",
        "shutdown": "Unavailable",
        "warning_voltage": "Unavailable",
        "shutdown_voltage": "Unavailable",
        "emergency_voltage": "Unavailable",
        "data_age": "Unavailable",
    }

    # Monitor service state
    try:
        service = subprocess.run(
            ["systemctl", "is-active", "lafvin-ups.service"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False
        )
        state = service.stdout.strip()

        if state == "active":
            result["monitor"] = "Running"
        elif state:
            result["monitor"] = state.capitalize()
    except Exception:
        pass

    # Live telemetry written by lafvin-ups.service
    try:
        with open("/run/lafvin-ups/status.json", encoding="utf-8") as f:
            status = json.load(f)

        percent = status.get("battery_percent_estimate")
        voltage = status.get(
            "filtered_voltage_v",
            status.get("voltage_v")
        )
        current_ma = status.get("current_ma")
        power_w = status.get("power_w")
        ups_state = status.get("state", "unknown")
        timestamp = status.get("timestamp")

        if percent is not None:
            result["battery"] = f"{float(percent):.1f}% (estimated)"

        if voltage is not None:
            result["voltage"] = f"{float(voltage):.2f} V"

        if current_ma is not None:
            current_ma = float(current_ma)
            current_a = current_ma / 1000.0

            result["current"] = (
                f"{current_a:.3f} A "
                f"({current_ma:.0f} mA)"
            )

            if current_ma > 25:
                result["charge_state"] = "Charging"
            elif current_ma < -25:
                result["charge_state"] = "Discharging"
            else:
                result["charge_state"] = "Idle"

        if power_w is not None:
            result["power"] = f"{float(power_w):.2f} W"

        state_names = {
            "normal": "Normal",
            "startup_grace": "Startup grace",
            "warning_pending": "Low-voltage warning pending",
            "warning": "Low-voltage warning",
            "shutdown_pending": "Shutdown pending",
            "shutdown_threshold": "Shutdown threshold reached",
            "emergency_shutdown": "Emergency shutdown",
            "sensor_error": "Sensor error",
            "sensor_unavailable": "Sensor unavailable",
        }

        result["state"] = state_names.get(
            ups_state,
            str(ups_state).replace("_", " ").title()
        )

        result["shutdown"] = (
            "Enabled"
            if status.get("shutdown_enabled") is True
            else "Disabled"
        )

        if timestamp is not None:
            age = max(0, time.time() - float(timestamp))

            if age <= 10:
                result["data_age"] = f"Live ({age:.0f} s old)"
            else:
                result["data_age"] = f"Stale ({age:.0f} s old)"

    except Exception:
        pass

    # Configured shutdown thresholds
    try:
        config = configparser.ConfigParser(interpolation=None)
        if config.read("/etc/lafvin-ups.conf"):
            monitor = config["monitor"]

            result["warning_voltage"] = (
                f'{monitor.getfloat("warning_voltage"):.1f} V'
            )
            result["shutdown_voltage"] = (
                f'{monitor.getfloat("shutdown_voltage"):.1f} V'
            )
            result["emergency_voltage"] = (
                f'{monitor.getfloat("emergency_voltage"):.1f} V'
            )
    except Exception:
        pass

    return result


def check_service_url(url, host=None):
    """Application-level service readiness check."""
    try:
        headers = {
            "User-Agent": "Vagabond-Admin/1.0"
        }

        if host:
            headers["Host"] = host

        request_obj = urllib.request.Request(
            url,
            headers=headers
        )

        context = None
        if url.startswith("https://"):
            context = ssl._create_unverified_context()

        with urllib.request.urlopen(
            request_obj,
            timeout=2,
            context=context
        ) as response:
            code = response.getcode()

        if 200 <= code < 400:
            return {
                "status": "Ready",
                "css": "status-ready"
            }

        return {
            "status": "Degraded",
            "css": "status-degraded"
        }

    except urllib.error.HTTPError:
        # The service answered, but not successfully.
        return {
            "status": "Degraded",
            "css": "status-degraded"
        }

    except Exception:
        return {
            "status": "Unavailable",
            "css": "status-unavailable"
        }


def get_service_status():
    checks = [
        (
            "Portal / nginx",
            "https://127.0.0.1/",
            "library.local"
        ),
        (
            "Wikipedia / Kiwix",
            "https://127.0.0.1/wiki/",
            "library.local"
        ),
        (
            "Media / VuIO",
            "http://127.0.0.1:8090/",
            None
        ),
        (
            "Maps",
            "https://127.0.0.1/maps/",
            "library.local"
        ),
        (
            "Notes / FlatNotes",
            "https://127.0.0.1/notes/",
            "library.local"
        ),
    ]

    services = []

    for name, url, host in checks:
        result = check_service_url(url, host)
        result["name"] = name
        services.append(result)

    return services


def run_diagnostics(kind):
    try:
        result = subprocess.run(
            [
                "sudo",
                "/usr/local/sbin/library-admin-diagnostics",
                kind
            ],
            capture_output=True,
            text=True,
            timeout=7,
            check=False
        )

        return json.loads(result.stdout)

    except Exception:
        return {
            "ok": False,
            "error": "Unable to retrieve diagnostics."
        }


def get_diagnostics_summary():
    result = run_diagnostics("summary")

    if result.get("ok"):
        return {
            "warnings": result.get("warnings_display", "Unknown")
        }

    return {
        "warnings": "Unavailable"
    }


@app.get("/api/diagnostics/<kind>")
def diagnostics_api(kind):
    if kind not in {"warnings", "ups", "network"}:
        return jsonify({
            "ok": False,
            "error": "Unsupported diagnostic request."
        }), 400

    result = run_diagnostics(kind)

    if not result.get("ok"):
        return jsonify(result), 500

    return jsonify(result)


@app.post("/api/wifi")
def save_wifi():
    if not request.is_json:
        return jsonify({
            "ok": False,
            "error": "JSON request required."
        }), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({
            "ok": False,
            "error": "Invalid request."
        }), 400

    payload["dry_run"] = False

    try:
        result = subprocess.run(
            [
                "sudo",
                "/usr/local/sbin/library-admin-network-write"
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=40,
            check=False
        )

        try:
            response = json.loads(result.stdout)
        except Exception:
            return jsonify({
                "ok": False,
                "error": "Network helper returned an invalid response."
            }), 500

        if not response.get("ok"):
            return jsonify(response), 400

        return jsonify(response)

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.get("/")
def index():
    hostname = socket.gethostname()

    active_profile = run_command([
        "nmcli", "-g", "GENERAL.CONNECTION",
        "device", "show", "wlan0"
    ])

    ip_address = run_command([
        "nmcli", "-g", "IP4.ADDRESS",
        "device", "show", "wlan0"
    ])

    if ip_address != "Unavailable":
        ip_address = ip_address.splitlines()[0] if ip_address else "None"

    wifi = get_wifi_settings()
    network_mode = get_network_mode(active_profile, wifi)
    internet_status = get_internet_status()
    ups = get_ups_data()
    storage_content = get_storage_content()
    services = get_service_status()

    # Add UPS/battery as an appliance-readiness item.
    try:
        voltage = float(ups["voltage"].split()[0])
        warning = float(ups["warning_voltage"].split()[0])
        shutdown = float(ups["shutdown_voltage"].split()[0])

        if voltage <= shutdown:
            ups_css = "status-unavailable"
        elif voltage <= warning:
            ups_css = "status-degraded"
        else:
            ups_css = "status-ready"

        ups_summary = (
            f'{ups["charge_state"]} — '
            f'{ups["battery"].replace(" (estimated)", "")} — '
            f'{ups["voltage"]}'
        )

        if ups["monitor"] != "Running":
            ups_css = "status-unavailable"
            ups_summary = f'UPS monitor {ups["monitor"]}'

        services.append({
            "name": "Power / UPS",
            "status": ups_summary,
            "css": ups_css
        })

    except Exception:
        services.append({
            "name": "Power / UPS",
            "status": "Unavailable",
            "css": "status-unavailable"
        })

    diagnostics = get_diagnostics_summary()

    if any(
        service.get("css") == "status-unavailable"
        for service in services
    ):
        overall_health = "Attention"
        overall_health_css = "status-unavailable"

    elif any(
        service.get("css") == "status-degraded"
        for service in services
    ):
        overall_health = "Degraded"
        overall_health_css = "status-degraded"

    else:
        overall_health = "Normal"
        overall_health_css = "status-ready"

    data = {
        "hostname": hostname,
        "uptime": get_uptime(),
        "memory": get_memory(),
        "storage": get_storage("/"),
        "temperature": get_temperature(),
        "profile": active_profile,
        "network_mode": network_mode,
        "internet_status": internet_status,
        "ssid": get_ssid(),
        "ip": ip_address,
        "home_ssid": wifi["home"]["ssid"],
        "hotspot_ssid": wifi["hotspot"]["ssid"],
        "hotspot_password": wifi["hotspot"]["password"],
        "ups": ups,
        "storage_content": storage_content,
        "services": services,
        "diagnostics": diagnostics,
        "overall_health": overall_health,
        "overall_health_css": overall_health_css,
    }

    return render_template_string(TEMPLATE, **data)


TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">

<title>Vagabond Administration</title>

<style>
body {
    font-family: sans-serif;
    max-width: 900px;
    margin: 40px auto;
    padding: 0 20px;
}

.subtitle {
    margin-top: -12px;
}

.admin-cards {
    display: grid;
    grid-template-columns: 1fr;
    gap: 20px;
    margin: 20px auto;
}

.admin-card {
    padding: 18px 20px;
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(128,128,128,0.35);
    border-radius: 10px;
}

.admin-card h2 {
    margin: 0 0 14px 0;
}

.info-row {
    display: grid;
    grid-template-columns: 150px 1fr;
    gap: 12px;
    padding: 7px 0;
    border-bottom: 1px solid rgba(128,128,128,0.20);
}

.info-row:last-child {
    border-bottom: none;
}

.label {
    font-weight: bold;
}

.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: bold;
}

.status-dot {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    display: inline-block;
}

.status-ready .status-dot {
    background: #2e7d32;
}

.status-degraded .status-dot {
    background: #d89b00;
}

.status-unavailable .status-dot {
    background: #c62828;
}

.home-link {
    display: inline-block;
    margin-top: 10px;
}

.service-card,
.diagnostics-card,
.wifi-card {
    grid-column: 1 / -1;
}

.wifi-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 24px;
}

.field {
    margin: 12px 0;
}

.field label {
    display: block;
    font-weight: bold;
    margin-bottom: 6px;
}

.field input[type="text"],
.field input[type="password"] {
    width: 100%;
    box-sizing: border-box;
    padding: 10px;
    font: inherit;
}

.help {
    font-size: 0.9em;
    opacity: 0.75;
    margin-top: 5px;
}

button {
    font: inherit;
    padding: 8px 12px;
    margin-top: 12px;
    cursor: pointer;
}

.edit-buttons {
    display: flex;
    gap: 10px;
    margin-top: 12px;
}

.diagnostic-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 16px;
}

.diagnostic-buttons button {
    margin-top: 0;
}

.log-output {
    margin-top: 16px;
    padding: 12px;
    border: 1px solid rgba(128,128,128,0.35);
    border-radius: 6px;
    background: rgba(128,128,128,0.08);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 380px;
    overflow-y: auto;
    font-family: monospace;
    font-size: 0.85em;
}

@media (min-width: 700px) {
    .wifi-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}

@media (min-width: 700px) {
    .admin-cards {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}
</style>
</head>

<body>

<h1>Vagabond Administration</h1>
<p class="subtitle">Offline Library Management</p>

<div class="admin-cards">

<section class="admin-card service-card">
    <h2>Service Status</h2>

    {% for service in services %}
    <div class="info-row">
        <div class="label">{{ service.name }}</div>
        <div class="status-badge {{ service.css }}">
            <span class="status-dot"></span>
            {{ service.status }}
        </div>
    </div>
    {% endfor %}
</section>

<section class="admin-card">
    <h2>System</h2>

    <div class="info-row">
        <div class="label">Hostname</div>
        <div>{{ hostname }}</div>
    </div>

    <div class="info-row">
        <div class="label">Uptime</div>
        <div>{{ uptime }}</div>
    </div>

    <div class="info-row">
        <div class="label">Temperature</div>
        <div>{{ temperature }}</div>
    </div>

    <div class="info-row">
        <div class="label">Memory</div>
        <div>{{ memory }}</div>
    </div>

    <div class="info-row">
        <div class="label">Storage</div>
        <div>{{ storage }}</div>
    </div>
</section>

<section class="admin-card">
    <h2>Network</h2>

    <div class="info-row">
        <div class="label">Interface</div>
        <div>wlan0</div>
    </div>

    <div class="info-row">
        <div class="label">Mode</div>
        <div><strong>{{ network_mode }}</strong></div>
    </div>

    <div class="info-row">
        <div class="label">Internet</div>
        <div>{{ internet_status }}</div>
    </div>

    <div class="info-row">
        <div class="label">Profile</div>
        <div>{{ profile }}</div>
    </div>

    <div class="info-row">
        <div class="label">SSID</div>
        <div>{{ ssid }}</div>
    </div>

    <div class="info-row">
        <div class="label">IPv4</div>
        <div>{{ ip }}</div>
    </div>
</section>

<section class="admin-card wifi-card">
    <h2>Wi-Fi Settings</h2>

    <div class="wifi-grid">

        <!-- HOME -->
        <div>
            <h3>Home Wi-Fi</h3>

            <div id="home-view">
                <div class="info-row">
                    <div class="label">SSID</div>
                    <div>{{ home_ssid }}</div>
                </div>

                <div class="info-row">
                    <div class="label">Password</div>
                    <div>Stored, not displayed</div>
                </div>

                <button type="button"
                        onclick="editWifi('home')">
                    Change settings
                </button>
            </div>

            <div id="home-edit" hidden>
                <div class="field">
                    <label for="home-ssid-edit">SSID</label>
                    <input
                        id="home-ssid-edit"
                        type="text"
                        value="{{ home_ssid }}"
                        maxlength="32">
                </div>

                <div class="field">
                    <label for="home-password-edit">
                        New password
                    </label>
                    <input
                        id="home-password-edit"
                        type="password"
                        autocomplete="new-password">
                </div>

                <div class="field">
                    <label for="home-password-confirm">
                        Confirm password
                    </label>
                    <input
                        id="home-password-confirm"
                        type="password"
                        autocomplete="new-password">
                </div>

                <label style="font-weight: normal;">
                    <input
                        id="home-show-password"
                        type="checkbox"
                        onchange="toggleEditPasswords('home')">
                    Show passwords
                </label>

                <div class="help">
                    The existing HOME password cannot be displayed.
                    Enter the password twice before saving.
                </div>

                <div class="edit-buttons">
                    <button type="button"
                            onclick="cancelWifi('home')">
                        Cancel
                    </button>

                    <button type="button"
                            onclick="saveWifiPreview('home')">
                        Save changes
                    </button>
                </div>
            </div>
        </div>


        <!-- HOTSPOT -->
        <div>
            <h3>OfflineLibrary Hotspot</h3>

            <div id="hotspot-view">
                <div class="info-row">
                    <div class="label">SSID</div>
                    <div>{{ hotspot_ssid }}</div>
                </div>

                <div class="info-row">
                    <div class="label">Password</div>
                    <div>
                        <span id="hotspot-password-display">
                            ••••••••
                        </span>
                    </div>
                </div>

                <label style="font-weight: normal;">
                    <input
                        id="show-hotspot-view-password"
                        type="checkbox"
                        onchange="toggleHotspotViewPassword()">
                    Show password
                </label>

                <br><br>

                <button type="button"
                        onclick="editWifi('hotspot')">
                    Change settings
                </button>
            </div>

            <div id="hotspot-edit" hidden>
                <div class="field">
                    <label for="hotspot-ssid-edit">SSID</label>
                    <input
                        id="hotspot-ssid-edit"
                        type="text"
                        value="{{ hotspot_ssid }}"
                        maxlength="32">
                </div>

                <div class="field">
                    <label for="hotspot-password-edit">
                        Password
                    </label>
                    <input
                        id="hotspot-password-edit"
                        type="password"
                        value="{{ hotspot_password }}"
                        autocomplete="new-password">
                </div>

                <div class="field">
                    <label for="hotspot-password-confirm">
                        Confirm password
                    </label>
                    <input
                        id="hotspot-password-confirm"
                        type="password"
                        value="{{ hotspot_password }}"
                        autocomplete="new-password">
                </div>

                <label style="font-weight: normal;">
                    <input
                        id="hotspot-show-password"
                        type="checkbox"
                        onchange="toggleEditPasswords('hotspot')">
                    Show passwords
                </label>

                <div class="edit-buttons">
                    <button type="button"
                            onclick="cancelWifi('hotspot')">
                        Cancel
                    </button>

                    <button type="button"
                            onclick="saveWifiPreview('hotspot')">
                        Save changes
                    </button>
                </div>
            </div>
        </div>

    </div>
</section>

<section class="admin-card ups-card">
    <h2>Power / UPS</h2>

    <div class="info-row">
        <div class="label">Charge state</div>
        <div>{{ ups.charge_state }}</div>
    </div>

    <div class="info-row">
        <div class="label">Battery</div>
        <div>{{ ups.battery }}</div>
    </div>

    <div class="info-row">
        <div class="label">Voltage</div>
        <div>{{ ups.voltage }}</div>
    </div>

    <div class="info-row">
        <div class="label">Current</div>
        <div>{{ ups.current }}</div>
    </div>

    <div class="info-row">
        <div class="label">Power</div>
        <div>{{ ups.power }}</div>
    </div>

    <div class="info-row">
        <div class="label">Battery status</div>
        <div>{{ ups.state }}</div>
    </div>

    <div class="info-row">
        <div class="label">UPS monitor</div>
        <div>{{ ups.monitor }}</div>
    </div>

    <div class="info-row">
        <div class="label">Telemetry</div>
        <div>{{ ups.data_age }}</div>
    </div>

    <div class="info-row">
        <div class="label">Auto shutdown</div>
        <div>{{ ups.shutdown }}</div>
    </div>

    <div class="info-row">
        <div class="label">Warning threshold</div>
        <div>{{ ups.warning_voltage }}</div>
    </div>

    <div class="info-row">
        <div class="label">Shutdown threshold</div>
        <div>{{ ups.shutdown_voltage }}</div>
    </div>

    <div class="info-row">
        <div class="label">Emergency threshold</div>
        <div>{{ ups.emergency_voltage }}</div>
    </div>

    <div class="help">
        Battery percentage is an estimate based on pack voltage.
    </div>
</section>

<section class="admin-card storage-card">
    <h2>Storage / Content</h2>

    <div class="info-row">
        <div class="label">Total storage</div>
        <div>{{ storage_content.total }}</div>
    </div>

    <div class="info-row">
        <div class="label">Used</div>
        <div>{{ storage_content.used }} ({{ storage_content.percent }})</div>
    </div>

    <div class="info-row">
        <div class="label">Free</div>
        <div>{{ storage_content.free }}</div>
    </div>

    <hr style="margin: 16px 0; border: 0; border-top: 1px solid rgba(128,128,128,0.3);">

    {% for item in storage_content.rows %}
    <div class="info-row">
        <div class="label">{{ item.name }}</div>
        <div>{{ item.size }}</div>
    </div>
    {% endfor %}
</section>

<section class="admin-card diagnostics-card">
    <h2>Diagnostics / Logs</h2>

    <div class="info-row">
        <div class="label">Overall health</div>
        <div class="status-badge {{ overall_health_css }}">
            <span class="status-dot"></span>
            {{ overall_health }}
        </div>
    </div>

    <div class="info-row">
        <div class="label">Warnings/errors</div>
        <div>{{ diagnostics.warnings }} since boot</div>
    </div>

    <div class="diagnostic-buttons">
        <button type="button"
                onclick="loadDiagnostic('warnings')">
            Recent warnings
        </button>

        <button type="button"
                onclick="loadDiagnostic('ups')">
            UPS log
        </button>

        <button type="button"
                onclick="loadDiagnostic('network')">
            Network log
        </button>
    </div>

    <pre id="diagnostic-output"
         class="log-output"
         hidden></pre>
</section>



</div>

<a class="home-link" href="/">← Back to Vagabond</a>

<script>
async function loadDiagnostic(kind) {
    const output = document.getElementById("diagnostic-output");

    output.hidden = false;
    output.textContent = "Loading...";

    try {
        const response = await fetch(
            "/admin/api/diagnostics/" + kind
        );

        const result = await response.json();

        if (!response.ok || !result.ok) {
            output.textContent =
                result.error || "Unable to retrieve diagnostics.";
            return;
        }

        output.textContent = result.text;

    } catch (error) {
        output.textContent =
            "Unable to retrieve diagnostics.";
    }
}


const wifiOriginal = {
    home: {
        ssid: {{ home_ssid|tojson }}
    },
    hotspot: {
        ssid: {{ hotspot_ssid|tojson }},
        password: {{ hotspot_password|tojson }}
    }
};

function editWifi(target) {
    document.getElementById(target + "-view").hidden = true;
    document.getElementById(target + "-edit").hidden = false;
}

function cancelWifi(target) {
    document.getElementById(target + "-ssid-edit").value =
        wifiOriginal[target].ssid;

    if (target === "home") {
        document.getElementById("home-password-edit").value = "";
        document.getElementById("home-password-confirm").value = "";
    } else {
        document.getElementById("hotspot-password-edit").value =
            wifiOriginal.hotspot.password;
        document.getElementById("hotspot-password-confirm").value =
            wifiOriginal.hotspot.password;
    }

    document.getElementById(target + "-show-password").checked = false;
    toggleEditPasswords(target);

    document.getElementById(target + "-edit").hidden = true;
    document.getElementById(target + "-view").hidden = false;
}

function toggleEditPasswords(target) {
    const checked =
        document.getElementById(target + "-show-password").checked;

    document.getElementById(target + "-password-edit").type =
        checked ? "text" : "password";

    document.getElementById(target + "-password-confirm").type =
        checked ? "text" : "password";
}

function toggleHotspotViewPassword() {
    const checked =
        document.getElementById("show-hotspot-view-password").checked;

    document.getElementById("hotspot-password-display").textContent =
        checked ? wifiOriginal.hotspot.password : "••••••••";
}

async function saveWifiPreview(target) {
    const ssid =
        document.getElementById(target + "-ssid-edit").value.trim();

    const password =
        document.getElementById(target + "-password-edit").value;

    const confirmPassword =
        document.getElementById(target + "-password-confirm").value;

    if (password !== confirmPassword) {
        alert("The two passwords do not match.");
        return;
    }

    if (password.length < 8 || password.length > 63) {
        alert("Wi-Fi password must be 8 to 63 characters.");
        return;
    }

    const warning =
        "Changing Wi-Fi settings may disconnect this device and " +
        "your current browser session.\\n\\n" +
        "If the SSID or password changes, you may need to reconnect " +
        "using the new settings.\\n\\n" +
        "Apply these changes?";

    if (!confirm(warning)) {
        return;
    }

    try {
        const response = await fetch("/admin/api/wifi", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                target: target,
                ssid: ssid,
                password: password,
                confirm: confirmPassword
            })
        });

        const result = await response.json();

        if (!response.ok || !result.ok) {
            alert(
                "Settings were not changed.\\n\\n" +
                (result.error || "Unknown error.")
            );
            return;
        }

        if (result.reconnect_scheduled) {
            alert(
                "Settings saved.\\n\\n" +
                "The active Wi-Fi connection will restart shortly. " +
                "Reconnect using the new settings if necessary."
            );
        } else {
            alert("Settings saved successfully.");
            window.location.reload();
        }

    } catch (error) {
        alert(
            "Unable to complete the request. The network may have changed."
        );
    }
}
</script>

</body>
</html>
"""
