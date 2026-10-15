#!/usr/bin/env bash
# Offline Emergency Library - Phase 20 Networking
# Install and stage HOME / AUTO / HOTSPOT without changing active Wi-Fi.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo 'ERROR: Run with sudo.' >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
WIFI_INTERFACE="wlan0"
AP_PROFILE="OfflineLibrary-AP"
BACKUP_DIR="/usr/local/lib/offline-library/network"
CONFIG_DIR="/etc/offline-library"

apt-get install -y network-manager iw gpiod python3-libgpiod

install -d -o root -g root -m 755 \
    "$CONFIG_DIR" \
    /etc/NetworkManager/dnsmasq-shared.d \
    /etc/systemd/system/library-network-auto.service.d \
    /etc/systemd/system/library-network-switch.service.d
install -d -o root -g root -m 700 "$BACKUP_DIR"

if ! systemctl is-active --quiet NetworkManager.service; then
    echo 'ERROR: NetworkManager must be active.' >&2
    exit 1
fi

# Detect the UUID of the connection currently bound to the Wi-Fi interface.
# Do not guess SSID, name, password, or UUID.
HOME_PROFILE_UUID="$(nmcli -t -f UUID,DEVICE connection show --active | \
    awk -F: -v iface="$WIFI_INTERFACE" '$2 == iface {print $1; exit}')"
if [[ -z "$HOME_PROFILE_UUID" ]]; then
    echo "ERROR: No active HOME Wi-Fi on $WIFI_INTERFACE. Connect through Raspberry Pi Imager first." >&2
    exit 1
fi

HOME_MODE="$(nmcli -g 802-11-wireless.mode connection show uuid "$HOME_PROFILE_UUID")"
if [[ "$HOME_MODE" != infrastructure ]]; then
    echo "ERROR: Active Wi-Fi on $WIFI_INTERFACE is not an infrastructure connection." >&2
    exit 1
fi
HOME_PROFILE="$(nmcli -g connection.id connection show uuid "$HOME_PROFILE_UUID")"
echo "Preserving HOME profile: $HOME_PROFILE (UUID: $HOME_PROFILE_UUID)"

# Initial snapshots; never replace the first-known-good copies on a rerun.
if [[ ! -e "$BACKUP_DIR/.initial-backup-complete" ]]; then
    if [[ -d /etc/netplan ]]; then
        cp -a /etc/netplan "$BACKUP_DIR/netplan-original"
    fi
    if [[ -d /etc/NetworkManager/system-connections ]]; then
        cp -a /etc/NetworkManager/system-connections "$BACKUP_DIR/system-connections-original"
    fi
    chmod -R go-rwx "$BACKUP_DIR"
    touch "$BACKUP_DIR/.initial-backup-complete"
fi

