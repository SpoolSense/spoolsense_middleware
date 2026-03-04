#!/usr/bin/env bash
# ==============================================================================
# nfc-toolchanger-spoolman — Interactive Install Script (BETA)
# ==============================================================================
# BETA: This script is under active testing. Please report issues on GitHub.
#
# Usage:
#   bash scripts/install-beta.sh
#
# Re-run at any time to reconfigure or uninstall.
# ==============================================================================

set -euo pipefail

# ------------------------------------------------------------------------------
# Colors
# ------------------------------------------------------------------------------
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
info()    { echo -e "${CYAN}${BOLD}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}${BOLD}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}${BOLD}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}${BOLD}[ERROR]${RESET} $*"; }
header()  { echo -e "\n${BOLD}$*${RESET}"; }

confirm() {
    # Usage: confirm "Question?" && do_thing
    local prompt="$1"
    local answer
    echo -en "${BOLD}${prompt} [y/N]: ${RESET}"
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]]
}

prompt() {
    # Usage: VAR=$(prompt "Label" "default")
    local label="$1"
    local default="$2"
    local value
    echo -en "${BOLD}${label}${RESET} [${default}]: "
    read -r value
    echo "${value:-$default}"
}

prompt_required() {
    # Usage: VAR=$(prompt_required "Label")
    # Keeps asking until non-empty input
    local label="$1"
    local value=""
    while [[ -z "$value" ]]; do
        echo -en "${BOLD}${label}${RESET}: "
        read -r value
        if [[ -z "$value" ]]; then
            warn "This field is required."
        fi
    done
    echo "$value"
}

prompt_password() {
    local label="$1"
    local value=""
    while [[ -z "$value" ]]; do
        echo -en "${BOLD}${label}${RESET}: "
        read -rs value
        echo
        if [[ -z "$value" ]]; then
            warn "This field is required."
        fi
    done
    echo "$value"
}

