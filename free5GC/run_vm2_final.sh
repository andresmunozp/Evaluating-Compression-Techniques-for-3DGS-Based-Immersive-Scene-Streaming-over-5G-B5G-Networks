#!/usr/bin/env bash

set -euo pipefail

# ============================================================
# VM2 - free-ran-ue + Measurement Orchestrator
#
# This script DOES NOT run:
#   sudo /usr/local/bin/setup-ran0.sh
#
# Recommended behavior:
#   - start gNB once
#   - start MAX_UES once
#   - keep all UE tunnels alive
#   - run one/half/all by selecting the first N ueTunX interfaces
#   - stop UEs only at the end
#
# Topology:
#   enp0s3 = NAT / Internet / DHCP
#   enp0s8 = 10.0.1.2/24, link to VM1 Core
#   ran0   = 10.0.3.1/24
#   ueTunX = 10.60.0.X/32
# ============================================================

RAN_DIR="$HOME/free-ran-ue"
CLIENT_SCRIPT="$HOME/free5gc_vm2_ue_client_measure_csv.py"

GNB_CONFIG="config/gnb.yaml"
UE_CONFIG="config/ue.yaml"

SERVER_IP="10.100.0.10"
HTTP_PORT="8080"

# VM2 physical interface toward VM1 Core
RAN_INTF="enp0s8"

# Maximum number of UEs to keep registered during the experiment.
# Scenarios will use only the first N tunnels.
MAX_UES="6"

# Measurement parameters
RUNS="5"
MAX_FILES="300"
IPERF_BASE_PORT="5201"
IPERF_SEC="10"
PING_COUNT="20"

# BW labels only identify the experiment.
# The actual bandwidth shaping is applied on VM1.
BW_LABELS=("1gbps" "10gbps" "100gbps")

# Scenario definitions:
# one  -> use ueTun0
# half -> use ueTun0, ueTun1, ueTun2
# all  -> use ueTun0, ueTun1, ueTun2, ueTun3, ueTun4, ueTun5
SCENARIOS=("one:1" "half:3" "all:6")

EXPERIMENT_SUFFIX="6ues_no_setup_ran"

OUT_DIR="$HOME/free5gc_results_vm2"
LOG_DIR="$HOME/ran_logs"
PID_DIR="$HOME/ran_pids"

# Manual synchronization with VM1.
# 1 = ask before each BW profile.
# 0 = automatic.
MANUAL_SYNC="0"

# If MANUAL_SYNC=0, this helps avoid failing immediately if VM1
# is still starting the HTTP server for the current BW profile.
HTTP_WAIT_TIMEOUT="180"

# Stop UEs when the script exits.
# gNB remains running.
STOP_UES_ON_EXIT="1"

# ============================================================
# Ping stability policy before starting measurements
# ============================================================

# Use 10 for <=10% packet loss.
# Use 0 if you want perfect ping before continuing.
PING_MAX_LOSS_PCT="10"

# Number of consecutive acceptable ping rounds required
PING_STABLE_ROUNDS="2"

# Packets per ping round
PING_CHECK_COUNT="10"

# Max seconds waiting per UE tunnel
PING_WAIT_TIMEOUT="180"

# ============================================================
# Helper functions
# ============================================================

join_by_comma() {
  local IFS=","
  echo "$*"
}

make_ue_ifaces() {
  local count="$1"
  local arr=()

  for i in $(seq 0 $((count - 1))); do
    arr+=("ueTun${i}")
  done

  join_by_comma "${arr[@]}"
}

warn_if_ran0_missing() {
  if ! ip link show ran0 >/dev/null 2>&1; then
    echo "[WARN] ran0 does not exist."
    echo "[WARN] This script will NOT run setup-ran0.sh."
    echo "[WARN] If gNB/UE fails, create/fix ran0 manually before running this script."
  else
    echo "[VM2] ran0 exists:"
    ip -br a show ran0 || true
  fi
}