# Only persistent keyfiles can be repaired by our zero-byte checker.
# Generated keyfiles under /run are not written back into /etc.
HOME_PROFILE_FILE=""
shopt -s nullglob
for file in /etc/NetworkManager/system-connections/*.nmconnection; do
    if [[ -f "$file" ]] && grep -Eq \
        "^[[:space:]]*uuid[[:space:]]*=[[:space:]]*${HOME_PROFILE_UUID}[[:space:]]*$" "$file"; then
        HOME_PROFILE_FILE="$file"
        break
    fi
done
shopt -u nullglob

HOME_PROFILE_BASELINE=""
if [[ -n "$HOME_PROFILE_FILE" ]]; then
    HOME_PROFILE_BASELINE="$BACKUP_DIR/home-${HOME_PROFILE_UUID}.nmconnection"
    if [[ ! -e "$HOME_PROFILE_BASELINE" ]]; then
        install -o root -g root -m 600 \
            "$HOME_PROFILE_FILE" "$HOME_PROFILE_BASELINE"
    fi
    echo 'HOME persistent keyfile backed up for zero-byte recovery.'
else
    echo 'HOME has no persistent NM keyfile in /etc; Netplan/source backup saved if present.'
    echo 'Zero-byte NM keyfile recovery will not run for this connection.'
fi

# Create hotspot if absent. Preserve existing AP password on rerun.
if nmcli -g connection.uuid connection show "$AP_PROFILE" >/dev/null 2>&1; then
    AP_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$AP_PROFILE")"
    EXISTING_AP_MODE="$(nmcli -g 802-11-wireless.mode connection show uuid "$AP_PROFILE_UUID")"
    EXISTING_AP_SSID="$(nmcli -g 802-11-wireless.ssid connection show uuid "$AP_PROFILE_UUID")"
    if [[ "$EXISTING_AP_MODE" != ap || "$EXISTING_AP_SSID" != OfflineLibrary ]]; then
        echo "ERROR: $AP_PROFILE exists but is not the expected OfflineLibrary hotspot." >&2
        exit 1
    fi
    echo 'Reusing existing OfflineLibrary hotspot and its password.'
else
    read -r -s -p 'OfflineLibrary hotspot password (8-63 characters): ' HOTSPOT_PASSWORD
    printf '\n'
    if (( ${#HOTSPOT_PASSWORD} < 8 || ${#HOTSPOT_PASSWORD} > 63 )); then
        echo 'ERROR: Hotspot password must be 8-63 characters.' >&2
        exit 1
    fi
    nmcli connection add type wifi ifname "$WIFI_INTERFACE" \
        con-name "$AP_PROFILE" ssid OfflineLibrary
    AP_PROFILE_UUID="$(nmcli -g connection.uuid connection show "$AP_PROFILE")"
    NEW_AP_PASSWORD="$HOTSPOT_PASSWORD"
    unset HOTSPOT_PASSWORD
fi

ap_settings=(
    802-11-wireless.mode ap
    802-11-wireless.band bg
    802-11-wireless-security.key-mgmt wpa-psk
    802-11-wireless-security.proto rsn
    802-11-wireless-security.pairwise ccmp
    802-11-wireless-security.group ccmp
    802-11-wireless-security.pmf 1
    ipv4.method shared
    ipv4.addresses 10.42.0.1/24
    ipv6.method disabled
    connection.autoconnect no
)
if [[ -n "${NEW_AP_PASSWORD:-}" ]]; then
    ap_settings+=(802-11-wireless-security.psk "$NEW_AP_PASSWORD")
fi
nmcli connection modify uuid "$AP_PROFILE_UUID" "${ap_settings[@]}"
unset NEW_AP_PASSWORD

# Root-owned config is sourced only by root-run controller/helpers.
config_tmp="$(mktemp "$CONFIG_DIR/.network.conf.XXXXXX")"
trap 'rm -f -- "${config_tmp:-}"' EXIT
chmod 600 "$config_tmp"
{
    printf 'WIFI_INTERFACE=%q\n' "$WIFI_INTERFACE"
    printf 'HOME_PROFILE=%q\n' "$HOME_PROFILE"
    printf 'HOME_PROFILE_UUID=%q\n' "$HOME_PROFILE_UUID"
    printf 'AP_PROFILE=%q\n' "$AP_PROFILE"
    printf 'AP_PROFILE_UUID=%q\n' "$AP_PROFILE_UUID"
    printf 'HOME_PROFILE_FILE=%q\n' "$HOME_PROFILE_FILE"
    printf 'HOME_PROFILE_BASELINE=%q\n' "$HOME_PROFILE_BASELINE"
} > "$config_tmp"
chown root:root "$config_tmp"
mv -f -- "$config_tmp" "$CONFIG_DIR/network.conf"
trap - EXIT

# Install generic controller scripts; normalize Windows CRLF endings.
for command_file in \
    library-home library-hotspot library-auto \
    library-network-auto library-network-switch library-network-profile-check
do
    install -o root -g root -m 755 \
        "$REPO_ROOT/files/network/bin/$command_file" \
        "/usr/local/sbin/$command_file"
    sed -i 's/\r$//' "/usr/local/sbin/$command_file"
done

# DNS captive portal remains owned by networking.
install -o root -g root -m 644 \
    "$REPO_ROOT/files/network/offline-library-captive-dnsmasq-pi.conf" \
    /etc/NetworkManager/dnsmasq-shared.d/offline-library-captive.conf

# Install systemd units and recovery hooks.
for service in library-network-auto library-network-switch; do
    install -o root -g root -m 644 \
        "$REPO_ROOT/files/systemd/${service}.service" \
        "/etc/systemd/system/${service}.service"
    sed -i 's/\r$//' "/etc/systemd/system/${service}.service"
    install -o root -g root -m 644 \
        "$REPO_ROOT/files/systemd/${service}.service.d/10-profile-recovery.conf" \
        "/etc/systemd/system/${service}.service.d/10-profile-recovery.conf"
    sed -i 's/\r$//' \
        "/etc/systemd/system/${service}.service.d/10-profile-recovery.conf"
done

# Verify syntax without switching Wi-Fi modes.
for command_file in library-home library-hotspot library-auto \
    library-network-auto library-network-profile-check; do
    bash -n "/usr/local/sbin/$command_file"
done
python3 -m py_compile /usr/local/sbin/library-network-switch
python3 -c 'import gpiod; from gpiod.line import Direction, Bias, Edge, Value' 
systemctl daemon-reload

# Conservative staging: don't take over wlan0 during SSH install or next boot.
systemctl disable library-network-switch.service >/dev/null 2>&1 || true
systemctl disable library-network-auto.service >/dev/null 2>&1 || true

echo 'PASS: HOME profile preserved; hotspot and controllers installed.'
echo 'The physical switch service is DISABLED pending a controlled test.'
echo 'When ready: sudo systemctl enable library-network-switch.service'