normalize_spoolman_url() {
    # Accept bare IP, IP:port, or full http:// URL
    # Defaults to port 7912 if no port given
    local input="$1"
    # Strip trailing slash
    input="${input%/}"
    # If already has http://, check if port is present
    if [[ "$input" == http://* ]]; then
        # Has scheme — check if port is included
        local host="${input#http://}"
        if [[ "$host" == *:* ]]; then
            echo "$input"
        else
            echo "${input}:7912"
        fi
    else
        # No scheme — check if port is included
        if [[ "$input" == *:* ]]; then
            echo "http://${input}"
        else
            echo "http://${input}:7912"
        fi
    fi
}

normalize_moonraker_url() {
    # Accept bare IP or full http:// URL — no default port (Moonraker uses 80)
    local input="$1"
    # Strip trailing slash
    input="${input%/}"
    if [[ "$input" == http://* ]]; then
        echo "$input"
    else
        echo "http://${input}"
    fi
}

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
INSTALL_DIR="$HOME/nfc_spoolman"
LISTENER_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/middleware/nfc_listener.py"
SERVICE_NAME="nfc-spoolman"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALLED_LISTENER="${INSTALL_DIR}/nfc_listener.py"

# ------------------------------------------------------------------------------
# Detect existing install
# ------------------------------------------------------------------------------
already_installed() {
    [[ -f "$INSTALLED_LISTENER" ]] || systemctl list-unit-files "${SERVICE_NAME}.service" &>/dev/null && \
    systemctl list-unit-files "${SERVICE_NAME}.service" 2>/dev/null | grep -q "${SERVICE_NAME}"
}

# ------------------------------------------------------------------------------
# Uninstall
# ------------------------------------------------------------------------------
do_uninstall() {
    header "Uninstall nfc-toolchanger-spoolman"
    echo
    warn "This will stop and remove the middleware service and nfc_listener.py."
    warn "Your Spoolman data and ESPHome configs will NOT be affected."
    echo
    confirm "Are you sure you want to uninstall?" || { info "Uninstall cancelled."; exit 0; }

    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Stopping service..."
        sudo systemctl stop "${SERVICE_NAME}"
        success "Service stopped"
    fi

    if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Disabling service..."
        sudo systemctl disable "${SERVICE_NAME}"
        success "Service disabled"
    fi

    if [[ -f "$SERVICE_FILE" ]]; then
        info "Removing service file..."
        sudo rm -f "$SERVICE_FILE"
        sudo systemctl daemon-reload
        success "Service file removed"
    fi

    if [[ -f "$INSTALLED_LISTENER" ]]; then
        info "Removing nfc_listener.py..."
        rm -f "$INSTALLED_LISTENER"
        success "nfc_listener.py removed"
    fi

    echo
    success "Uninstall complete. Directory ${INSTALL_DIR} left intact (may contain logs or other files)."
    exit 0
}

# ------------------------------------------------------------------------------
# Connectivity checks
# ------------------------------------------------------------------------------
check_mqtt() {
    local broker="$1"
    local port="$2"
    if command -v nc &>/dev/null; then
        nc -z -w3 "$broker" "$port" &>/dev/null && return 0 || return 1
    elif command -v curl &>/dev/null; then
        curl -s --connect-timeout 3 "telnet://${broker}:${port}" &>/dev/null && return 0 || return 1
    else
        warn "Cannot check MQTT connectivity — nc and curl not available. Skipping."
        return 0
    fi
}

check_spoolman() {
    local url="$1"
    if command -v curl &>/dev/null; then
        curl -sf --connect-timeout 3 "${url}/api/v1/info" &>/dev/null && return 0 || return 1
    else
        warn "Cannot check Spoolman connectivity — curl not available. Skipping."
        return 0
    fi
}

# ------------------------------------------------------------------------------
# Write configured nfc_listener.py
# ------------------------------------------------------------------------------
write_listener() {
    local broker="$1"
    local port="$2"
    local username="$3"
    local password="$4"
    local toolheads_str="$5"
    local spoolman_url="$6"
    local moonraker_url="$7"
    local threshold="$8"
    local toolhead_mode="$9"

    mkdir -p "$INSTALL_DIR"

    sed \
        -e "s|TOOLHEAD_MODE = \"toolchanger\"|TOOLHEAD_MODE = \"${toolhead_mode}\"|" \
        -e "s|MQTT_BROKER = \"YOUR_HOME_ASSISTANT_IP\"|MQTT_BROKER = \"${broker}\"|" \
        -e "s|MQTT_PORT = 1883|MQTT_PORT = ${port}|" \
        -e "s|MQTT_USERNAME = \"your_mqtt_username\"|MQTT_USERNAME = \"${username}\"|" \
        -e "s|MQTT_PASSWORD = \"your_mqtt_password\"|MQTT_PASSWORD = \"${password}\"|" \
        -e "s|TOOLHEADS = \[\"T0\", \"T1\", \"T2\", \"T3\"\]|TOOLHEADS = ${toolheads_str}|" \
        -e "s|SPOOLMAN_URL = \"http://YOUR_SPOOLMAN_IP:7912\"|SPOOLMAN_URL = \"${spoolman_url}\"|" \
        -e "s|MOONRAKER_URL = \"http://YOUR_KLIPPER_IP\"|MOONRAKER_URL = \"${moonraker_url}\"|" \
        -e "s|LOW_SPOOL_THRESHOLD = 100|LOW_SPOOL_THRESHOLD = ${threshold}|" \
        "$LISTENER_SRC" > "$INSTALLED_LISTENER"

    chmod 644 "$INSTALLED_LISTENER"
}

# ------------------------------------------------------------------------------
# Write systemd service file
# ------------------------------------------------------------------------------
write_service() {
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=NFC Spoolman Listener
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 ${INSTALLED_LISTENER}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
}

# ------------------------------------------------------------------------------
# Generate ESPHome YAML files
# TODO: When shared base config support is added, replace per-file generation
#       with a single base yaml + substitutions file approach.
# ------------------------------------------------------------------------------
generate_esphome_yaml() {
    local toolhead="$1"   # e.g. T0
    local broker="$2"
    local static_ip="$3"  # e.g. 192.168.1.120
    local tl_lower="${toolhead,,}"  # e.g. t0
    local repo_root
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    local src="${repo_root}/esphome/toolhead-t0.yaml"
    local out_dir="${repo_root}/esphome/generated"
    local out="${out_dir}/toolhead-${tl_lower}.yaml"

    mkdir -p "$out_dir"

    sed \
        -e "s|toolhead-t0|toolhead-${tl_lower}|g" \
        -e "s|Toolhead T0 NFC|Toolhead ${toolhead} NFC|g" \
        -e "s|YOUR_HOME_ASSISTANT_IP|${broker}|g" \
        -e "s|nfc/toolhead/T0|nfc/toolhead/${toolhead}|g" \
        -e "s|\"T0 Status LED\"|\"${toolhead} Status LED\"|g" \
        -e "s|format: \"Tag scanned on T0|format: \"Tag scanned on ${toolhead}|g" \
        -e "s|Toolhead-T0 Fallback|Toolhead-${toolhead} Fallback|g" \
        -e "s|\\\\\"toolhead\\\\\": \\\\\"T0\\\\\"|\\\\\"toolhead\\\\\": \\\\\"${toolhead}\\\\\"|g" \
        "$src" > "$out"

    local gateway
    gateway="$(echo "$static_ip" | sed 's/\.[0-9]*$/.1/')"
    sed -i \
        -e "s|static_ip: 192\.168\.X\.X|static_ip: ${static_ip}|g" \
        -e "s|gateway: 192\.168\.X\.1|gateway: ${gateway}|g" \
        "$out"

    success "Generated esphome/generated/toolhead-${tl_lower}.yaml (IP: ${static_ip})"
}
build_toolheads_str() {
    local mode="$1"
    local custom="$2"
    if [[ "$mode" == "single" ]]; then
        echo '["T0"]'
    elif [[ "$mode" == "ktc" ]]; then
        # Build T0..T(n-1) based on count
        local count="$custom"
        local result='['
        for (( i=0; i<count; i++ )); do
            if [[ $i -gt 0 ]]; then result+=', '; fi
            result+="\"T${i}\""
        done
        result+=']'
        echo "$result"
    else
        # Custom — user provided comma-separated names, build Python list
        local result='['
        IFS=',' read -ra names <<< "$custom"
        for i in "${!names[@]}"; do
            name=$(echo "${names[$i]}" | xargs)  # trim whitespace
            if [[ $i -gt 0 ]]; then result+=', '; fi
            result+="\"${name}\""
        done
        result+=']'
        echo "$result"
    fi
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
echo
echo -e "${BOLD}=================================================${RESET}"
echo -e "${BOLD}  nfc-toolchanger-spoolman — Install Script${RESET}"
echo -e "${YELLOW}${BOLD}  BETA — Please report issues on GitHub${RESET}"
echo -e "${BOLD}=================================================${RESET}"
echo
echo -e "${YELLOW}${BOLD}  IMPORTANT: Static IP addresses recommended${RESET}"
echo -e "  Each NFC scanner (ESP32-S3) should be assigned a static IP"
echo -e "  address on your network before running this script. Without"
echo -e "  a static IP, the scanner's address may change after a reboot"
echo -e "  and ESPHome will lose contact with the device."
echo -e "  Set static IPs in your router's DHCP reservation table, or"
echo -e "  configure them directly in the generated ESPHome YAML files."
echo

# Verify we're running from the repo root
if [[ ! -f "$LISTENER_SRC" ]]; then
    error "Cannot find middleware/nfc_listener.py"
    error "Please run this script from the repo root: bash scripts/install-beta.sh"
    exit 1
fi

# Handle existing install
if already_installed; then
    warn "An existing installation was detected."
    echo
    echo -e "  ${BOLD}1)${RESET} Reconfigure"
    echo -e "  ${BOLD}2)${RESET} Uninstall"
    echo -e "  ${BOLD}3)${RESET} Cancel"
    echo
    echo -en "${BOLD}Choose an option [1/2/3]: ${RESET}"
    read -r choice
    case "$choice" in
        1) info "Continuing to reconfigure..." ;;
        2) do_uninstall ;;
        *) info "Cancelled."; exit 0 ;;
    esac
fi

# ------------------------------------------------------------------------------
# Gather config
# ------------------------------------------------------------------------------
header "Step 1 of 4 — Configuration"
echo "Press Enter to accept the default shown in [brackets]."
echo

echo -e "${BOLD}Toolhead mode:${RESET}"
echo -e "  ${BOLD}1)${RESET} Single toolhead"
echo -e "  ${BOLD}2)${RESET} Toolchanger (e.g. MadMax / StealthChanger — T0, T1, ...)"
echo -e "  ${BOLD}3)${RESET} Toolchanger — Custom names"
echo -en "${BOLD}Choose [1/2/3]: ${RESET}"
read -r th_choice

case "$th_choice" in
    1) TH_MODE="single"; TH_CUSTOM=""; TOOLHEAD_MODE="single" ;;
    2)
        TH_MODE="ktc"; TOOLHEAD_MODE="toolchanger"
        echo -en "${BOLD}How many toolheads? ${RESET}"
        read -r th_count
        TH_CUSTOM="$th_count"
        ;;
    3)
        TH_MODE="custom"; TOOLHEAD_MODE="toolchanger"
        TH_CUSTOM=$(prompt_required "Enter toolhead names (comma-separated, e.g. tool_carriage_0, tool_carriage_1)")
        ;;
    *) warn "Invalid choice, defaulting to toolchanger 4 toolheads (T0–T3)"; TH_MODE="ktc"; TH_CUSTOM="4"; TOOLHEAD_MODE="toolchanger" ;;
