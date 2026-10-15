#!/usr/bin/env bash

# Persistent setup recorded in notes-on-pi-emergency-config-1 through -6.
# Run from any directory with: sudo bash /path/to/installer/From_Notes_ver4.sh

set -euo pipefail

# Paths
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
WEBROOT=/var/www/offline-library
LIBRARY_ROOT=/srv/library

# Require root before making system changes
if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: Run this script with sudo." >&2
    exit 1
fi

## 00 Preflight

echo "Checking system compatibility..."

# Verify operating system
if [[ ! -r /etc/os-release ]]; then
    echo "ERROR: Cannot identify the operating system." >&2
    exit 1
fi

source /etc/os-release

if [[ "${ID:-}" != "raspbian" && "${ID:-}" != "debian" ]]; then
    echo "ERROR: This installer requires Raspberry Pi OS / Debian." >&2
    exit 1
fi

# Verify architecture
ARCH="$(dpkg --print-architecture)"

if [[ "$ARCH" != "arm64" ]]; then
    echo "ERROR: This installer currently requires 64-bit Raspberry Pi OS (arm64)." >&2
    echo "Detected architecture: $ARCH" >&2
    exit 1
fi

# Verify Raspberry Pi hardware
if [[ ! -r /proc/device-tree/model ]]; then
    echo "ERROR: Raspberry Pi hardware not detected." >&2
    exit 1
fi

PI_MODEL="$(tr -d '\0' < /proc/device-tree/model)"
echo "Hardware: $PI_MODEL"

if [[ "$PI_MODEL" != *"Raspberry Pi"* ]]; then
    echo "ERROR: Unsupported hardware: $PI_MODEL" >&2
    exit 1
fi

# Identify the user who invoked sudo
if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
    echo "ERROR: Run this installer using sudo from your normal user account." >&2
    exit 1
fi

PI_USER="$SUDO_USER"

if ! id "$PI_USER" >/dev/null 2>&1; then
    echo "ERROR: User '$PI_USER' does not exist." >&2
    exit 1
fi

echo "Installation user: $PI_USER"

# Verify Internet access
if ! curl -fsSI --connect-timeout 10 https://deb.debian.org/ >/dev/null; then
    echo "ERROR: Internet connection unavailable." >&2
    exit 1
fi

# Common installation dependencies
apt-get update

apt-get install -y \
    curl git tar 7zip sudo jq \
    python3 python3-numpy \
    network-manager iw openssl 

# Create disk-backed temporary workspace
WORK_DIR="$(mktemp -d /var/tmp/offline-library.XXXXXX)"
trap 'rm -rf -- "$WORK_DIR"' EXIT

# Create common library root
install -d -o root -g root -m 755 "$LIBRARY_ROOT"

# Systemwide journalling
mkdir -p  /etc/systemd/journald.conf.d
cp "$REPO_ROOT/files/systemd/journald-persistent.conf" \
    /etc/systemd/journald.conf.d/persistent.conf
systemctl restart systemd-journald
journalctl --flush

echo "Preflight complete."

## 10 Hardware
apt-get install -y gpiod python3-gpiozero python3-libgpiod \
    python3-pil python3-spidev python3-numpy fonts-dejavu-core 

# Notes 4: install the UPS monitor, safe shutdown service, and persistent journal.
git clone https://github.com/WiringPi/WiringPi "$WORK_DIR/WiringPi"
(cd "$WORK_DIR/WiringPi" && ./build)
git clone https://github.com/lafvintech/UPS-Module-3S.git "$WORK_DIR/UPS-Module-3S" #TODO pin a known good version to avoid future breakage
chmod +x "$WORK_DIR/UPS-Module-3S/install.sh" "$WORK_DIR/UPS-Module-3S/uninstall.sh"
(cd "$WORK_DIR/UPS-Module-3S" && ./install.sh)

# Register and enable UPS monitoring service
systemctl daemon-reload
systemctl enable lafvin-ups.service

# Start UPS monitoring if the hardware is installed and ready
systemctl start lafvin-ups.service

if ! systemctl is-active --quiet lafvin-ups.service; then
    echo "ERROR: UPS monitoring service failed to start." >&2
    journalctl -u lafvin-ups.service -n 30 --no-pager
    exit 1
fi

echo "UPS hardware complete."

