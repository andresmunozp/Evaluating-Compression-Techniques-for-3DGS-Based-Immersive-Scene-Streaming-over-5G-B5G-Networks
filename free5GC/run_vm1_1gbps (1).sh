#!/usr/bin/env bash

set -euo pipefail

# ============================================================
# VM1 - free5GC Core + DN/Streaming Server Orchestrator
# Topology:
#   enp0s3 = NAT / Internet / DHCP
#   enp0s8 = 10.0.1.1/24, link to VM2 RAN/UE
# ============================================================

CORE_DIR="$HOME/free5gc"
ORCHESTRATOR="./free5gc_vm1_orchestrator.py"
SERVER_SCRIPT="./free5gc_vm1_core_dn_server_monitor_csv.py"

# Interface used by free5GC for N2/N3 toward VM2.
# If your free5GC currently only works with enp0s3, change this to enp0s3.
RELOAD_INTF="enp0s8"

BIND_IP="10.100.0.10"
HTTP_SRC_DIR="$HOME/streaming_files"
HTTP_PORT="8080"
MAX_FILES="300"

IPERF_BASE_PORT="5201"
IPERF_SERVERS="6"

# Current simple DN/HTTP server uses 10.100.0.10 on lo.
# If later you create a real DN interface, replace lo with that interface.
TC_SPEC="lo"

# Network profiles
BWS="1000,10000,100000"
LABELS="1gbps,10gbps,100gbps"

DELAY_MS="2"
JITTER_MS="0.2"
LOSS_PCT="0.01"

# Monitor both NAT and VM1<->VM2 link. lo is included because DN server is bound there.
MONITOR_SPECS="lo,enp0s3,enp0s8"
TCPDUMP_SPECS="lo,enp0s3,enp0s8"

DURATION="900"
EXPERIMENT_SUFFIX="6ues_all_scenarios"

OUT_DIR="$HOME/free5gc_results_vm1"
LOG_DIR="$HOME/free5gc_logs"

# ============================================================
# Validations
# ============================================================

if [ ! -f "$ORCHESTRATOR" ]; then
  echo "[ERROR] No se encontró: $ORCHESTRATOR"
  echo "Ejecuta este .sh desde la carpeta donde está free5gc_vm1_orchestrator.py"
  exit 1
fi

if [ ! -f "$SERVER_SCRIPT" ]; then
  echo "[ERROR] No se encontró: $SERVER_SCRIPT"
  echo "Debe estar en la misma carpeta o cambia SERVER_SCRIPT."
  exit 1
fi

if [ ! -d "$CORE_DIR" ]; then
  echo "[ERROR] No existe CORE_DIR: $CORE_DIR"
  exit 1
fi

if [ ! -d "$HTTP_SRC_DIR" ]; then
  echo "[ERROR] No existe HTTP_SRC_DIR: $HTTP_SRC_DIR"
  echo "Crea la carpeta y pon ahí los archivos de streaming."
  exit 1
fi

mkdir -p "$OUT_DIR" "$LOG_DIR"

echo "============================================================"
echo "VM1 Orchestrator"
echo "Core dir:       $CORE_DIR"
echo "Reload intf:    $RELOAD_INTF"
echo "Bind IP:        $BIND_IP"
echo "HTTP src dir:   $HTTP_SRC_DIR"
echo "BW profiles:    $BWS"
echo "Monitor specs:  $MONITOR_SPECS"
echo "TCPDump specs:  $TCPDUMP_SPECS"
echo "Output dir:     $OUT_DIR"
echo "============================================================"

sudo python3 "$ORCHESTRATOR" \
  --core_dir "$CORE_DIR" \
  --reload_intf "$RELOAD_INTF" \
  --server_script "$SERVER_SCRIPT" \
  --bind_ip "$BIND_IP" \
  --http_src_dir "$HTTP_SRC_DIR" \
  --http_port "$HTTP_PORT" \
  --max_files "$MAX_FILES" \
  --iperf_base_port "$IPERF_BASE_PORT" \
  --iperf_servers "$IPERF_SERVERS" \
  --tc_spec "$TC_SPEC" \
  --bws "$BWS" \
  --labels "$LABELS" \
  --delay_ms "$DELAY_MS" \
  --jitter_ms "$JITTER_MS" \
  --loss_pct "$LOSS_PCT" \
  --monitor_specs "$MONITOR_SPECS" \
  --tcpdump_specs "$TCPDUMP_SPECS" \
  --duration "$DURATION" \
  --experiment_suffix "$EXPERIMENT_SUFFIX" \
  --out_dir "$OUT_DIR" \
  --log_dir "$LOG_DIR"

echo "============================================================"
echo "[OK] VM1 orchestrator finished"
echo "Results: $OUT_DIR"
echo "============================================================"