esac

TOOLHEADS_STR=$(build_toolheads_str "$TH_MODE" "$TH_CUSTOM")

# Prompt for a static IP per scanner
echo
echo -e "${BOLD}Scanner IP addresses${RESET}"
echo -e "  Enter the static IP for each NFC scanner (ESP32-S3)."
echo -e "  These will be written into the generated ESPHome YAML files."
echo
declare -A SCANNER_IPS
IFS=',' read -ra th_list <<< "$(echo "$TOOLHEADS_STR" | tr -d '[]"' )"
for th in "${th_list[@]}"; do
    th=$(echo "$th" | xargs)
    [[ -z "$th" ]] && continue
    SCANNER_IPS["$th"]=$(prompt_required "Static IP for ${th} scanner")
done

echo
MQTT_BROKER=$(prompt_required "MQTT broker IP (your Home Assistant IP)")
MQTT_PORT=$(prompt "MQTT port" "1883")
MQTT_USERNAME=$(prompt_required "MQTT username")
MQTT_PASSWORD=$(prompt_password "MQTT password")

echo
SPOOLMAN_RAW=$(prompt_required "Spoolman IP or URL (e.g. 192.168.1.100 or http://192.168.1.100:7912)")
SPOOLMAN_URL=$(normalize_spoolman_url "$SPOOLMAN_RAW")
MOONRAKER_RAW=$(prompt_required "Moonraker IP or URL (e.g. 192.168.1.100 or http://192.168.1.100)")
MOONRAKER_URL=$(normalize_moonraker_url "$MOONRAKER_RAW")
LOW_SPOOL_THRESHOLD=$(prompt "Low spool warning threshold (grams)" "100")

