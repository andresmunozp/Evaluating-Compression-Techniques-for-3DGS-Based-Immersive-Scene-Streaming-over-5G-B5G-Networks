#!/usr/bin/env python3
"""
free5gc_vm1_orchestrator.py

VM1 orchestrator for a free5GC experiment.
Runs on the Core/DN VM.

It can:
1) Optionally run reload_host_config.sh.
2) Start free5GC Core in background with logs.
3) Run the VM1 DN/server/monitor CSV script once per bandwidth profile.

Expected dependency in the same folder or given by --server_script:
  free5gc_vm1_core_dn_server_monitor_csv.py

Recommended use:
  sudo python3 free5gc_vm1_orchestrator.py \
    --reload_intf enp0s3 \
    --bind_ip 10.100.0.10 \
    --http_src_dir ~/streaming_files \
    --tc_spec lo \
    --monitor_specs lo,enp0s3 \
    --tcpdump_specs lo,enp0s3 \
    --duration 900
"""

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple


PIDS_TO_STOP: List[int] = []


def expand(p: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(p))).resolve())


def run(cmd: str, cwd: str = "", timeout: int = 0, check: bool = True) -> Tuple[int, str]:
    print(f"\n[CMD] {cmd}")
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd or None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout if timeout and timeout > 0 else None,
        )
        if p.stdout:
            print(p.stdout)
        if check and p.returncode != 0:
            raise RuntimeError(f"Command failed rc={p.returncode}: {cmd}\n{p.stdout}")
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        print(out)
        if check:
            raise RuntimeError(f"Command timed out: {cmd}\n{out}")
        return 124, out


def start_background(cmd: str, log_file: str, cwd: str = "") -> int:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n[BG] {cmd}")
    print(f"[LOG] {log_file}")
    f = open(log_file, "ab", buffering=0)
    p = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd or None,
        stdout=f,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    PIDS_TO_STOP.append(p.pid)
    return p.pid


def infer_labels(bws: List[float]) -> List[str]:
    labels = []
    for bw in bws:
        if bw >= 1000 and abs(bw % 1000) < 1e-9:
            labels.append(f"{int(bw/1000)}gbps")
        else:
            labels.append(f"{int(bw)}mbps")
    return labels


def parse_bws(s: str) -> List[float]:
    out = []
    for x in s.split(","):
        x = x.strip()
        if x:
            out.append(float(x))
    if not out:
        raise ValueError("--bws cannot be empty")
    return out