wait_for_ue_tunnels() {
  local count="$1"
  local timeout_s="${2:-120}"
  local start_ts

  start_ts=$(date +%s)

  echo "[VM2] Waiting for ${count} UE tunnel(s)..."

  while true; do
    local ok="1"

    for i in $(seq 0 $((count - 1))); do
      if ! ip link show "ueTun${i}" >/dev/null 2>&1; then
        ok="0"
        break
      fi
    done

    if [ "$ok" = "1" ]; then
      echo "[VM2] UE tunnels are ready:"
      ip -br a | grep ueTun || true
      return 0
    fi

    local now
    now=$(date +%s)

    if [ $((now - start_ts)) -ge "$timeout_s" ]; then
      echo "[ERROR] Timeout waiting for UE tunnels."
      echo "Expected:"
      for i in $(seq 0 $((count - 1))); do
        echo "  ueTun${i}"
      done
      echo ""
      echo "Current:"
      ip -br a | grep ueTun || true
      echo ""
      echo "Check logs:"
      echo "  tail -f $LOG_DIR/gnb.log"
      echo "  tail -f $LOG_DIR/ue_current.log"
      exit 1
    fi

    sleep 2
  done
}

wait_for_ping_quality_one_iface() {
  local iface="$1"
  local max_loss="$2"
  local stable_rounds_required="$3"
  local timeout_s="$4"

  local stable_rounds="0"
  local start_ts
  start_ts=$(date +%s)

  echo "[VM2] Waiting for ping quality on $iface: loss <= ${max_loss}%"

  while true; do
    local ping_out
    local loss_pct
    local now

    ping_out=$(ping -I "$iface" "$SERVER_IP" -c "$PING_CHECK_COUNT" -W 2 || true)
    echo "$ping_out"

    loss_pct=$(echo "$ping_out" | sed -n 's/.* \([0-9.]\+\)% packet loss.*/\1/p' | tail -n 1)

    if [ -z "$loss_pct" ]; then
      loss_pct="100"
    fi

    echo "[VM2] $iface packet loss: ${loss_pct}%"

    if awk -v loss="$loss_pct" -v max="$max_loss" 'BEGIN { exit !(loss <= max) }'; then
      stable_rounds=$((stable_rounds + 1))
      echo "[VM2] $iface stable round ${stable_rounds}/${stable_rounds_required}"

      if [ "$stable_rounds" -ge "$stable_rounds_required" ]; then
        echo "[VM2] $iface ping quality is acceptable."
        return 0
      fi
    else
      stable_rounds="0"
      echo "[VM2] $iface is not stable yet. Waiting..."
    fi

    now=$(date +%s)

    if [ $((now - start_ts)) -ge "$timeout_s" ]; then
      echo "[ERROR] Timeout waiting for acceptable ping quality on $iface."
      echo "Required: loss <= ${max_loss}%"
      echo "Last observed loss: ${loss_pct}%"
      echo ""
      echo "Check manually:"
      echo "  ping -I $iface $SERVER_IP -c 10"
      echo "  curl --interface $iface http://${SERVER_IP}:${HTTP_PORT}/manifest.json"
      exit 1
    fi

    sleep 5
  done
}

wait_for_ping_quality_all_ifaces() {
  local ue_count="$1"

  echo "============================================================"
  echo "[VM2] Checking ping quality for ${ue_count} UE tunnel(s)"
  echo "Required packet loss <= ${PING_MAX_LOSS_PCT}%"
  echo "Stable rounds required: ${PING_STABLE_ROUNDS}"
  echo "============================================================"

  for i in $(seq 0 $((ue_count - 1))); do
    wait_for_ping_quality_one_iface "ueTun${i}" "$PING_MAX_LOSS_PCT" "$PING_STABLE_ROUNDS" "$PING_WAIT_TIMEOUT"
  done
}

check_http_manifest() {
  local iface="$1"
  local start_ts
  local now

  start_ts=$(date +%s)

  echo "[VM2] Checking HTTP manifest through $iface..."
  echo "[VM2] URL: http://${SERVER_IP}:${HTTP_PORT}/manifest.json"

  while true; do
    if curl --interface "$iface" -s --connect-timeout 5 "http://${SERVER_IP}:${HTTP_PORT}/manifest.json" >/dev/null; then
      echo "[VM2] HTTP manifest is reachable through $iface."
      return 0
    fi

    now=$(date +%s)

    if [ $((now - start_ts)) -ge "$HTTP_WAIT_TIMEOUT" ]; then
      echo "[ERROR] HTTP manifest is not reachable through $iface."
      echo ""
      echo "Check VM1:"
      echo "  curl http://${SERVER_IP}:${HTTP_PORT}/manifest.json"
      echo "  ss -lntp | grep ${HTTP_PORT}"
      echo ""
      echo "Check VM2:"
      echo "  curl --interface $iface http://${SERVER_IP}:${HTTP_PORT}/manifest.json"
      echo "  ping -I $iface $SERVER_IP -c 10"
      exit 1
    fi

    echo "[VM2] Manifest not ready yet through $iface. Waiting..."
    sleep 5
  done
}