# ------------------------------------------------------------------------------
# Summary + confirmation
# ------------------------------------------------------------------------------
header "Step 2 of 4 — Review"
echo
echo -e "  ${BOLD}Toolhead mode:${RESET}     ${TOOLHEAD_MODE}"
echo -e "  ${BOLD}Toolheads:${RESET}         ${TOOLHEADS_STR}"
for th in "${!SCANNER_IPS[@]}"; do
    echo -e "  ${BOLD}${th} scanner IP:${RESET}    ${SCANNER_IPS[$th]}"
done
echo -e "  ${BOLD}MQTT broker:${RESET}       ${MQTT_BROKER}:${MQTT_PORT}"
echo -e "  ${BOLD}MQTT username:${RESET}     ${MQTT_USERNAME}"
echo -e "  ${BOLD}MQTT password:${RESET}     $(echo "$MQTT_PASSWORD" | sed 's/./*/g')"
echo -e "  ${BOLD}Spoolman URL:${RESET}      ${SPOOLMAN_URL}"
echo -e "  ${BOLD}Moonraker URL:${RESET}     ${MOONRAKER_URL}"
echo -e "  ${BOLD}Low spool threshold:${RESET} ${LOW_SPOOL_THRESHOLD}g"
echo
echo -e "  ${BOLD}Will write:${RESET}        ${INSTALLED_LISTENER}"
echo -e "  ${BOLD}Will install:${RESET}      ${SERVICE_FILE}"
echo -e "  ${BOLD}Will enable:${RESET}       ${SERVICE_NAME}.service"
echo -e "  ${BOLD}Will generate:${RESET}     esphome/generated/toolhead-{t}.yaml for each toolhead"
echo