def stop_bg() -> None:
    for pid in reversed(PIDS_TO_STOP):
        try:
            print(f"[STOP] process group pid={pid}")
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()

    # free5GC Core lifecycle
    ap.add_argument("--core_dir", default="~/free5gc")
    ap.add_argument("--reload_intf", default="", help="Interface passed to reload_host_config.sh, e.g., enp0s3. Empty = skip reload.")
    ap.add_argument("--skip_core", action="store_true", help="Do not start ./run.sh. Use this if free5GC is already running.")
    ap.add_argument("--core_wait_s", type=int, default=25)
    ap.add_argument("--stop_core_on_exit", action="store_true")

    # Dependency script
    ap.add_argument("--server_script", default="./free5gc_vm1_core_dn_server_monitor_csv.py")

    # Experiment profiles
    ap.add_argument("--bws", default="1000,10000,100000", help="Comma-separated Mbps profiles.")
    ap.add_argument("--labels", default="", help="Optional comma labels, e.g., 1gbps,10gbps,100gbps")
    ap.add_argument("--experiment_suffix", default="6ues_all_scenarios")
    ap.add_argument("--duration", type=float, default=900.0, help="Seconds per bandwidth profile.")
    ap.add_argument("--out_dir", default="~/free5gc_results_vm1")
    ap.add_argument("--log_dir", default="~/free5gc_logs")

    # VM1 server/monitor args
    ap.add_argument("--bind_ip", default="10.100.0.10")
    ap.add_argument("--server_netns", default="")
    ap.add_argument("--http_src_dir", default="~/streaming_files")
    ap.add_argument("--http_port", type=int, default=8080)
    ap.add_argument("--max_files", type=int, default=300)
    ap.add_argument("--iperf_base_port", type=int, default=5201)
    ap.add_argument("--iperf_servers", type=int, default=6)
    ap.add_argument("--tc_spec", default="lo", help="Interface to shape, e.g., lo, veth-dn, veth-dn@dn")
    ap.add_argument("--delay_ms", type=float, default=2.0)
    ap.add_argument("--jitter_ms", type=float, default=0.2)
    ap.add_argument("--loss_pct", type=float, default=0.01)
    ap.add_argument("--clear_tc_on_exit", action="store_true")
    ap.add_argument("--monitor_specs", default="lo,enp0s3")
    ap.add_argument("--tcpdump_specs", default="lo,enp0s3")
    ap.add_argument("--sample_interval", type=float, default=1.0)

    args = ap.parse_args()

    core_dir = expand(args.core_dir)
    server_script = expand(args.server_script)
    out_dir = Path(expand(args.out_dir))
    log_dir = Path(expand(args.log_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    bws = parse_bws(args.bws)
    labels = [x.strip() for x in args.labels.split(",") if x.strip()] if args.labels else infer_labels(bws)
    if len(labels) != len(bws):
        raise ValueError("--labels length must match --bws length")

    try:
        if args.reload_intf:
            reload_script = str(Path(core_dir) / "reload_host_config.sh")
            run(f"sudo {shlex.quote(reload_script)} {shlex.quote(args.reload_intf)}", check=True)

        if not args.skip_core:
            core_log = str(log_dir / "free5gc_core.log")
            pid = start_background("sudo ./run.sh", core_log, cwd=core_dir)
            pid_file = log_dir / "free5gc_core.pid"
            pid_file.write_text(str(pid), encoding="utf-8")
            print(f"[INFO] free5GC Core PID: {pid}")
            print(f"[INFO] Waiting {args.core_wait_s}s for Core startup...")
            time.sleep(args.core_wait_s)
        else:
            print("[INFO] --skip_core enabled. Assuming free5GC is already running.")

        for bw, label in zip(bws, labels):
            exp_id = f"{label}_{args.experiment_suffix}"
            json_out = out_dir / f"vm1_core_dn_{exp_id}.json"
            csv_dir = out_dir / f"vm1_core_dn_{exp_id}_csv"
            work_dir = out_dir / f"work_vm1_{exp_id}"
            tcpdump_dir = out_dir / f"pcap_vm1_{exp_id}"

            cmd = [
                "sudo", "python3", server_script,
                "--experiment_id", exp_id,
                "--bind_ip", args.bind_ip,
                "--http_src_dir", expand(args.http_src_dir),
                "--http_port", str(args.http_port),
                "--max_files", str(args.max_files),
                "--work_dir", str(work_dir),
                "--iperf_base_port", str(args.iperf_base_port),
                "--iperf_servers", str(args.iperf_servers),
                "--bw_mbps", str(bw),
                "--delay_ms", str(args.delay_ms),
                "--jitter_ms", str(args.jitter_ms),
                "--loss_pct", str(args.loss_pct),
                "--monitor_specs", args.monitor_specs,
                "--sample_interval", str(args.sample_interval),
                "--tcpdump_specs", args.tcpdump_specs,
                "--tcpdump_dir", str(tcpdump_dir),
                "--duration", str(args.duration),
                "--out", str(json_out),
                "--csv_dir", str(csv_dir),
            ]
            if args.server_netns:
                cmd.extend(["--server_netns", args.server_netns])
            if args.tc_spec:
                cmd.extend(["--tc_spec", args.tc_spec])
            if args.clear_tc_on_exit:
                cmd.append("--clear_tc_on_exit")

            print("\n" + "=" * 80)
            print(f"[PROFILE] {exp_id} | BW={bw} Mbps")
            print("[ACTION] Start the VM2 orchestrator if it is not already running.")
            print("=" * 80)
            run(" ".join(shlex.quote(str(x)) for x in cmd), check=True)

        print("\n[OK] VM1 profiles finished.")

    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Ctrl+C received.")
    finally:
        if args.stop_core_on_exit:
            stop_bg()
        else:
            print("[INFO] Core was not stopped. Use pkill/run.sh terminal if you want to stop it.")


if __name__ == "__main__":
    main()
