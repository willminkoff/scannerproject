#!/bin/bash
set -euo pipefail

log() {
  echo "[sb3-ap-fallback] $*"
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    log "ERROR: must run as root"
    exit 1
  fi
}

ping_reachable() {
  local target="$1"
  local count="$2"
  local timeout="$3"
  local iface="$4"
  local cmd=(ping -c "$count" -W "$timeout")
  if [[ -n "$iface" ]]; then
    cmd+=( -I "$iface" )
  fi
  cmd+=("$target")
  "${cmd[@]}" >/dev/null 2>&1
}

start_ap() {
  local iface="$1"
  local ssid="$2"
  local channel="$3"
  local country="$4"
  local ap_ip="$5"
  local ap_cidr="$6"
  local dhcp_start="$7"
  local dhcp_end="$8"
  local lease_time="$9"
  local passphrase="${10}"
  local hostapd_bin="${11}"
  local dnsmasq_bin="${12}"
  local state_dir="${13}"
  local netmask

  if [[ ! -x "$hostapd_bin" ]]; then
    log "ERROR: hostapd not found at $hostapd_bin"
    exit 1
  fi
  if [[ ! -x "$dnsmasq_bin" ]]; then
    log "ERROR: dnsmasq not found at $dnsmasq_bin"
    exit 1
  fi

  if ! ip link show "$iface" >/dev/null 2>&1; then
    log "ERROR: interface $iface not found"
    exit 1
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "wpa_supplicant@${iface}.service" >/dev/null 2>&1 || true
    systemctl stop wpa_supplicant.service >/dev/null 2>&1 || true
  fi

  if command -v nmcli >/dev/null 2>&1; then
    nmcli dev disconnect "$iface" >/dev/null 2>&1 || true
  fi

  if command -v rfkill >/dev/null 2>&1; then
    rfkill unblock wlan >/dev/null 2>&1 || true
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop hostapd >/dev/null 2>&1 || true
    systemctl stop dnsmasq >/dev/null 2>&1 || true
  fi

  ip link set "$iface" down || true
  ip addr flush dev "$iface" || true
  ip link set "$iface" up
  ip addr add "${ap_ip}/${ap_cidr}" dev "$iface"

  netmask=$(cidr_to_netmask "$ap_cidr")

  mkdir -p "$state_dir"

  local hostapd_conf="$state_dir/hostapd.conf"
  local dnsmasq_conf="$state_dir/dnsmasq.conf"

  cat > "$hostapd_conf" <<EOF_HOSTAPD
interface=$iface
driver=nl80211
ssid=$ssid
country_code=$country
hw_mode=g
channel=$channel
wmm_enabled=1
ieee80211n=1
auth_algs=1
ignore_broadcast_ssid=0
EOF_HOSTAPD

  if [[ -n "$passphrase" ]]; then
    cat >> "$hostapd_conf" <<EOF_HOSTAPD_WPA
wpa=2
wpa_passphrase=$passphrase
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF_HOSTAPD_WPA
  fi

  cat > "$dnsmasq_conf" <<EOF_DNSMASQ
interface=$iface
bind-interfaces
listen-address=$ap_ip
dhcp-range=$dhcp_start,$dhcp_end,$netmask,$lease_time
dhcp-option=3,$ap_ip
dhcp-option=6,$ap_ip
EOF_DNSMASQ

  log "Starting hostapd for SSID '$ssid' on $iface"
  "$hostapd_bin" "$hostapd_conf" -B -P "$state_dir/hostapd.pid"

  log "Starting dnsmasq DHCP on $iface ($dhcp_start-$dhcp_end)"
  "$dnsmasq_bin" \
    --conf-file="$dnsmasq_conf" \
    --dhcp-leasefile="$state_dir/dnsmasq.leases" \
    --pid-file="$state_dir/dnsmasq.pid"

  log "AP online: $ssid ($ap_ip/$ap_cidr)"
}

cidr_to_netmask() {
  local cidr="$1"
  local mask=""
  local i bits val

  for i in 1 2 3 4; do
    if (( cidr >= 8 )); then
      bits=8
    elif (( cidr > 0 )); then
      bits=$cidr
    else
      bits=0
    fi

    if (( bits == 0 )); then
      val=0
    else
      val=$(( 256 - (1 << (8 - bits)) ))
    fi

    if [[ -z "$mask" ]]; then
      mask="$val"
    else
      mask="${mask}.${val}"
    fi

    cidr=$(( cidr - bits ))
  done

  echo "$mask"
}

require_root

BOOT_WAIT_SEC="${BOOT_WAIT_SEC:-25}"
PING_IP="${PING_IP:-1.1.1.1}"
PING_COUNT="${PING_COUNT:-1}"
PING_TIMEOUT="${PING_TIMEOUT:-2}"
PING_IFACE="${PING_IFACE:-}"

AP_INTERFACE="${AP_INTERFACE:-wlan0}"
AP_SSID="${AP_SSID:-SB3-CTRL}"
AP_CHANNEL="${AP_CHANNEL:-6}"
AP_COUNTRY="${AP_COUNTRY:-US}"
AP_IP="${AP_IP:-192.168.4.1}"
AP_CIDR="${AP_CIDR:-24}"
AP_DHCP_START="${AP_DHCP_START:-192.168.4.20}"
AP_DHCP_END="${AP_DHCP_END:-192.168.4.200}"
AP_LEASE_TIME="${AP_LEASE_TIME:-12h}"
AP_PASSPHRASE="${AP_PASSPHRASE:-}"
HOSTAPD_BIN="${HOSTAPD_BIN:-/usr/sbin/hostapd}"
DNSMASQ_BIN="${DNSMASQ_BIN:-/usr/sbin/dnsmasq}"
STATE_DIR="${STATE_DIR:-/run/sb3-ap}"

log "Boot wait ${BOOT_WAIT_SEC}s before reachability check"
sleep "$BOOT_WAIT_SEC"

log "Pinging ${PING_IP}"
if ping_reachable "$PING_IP" "$PING_COUNT" "$PING_TIMEOUT" "$PING_IFACE"; then
  log "Reachability OK; AP not started"
  exit 0
fi

log "Reachability failed; starting fallback AP"
start_ap "$AP_INTERFACE" "$AP_SSID" "$AP_CHANNEL" "$AP_COUNTRY" \
  "$AP_IP" "$AP_CIDR" "$AP_DHCP_START" "$AP_DHCP_END" "$AP_LEASE_TIME" \
  "$AP_PASSPHRASE" "$HOSTAPD_BIN" "$DNSMASQ_BIN" "$STATE_DIR"