# Notes 5: final e-ink files and SPI0 CE0 overlay; the earlier timer was removed.
grep -Fqx 'dtoverlay=spi0-1cs,no_miso' /boot/firmware/config.txt || \
    printf '\n# E-ink display: SPI0 CE0, GPIO9 used for reset\ndtoverlay=spi0-1cs,no_miso\n' \
        >> /boot/firmware/config.txt
git clone https://github.com/lafvintech/LAFVIN-2.13inch-ePaper-HAT.git "$WORK_DIR/eink-driver"
mkdir -p /opt/offline-library/eink
cp -a "$WORK_DIR/eink-driver/RaspberryPi/python/lib/lafvin_epd" \
    /opt/offline-library/eink/
if grep -q 'RST_PIN  = 17' /opt/offline-library/eink/lafvin_epd/epdconfig.py; then
    sed -i '0,/RST_PIN  = 17/s//RST_PIN  = 9/' \
        /opt/offline-library/eink/lafvin_epd/epdconfig.py
elif ! grep -q 'RST_PIN  = 9' /opt/offline-library/eink/lafvin_epd/epdconfig.py; then
    echo "ERROR: Unexpected E-Ink reset-pin configuration." >&2
    exit 1
fi
cp "$REPO_ROOT/files/eink/status_display.py" /opt/offline-library/eink/status_display.py
chmod 755 /opt/offline-library/eink/status_display.py
cp "$REPO_ROOT/files/systemd/offline-eink.service" \
    /etc/systemd/system/offline-eink.service
systemctl daemon-reload
systemctl enable offline-eink.service
echo "E-ink hardware complete."

echo "Hardware setup complete."


## 20 Network
bash "$REPO_ROOT/phase20-network.sh"

## 30 Nginx
apt-get install -y nginx openssl
mkdir -p \
    /etc/nginx/sites-available \
    /etc/nginx/conf.d \
    /etc/offline-library/tls \
    "$WEBROOT" \
    "$WEBROOT/certs" \
    "$WEBROOT/setup"

#TODO: Need a better way to handle certificates. Can we make it generate its own? Or how to handle reruns if this exits? Will above thinsg fail when rerun? (ie networking setup)
if [[ ! -r "$REPO_ROOT/OfflineLibraryCA/OfflineLibraryRootCA.cer" || \
      ! -r "$REPO_ROOT/OfflineLibraryCA/library.local.pfx" ]]; then
    echo "The OfflineLibraryCA certificate and PFX must be present in the repo checkout." >&2
    exit 1
fi

# captive portal nginx redirect config
cp "$REPO_ROOT/files/nginx/offline-library-captive.conf" \
    /etc/nginx/conf.d/offline-library-captive.conf

# Notes 4: install the Windows-created CA and server certificate from the repo.
cp "$REPO_ROOT/OfflineLibraryCA/OfflineLibraryRootCA.cer" \
    /etc/offline-library/tls/OfflineLibraryRootCA.cer
openssl pkcs12 -in "$REPO_ROOT/OfflineLibraryCA/library.local.pfx" \
    -clcerts -nokeys -out /etc/offline-library/tls/library.local.crt  #TODO: it will ask for password- need a way to help user create the crt and pfx
openssl pkcs12 -in "$REPO_ROOT/OfflineLibraryCA/library.local.pfx" \
    -nocerts -nodes -out /etc/offline-library/tls/library.local.key