stop_ues() {
  echo "[VM2] Stopping UE processes..."

  sudo pkill -TERM -f "free-ran-ue ue" >/dev/null 2>&1 || true

  sleep 3

  sudo pkill -KILL -f "free-ran-ue ue" >/dev/null 2>&1 || true

  echo "[VM2] Removing old ueTun interfaces if they remain..."

  for i in 0 1 2 3 4 5 6 7 8 9; do
    if ip link show "ueTun${i}" >/dev/null 2>&1; then
      sudo ip link del "ueTun${i}" >/dev/null 2>&1 || true
    fi
  done
}

cleanup_on_exit() {
  if [ "${STOP_UES_ON_EXIT:-1}" = "1" ]; then
    echo "[VM2] Cleanup on exit: stopping UEs. gNB remains running."
    stop_ues || true
  fi
}

start_gnb_if_needed() {
  if pgrep -af "free-ran-ue gnb" >/dev/null 2>&1; then
    echo "[VM2] gNB is already running."
    return 0
  fi

  echo "[VM2] Starting gNB..."

  cd "$RAN_DIR"

  sudo nohup ./build/free-ran-ue gnb -c "$GNB_CONFIG" > "$LOG_DIR/gnb.log" 2>&1 &

  echo $! | sudo tee "$PID_DIR/gnb.pid" >/dev/null

  sleep 5

  if ! pgrep -af "free-ran-ue gnb" >/dev/null 2>&1; then
    echo "[ERROR] gNB did not start."
    echo "Check log:"
    echo "  tail -f $LOG_DIR/gnb.log"
    exit 1
  fi

  echo "[VM2] gNB started."
}

start_ues_np() {
  local count="$1"

  stop_ues

  echo "[VM2] Starting ${count} UE(s) once with:"
  echo "sudo ./build/free-ran-ue ue -c $UE_CONFIG -n $count -p $count"

  cd "$RAN_DIR"

  sudo nohup ./build/free-ran-ue ue -c "$UE_CONFIG" -n "$count" -p "$count" > "$LOG_DIR/ue_current.log" 2>&1 &

  echo $! | sudo tee "$PID_DIR/ue_current.pid" >/dev/null

  wait_for_ue_tunnels "$count" 180
}

run_measurement() {
  local bw_label="$1"
  local scenario_name="$2"
  local ue_count="$3"

  local ue_ifaces
  ue_ifaces=$(make_ue_ifaces "$ue_count")

  local monitor_ifaces="${ue_ifaces},${RAN_INTF},ran0"
  local tcpdump_ifaces="${ue_ifaces},${RAN_INTF},ran0"

  local experiment_id="${bw_label}_${scenario_name}_${EXPERIMENT_SUFFIX}"
  local out_json="$OUT_DIR/vm2_ue_${experiment_id}.json"
  local csv_dir="$OUT_DIR/vm2_ue_${experiment_id}_csv"

  mkdir -p "$csv_dir"

  # ============================================================
  # Traceability table for active UE tunnels
  # ============================================================

  TRACE_FILE="$csv_dir/ue_tunnel_traceability.csv"

  echo "experiment_id,scenario,ue_count,iface,ip,server_ip,http_port" > "$TRACE_FILE"

  for i in $(seq 0 $((ue_count - 1))); do
    iface="ueTun${i}"
    ip_addr=$(ip -4 -o addr show dev "$iface" | awk '{print $4}' | cut -d/ -f1)

    echo "${experiment_id},${scenario_name},${ue_count},${iface},${ip_addr},${SERVER_IP},${HTTP_PORT}" >> "$TRACE_FILE"
  done

  echo "[VM2] Traceability saved to $TRACE_FILE"

  echo "============================================================"
  echo "[VM2] Running measurement"
  echo "BW label:       $bw_label"
  echo "Scenario:       $scenario_name"
  echo "UE count used:  $ue_count"
  echo "UE ifaces used: $ue_ifaces"
  echo "Experiment ID:  $experiment_id"
  echo "Output JSON:    $out_json"
  echo "CSV dir:        $csv_dir"
  echo "============================================================"

  # All MAX_UES are registered, but this measurement only uses
  # the first N interfaces according to the scenario.
  sudo python3 "$CLIENT_SCRIPT" \
    --experiment_id "$experiment_id" \
    --server_ip "$SERVER_IP" \
    --http_port "$HTTP_PORT" \
    --ue_ifaces "$ue_ifaces" \
    --clients "$ue_count" \
    --download_scenarios "all" \
    --scenario_label_override "$scenario_name" \
    --runs "$RUNS" \
    --max_files "$MAX_FILES" \
    --iperf_base_port "$IPERF_BASE_PORT" \
    --iperf_sec "$IPERF_SEC" \
    --ping_count "$PING_COUNT" \
    --monitor_ifaces "$monitor_ifaces" \
    --tcpdump_ifaces "$tcpdump_ifaces" \
    --out "$out_json" \
    --csv_dir "$csv_dir"
}

