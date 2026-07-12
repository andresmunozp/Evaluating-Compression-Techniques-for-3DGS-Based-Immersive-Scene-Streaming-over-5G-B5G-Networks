#!/usr/bin/env python3
"""
free5gc_vm2_orchestrator.py

VM2 orchestrator for a free5GC experiment.
Runs on the UE/RAN VM.

It can:
1) Run setup-ran0.sh once.
2) Start gNB in background.
3) Start N UE processes in background using config/ue1.yaml...config/ueN.yaml.
4) Wait until ueTun0...ueTunN-1 are present.
5) Run the VM2 UE/RAN client measurement CSV script once per bandwidth profile.

Expected dependency in the same folder or given by --client_script:
  free5gc_vm2_ue_client_measure_csv.py

Recommended use:
  sudo python3 free5gc_vm2_orchestrator.py \
    --server_ip 10.100.0.10 \
    --ue_count 6 \
    --ran_intf enp0s3
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


def iface_exists(name: str) -> bool:
    return Path(f"/sys/class/net/{name}").exists()


def wait_for_ifaces(ifaces: List[str], timeout_s: int, poll_s: float = 2.0) -> None:
    deadline = time.time() + timeout_s
    missing = list(ifaces)
    while time.time() < deadline:
        missing = [x for x in ifaces if not iface_exists(x)]
        if not missing:
            print(f"[OK] All UE tunnel interfaces are present: {', '.join(ifaces)}")
            return
        print(f"[WAIT] Missing UE tunnels: {', '.join(missing)}")
        time.sleep(poll_s)
    raise RuntimeError(f"Timeout waiting for UE tunnels: {', '.join(missing)}")


def stop_bg() -> None:
    for pid in reversed(PIDS_TO_STOP):
        try:
            print(f"[STOP] process group pid={pid}")
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()

    # RAN lifecycle
    ap.add_argument("--ran_dir", default="~/free-ran-ue")
    ap.add_argument("--setup_ran", default="/usr/local/bin/setup-ran0.sh")
    ap.add_argument("--skip_setup", action="store_true")
    ap.add_argument("--skip_gnb", action="store_true", help="Use this if gNB is already running.")
    ap.add_argument("--skip_ues", action="store_true", help="Use this if UEs are already running.")
    ap.add_argument("--kill_existing", action="store_true", help="Kill existing free-ran-ue processes before starting.")
    ap.add_argument("--gnb_config", default="config/gnb.yaml")
    ap.add_argument("--ue_count", type=int, default=6)
    ap.add_argument("--ue_config_pattern", default="config/ue{}.yaml", help="Pattern with {}, e.g., config/ue{}.yaml")
    ap.add_argument("--ue_start_gap_s", type=float, default=3.0)
    ap.add_argument("--wait_tunnels_s", type=int, default=180)
    ap.add_argument("--stop_ran_on_exit", action="store_true")

    # Dependency script
    ap.add_argument("--client_script", default="./free5gc_vm2_ue_client_measure_csv.py")

    # Experiment profiles
    ap.add_argument("--bws", default="1000,10000,100000", help="Comma-separated Mbps profiles. Used only for labels/order; shaping is done on VM1.")
    ap.add_argument("--labels", default="", help="Optional comma labels, e.g., 1gbps,10gbps,100gbps")
    ap.add_argument("--experiment_suffix", default="6ues_all_scenarios")
    ap.add_argument("--out_dir", default="~/free5gc_results_vm2")
    ap.add_argument("--log_dir", default="~/ran_logs")
    ap.add_argument("--between_profiles_s", type=float, default=5.0)

    # VM2 measurement args
    ap.add_argument("--server_ip", default="10.100.0.10")
    ap.add_argument("--http_port", type=int, default=8080)
    ap.add_argument("--clients", type=int, default=6)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--download_scenarios", default="one,half,all")
    ap.add_argument("--scenario_select", choices=["first", "random"], default="first")
    ap.add_argument("--iperf_base_port", type=int, default=5201)
    ap.add_argument("--iperf_sec", type=int, default=10)
    ap.add_argument("--udp", action="store_true")
    ap.add_argument("--udp_bw", default="100M")
    ap.add_argument("--ping_count", type=int, default=20)
    ap.add_argument("--max_files", type=int, default=300)
    ap.add_argument("--ran_intf", default="enp0s3", help="VM2 interface toward VM1, included in monitor/tcpdump.")
    ap.add_argument("--sample_interval", type=float, default=1.0)

    args = ap.parse_args()

    ran_dir = expand(args.ran_dir)
    client_script = expand(args.client_script)
    out_dir = Path(expand(args.out_dir))
    log_dir = Path(expand(args.log_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    bws = parse_bws(args.bws)
    labels = [x.strip() for x in args.labels.split(",") if x.strip()] if args.labels else infer_labels(bws)
    if len(labels) != len(bws):
        raise ValueError("--labels length must match --bws length")

    ue_ifaces = [f"ueTun{i}" for i in range(args.ue_count)]
    ue_ifaces_csv = ",".join(ue_ifaces)
    monitor_ifaces = ",".join(ue_ifaces + ([args.ran_intf] if args.ran_intf else []))
    tcpdump_ifaces = monitor_ifaces

    try:
        if args.kill_existing:
            run("sudo pkill -f free-ran-ue || true", check=False)
            time.sleep(2)

        if not args.skip_setup:
            run(f"sudo {shlex.quote(expand(args.setup_ran))}", check=True)
        else:
            print("[INFO] --skip_setup enabled.")

        if not args.skip_gnb:
            gnb_cfg = args.gnb_config
            gnb_log = str(log_dir / "gnb.log")
            pid = start_background(f"sudo ./build/free-ran-ue gnb -c {shlex.quote(gnb_cfg)}", gnb_log, cwd=ran_dir)
            (log_dir / "gnb.pid").write_text(str(pid), encoding="utf-8")
            print(f"[INFO] gNB PID: {pid}")
            time.sleep(5)
        else:
            print("[INFO] --skip_gnb enabled.")

        if not args.skip_ues:
            for i in range(1, args.ue_count + 1):
                ue_cfg = args.ue_config_pattern.format(i)
                ue_log = str(log_dir / f"ue{i}.log")
                pid = start_background(f"sudo ./build/free-ran-ue ue -c {shlex.quote(ue_cfg)}", ue_log, cwd=ran_dir)
                (log_dir / f"ue{i}.pid").write_text(str(pid), encoding="utf-8")
                print(f"[INFO] UE{i} PID: {pid}")
                time.sleep(args.ue_start_gap_s)
        else:
            print("[INFO] --skip_ues enabled.")

        wait_for_ifaces(ue_ifaces, timeout_s=args.wait_tunnels_s)
        run("ip -br a | grep ueTun || true", check=False)

        for label in labels:
            exp_id = f"{label}_{args.experiment_suffix}"
            json_out = out_dir / f"vm2_ue_{exp_id}.json"
            csv_dir = out_dir / f"vm2_ue_{exp_id}_csv"
            work_dir = out_dir / f"work_vm2_{exp_id}"
            tcpdump_dir = out_dir / f"pcap_vm2_{exp_id}"

            cmd = [
                "python3", client_script,
                "--experiment_id", exp_id,
                "--server_ip", args.server_ip,
                "--http_port", str(args.http_port),
                "--ue_ifaces", ue_ifaces_csv,
                "--clients", str(args.clients),
                "--runs", str(args.runs),
                "--download_scenarios", args.download_scenarios,
                "--scenario_select", args.scenario_select,
                "--iperf_base_port", str(args.iperf_base_port),
                "--iperf_sec", str(args.iperf_sec),
                "--udp_bw", args.udp_bw,
                "--ping_count", str(args.ping_count),
                "--max_files", str(args.max_files),
                "--monitor_ifaces", monitor_ifaces,
                "--sample_interval", str(args.sample_interval),
                "--tcpdump_ifaces", tcpdump_ifaces,
                "--tcpdump_dir", str(tcpdump_dir),
                "--work_dir", str(work_dir),
                "--out", str(json_out),
                "--csv_dir", str(csv_dir),
            ]
            if args.udp:
                cmd.append("--udp")

            print("\n" + "=" * 80)
            print(f"[PROFILE] {exp_id}")
            print("[ACTION] Make sure the VM1 orchestrator is currently running the same profile.")
            print("=" * 80)
            run(" ".join(shlex.quote(str(x)) for x in cmd), check=True)

            if args.between_profiles_s > 0:
                print(f"[INFO] Waiting {args.between_profiles_s}s before next profile...")
                time.sleep(args.between_profiles_s)

        print("\n[OK] VM2 profiles finished.")

    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Ctrl+C received.")
    finally:
        if args.stop_ran_on_exit:
            stop_bg()
        else:
            print("[INFO] RAN/UE processes were not stopped. Use --stop_ran_on_exit or pkill if needed.")


if __name__ == "__main__":
    main()