confirm "Proceed with install?" || { info "Install cancelled."; exit 0; }

# ------------------------------------------------------------------------------
# Install Python dependencies
# ------------------------------------------------------------------------------
header "Step 3 of 4 — Installing dependencies"
echo
info "Installing paho-mqtt and requests..."
pip3 install paho-mqtt requests --break-system-packages --quiet && \
    success "Dependencies installed" || \
    { error "pip3 install failed — check your Python environment"; exit 1; }

# ------------------------------------------------------------------------------
# Write listener + service
# ------------------------------------------------------------------------------
header "Step 4 of 4 — Installing"
echo

info "Writing nfc_listener.py to ${INSTALL_DIR}..."
write_listener "$MQTT_BROKER" "$MQTT_PORT" "$MQTT_USERNAME" "$MQTT_PASSWORD" \
    "$TOOLHEADS_STR" "$SPOOLMAN_URL" "$MOONRAKER_URL" "$LOW_SPOOL_THRESHOLD" "$TOOLHEAD_MODE"
success "nfc_listener.py written"

info "Writing systemd service file..."
write_service
success "Service file written to ${SERVICE_FILE}"

info "Enabling and starting service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
success "Service enabled and started"

# Generate ESPHome YAML files
echo
info "Generating ESPHome YAML files..."
# Parse toolheads from the Python list string — extract quoted identifiers
IFS=',' read -ra th_raw <<< "$(echo "$TOOLHEADS_STR" | tr -d '[]"' )"
for th in "${th_raw[@]}"; do
    th=$(echo "$th" | xargs)  # trim whitespace
    [[ -n "$th" ]] && generate_esphome_yaml "$th" "$MQTT_BROKER" "${SCANNER_IPS[$th]}"
done
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
info "ESPHome files written to ${REPO_ROOT}/esphome/generated/"
warn "Flash each device manually via web.esphome.io — copy the generated YAML for each toolhead"

# ------------------------------------------------------------------------------
# Connectivity checks
# ------------------------------------------------------------------------------
echo
info "Running connectivity checks..."

if check_mqtt "$MQTT_BROKER" "$MQTT_PORT"; then
    success "MQTT broker reachable at ${MQTT_BROKER}:${MQTT_PORT}"
else
    warn "Cannot reach MQTT broker at ${MQTT_BROKER}:${MQTT_PORT} — check IP and port"
fi

if check_spoolman "$SPOOLMAN_URL"; then
    success "Spoolman reachable at ${SPOOLMAN_URL}"
else
    warn "Cannot reach Spoolman at ${SPOOLMAN_URL} — check URL and ensure Spoolman is running"
fi

# ------------------------------------------------------------------------------
# Done
# ------------------------------------------------------------------------------
echo
echo -e "${GREEN}${BOLD}=================================================${RESET}"
echo -e "${GREEN}${BOLD}  Install complete!${RESET}"
echo -e "${GREEN}${BOLD}=================================================${RESET}"
echo
echo -e "  Check service status:  ${BOLD}sudo systemctl status ${SERVICE_NAME}${RESET}"
echo -e "  Follow logs:           ${BOLD}journalctl -u ${SERVICE_NAME} -f${RESET}"
echo -e "  Reconfigure/uninstall: ${BOLD}bash scripts/install-beta.sh${RESET}"
echo
echo -e "${BOLD}Klipper setup — add to your printer.cfg:${RESET}"
if [[ "$TOOLHEAD_MODE" == "toolchanger" ]]; then
    echo -e "  ${CYAN}[include klipper/toolhead_macros_example.cfg]${RESET}"
    echo -e "  SET_ACTIVE_SPOOL / CLEAR_ACTIVE_SPOOL are handled automatically"
    echo -e "  by klipper-toolchanger at each toolchange — no further changes needed."
else
    echo -e "  ${CYAN}[include klipper/spoolman_macros.cfg]${RESET}"
fi
echo