# ============================================================
# Validations
# ============================================================

sudo -v

if [ ! -d "$RAN_DIR" ]; then
  echo "[ERROR] No existe RAN_DIR: $RAN_DIR"
  exit 1
fi

if [ ! -f "$CLIENT_SCRIPT" ]; then
  echo "[ERROR] No se encontró CLIENT_SCRIPT: $CLIENT_SCRIPT"
  echo "Verifica la ruta en CLIENT_SCRIPT."
  exit 1
fi

if [ ! -f "$RAN_DIR/$GNB_CONFIG" ]; then
  echo "[ERROR] No existe gNB config: $RAN_DIR/$GNB_CONFIG"
  exit 1
fi

if [ ! -f "$RAN_DIR/$UE_CONFIG" ]; then
  echo "[ERROR] No existe UE config: $RAN_DIR/$UE_CONFIG"
  exit 1
fi

mkdir -p "$OUT_DIR" "$LOG_DIR" "$PID_DIR"

trap cleanup_on_exit EXIT

echo "============================================================"
echo "VM2 Full Script WITHOUT setup-ran0.sh"
echo "RAN dir:        $RAN_DIR"
echo "Server IP:      $SERVER_IP"
echo "HTTP port:      $HTTP_PORT"
echo "RAN intf:       $RAN_INTF"
echo "UE config:      $UE_CONFIG"
echo "MAX UEs:        $MAX_UES"
echo "BW labels:      ${BW_LABELS[*]}"
echo "Scenarios:      ${SCENARIOS[*]}"
echo "Ping max loss:  ${PING_MAX_LOSS_PCT}%"
echo "Output dir:     $OUT_DIR"
echo "============================================================"

warn_if_ran0_missing

# ============================================================
# Start gNB once
# ============================================================

start_gnb_if_needed

# ============================================================
# Start all UEs once
# ============================================================

echo "============================================================"
echo "[VM2] Starting MAX_UES once before running all scenarios"
echo "MAX_UES: $MAX_UES"
echo "============================================================"

start_ues_np "$MAX_UES"

echo "============================================================"
echo "[VM2] All UE tunnels created:"
ip -br a | grep ueTun || true
echo "============================================================"

# ============================================================
# Main experiment loop
# ============================================================

for bw_label in "${BW_LABELS[@]}"; do
  echo "============================================================"
  echo "[VM2] BW profile label: $bw_label"
  echo "Make sure VM1 is currently running the same BW profile."
  echo "============================================================"

  if [ "$MANUAL_SYNC" = "1" ]; then
    read -rp "Press ENTER when VM1 is ready for $bw_label..."
  else
    echo "[VM2] MANUAL_SYNC=0. The script will wait for HTTP manifest automatically."
  fi

  # Wait until VM1 HTTP server for this profile is reachable.
  check_http_manifest "ueTun0"

  # Validate all tunnels once per bandwidth profile.
  # This avoids measuring if one tunnel lost connectivity.
  wait_for_ping_quality_all_ifaces "$MAX_UES"

  for item in "${SCENARIOS[@]}"; do
    scenario_name="${item%%:*}"
    ue_count="${item##*:}"

    echo "============================================================"
    echo "[VM2] Scenario: $scenario_name | active measurement tunnels: $ue_count"
    echo "[VM2] UEs remain registered; only first $ue_count tunnels generate traffic."
    echo "============================================================"

    run_measurement "$bw_label" "$scenario_name" "$ue_count"

    echo "[VM2] Finished scenario $scenario_name for $bw_label"
  done
done

echo "============================================================"
echo "[OK] VM2 all measurements finished"
echo "Results: $OUT_DIR"
echo "============================================================"
