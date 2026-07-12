#!/usr/bin/env python3
"""
free5gc_vm1_core_dn_server_monitor.py

VM1 script for a free5GC experiment:
- Runs on the Core/DN machine.
- Starts the DN streaming HTTP server.
- Starts iperf3 servers.
- Optionally applies tc shaping on a selected N6/N3 interface.
- Optionally captures tcpdump on selected interfaces.
- Monitors interface counters, CPU, memory, and tc configuration.
- Writes a JSON file that can be compared with the VM2 UE/RAN client results.

Interface specification format:
  ens33              -> interface in the current namespace
  veth-dn@dn         -> interface veth-dn inside Linux namespace "dn"
"""

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def now_ms() -> int:
    return int(time.time() * 1000)


def run_cmd(cmd: str, timeout: int = 30, check: bool = False) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        if check and p.returncode != 0:
            raise RuntimeError(f"Command failed rc={p.returncode}: {cmd}\n{p.stdout}")
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + "\n[TIMEOUT]\n"
        if check:
            raise RuntimeError(f"Timeout: {cmd}\n{out}")
        return 124, out


class IfaceSpec:
    def __init__(self, intf: str, netns: str = ""):
        self.intf = intf
        self.netns = netns

    @property
    def label(self) -> str:
        return f"{self.intf}@{self.netns}" if self.netns else self.intf

    @property
    def prefix(self) -> str:
        return f"ip netns exec {self.netns} " if self.netns else ""


def parse_iface_spec(spec: str) -> IfaceSpec:
    spec = spec.strip()
    if not spec:
        raise ValueError("Empty interface specification")
    if "@" in spec:
        intf, ns = spec.split("@", 1)
        return IfaceSpec(intf=intf.strip(), netns=ns.strip())
    return IfaceSpec(intf=spec)


def parse_iface_specs(s: str) -> List[IfaceSpec]:
    if not s:
        return []
    return [parse_iface_spec(x) for x in s.split(",") if x.strip()]


def ns_prefix(netns: str) -> str:
    return f"ip netns exec {netns} " if netns else ""


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


IFACE_COUNTERS = [
    "rx_bytes", "tx_bytes",
    "rx_packets", "tx_packets",
    "rx_errors", "tx_errors",
    "rx_dropped", "tx_dropped",
]


def read_iface_stats(spec: IfaceSpec) -> Dict[str, Optional[int]]:
    stats = {}
    for c in IFACE_COUNTERS:
        cmd = f"{spec.prefix}cat /sys/class/net/{spec.intf}/statistics/{c} 2>/dev/null"
        rc, out = run_cmd(cmd, timeout=5)
        if rc == 0 and out.strip().isdigit():
            stats[c] = int(out.strip())
        else:
            stats[c] = None
    return stats


def read_all_iface_stats(specs: List[IfaceSpec]) -> Dict[str, Dict[str, Optional[int]]]:
    return {s.label: read_iface_stats(s) for s in specs}


def diff_iface_stats(before: Dict, after: Dict) -> Dict:
    out = {}
    for label, bstats in before.items():
        astats = after.get(label, {})
        out[label] = {}
        for k, bv in bstats.items():
            av = astats.get(k)
            out[label][k] = None if bv is None or av is None else av - bv
    return out


def read_meminfo() -> Dict[str, Optional[int]]:
    keys = {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"}
    out = {k: None for k in keys}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0].rstrip(":") in keys:
                    out[parts[0].rstrip(":")] = int(parts[1])  # kB
    except Exception:
        pass
    return out


def read_cpu_times() -> Optional[List[int]]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline().split()
        if first[0] != "cpu":
            return None
        return [int(x) for x in first[1:]]
    except Exception:
        return None


def cpu_usage_percent(prev: Optional[List[int]], cur: Optional[List[int]]) -> Optional[float]:
    if not prev or not cur:
        return None
    prev_idle = prev[3] + (prev[4] if len(prev) > 4 else 0)
    cur_idle = cur[3] + (cur[4] if len(cur) > 4 else 0)
    total_delta = sum(cur) - sum(prev)
    idle_delta = cur_idle - prev_idle
    if total_delta <= 0:
        return None
    return 100.0 * (1.0 - idle_delta / total_delta)