chown root:root /etc/offline-library/tls/*
chmod 644 /etc/offline-library/tls/*.crt /etc/offline-library/tls/*.cer
chmod 600 /etc/offline-library/tls/library.local.key
mkdir -p "$WEBROOT/certs" "$WEBROOT/setup"
cp /etc/offline-library/tls/OfflineLibraryRootCA.cer "$WEBROOT/certs/"
cp "$REPO_ROOT/files/www/setup/index.html" "$WEBROOT/setup/index.html"

# Notes 5: final saved homepage and final Nginx routes.
cp "$REPO_ROOT/files/www/index.html" "$WEBROOT/index.html"
cp "$REPO_ROOT/files/nginx/offline-library" /etc/nginx/sites-available/offline-library
rm -f /etc/nginx/sites-enabled/default
ln -sfn /etc/nginx/sites-available/offline-library \
    /etc/nginx/sites-enabled/offline-library

# Validate Nginx configuration before applying it
if ! nginx -t; then
    echo "ERROR: Nginx configuration validation failed." >&2
    exit 1
fi

# Enable Nginx to start automatically at boot
systemctl enable nginx.service

# Start Nginx, or reload it if already running
if systemctl is-active --quiet nginx.service; then
    systemctl reload nginx.service
else
    systemctl start nginx.service
fi

# Verify Nginx is running
if ! systemctl is-active --quiet nginx.service; then
    echo "ERROR: Nginx failed to start." >&2
    journalctl -u nginx.service -n 30 --no-pager
    exit 1
fi

# Verify the local homepage responds
if ! curl -fsS -o /dev/null http://127.0.0.1/; then
    echo "ERROR: Nginx is running, but the homepage is not responding." >&2
    exit 1
fi

echo "Nginx installation complete."

## 35 Admin Interface page
#apt-get install -y python3-venv apache2-utils sudo

# Notes 5: admin account, saved app/service, helper commands, and HTTP auth.
#useradd --system --home-dir /opt/offline-library-admin \
#    --shell /usr/sbin/nologin library-admin 2>/dev/null || true
#install -d -o library-admin -g library-admin /opt/offline-library-admin
#sudo -u library-admin python3 -m venv /opt/offline-library-admin/venv
#sudo -u library-admin /opt/offline-library-admin/venv/bin/pip install Flask gunicorn
#cp "$REPO_ROOT/files/admin/app.py" /opt/offline-library-admin/app.py
#chown library-admin:library-admin /opt/offline-library-admin/app.py
#for command_file in library-admin-network-read library-admin-network-write library-admin-diagnostics; do
#    cp "$REPO_ROOT/files/admin/bin/$command_file" "/usr/local/sbin/$command_file"
#    chmod 755 "/usr/local/sbin/$command_file"
#done
#cp "$REPO_ROOT/files/admin/offline-library-admin.sudoers" \
#    /etc/sudoers.d/offline-library-admin
#chmod 440 /etc/sudoers.d/offline-library-admin
#htpasswd -B -c /etc/nginx/.offline-library-admin.htpasswd admin #TODO: this will ask for password, need notes in instructins about that and what it is for.
#TODO: create admin password only if it does not exist- so need to check first. Right now this script will reset the credentials every time it is ran.
#chown root:www-data /etc/nginx/.offline-library-admin.htpasswd
#chmod 640 /etc/nginx/.offline-library-admin.htpasswd
#cp "$REPO_ROOT/files/systemd/offline-library-admin.service" \
#    /etc/systemd/system/offline-library-admin.service


## 35 Admin Interface

apt-get install -y python3-venv apache2-utils sudo

# Create dedicated admin service account
if ! id library-admin >/dev/null 2>&1; then
    useradd --system \
        --home-dir /opt/offline-library-admin \
        --shell /usr/sbin/nologin \
        library-admin
fi

install -d -o library-admin -g library-admin -m 755 \
    /opt/offline-library-admin

# Create virtual environment if missing
if [[ ! -x /opt/offline-library-admin/venv/bin/python ]]; then
    sudo -u library-admin python3 -m venv \
        /opt/offline-library-admin/venv
fi

# Install Python dependencies
sudo -u library-admin \
    /opt/offline-library-admin/venv/bin/pip install Flask gunicorn

# Install application
install -o library-admin -g library-admin -m 644 \
    "$REPO_ROOT/files/admin/app.py" \
    /opt/offline-library-admin/app.py

# Install privileged helper commands
for command_file in \
    library-admin-network-read \
    library-admin-network-write \
    library-admin-diagnostics
do
    install -o root -g root -m 755 \
        "$REPO_ROOT/files/admin/bin/$command_file" \
        "/usr/local/sbin/$command_file"
done

# Install and validate sudoers rules
install -o root -g root -m 440 \
    "$REPO_ROOT/files/admin/offline-library-admin.sudoers" \
    /etc/sudoers.d/offline-library-admin

if ! visudo -cf /etc/sudoers.d/offline-library-admin; then
    echo "ERROR: Invalid admin sudoers configuration." >&2
    exit 1
fi

# Create HTTP authentication only if missing
AUTH_FILE="/etc/nginx/.offline-library-admin.htpasswd"

if [[ ! -f "$AUTH_FILE" ]]; then
    echo "Create the password for the Offline Library web admin account."
    htpasswd -B -c "$AUTH_FILE" admin
fi

chown root:www-data "$AUTH_FILE"
chmod 640 "$AUTH_FILE"

# Install systemd service
install -o root -g root -m 644 \
    "$REPO_ROOT/files/systemd/offline-library-admin.service" \
    /etc/systemd/system/offline-library-admin.service

# Reload systemd and activate service
systemctl daemon-reload
systemctl enable offline-library-admin.service
systemctl restart offline-library-admin.service

# Verify service is running
if ! systemctl is-active --quiet offline-library-admin.service; then
    echo "ERROR: Admin service failed to start." >&2
    journalctl -u offline-library-admin.service -n 30 --no-pager
    exit 1
fi

echo "Admin interface installation complete."


## 40 Kiwix
mkdir -p "$LIBRARY_ROOT/kiwix"
apt-get install -y kiwix-tools

# Install the systemd service
cp "$REPO_ROOT/files/systemd/kiwix.service" \
    /etc/systemd/system/kiwix.service
# Download small test ZIM if not already present
TEST_ZIM="$LIBRARY_ROOT/kiwix/wiktionary_en-simple_all_nopic_2026-07.zim"

if [[ ! -s "$TEST_ZIM" ]]; then
    curl -fL --retry 3 \
        --output "${TEST_ZIM}.part" \
        https://lb.download.kiwix.org/zim/wiktionary/wiktionary_en-simple_all_nopic_2026-07.zim
    mv "${TEST_ZIM}.part" "$TEST_ZIM"
fi

chmod 644 "$LIBRARY_ROOT"/kiwix/*.zim

# Rebuild catalog from available ZIM files
rm -f "$LIBRARY_ROOT/kiwix/library.xml"  #TODO: avoid deleting and instead build tempp catalog an dthen atomically replace the existing one. Due to --monitorlibrary

for zim in "$LIBRARY_ROOT"/kiwix/*.zim; do
    kiwix-manage "$LIBRARY_ROOT/kiwix/library.xml" add "$zim"
done
chmod 644 "$LIBRARY_ROOT/kiwix/library.xml"

# Register and start service
systemctl daemon-reload
systemctl enable --now kiwix.service
systemctl restart kiwix.service

# Verify service status
systemctl is-active --quiet kiwix.service || {
    echo "ERROR: Kiwix failed to start" >&2
    journalctl -u kiwix.service -n 30 --no-pager
    exit 1
}

echo "Kiwix installation complete."

## 50 VuIO
mkdir -p  "$LIBRARY_ROOT/media"

# Notes 2: VuIO media server.
#curl -fL --output "$WORK_DIR/vuio_arm64.deb" \
#    https://vuiodev.github.io/vuio/packages/vuio_latest_arm64.deb #TODO: Pin to a certain version rather than latest, to avoid future breakage.
#apt-get install -y "$WORK_DIR/vuio_arm64.deb"
#groupadd --system library-media 2>/dev/null || true
#usermod -aG library-media "$PI_USER"
#usermod -aG library-media vuio
#chgrp -R library-media "$LIBRARY_ROOT/media"
#find "$LIBRARY_ROOT/media" -type d -exec chmod 2775 {} \;
#find "$LIBRARY_ROOT/media" -type f -exec chmod 664 {} \;
#cp "$REPO_ROOT/files/vuio/vuio.toml" /etc/vuio/vuio.toml
#mkdir -p /etc/systemd/system/vuio.service.d
#cp "$REPO_ROOT/files/vuio/dropins/lan.conf" /etc/systemd/system/vuio.service.d/lan.conf
#cp "$REPO_ROOT/files/vuio/dropins/config-write.conf" \
#    /etc/systemd/system/vuio.service.d/config-write.conf


## 50 VuIO

# Create shared media group if it does not exist
if ! getent group library-media >/dev/null; then
    groupadd --system library-media
fi

# Create media directory with appropriate ownership and permissions
install -d -o "$PI_USER" -g library-media -m 2775 \
    "$LIBRARY_ROOT/media"

# Verify CPU architecture
if [[ "$(dpkg --print-architecture)" != "arm64" ]]; then
    echo "ERROR: VuIO package requires arm64." >&2
    exit 1
fi

# Download and install VuIO
curl -fL --retry 3 \
    --output "$WORK_DIR/vuio_arm64.deb" \
    https://vuiodev.github.io/vuio/packages/vuio_latest_arm64.deb

apt-get install -y "$WORK_DIR/vuio_arm64.deb"

# Verify VuIO service account was created
if ! id vuio >/dev/null 2>&1; then
    echo "ERROR: VuIO service account not found." >&2
    exit 1
fi

# Add users to shared media group
usermod -aG library-media "$PI_USER"
usermod -aG library-media vuio

# Install Offline Library configuration only on first setup
# Preserve user changes on subsequent runs
mkdir -p /etc/offline-library /etc/vuio

VUIO_MARKER="/etc/offline-library/vuio-configured"

if [[ ! -f "$VUIO_MARKER" ]]; then

    # Preserve existing configuration, if any
    if [[ -f /etc/vuio/vuio.toml ]]; then
        cp -p /etc/vuio/vuio.toml \
            /etc/vuio/vuio.toml.pre-offline-library
    fi

    install -o root -g root -m 644 \
        "$REPO_ROOT/files/vuio/vuio.toml" \
        /etc/vuio/vuio.toml

    touch "$VUIO_MARKER"
fi

# Install systemd service overrides
install -d -m 755 /etc/systemd/system/vuio.service.d

install -o root -g root -m 644 \
    "$REPO_ROOT/files/vuio/dropins/lan.conf" \
    /etc/systemd/system/vuio.service.d/lan.conf

install -o root -g root -m 644 \
    "$REPO_ROOT/files/vuio/dropins/config-write.conf" \
    /etc/systemd/system/vuio.service.d/config-write.conf

# Reload systemd and enable VuIO at boot
systemctl daemon-reload
systemctl enable vuio.service

# Start or restart VuIO with the installed configuration
systemctl restart vuio.service

# Verify service started
if ! systemctl is-active --quiet vuio.service; then
    echo "ERROR: VuIO service failed to start." >&2
    journalctl -u vuio.service -n 30 --no-pager
    exit 1
fi

# Verify browser interface responds
if ! curl -fsS --retry 5 --retry-delay 2 \
    -o /dev/null http://127.0.0.1:8090/; then
    echo "ERROR: VuIO browser interface is not responding." >&2
    journalctl -u vuio.service -n 30 --no-pager
    exit 1
fi

echo "VuIO installation complete."


## 60 Maps

# Create map directories
mkdir -p \
    "$LIBRARY_ROOT/maps" \
    "$LIBRARY_ROOT/maps/assets" \
    "$LIBRARY_ROOT/maps/data" \
    "$LIBRARY_ROOT/maps/data/terrain"

# Install map application files
cp -a "$REPO_ROOT/files/maps/." "$LIBRARY_ROOT/maps/"

# Install bundled Protomaps fonts/glyphs
GLYPH_ARCHIVE="$REPO_ROOT/files/archives/protomaps-font-glyphs-reference-pi.tar.gz"

if [[ ! -s "$GLYPH_ARCHIVE" ]]; then
    echo "ERROR: Map glyph archive missing or empty." >&2
    exit 1
fi

tar -xzf "$GLYPH_ARCHIVE" \
    -C "$LIBRARY_ROOT/maps/assets" \
    --no-same-owner --no-same-permissions

# Make map application files readable by Nginx
chmod -R a+rX "$LIBRARY_ROOT/maps/assets"

# Make maps accessible through Nginx
ln -sfn "$LIBRARY_ROOT/maps" "$WEBROOT/maps"

echo "Map application installed."
echo "Geographic map content is installed separately."



## 70 Flatnotes
# Notes 2: FlatNotes Pi builds failed; install the captured source and Windows-built client.
#mkdir -p /opt/flatnotes "$LIBRARY_ROOT/notes"
#tar -xzf "$REPO_ROOT/files/archives/flatnotes-v5.5.5.tar.gz" \
#    -C /opt/flatnotes --no-same-owner --no-same-permissions
#curl -fL --output "$WORK_DIR/uv-install.sh" https://astral.sh/uv/install.sh
#sh "$WORK_DIR/uv-install.sh"
#source /root/.local/bin/env
#uv sync --locked --no-dev --project /opt/flatnotes
#useradd --system --home-dir "$LIBRARY_ROOT/notes" \
#    --shell /usr/sbin/nologin flatnotes 2>/dev/null || true
#chown -R root:root /opt/flatnotes
#chown -R flatnotes:flatnotes "$LIBRARY_ROOT/notes"
#chmod -R a+rX /opt/flatnotes/client/dist
#chown flatnotes:flatnotes /opt/flatnotes/client/dist/index.html
#chmod 664 /opt/flatnotes/client/dist/index.html
#cp "$REPO_ROOT/files/systemd/flatnotes.service" /etc/systemd/system/flatnotes.service


## 70 FlatNotes

apt-get install -y python3-venv

# Create dedicated FlatNotes service account
if ! id flatnotes >/dev/null 2>&1; then
    useradd --system \
        --home-dir "$LIBRARY_ROOT/notes" \
        --shell /usr/sbin/nologin \
        flatnotes
fi

# Create application and notes directories
install -d -o root -g root -m 755 /opt/flatnotes

# Preserve existing notes and their ownership
if [[ ! -d "$LIBRARY_ROOT/notes" ]]; then
    install -d -o flatnotes -g flatnotes -m 750 \
        "$LIBRARY_ROOT/notes"
fi

# Install captured FlatNotes source/client if not installed
if [[ ! -f /opt/flatnotes/pyproject.toml ]]; then
    tar -xzf \
        "$REPO_ROOT/files/archives/flatnotes-v5.5.5.tar.gz" \
        -C /opt/flatnotes \
        --no-same-owner --no-same-permissions
fi

# Install uv if not already installed
if [[ ! -x /root/.local/bin/uv ]]; then
    curl -fL --retry 3 \
        --output "$WORK_DIR/uv-install.sh" \
        https://astral.sh/uv/install.sh

    sh "$WORK_DIR/uv-install.sh"
fi

# Use the installed uv executable directly
UV="/root/.local/bin/uv"

if [[ ! -x "$UV" ]]; then
    echo "ERROR: uv installation failed." >&2
    exit 1
fi

# Install Python dependencies using the captured lockfile
"$UV" sync --locked --no-dev --project /opt/flatnotes

# Application files owned by root
chown -R root:root /opt/flatnotes

# Browser client must be readable
chmod -R a+rX /opt/flatnotes/client/dist

# Preserve existing behavior: FlatNotes can update index.html
#TODO relook if his behavior is needed or not and whay flatnotes would be able to do this.
chown flatnotes:flatnotes \
    /opt/flatnotes/client/dist/index.html
chmod 664 /opt/flatnotes/client/dist/index.html

# Install systemd service
install -o root -g root -m 644 \
    "$REPO_ROOT/files/systemd/flatnotes.service" \
    /etc/systemd/system/flatnotes.service

# Register and activate service
systemctl daemon-reload
systemctl enable flatnotes.service
systemctl restart flatnotes.service

# Verify service started
if ! systemctl is-active --quiet flatnotes.service; then
    echo "ERROR: FlatNotes failed to start." >&2
    journalctl -u flatnotes.service -n 30 --no-pager
    exit 1
fi

# Verify FlatNotes responds through its configured URL prefix
if ! curl -fsS --retry 5 --retry-delay 2 \
    -o /dev/null http://127.0.0.1:8082/notes/; then
    echo "ERROR: FlatNotes HTTP endpoint is not responding." >&2
    journalctl -u flatnotes.service -n 30 --no-pager
    exit 1
fi

echo "FlatNotes installation complete."


## 80 Games Emulator
apt-get install -y 7zip

# Notes 4: EmulatorJS, final games page, and ROM location.
mkdir -p "$WEBROOT/games/emulatorjs" "$WEBROOT/games/roms/nes"
# Use bundled EmulatorJS archive, or download if missing
EMULATOR_ARCHIVE="$REPO_ROOT/packages/Emulator-js-4.2.3.7z"
if [[ ! -s "$EMULATOR_ARCHIVE" ]]; then
    echo "Local EmulatorJS archive not found. Downloading..."
    EMULATOR_ARCHIVE="$WORK_DIR/Emulator-js-4.2.3.7z"
    curl -fL --retry 3 \
        --output "$EMULATOR_ARCHIVE" \
        https://cdn.emulatorjs.org/releases/4.2.3.7z
fi
# Extract whichever archive was selected
7z x -bd -y "$EMULATOR_ARCHIVE" \
    "-o$WORK_DIR/emulatorjs" >/dev/null
if [[ ! -d "$WORK_DIR/emulatorjs/data" ]]; then
    echo "ERROR: EmulatorJS data directory missing after extraction." >&2
    exit 1
fi
cp -a "$WORK_DIR/emulatorjs/data" "$WEBROOT/games/emulatorjs/"
cp "$REPO_ROOT/files/games/index.html" "$WEBROOT/games/index.html"
#TODO: NEED TO ADD SOMETHING FOR GETTING GAME ROMS INTO /var/www/offline-library/games/roms/nes/

echo "Games emulator installation complete."

## 90 Static Tools

for archive in \
    cyberchef-v11.4.0.tar.gz \
    omnitools-v0.6.0-89a50e3.tar.gz \
    excalidraw-c0ad61c6.tar.gz \
    minipaint-v4.14.3.tar.gz
do
    if [[ ! -s "$REPO_ROOT/files/archives/$archive" ]]; then
        echo "ERROR: Missing or empty archive: $archive" >&2
        exit 1
    fi
done
# Notes 3: unpack the archived browser tools into their final paths.
mkdir -p "$WEBROOT/tools/cyberchef" "$WEBROOT/tools/omnitools" \
    "$WEBROOT/tools/excalidraw" "$WEBROOT/tools/minipaint"
tar -xzf "$REPO_ROOT/files/archives/cyberchef-v11.4.0.tar.gz" \
    -C "$WEBROOT/tools/cyberchef" --no-same-owner --no-same-permissions
cp "$WEBROOT/tools/cyberchef/CyberChef_v11.4.0.html" \
    "$WEBROOT/tools/cyberchef/index.html"
tar -xzf "$REPO_ROOT/files/archives/omnitools-v0.6.0-89a50e3.tar.gz" \
    -C "$WEBROOT/tools/omnitools" --no-same-owner --no-same-permissions
tar -xzf "$REPO_ROOT/files/archives/excalidraw-c0ad61c6.tar.gz" \
    -C "$WEBROOT/tools/excalidraw" --no-same-owner --no-same-permissions
tar -xzf "$REPO_ROOT/files/archives/minipaint-v4.14.3.tar.gz" \
    -C "$WEBROOT/tools/minipaint" --no-same-owner --no-same-permissions

# Ensure Nginx can read the static applications
chmod -R a+rX "$WEBROOT/tools"

for app in cyberchef omnitools excalidraw minipaint; do
    if [[ ! -s "$WEBROOT/tools/$app/index.html" ]]; then
        echo "ERROR: Missing index.html for $app" >&2
        #exit 1
    fi
done

for app in cyberchef omnitools excalidraw minipaint; do
    if ! curl -fsS -o /dev/null \
        "http://127.0.0.1/tools/$app/"; then
        echo "ERROR: Cannot access $app through Nginx." >&2
        #exit 1
    fi
done

echo "Static tools installation complete."

## 100 Kiosk

## 110 Validation

## Download Kiwix Data

# Notes 1: download the final Kiwix collection into its permanent directory.
curl -fL --output "$LIBRARY_ROOT/kiwix/wikipedia_en_all_maxi_2026-08.zim" \
    https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2026-08.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/ifixit_en_all_2025-12.zim" \
    https://download.kiwix.org/zim/ifixit/ifixit_en_all_2025-12.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/www.ready.gov_en_2024-12.zim" \
    https://lb.download.kiwix.org/zim/zimit/www.ready.gov_en_2024-12.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/wikivoyage_en_all_maxi_2026-09.zim" \
    https://lb.download.kiwix.org/zim/wikivoyage/wikivoyage_en_all_maxi_2026-09.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/rabbitwaves.ca_en_all_2026-09.zim" \
    https://lb.download.kiwix.org/zim/zimit/rabbitwaves.ca_en_all_2026-09.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/irp.fas.org_en_military-medicine_2026-05.zim" \
    https://lb.download.kiwix.org/zim/zimit/irp.fas.org_en_military-medicine_2026-05.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/wikiciv_en_all_maxi_2025-11.zim" \
    https://lb.download.kiwix.org/zim/other/wikiciv_en_all_maxi_2025-11.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/foss.cooking_en_all_2026-05.zim" \
    https://lb.download.kiwix.org/zim/zimit/foss.cooking_en_all_2026-05.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/devdocs_en_javascript_2026-10.zim" \
    https://lb.download.kiwix.org/zim/devdocs/devdocs_en_javascript_2026-10.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/devdocs_en_css_2026-10.zim" \
    https://lb.download.kiwix.org/zim/devdocs/devdocs_en_css_2026-10.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/devdocs_en_http_2026-10.zim" \
    https://lb.download.kiwix.org/zim/devdocs/devdocs_en_http_2026-10.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/devdocs_en_html_2026-10.zim" \
    https://lb.download.kiwix.org/zim/devdocs/devdocs_en_html_2026-10.zim
curl -fL --output "$LIBRARY_ROOT/kiwix/wikivet.net_en_all_maxi_2026-07a.zim" \
    https://lb.download.kiwix.org/zim/other/wikivet.net_en_all_maxi_2026-07a.zim

#chmod 644 "$LIBRARY_ROOT"/kiwix/*.zim
#for zim in "$LIBRARY_ROOT"/kiwix/*.zim; do
#    kiwix-manage "$LIBRARY_ROOT/kiwix/library.xml" add "$zim"
#done
# Verify that ZIM files exist
shopt -s nullglob
zim_files=("$LIBRARY_ROOT"/kiwix/*.zim)
shopt -u nullglob

if (( ${#zim_files[@]} == 0 )); then
    echo "ERROR: No Kiwix ZIM files found." >&2
    exit 1
fi

# Make all ZIM files readable by Kiwix
chmod 644 "${zim_files[@]}"

# Build a fresh catalog without modifying the active one
CATALOG="$LIBRARY_ROOT/kiwix/library.xml"
NEW_CATALOG="$LIBRARY_ROOT/kiwix/library-new.xml"

rm -f "$NEW_CATALOG"

for zim in "${zim_files[@]}"; do
    kiwix-manage "$NEW_CATALOG" add "$zim"
done

# Activate the new catalog
chmod 644 "$NEW_CATALOG"
mv -f "$NEW_CATALOG" "$CATALOG"

# Restart Kiwix to ensure it loads the updated catalog
systemctl restart kiwix.service

if ! systemctl is-active --quiet kiwix.service; then
    echo "ERROR: Kiwix failed after catalog update." >&2
    journalctl -u kiwix.service -n 30 --no-pager
    exit 1
fi

echo "Kiwix catalog updated with ${#zim_files[@]} ZIM files."


## Download Map Data

# Geographic boundaries: west, south, east, north
WEST=-74.55
SOUTH=40.90
EAST=-73.35
NORTH=42.85

# Install PMTiles command-line utility
curl -fL --retry 3 \
    --output "$WORK_DIR/pmtiles.tar.gz" \
    https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_arm64.tar.gz

mkdir -p "$WORK_DIR/pmtiles"

tar -xzf "$WORK_DIR/pmtiles.tar.gz" \
    -C "$WORK_DIR/pmtiles"

install -m 755 \
    "$WORK_DIR/pmtiles/pmtiles" \
    /usr/local/bin/pmtiles

# Identify a recent available Protomaps build
BUILD_DATE=""

for days_ago in {0..7}; do
    CANDIDATE="$(date -u -d "$days_ago days ago" +%Y%m%d)"

    if curl -fsSL --range 0-0 \
        "https://build.protomaps.com/${CANDIDATE}.pmtiles" \
        --output /dev/null; then

        BUILD_DATE="$CANDIDATE"
        break
    fi
done

if [[ -z "$BUILD_DATE" ]]; then
    echo "ERROR: Could not find a recent Protomaps build." >&2
    exit 1
fi

# Extract selected geographic region
pmtiles extract \
    "https://build.protomaps.com/${BUILD_DATE}.pmtiles" \
    "$LIBRARY_ROOT/maps/data/hudson-valley.pmtiles" \
    --bbox="$WEST,$SOUTH,$EAST,$NORTH"

# Download terrain elevation tiles
python3 "$SCRIPT_DIR/download_terrain_tiles.py" \
    "$WEST" "$SOUTH" "$EAST" "$NORTH"

echo "Map content installation complete."




echo "Setup commands completed. Add media and NES ROM files separately."
echo "Install $WEBROOT/certs/OfflineLibraryRootCA.cer as a trusted root on client devices."
echo "Reboot the Pi to activate the SPI overlay for the e-ink display."