class Sampler:
    def __init__(self, iface_specs: List[IfaceSpec], interval_s: float):
        self.iface_specs = iface_specs
        self.interval_s = interval_s
        self.samples: List[Dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="vm1_sampler", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2, self.interval_s + 1))

    def _loop(self):
        prev_cpu = read_cpu_times()
        while not self._stop.is_set():
            time.sleep(self.interval_s)
            cur_cpu = read_cpu_times()
            loadavg = os.getloadavg() if hasattr(os, "getloadavg") else None
            self.samples.append({
                "ts_ms": now_ms(),
                "cpu_usage_pct": cpu_usage_percent(prev_cpu, cur_cpu),
                "loadavg": list(loadavg) if loadavg else None,
                "meminfo_kb": read_meminfo(),
                "interfaces": read_all_iface_stats(self.iface_specs),
            })
            prev_cpu = cur_cpu


def prepare_http_content(src_dir: str, serve_dir: str, max_files: int) -> List[str]:
    src = Path(src_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"--http_src_dir does not exist or is not a directory: {src_dir}")

    serve = Path(serve_dir)
    if serve.exists():
        shutil.rmtree(serve)
    serve.mkdir(parents=True, exist_ok=True)

    files = []
    for root, _, names in os.walk(src):
        for name in names:
            p = Path(root) / name
            if p.is_file():
                files.append(p)
    files.sort(key=lambda p: str(p))

    chosen = files[:max_files] if max_files > 0 else files
    copied_names = []
    for i, p in enumerate(chosen):
        out_name = f"{i:05d}_{p.name}"
        shutil.copy2(p, serve / out_name)
        copied_names.append(out_name)

    manifest = {"created_ts_ms": now_ms(), "file_count": len(copied_names), "files": copied_names}
    with open(serve / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return copied_names


def write_threaded_http_script(script_path: str, serve_dir: str, bind_ip: str, port: int) -> None:
    content = f"""#!/usr/bin/env python3
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

os.chdir({serve_dir!r})
server = ThreadedHTTPServer(({bind_ip!r}, {port}), SimpleHTTPRequestHandler)
print("HTTP server listening on {bind_ip}:{port}, serving {serve_dir}", flush=True)
server.serve_forever()
"""
    Path(script_path).write_text(content, encoding="utf-8")
    os.chmod(script_path, 0o755)


def start_http_server(server_netns: str, serve_dir: str, bind_ip: str, port: int, log_path: str) -> subprocess.Popen:
    http_script = f"/tmp/free5gc_threaded_http_{port}_{os.getpid()}.py"
    write_threaded_http_script(http_script, serve_dir, bind_ip, port)
    cmd = f"{ns_prefix(server_netns)}python3 {http_script}"
    log = open(log_path, "w", encoding="utf-8")
    return subprocess.Popen(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)


def start_iperf_servers(server_netns: str, bind_ip: str, base_port: int, count: int) -> List[Dict]:
    started = []
    for i in range(count):
        port = base_port + i
        cmd = f"{ns_prefix(server_netns)}iperf3 -s -B {bind_ip} -p {port} -D"
        rc, out = run_cmd(cmd, timeout=10)
        started.append({"port": port, "rc": rc, "output": out.strip()})
        time.sleep(0.1)
    return started


def stop_iperf_servers(server_netns: str, base_port: int, count: int) -> None:
    for i in range(count):
        port = base_port + i
        run_cmd(f"{ns_prefix(server_netns)}pkill -f 'iperf3 -s .* -p {port}' >/dev/null 2>&1 || true", timeout=5)


def apply_tc_htb_netem(spec: IfaceSpec, bw_mbps: float, delay_ms: float, jitter_ms: float, loss_pct: float) -> Dict:
    run_cmd(f"{spec.prefix}tc qdisc del dev {spec.intf} root >/dev/null 2>&1 || true", timeout=5)
    run_cmd(f"{spec.prefix}tc qdisc add dev {spec.intf} root handle 1: htb default 10", timeout=10, check=True)
    run_cmd(
        f"{spec.prefix}tc class add dev {spec.intf} parent 1: classid 1:10 "
        f"htb rate {bw_mbps}mbit ceil {bw_mbps}mbit",
        timeout=10,
        check=True,
    )

    if jitter_ms and jitter_ms > 0:
        delay_part = f"delay {delay_ms}ms {jitter_ms}ms distribution normal"
    else:
        delay_part = f"delay {delay_ms}ms"

    run_cmd(
        f"{spec.prefix}tc qdisc add dev {spec.intf} parent 1:10 handle 10: "
        f"netem {delay_part} loss {loss_pct}%",
        timeout=10,
        check=True,
    )
    rc, qdisc = run_cmd(f"{spec.prefix}tc qdisc show dev {spec.intf}", timeout=5)
    return {
        "interface": spec.label,
        "bw_mbps": bw_mbps,
        "delay_ms": delay_ms,
        "jitter_ms": jitter_ms,
        "loss_pct": loss_pct,
        "qdisc": qdisc.strip(),
    }


def clear_tc(spec: IfaceSpec) -> None:
    run_cmd(f"{spec.prefix}tc qdisc del dev {spec.intf} root >/dev/null 2>&1 || true", timeout=5)


def show_tc(spec: IfaceSpec) -> str:
    rc, out = run_cmd(f"{spec.prefix}tc qdisc show dev {spec.intf}", timeout=5)
    return out.strip()


def start_tcpdump(spec: IfaceSpec, out_dir: str, experiment_id: str) -> Dict:
    ensure_dir(out_dir)
    safe_label = spec.label.replace("/", "_").replace("@", "_")
    pcap_path = str(Path(out_dir) / f"{experiment_id}_{safe_label}.pcap")
    log_path = str(Path(out_dir) / f"{experiment_id}_{safe_label}.tcpdump.log")
    cmd = f"{spec.prefix}tcpdump -i {spec.intf} -s 0 -w {pcap_path}"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
    return {"interface": spec.label, "pcap": pcap_path, "log": log_path, "proc": proc}


def stop_process_group(proc: subprocess.Popen, grace_s: float = 2.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        proc.wait(timeout=grace_s)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=grace_s)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass


# -------------------------
# CSV export helpers
# -------------------------
def flatten_dict(d, parent_key="", sep="__"):
    """Flatten nested dicts for CSV-friendly columns."""
    items = {}
    if not isinstance(d, dict):
        return {parent_key or "value": d}
    for k, v in d.items():
        key = f"{parent_key}{sep}{k}" if parent_key else str(k)
        if isinstance(v, dict):
            items.update(flatten_dict(v, key, sep=sep))
        elif isinstance(v, list):
            items[key] = json.dumps(v, ensure_ascii=False)
        else:
            items[key] = v
    return items


def write_csv(path: str, rows: List[Dict]) -> None:
    """Write rows to CSV with the union of all keys as columns."""
    ensure_dir(str(Path(path).parent))
    rows = rows or []
    if not rows:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    fieldnames = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def export_vm1_csv(result: Dict, csv_dir: str) -> Dict[str, str]:
    """Export VM1 Core/DN server-monitor JSON structure into CSV tables."""
    ensure_dir(csv_dir)
    base = {
        "experiment_id": result.get("experiment_id"),
        "vm_role": result.get("vm_role"),
        "started_ts_ms": result.get("started_ts_ms"),
        "finished_ts_ms": result.get("finished_ts_ms"),
        "duration_s": result.get("duration_s"),
    }

    summary_rows = [{
        **base,
        "bind_ip": result.get("config", {}).get("bind_ip"),
        "http_port": result.get("config", {}).get("http_port"),
        "http_file_count": result.get("http", {}).get("file_count"),
        "http_manifest_url": result.get("http", {}).get("manifest_url"),
        "http_served_dir": result.get("http", {}).get("served_dir"),
        "iperf_servers_count": result.get("config", {}).get("iperf_servers"),
        "iperf_base_port": result.get("config", {}).get("iperf_base_port"),
        "tc_spec": result.get("config", {}).get("tc_spec"),
        "bw_mbps": result.get("config", {}).get("bw_mbps"),
        "delay_ms": result.get("config", {}).get("delay_ms"),
        "jitter_ms": result.get("config", {}).get("jitter_ms"),
        "loss_pct": result.get("config", {}).get("loss_pct"),
        "errors": json.dumps(result.get("errors", []), ensure_ascii=False),
    }]

    http_rows = [{**base, **flatten_dict(result.get("http", {}))}]
    tc_rows = []
    if result.get("tc"):
        tc_rows.append({**base, **flatten_dict(result.get("tc", {}))})

    iperf_rows = []
    for srv in result.get("iperf", {}).get("servers", []) or []:
        iperf_rows.append({**base, **flatten_dict(srv)})

    tcpdump_rows = []
    for entry in result.get("tcpdump", []) or []:
        tcpdump_rows.append({**base, **flatten_dict(entry)})

    iface_delta_rows = []
    for iface, stats in result.get("monitoring", {}).get("iface_delta", {}).items():
        iface_delta_rows.append({**base, "iface": iface, **flatten_dict(stats)})

    sample_rows = []
    for sample_idx, sample in enumerate(result.get("monitoring", {}).get("samples", []) or []):
        sbase = {
            **base,
            "sample_idx": sample_idx,
            "sample_ts_ms": sample.get("ts_ms"),
            "cpu_usage_pct": sample.get("cpu_usage_pct"),
        }
        loadavg = sample.get("loadavg") or []
        if len(loadavg) >= 3:
            sbase.update({"loadavg_1m": loadavg[0], "loadavg_5m": loadavg[1], "loadavg_15m": loadavg[2]})
        mem = sample.get("meminfo_kb", {}) or {}
        sbase.update({f"mem_{k}_kb": v for k, v in mem.items()})
        for iface, stats in (sample.get("interfaces", {}) or {}).items():
            sample_rows.append({**sbase, "iface": iface, **flatten_dict(stats)})

    files = {
        "summary": str(Path(csv_dir) / "vm1_summary.csv"),
        "http": str(Path(csv_dir) / "vm1_http.csv"),
        "iperf_servers": str(Path(csv_dir) / "vm1_iperf_servers.csv"),
        "tc": str(Path(csv_dir) / "vm1_tc.csv"),
        "tcpdump": str(Path(csv_dir) / "vm1_tcpdump.csv"),
        "iface_delta": str(Path(csv_dir) / "vm1_interface_delta.csv"),
        "samples": str(Path(csv_dir) / "vm1_monitor_samples.csv"),
    }
    write_csv(files["summary"], summary_rows)
    write_csv(files["http"], http_rows)
    write_csv(files["iperf_servers"], iperf_rows)
    write_csv(files["tc"], tc_rows)
    write_csv(files["tcpdump"], tcpdump_rows)
    write_csv(files["iface_delta"], iface_delta_rows)
    write_csv(files["samples"], sample_rows)
    return files


def main():
    ap = argparse.ArgumentParser(description="VM1 Core/DN server and monitor for free5GC streaming experiments.")

    ap.add_argument("--experiment_id", required=True, help="Shared experiment id, e.g., 1gbps_all_run01")
    ap.add_argument("--bind_ip", required=True, help="DN server IP, e.g., 10.100.0.10")
    ap.add_argument("--server_netns", default="", help="Namespace where HTTP/iperf should run, e.g., dn. Empty = current namespace.")

    ap.add_argument("--http_src_dir", required=True, help="Directory with streaming files or segments.")
    ap.add_argument("--http_port", type=int, default=8080)
    ap.add_argument("--max_files", type=int, default=300)
    ap.add_argument("--work_dir", default="/tmp/free5gc_vm1_dn_work", help="Working dir for served content and logs.")

    ap.add_argument("--iperf_base_port", type=int, default=5201)
    ap.add_argument("--iperf_servers", type=int, default=6)

    ap.add_argument("--tc_spec", default="", help="Interface to shape, e.g., veth-dn@dn or ens33.")
    ap.add_argument("--bw_mbps", type=float, default=1000.0)
    ap.add_argument("--delay_ms", type=float, default=2.0)
    ap.add_argument("--jitter_ms", type=float, default=0.2)
    ap.add_argument("--loss_pct", type=float, default=0.01)
    ap.add_argument("--clear_tc_on_exit", action="store_true", help="Remove qdisc at the end.")

    ap.add_argument("--monitor_specs", default="", help="Comma list of interfaces to monitor, e.g., ens33,veth-dn@dn")
    ap.add_argument("--sample_interval", type=float, default=1.0)

    ap.add_argument("--tcpdump_specs", default="", help="Comma list of interfaces to capture, e.g., ens33,veth-dn@dn")
    ap.add_argument("--tcpdump_dir", default="", help="Where to write pcaps. Default: <work_dir>/pcap")

    ap.add_argument("--duration", type=float, default=0.0, help="Seconds to run. 0 = until Ctrl+C.")
    ap.add_argument("--out", default="", help="Output JSON. Default: <work_dir>/<experiment_id>_vm1_core_dn.json")
    ap.add_argument("--csv_dir", default="", help="Directory for CSV tables. Default: <JSON name>_csv")

    args = ap.parse_args()

    work_dir = Path(args.work_dir).absolute()
    ensure_dir(str(work_dir))
    pcap_dir = Path(args.tcpdump_dir).absolute() if args.tcpdump_dir else work_dir / "pcap"
    out_path = Path(args.out).absolute() if args.out else work_dir / f"{args.experiment_id}_vm1_core_dn.json"

    serve_dir = work_dir / "serve"
    logs_dir = work_dir / "logs"
    ensure_dir(str(logs_dir))

    monitor_specs = parse_iface_specs(args.monitor_specs)
    tcpdump_specs = parse_iface_specs(args.tcpdump_specs)
    tc_spec = parse_iface_spec(args.tc_spec) if args.tc_spec else None

    stop_event = threading.Event()

    def _handle_signal(signo, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    result = {
        "experiment_id": args.experiment_id,
        "vm_role": "vm1_core_dn_server_monitor",
        "started_ts_ms": now_ms(),
        "config": vars(args),
        "http": {},
        "iperf": {},
        "tc": {},
        "tcpdump": [],
        "monitoring": {},
        "errors": [],
    }

    http_proc = None
    tcpdump_entries = []
    sampler = Sampler(monitor_specs, args.sample_interval) if monitor_specs else None

    try:
        print(f"[VM1] Preparing HTTP content from {args.http_src_dir} ...", flush=True)
        files = prepare_http_content(args.http_src_dir, str(serve_dir), args.max_files)
        result["http"]["served_dir"] = str(serve_dir)
        result["http"]["file_count"] = len(files)
        result["http"]["manifest_url"] = f"http://{args.bind_ip}:{args.http_port}/manifest.json"

        if tc_spec:
            print(f"[VM1] Applying tc on {tc_spec.label}: {args.bw_mbps} Mbps ...", flush=True)
            result["tc"]["applied"] = apply_tc_htb_netem(
                tc_spec,
                bw_mbps=args.bw_mbps,
                delay_ms=args.delay_ms,
                jitter_ms=args.jitter_ms,
                loss_pct=args.loss_pct,
            )

        print(f"[VM1] Starting HTTP server on {args.bind_ip}:{args.http_port} ...", flush=True)
        http_log = str(logs_dir / f"{args.experiment_id}_http.log")
        http_proc = start_http_server(args.server_netns, str(serve_dir), args.bind_ip, args.http_port, http_log)
        result["http"]["log"] = http_log
        result["http"]["pid"] = http_proc.pid
        time.sleep(0.5)

        print(f"[VM1] Starting {args.iperf_servers} iperf3 servers ...", flush=True)
        result["iperf"]["servers"] = start_iperf_servers(
            args.server_netns, args.bind_ip, args.iperf_base_port, args.iperf_servers
        )

        if tcpdump_specs:
            print(f"[VM1] Starting tcpdump on: {', '.join(s.label for s in tcpdump_specs)}", flush=True)
            for spec in tcpdump_specs:
                entry = start_tcpdump(spec, str(pcap_dir), args.experiment_id)
                tcpdump_entries.append(entry)
                result["tcpdump"].append({k: v for k, v in entry.items() if k != "proc"})
            time.sleep(0.5)

        before = read_all_iface_stats(monitor_specs) if monitor_specs else {}
        result["monitoring"]["iface_before"] = before

        if sampler:
            sampler.start()

        print("[VM1] Ready. Start the VM2 client script now.", flush=True)
        if args.duration and args.duration > 0:
            end_at = time.time() + args.duration
            while time.time() < end_at and not stop_event.is_set():
                time.sleep(0.5)
        else:
            while not stop_event.is_set():
                time.sleep(0.5)

        after = read_all_iface_stats(monitor_specs) if monitor_specs else {}
        result["monitoring"]["iface_after"] = after
        result["monitoring"]["iface_delta"] = diff_iface_stats(before, after)

        if sampler:
            sampler.stop()
            result["monitoring"]["samples"] = sampler.samples

        if tc_spec:
            result["tc"]["final_qdisc"] = show_tc(tc_spec)

    except Exception as e:
        result["errors"].append(str(e))
        print(f"[VM1][ERROR] {e}", file=sys.stderr, flush=True)

    finally:
        print("[VM1] Stopping services and writing JSON ...", flush=True)

        if sampler:
            sampler.stop()

        for entry in tcpdump_entries:
            stop_process_group(entry["proc"])

        stop_iperf_servers(args.server_netns, args.iperf_base_port, args.iperf_servers)

        if http_proc:
            stop_process_group(http_proc)

        if tc_spec and args.clear_tc_on_exit:
            clear_tc(tc_spec)
            result["tc"]["cleared_on_exit"] = True

        result["finished_ts_ms"] = now_ms()
        result["duration_s"] = (result["finished_ts_ms"] - result["started_ts_ms"]) / 1000.0

        ensure_dir(str(out_path.parent))
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        csv_dir = args.csv_dir or str(out_path.with_suffix("")) + "_csv"
        csv_files = export_vm1_csv(result, csv_dir)

        print(f"[VM1][OK] Results saved to: {out_path}", flush=True)
        print(f"[VM1][OK] CSV tables saved to: {csv_dir}", flush=True)


if __name__ == "__main__":
    main()
