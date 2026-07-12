#!/usr/bin/env python3
"""
free5gc_vm2_ue_client_measure.py

VM2 script for a free5GC experiment:
- Runs on the UE/RAN machine.
- Measures from one or more UE tunnel interfaces, e.g., ueTun0, ueTun1.
- Downloads streaming files from the DN server through UPF.
- Runs ping, iperf3 TCP/UDP, HTTP download, simple QoE, interface counters, optional tcpdump.
- Produces JSON results compatible with the VM1 Core/DN server-monitor JSON.

Typical use:
  python3 free5gc_vm2_ue_client_measure.py \
    --experiment_id 1gbps_one_run01 \
    --server_ip 10.100.0.10 \
    --http_port 8080 \
    --ue_ifaces ueTun0 \
    --clients 1 \
    --download_scenarios one \
    --runs 5 \
    --max_files 300 \
    --monitor_ifaces ueTun0,ens33 \
    --tcpdump_ifaces ueTun0,ens33 \
    --out vm2_ue_1gbps_one_run01.json
"""

import argparse
import csv
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from math import sqrt
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# -------------------------
# Generic helpers
# -------------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def run_cmd(cmd: str, timeout: int = 60, check: bool = False) -> Tuple[int, str]:
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


def ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


def safe_json_load(s: str) -> Optional[dict]:
    try:
        return json.loads(s)
    except Exception:
        return None


def mean_std_ci95(xs: List[float]) -> Dict[str, Optional[float]]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"mean": None, "std": None, "ci95_halfwidth": None, "n": 0}
    n = len(xs)
    mu = statistics.mean(xs)
    std = statistics.pstdev(xs) if n > 1 else 0.0
    ci = 1.96 * (std / sqrt(n)) if n > 1 else 0.0
    return {"mean": mu, "std": std, "ci95_halfwidth": ci, "n": n}


def jains_fairness(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None and v >= 0]
    if not vals:
        return None
    s = sum(vals)
    if s == 0:
        return 0.0
    return (s * s) / (len(vals) * sum(v * v for v in vals))


# -------------------------
# Interface helpers
# -------------------------
IFACE_COUNTERS = [
    "rx_bytes", "tx_bytes",
    "rx_packets", "tx_packets",
    "rx_errors", "tx_errors",
    "rx_dropped", "tx_dropped",
]


def read_iface_stats(iface: str) -> Dict[str, Optional[int]]:
    stats = {}
    for c in IFACE_COUNTERS:
        path = f"/sys/class/net/{iface}/statistics/{c}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                stats[c] = int(f.read().strip())
        except Exception:
            stats[c] = None
    return stats


def read_all_iface_stats(ifaces: List[str]) -> Dict[str, Dict[str, Optional[int]]]:
    return {iface: read_iface_stats(iface) for iface in ifaces}


def diff_iface_stats(before: Dict, after: Dict) -> Dict:
    out = {}
    for iface, bstats in before.items():
        astats = after.get(iface, {})
        out[iface] = {}
        for k, bv in bstats.items():
            av = astats.get(k)
            out[iface][k] = None if bv is None or av is None else av - bv
    return out


def get_iface_ipv4(iface: str) -> Optional[str]:
    rc, out = run_cmd(f"ip -4 -o addr show dev {iface}", timeout=5)
    if rc != 0:
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out)
    return m.group(1) if m else None


def read_meminfo() -> Dict[str, Optional[int]]:
    keys = {"MemTotal", "MemAvailable", "MemFree", "Buffers", "Cached", "SwapTotal", "SwapFree"}
    out = {k: None for k in keys}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if parts and parts[0].rstrip(":") in keys:
                    out[parts[0].rstrip(":")] = int(parts[1])
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
    def __init__(self, ifaces: List[str], interval_s: float):
        self.ifaces = ifaces
        self.interval_s = interval_s
        self.samples: List[Dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="vm2_sampler", daemon=True)
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
                "interfaces": read_all_iface_stats(self.ifaces),
            })
            prev_cpu = cur_cpu


# -------------------------
# tcpdump helpers
# -------------------------
def start_tcpdump(iface: str, out_dir: str, experiment_id: str, run_label: str) -> Dict:
    ensure_dir(out_dir)
    safe = iface.replace("/", "_")
    pcap_path = str(Path(out_dir) / f"{experiment_id}_{run_label}_{safe}.pcap")
    log_path = str(Path(out_dir) / f"{experiment_id}_{run_label}_{safe}.tcpdump.log")
    cmd = f"tcpdump -i {iface} -s 0 -w {pcap_path}"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, shell=True, stdout=log, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
    return {"interface": iface, "pcap": pcap_path, "log": log_path, "proc": proc}


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
# Ping parsing
# -------------------------
PING_SUMMARY_RE = re.compile(
    r"(?P<tx>\d+)\s+packets transmitted,\s+(?P<rx>\d+)\s+received.*?(?P<loss>[\d.]+)%\s+packet loss",
    re.S,
)
PING_RTT_RE = re.compile(
    r"rtt min/avg/max/mdev = (?P<min>[\d.]+)/(?P<avg>[\d.]+)/(?P<max>[\d.]+)/(?P<mdev>[\d.]+)\s+ms"
)


def parse_ping(output: str) -> Dict:
    d = {"raw": output}
    m = PING_SUMMARY_RE.search(output)
    if m:
        tx = int(m.group("tx"))
        rx = int(m.group("rx"))
        loss = float(m.group("loss"))
        d.update({"tx": tx, "rx": rx, "loss_pct": loss})
    m2 = PING_RTT_RE.search(output)
    if m2:
        d.update({
            "rtt_ms": {
                "min": float(m2.group("min")),
                "avg": float(m2.group("avg")),
                "max": float(m2.group("max")),
                "mdev": float(m2.group("mdev")),
            }
        })
        d["jitter_ms_est"] = d["rtt_ms"]["mdev"]
    return d


def run_ping(iface: str, server_ip: str, count: int, interval_s: float) -> Dict:
    cmd = f"ping -I {iface} -c {count} -i {interval_s} {server_ip}"
    rc, out = run_cmd(cmd, timeout=max(10, int(count * interval_s + 10)))
    d = parse_ping(out)
    d["rc"] = rc
    return d


# -------------------------
# iperf and HTTP/QoE
# -------------------------
def run_iperf_tcp(iface: str, server_ip: str, bind_ip: Optional[str], port: int, seconds: int) -> Dict:
    bind_part = f"-B {bind_ip}" if bind_ip else ""
    cmd = f"iperf3 -c {server_ip} {bind_part} -p {port} -t {seconds} -J"
    rc, out = run_cmd(cmd, timeout=seconds + 30)
    js = safe_json_load(out)
    if js is None:
        return {"ok": False, "rc": rc, "raw": out}

    kpis = {"ok": True}
    try:
        recv = js.get("end", {}).get("sum_received", {})
        sent = js.get("end", {}).get("sum_sent", {})
        stream0_sender = js.get("end", {}).get("streams", [{}])[0].get("sender", {})
        kpis.update({
            "throughput_mbps": float(recv.get("bits_per_second", 0.0)) / 1e6,
            "seconds": recv.get("seconds"),
            "retransmits": sent.get("retransmits"),
            "snd_cwnd_bytes_last": stream0_sender.get("snd_cwnd"),
            "rtt_us_last": stream0_sender.get("rtt"),
            "rttvar_us_last": stream0_sender.get("rttvar"),
        })
    except Exception as e:
        kpis["extract_error"] = str(e)

    return {"ok": True, "rc": rc, "kpis": kpis, "json": js}


def run_iperf_udp(iface: str, server_ip: str, bind_ip: Optional[str], port: int, seconds: int, udp_bw: str) -> Dict:
    bind_part = f"-B {bind_ip}" if bind_ip else ""
    cmd = f"iperf3 -u -c {server_ip} {bind_part} -p {port} -t {seconds} -b {udp_bw} -J"
    rc, out = run_cmd(cmd, timeout=seconds + 30)
    js = safe_json_load(out)
    if js is None:
        return {"ok": False, "rc": rc, "raw": out}

    kpis = {"ok": True}
    try:
        end = js.get("end", {})
        udp_sum = end.get("sum", {}) or end.get("sum_received", {})
        kpis.update({
            "throughput_mbps": float(udp_sum.get("bits_per_second", 0.0)) / 1e6,
            "jitter_ms": udp_sum.get("jitter_ms"),
            "lost_packets": udp_sum.get("lost_packets"),
            "packets": udp_sum.get("packets"),
            "lost_percent": udp_sum.get("lost_percent"),
            "seconds": udp_sum.get("seconds"),
        })
    except Exception as e:
        kpis["extract_error"] = str(e)

    return {"ok": True, "rc": rc, "kpis": kpis, "json": js}


def fetch_manifest(server_ip: str, http_port: int, iface: str, timeout: int = 20) -> Dict:
    url = f"http://{server_ip}:{http_port}/manifest.json"
    cmd = f"curl --interface {iface} -L -s --max-time {timeout} {url}"
    rc, out = run_cmd(cmd, timeout=timeout + 5)
    js = safe_json_load(out)
    if js is None:
        return {"ok": False, "rc": rc, "url": url, "raw": out}
    return {"ok": True, "rc": rc, "url": url, "manifest": js}


def curl_download_file(iface: str, url: str, out_path: str, timeout_s: int = 3600) -> Dict:
    # Keep format simple for parsing.
    write_fmt = (
        "http_code:%{http_code}\\n"
        "size_download:%{size_download}\\n"
        "time_namelookup:%{time_namelookup}\\n"
        "time_connect:%{time_connect}\\n"
        "time_starttransfer:%{time_starttransfer}\\n"
        "time_total:%{time_total}\\n"
        "speed_download:%{speed_download}\\n"
    )
    cmd = f"curl --interface {iface} -L -s -o {out_path} -w '{write_fmt}' --max-time {timeout_s} {url}"
    rc, out = run_cmd(cmd, timeout=timeout_s + 10)
    parsed = {"rc": rc, "raw": out}
    for line in out.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip()
            try:
                parsed[k.strip()] = float(v) if "." in v else int(v)
            except Exception:
                parsed[k.strip()] = v
    return parsed


def simulate_qoe(segments: List[Dict], playback_rate_segments_per_s: float, initial_buffer_s: float) -> Dict:
    if not segments:
        return {
            "startup_delay_s": None,
            "stall_count": 0,
            "stall_time_s": 0.0,
            "quality_switches": 0,
            "avg_quality_level": None,
        }

    seg_play_s = 1.0 / max(1e-9, playback_rate_segments_per_s)
    startup_target_s = max(0.0, float(initial_buffer_s))
    buffer_s = 0.0
    startup_delay_s = 0.0
    stall_count = 0
    stall_time_s = 0.0
    playback_started = startup_target_s <= 0.0

    for seg in segments:
        t = float(seg.get("download_time_s", 0.0))
        stall = 0.0

        if not playback_started:
            startup_delay_s += t
            buffer_s += seg_play_s
            seg["stall_s"] = 0.0
            if buffer_s >= startup_target_s:
                playback_started = True
            continue

        if buffer_s > 0:
            if t >= buffer_s:
                stall = t - buffer_s
                stall_time_s += stall
                stall_count += 1
                buffer_s = 0.0
            else:
                buffer_s -= t
        else:
            stall = t
            stall_time_s += stall
            stall_count += 1

        buffer_s += seg_play_s
        seg["stall_s"] = stall

    qualities = [int(s.get("quality", 0)) for s in segments]
    switches = sum(1 for i in range(1, len(qualities)) if qualities[i] != qualities[i - 1])
    avg_q = statistics.mean(qualities) if qualities else None

    return {
        "startup_delay_s": startup_delay_s,
        "stall_count": stall_count,
        "stall_time_s": stall_time_s,
        "quality_switches": switches,
        "avg_quality_level": avg_q,
    }


def qoe_score_simple(qoe: Dict) -> Optional[float]:
    if not qoe or qoe.get("startup_delay_s") is None:
        return None
    startup = float(qoe.get("startup_delay_s", 0.0))
    stalls = float(qoe.get("stall_time_s", 0.0))
    avgq = float(qoe.get("avg_quality_level", 0.0))
    return max(0.0, (avgq + 1.0) * 10.0 - (startup * 2.0) - (stalls * 5.0))


def repair_run_qoe_metrics(run: Dict, fallback_config: Optional[Dict] = None) -> Dict:
    fallback_config = fallback_config or {}
    run_config = run.get("config", {}) or {}
    playback_rate = float(run_config.get("playback_rate", fallback_config.get("playback_rate", 1.0)) or 1.0)
    initial_buffer = float(run_config.get("initial_buffer", fallback_config.get("initial_buffer", 2.0)) or 0.0)

    startup_vals = []
    stall_vals = []
    qoe_vals = []

    for cdata in (run.get("per_client", {}) or {}).values():
        http = cdata.get("http_qoe", {}) or {}
        if not http.get("enabled"):
            continue

        qoe = simulate_qoe(http.get("segments", []) or [], playback_rate, initial_buffer)
        http["qoe"] = qoe

        if qoe.get("startup_delay_s") is not None:
            startup_vals.append(float(qoe["startup_delay_s"]))
        stall_vals.append(float(qoe.get("stall_time_s", 0.0)))
        score = qoe_score_simple(qoe)
        if score is not None:
            qoe_vals.append(float(score))

    summary = run.setdefault("summary", {})
    summary["mean_startup_delay_s"] = statistics.mean(startup_vals) if startup_vals else None
    summary["mean_stall_time_s"] = statistics.mean(stall_vals) if stall_vals else None

    fairness = run.setdefault("fairness", {})
    fairness["jain_qoe_score"] = jains_fairness(qoe_vals)
    fairness.setdefault("inputs", {})["qoe_score_simple"] = qoe_vals
    return run


def repair_output_qoe_metrics(output: Dict) -> Dict:
    cfg = output.get("config", {}) or {}
    per_run = output.get("per_run", []) or []
    for run in per_run:
        repair_run_qoe_metrics(run, cfg)

    agg_start = [r.get("summary", {}).get("mean_startup_delay_s") for r in per_run]
    agg_stall = [r.get("summary", {}).get("mean_stall_time_s") for r in per_run]
    agg_jain_qoe = [r.get("fairness", {}).get("jain_qoe_score") for r in per_run]

    aggregate = output.setdefault("aggregate_stats", {})
    aggregate["mean_startup_delay_s"] = mean_std_ci95([x for x in agg_start if x is not None])
    aggregate["mean_stall_time_s"] = mean_std_ci95([x for x in agg_stall if x is not None])
    aggregate["jain_qoe_score"] = mean_std_ci95([x for x in agg_jain_qoe if x is not None])
    return output


def download_segments(
    iface: str,
    server_ip: str,
    http_port: int,
    filenames: List[str],
    quality_levels: int,
    quality_mode: str,
    playback_rate: float,
    initial_buffer: float,
    client_label: str,
    run_dir: str,
) -> Dict:
    ensure_dir(run_dir)
    dl_dir = Path(run_dir) / f"download_{client_label}"
    ensure_dir(str(dl_dir))

    segments = []
    for idx, fname in enumerate(filenames):
        if quality_mode == "fixed":
            q = max(0, quality_levels - 1)
        elif quality_mode == "random":
            q = random.randint(0, max(0, quality_levels - 1))
        else:
            q = idx % max(1, quality_levels)

        url = f"http://{server_ip}:{http_port}/{fname}"
        out_file = str(dl_dir / fname)
        t0 = now_ms()
        res = curl_download_file(iface, url, out_file)
        t1 = now_ms()

        size_b = int(res.get("size_download", 0) or 0)
        time_total = float(res.get("time_total", 0.0) or 0.0)
        goodput_mbps = (size_b * 8.0) / (time_total * 1e6) if time_total > 0 else 0.0

        segments.append({
            "idx": idx,
            "filename": fname,
            "quality": q,
            "bytes": size_b,
            "download_time_s": time_total,
            "goodput_mbps": goodput_mbps,
            "time_starttransfer_s": float(res.get("time_starttransfer", 0.0) or 0.0),
            "http_code": res.get("http_code"),
            "curl": res,
            "timeline": {"start_ms": t0, "end_ms": t1},
            "stall_s": 0.0,
        })

    qoe = simulate_qoe(segments, playback_rate, initial_buffer)
    total_bytes = sum(s["bytes"] for s in segments)
    total_time = sum(float(s["download_time_s"]) for s in segments)
    total_goodput_mbps = (total_bytes * 8.0) / (total_time * 1e6) if total_time > 0 else 0.0

    return {
        "enabled": True,
        "segments": segments,
        "http_total": {
            "bytes": total_bytes,
            "time_s": total_time,
            "goodput_mbps": total_goodput_mbps,
        },
        "qoe": qoe,
    }


# -------------------------
# Scenarios
# -------------------------
def parse_scenarios(s: str) -> List[str]:
    items = [x.strip().lower() for x in (s or "").split(",") if x.strip()]
    valid = {"one", "half", "all"}
    out = [x for x in items if x in valid]
    return out or ["all"]


def choose_active_clients(client_names: List[str], scenario: str, select: str, seed: Optional[int]) -> List[str]:
    n = len(client_names)
    if scenario == "all":
        return list(client_names)
    if scenario == "half":
        k = max(1, (n + 1) // 2)
    else:
        k = 1

    if select == "random":
        rng = random.Random(seed if seed is not None else None)
        shuffled = list(client_names)
        rng.shuffle(shuffled)
        return shuffled[:k]
    return list(client_names)[:k]


def build_logical_clients(ue_ifaces: List[str], clients: int) -> List[Dict]:
    out = []
    for i in range(clients):
        iface = ue_ifaces[i % len(ue_ifaces)]
        out.append({
            "name": f"ue_client_{i+1}",
            "iface": iface,
            "bind_ip": get_iface_ipv4(iface),
            "logical_index": i,
        })
    return out


# -------------------------
# One experiment run
# -------------------------
def run_once(args, scenario: str, run_idx: int, filenames: List[str], base_out_dir: str) -> Dict:
    seed = args.seed + run_idx if args.seed is not None else None
    if seed is not None:
        random.seed(seed)

    clients = build_logical_clients(args.ue_ifaces, args.clients)
    client_names = [c["name"] for c in clients]
    active_clients = choose_active_clients(client_names, scenario, args.scenario_select, seed)
    active_set = set(active_clients)

    run_label = f"{scenario}_run{run_idx+1:02d}"
    run_dir = str(Path(base_out_dir) / run_label)
    ensure_dir(run_dir)

    tcpdump_entries = []
    if args.tcpdump_ifaces:
        for iface in args.tcpdump_ifaces:
            entry = start_tcpdump(iface, args.tcpdump_dir or str(Path(base_out_dir) / "pcap"), args.experiment_id, run_label)
            tcpdump_entries.append(entry)
        time.sleep(0.5)

    monitor_ifaces = args.monitor_ifaces or args.ue_ifaces
    before = read_all_iface_stats(monitor_ifaces)
    sampler = Sampler(monitor_ifaces, args.sample_interval) if monitor_ifaces else None
    if sampler:
        sampler.start()

    per_client: Dict[str, Dict] = {}
    lock = threading.Lock()
    barrier = threading.Barrier(max(1, len(active_clients)))

    def test_client(client: Dict):
        name = client["name"]
        iface = client["iface"]
        bind_ip = client["bind_ip"]
        port = args.iperf_base_port + client["logical_index"]

        item = {
            "client_name": name,
            "iface": iface,
            "bind_ip": bind_ip,
            "is_active_downloader": name in active_set,
            "ping": {},
            "iperf3_tcp": {},
            "iperf3_udp": {},
            "http_qoe": {"enabled": False},
        }

        item["ping"] = run_ping(iface, args.server_ip, args.ping_count, args.ping_interval)

        if args.iperf_sec > 0:
            item["iperf3_tcp"] = run_iperf_tcp(iface, args.server_ip, bind_ip, port, args.iperf_sec)
            if args.udp:
                item["iperf3_udp"] = run_iperf_udp(iface, args.server_ip, bind_ip, port, args.iperf_sec, args.udp_bw)

        if name in active_set and filenames:
            barrier.wait()
            item["download_timeline"] = {"start_ms": now_ms()}
            item["http_qoe"] = download_segments(
                iface=iface,
                server_ip=args.server_ip,
                http_port=args.http_port,
                filenames=filenames,
                quality_levels=args.quality_levels,
                quality_mode=args.quality_mode,
                playback_rate=args.playback_rate,
                initial_buffer=args.initial_buffer,
                client_label=name,
                run_dir=run_dir,
            )
            item["download_timeline"]["end_ms"] = now_ms()
        else:
            item["download_timeline"] = {"enabled": False}

        with lock:
            per_client[name] = item

    threads = []
    t0 = now_ms()
    for c in clients:
        t = threading.Thread(target=test_client, args=(c,), name=f"{run_label}_{c['name']}")
        threads.append(t)
        t.start()

    for t in threads:
        t.join()
    t1 = now_ms()

    if sampler:
        sampler.stop()

    after = read_all_iface_stats(monitor_ifaces)

    for entry in tcpdump_entries:
        stop_process_group(entry["proc"])

    # Summary and fairness
    thr_vals = []
    gp_vals = []
    qoe_vals = []
    startup_vals = []
    stall_vals = []
    ping_loss_vals = []
    ping_rtt_vals = []
    udp_jitter_vals = []
    udp_loss_vals = []

    for cdata in per_client.values():
        k = cdata.get("iperf3_tcp", {}).get("kpis", {})
        if k.get("throughput_mbps") is not None:
            thr_vals.append(float(k["throughput_mbps"]))

        p = cdata.get("ping", {})
        if p.get("loss_pct") is not None:
            ping_loss_vals.append(float(p["loss_pct"]))
        if p.get("rtt_ms", {}).get("avg") is not None:
            ping_rtt_vals.append(float(p["rtt_ms"]["avg"]))

        uk = cdata.get("iperf3_udp", {}).get("kpis", {})
        if uk.get("jitter_ms") is not None:
            udp_jitter_vals.append(float(uk["jitter_ms"]))
        if uk.get("lost_percent") is not None:
            udp_loss_vals.append(float(uk["lost_percent"]))

        h = cdata.get("http_qoe", {})
        if h.get("enabled"):
            gp = h.get("http_total", {}).get("goodput_mbps")
            if gp is not None:
                gp_vals.append(float(gp))
            q = h.get("qoe", {})
            if q.get("startup_delay_s") is not None:
                startup_vals.append(float(q["startup_delay_s"]))
            stall_vals.append(float(q.get("stall_time_s", 0.0)))
            score = qoe_score_simple(q)
            if score is not None:
                qoe_vals.append(float(score))

    summary = {
        "clients": args.clients,
        "ue_ifaces": args.ue_ifaces,
        "active_downloaders_count": len(active_clients),
        "active_downloaders": active_clients,
        "success_ping_clients": sum(
            1 for c in per_client.values()
            if c.get("ping", {}).get("loss_pct") is not None and c["ping"]["loss_pct"] < 100.0
        ),
        "success_iperf_tcp_clients": sum(
            1 for c in per_client.values()
            if c.get("iperf3_tcp", {}).get("kpis", {}).get("ok") is True
        ),
        "mean_ping_rtt_ms": statistics.mean(ping_rtt_vals) if ping_rtt_vals else None,
        "mean_ping_loss_pct": statistics.mean(ping_loss_vals) if ping_loss_vals else None,
        "mean_udp_jitter_ms": statistics.mean(udp_jitter_vals) if udp_jitter_vals else None,
        "mean_udp_loss_pct": statistics.mean(udp_loss_vals) if udp_loss_vals else None,
        "mean_throughput_mbps": statistics.mean(thr_vals) if thr_vals else None,
        "mean_http_goodput_mbps": statistics.mean(gp_vals) if gp_vals else None,
        "mean_startup_delay_s": statistics.mean(startup_vals) if startup_vals else None,
        "mean_stall_time_s": statistics.mean(stall_vals) if stall_vals else None,
    }

    return {
        "meta": {
            "experiment_id": args.experiment_id,
            "vm_role": "vm2_ue_ran_client",
            "scenario": args.scenario_label_override or scenario,
            "scenario_internal": scenario,
            "run_idx": run_idx,
            "run_label": run_label,
            "seed": seed,
            "started_ts_ms": t0,
            "finished_ts_ms": t1,
            "duration_s": (t1 - t0) / 1000.0,
        },
        "config": {
            "server_ip": args.server_ip,
            "http_port": args.http_port,
            "iperf_base_port": args.iperf_base_port,
            "iperf_sec": args.iperf_sec,
            "ping_count": args.ping_count,
            "ping_interval": args.ping_interval,
            "udp": args.udp,
            "udp_bw": args.udp_bw,
            "quality_levels": args.quality_levels,
            "quality_mode": args.quality_mode,
            "playback_rate": args.playback_rate,
            "initial_buffer": args.initial_buffer,
            "max_files": args.max_files,
        },
        "tcpdump": [{k: v for k, v in e.items() if k != "proc"} for e in tcpdump_entries],
        "monitoring": {
            "iface_before": before,
            "iface_after": after,
            "iface_delta": diff_iface_stats(before, after),
            "samples": sampler.samples if sampler else [],
        },
        "per_client": per_client,
        "fairness": {
            "jain_throughput": jains_fairness(thr_vals),
            "jain_http_goodput": jains_fairness(gp_vals),
            "jain_qoe_score": jains_fairness(qoe_vals),
            "inputs": {
                "throughput_mbps": thr_vals,
                "http_goodput_mbps": gp_vals,
                "qoe_score_simple": qoe_vals,
            },
        },
        "summary": summary,
    }


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
            # Keep lists compact unless they are expanded by a specific exporter.
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


def export_vm2_csv(output: Dict, csv_dir: str) -> Dict[str, str]:
    """Export VM2 JSON structure into analysis-ready CSV tables."""
    repair_output_qoe_metrics(output)
    ensure_dir(csv_dir)
    exp_id = output.get("experiment_id")
    role = output.get("vm_role")

    aggregate_rows = []
    for metric, stats in output.get("aggregate_stats", {}).items():
        row = {"experiment_id": exp_id, "vm_role": role, "metric": metric}
        if isinstance(stats, dict):
            row.update(stats)
        else:
            row["value"] = stats
        aggregate_rows.append(row)

    run_summary_rows = []
    per_client_rows = []
    segment_rows = []
    iface_delta_rows = []
    sample_rows = []
    tcpdump_rows = []

    for run in output.get("per_run", []):
        meta = run.get("meta", {})
        run_base = {
            "experiment_id": exp_id,
            "vm_role": role,
            "scenario": meta.get("scenario"),
            "run_idx": meta.get("run_idx"),
            "run_label": meta.get("run_label"),
            "seed": meta.get("seed"),
            "started_ts_ms": meta.get("started_ts_ms"),
            "finished_ts_ms": meta.get("finished_ts_ms"),
            "duration_s": meta.get("duration_s"),
        }
        run_summary_rows.append({**run_base, **flatten_dict(run.get("summary", {}))})

        for t in run.get("tcpdump", []) or []:
            tcpdump_rows.append({**run_base, **flatten_dict(t)})

        for iface, stats in run.get("monitoring", {}).get("iface_delta", {}).items():
            iface_delta_rows.append({**run_base, "iface": iface, **flatten_dict(stats)})

        for sample_idx, sample in enumerate(run.get("monitoring", {}).get("samples", []) or []):
            sbase = {
                **run_base,
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

        for client_name, cdata in run.get("per_client", {}).items():
            ping = cdata.get("ping", {}) or {}
            tcp = cdata.get("iperf3_tcp", {}).get("kpis", {}) or {}
            udp = cdata.get("iperf3_udp", {}).get("kpis", {}) or {}
            http = cdata.get("http_qoe", {}) or {}
            http_total = http.get("http_total", {}) or {}
            qoe = http.get("qoe", {}) or {}
            timeline = cdata.get("download_timeline", {}) or {}

            per_client_rows.append({
                **run_base,
                "client_name": cdata.get("client_name", client_name),
                "iface": cdata.get("iface"),
                "bind_ip": cdata.get("bind_ip"),
                "is_active_downloader": cdata.get("is_active_downloader"),
                "ping_tx": ping.get("tx"),
                "ping_rx": ping.get("rx"),
                "ping_loss_pct": ping.get("loss_pct"),
                "ping_rtt_min_ms": (ping.get("rtt_ms") or {}).get("min"),
                "ping_rtt_avg_ms": (ping.get("rtt_ms") or {}).get("avg"),
                "ping_rtt_max_ms": (ping.get("rtt_ms") or {}).get("max"),
                "ping_rtt_mdev_ms": (ping.get("rtt_ms") or {}).get("mdev"),
                "ping_jitter_ms_est": ping.get("jitter_ms_est"),
                "tcp_ok": tcp.get("ok"),
                "tcp_throughput_mbps": tcp.get("throughput_mbps"),
                "tcp_retransmits": tcp.get("retransmits"),
                "tcp_seconds": tcp.get("seconds"),
                "tcp_sender_rtt_us_last": tcp.get("rtt_us_last") or tcp.get("rtt_ms_last"),
                "tcp_sender_rttvar_us_last": tcp.get("rttvar_us_last") or tcp.get("rttvar_ms_last"),
                "udp_ok": udp.get("ok"),
                "udp_throughput_mbps": udp.get("throughput_mbps"),
                "udp_jitter_ms": udp.get("jitter_ms"),
                "udp_lost_percent": udp.get("lost_percent"),
                "http_enabled": http.get("enabled"),
                "http_total_bytes": http_total.get("bytes"),
                "http_total_time_s": http_total.get("time_s"),
                "http_goodput_mbps": http_total.get("goodput_mbps"),
                "startup_delay_s": qoe.get("startup_delay_s"),
                "stall_count": qoe.get("stall_count"),
                "stall_time_s": qoe.get("stall_time_s"),
                "quality_switches": qoe.get("quality_switches"),
                "avg_quality_level": qoe.get("avg_quality_level"),
                "download_start_ms": timeline.get("start_ms"),
                "download_end_ms": timeline.get("end_ms"),
            })

            for seg in http.get("segments", []) or []:
                segment_rows.append({
                    **run_base,
                    "client_name": cdata.get("client_name", client_name),
                    "iface": cdata.get("iface"),
                    "bind_ip": cdata.get("bind_ip"),
                    **flatten_dict(seg),
                })

    files = {
        "aggregate_stats": str(Path(csv_dir) / "vm2_aggregate_stats.csv"),
        "run_summary": str(Path(csv_dir) / "vm2_run_summary.csv"),
        "per_client": str(Path(csv_dir) / "vm2_per_client_metrics.csv"),
        "segments": str(Path(csv_dir) / "vm2_http_segments.csv"),
        "iface_delta": str(Path(csv_dir) / "vm2_interface_delta.csv"),
        "samples": str(Path(csv_dir) / "vm2_monitor_samples.csv"),
        "tcpdump": str(Path(csv_dir) / "vm2_tcpdump.csv"),
    }
    write_csv(files["aggregate_stats"], aggregate_rows)
    write_csv(files["run_summary"], run_summary_rows)
    write_csv(files["per_client"], per_client_rows)
    write_csv(files["segments"], segment_rows)
    write_csv(files["iface_delta"], iface_delta_rows)
    write_csv(files["samples"], sample_rows)
    write_csv(files["tcpdump"], tcpdump_rows)
    return files


def main():
    ap = argparse.ArgumentParser(description="VM2 UE/RAN client measurement script for free5GC streaming experiments.")

    ap.add_argument("--experiment_id", required=True)
    ap.add_argument("--server_ip", required=True, help="DN server IP, e.g., 10.100.0.10")
    ap.add_argument("--http_port", type=int, default=8080)

    ap.add_argument("--ue_ifaces", required=True, help="Comma list, e.g., ueTun0 or ueTun0,ueTun1")
    ap.add_argument("--clients", type=int, default=1, help="Logical clients. If > ue_ifaces, interfaces are reused round-robin.")

    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=2000)
    ap.add_argument("--download_scenarios", default="one", help="one,half,all or comma list.")
    ap.add_argument("--scenario_label_override", default="", help="External scenario label to store even if internal download_scenarios differs.")
    ap.add_argument("--scenario_select", choices=["first", "random"], default="first")

    ap.add_argument("--iperf_base_port", type=int, default=5201)
    ap.add_argument("--iperf_sec", type=int, default=10)
    ap.add_argument("--udp", action="store_true", help="Also run iperf3 UDP.")
    ap.add_argument("--udp_bw", default="100M")

    ap.add_argument("--ping_count", type=int, default=20)
    ap.add_argument("--ping_interval", type=float, default=0.2)

    ap.add_argument("--max_files", type=int, default=300)
    ap.add_argument("--quality_levels", type=int, default=3)
    ap.add_argument("--quality_mode", choices=["fixed", "roundrobin", "random"], default="roundrobin")
    ap.add_argument("--playback_rate", type=float, default=1.0, help="Segments per second for simple QoE model.")
    ap.add_argument("--initial_buffer", type=float, default=2.0)

    ap.add_argument("--monitor_ifaces", default="", help="Comma list. Default = ue_ifaces.")
    ap.add_argument("--sample_interval", type=float, default=1.0)

    ap.add_argument("--tcpdump_ifaces", default="", help="Comma list, e.g., ueTun0,ens33")
    ap.add_argument("--tcpdump_dir", default="", help="Default: <work_dir>/pcap")

    ap.add_argument("--work_dir", default="/tmp/free5gc_vm2_ue_work")
    ap.add_argument("--out", default="", help="Output JSON. Default: <work_dir>/<experiment_id>_vm2_ue.json")
    ap.add_argument("--csv_dir", default="", help="Directory for CSV tables. Default: <JSON name>_csv")

    args = ap.parse_args()

    args.ue_ifaces = [x.strip() for x in args.ue_ifaces.split(",") if x.strip()]
    if not args.ue_ifaces:
        raise ValueError("--ue_ifaces must contain at least one interface")

    args.monitor_ifaces = [x.strip() for x in args.monitor_ifaces.split(",") if x.strip()] if args.monitor_ifaces else list(args.ue_ifaces)
    args.tcpdump_ifaces = [x.strip() for x in args.tcpdump_ifaces.split(",") if x.strip()] if args.tcpdump_ifaces else []

    work_dir = Path(args.work_dir).absolute()
    ensure_dir(str(work_dir))
    args.tcpdump_dir = args.tcpdump_dir or str(work_dir / "pcap")
    out_path = Path(args.out).absolute() if args.out else work_dir / f"{args.experiment_id}_vm2_ue.json"

    # Verify UE interfaces
    iface_info = {}
    for iface in args.ue_ifaces:
        ip = get_iface_ipv4(iface)
        iface_info[iface] = {"ipv4": ip, "exists": ip is not None}
        if ip is None:
            print(f"[VM2][WARN] Could not find IPv4 for {iface}. Commands may fail.", file=sys.stderr)

    # Fetch manifest through the first UE interface.
    print(f"[VM2] Fetching manifest through {args.ue_ifaces[0]} ...", flush=True)
    manifest_res = fetch_manifest(args.server_ip, args.http_port, args.ue_ifaces[0])
    if not manifest_res.get("ok"):
        print("[VM2][ERROR] Could not fetch manifest.json through the UE interface.", file=sys.stderr)
        print(manifest_res.get("raw", ""), file=sys.stderr)
        sys.exit(2)

    files = manifest_res["manifest"].get("files", [])
    if args.max_files > 0:
        files = files[:args.max_files]
    print(f"[VM2] Manifest OK. Files selected: {len(files)}", flush=True)

    scenarios = parse_scenarios(args.download_scenarios)
    per_run = []

    for scenario in scenarios:
        for run_idx in range(args.runs):
            print(f"[VM2] Scenario={scenario} run={run_idx+1}/{args.runs}", flush=True)
            per_run.append(run_once(args, scenario, run_idx, files, str(work_dir)))

    agg_thr = [r["summary"].get("mean_throughput_mbps") for r in per_run]
    agg_gp = [r["summary"].get("mean_http_goodput_mbps") for r in per_run]
    agg_start = [r["summary"].get("mean_startup_delay_s") for r in per_run]
    agg_stall = [r["summary"].get("mean_stall_time_s") for r in per_run]
    agg_rtt = [r["summary"].get("mean_ping_rtt_ms") for r in per_run]
    agg_loss = [r["summary"].get("mean_ping_loss_pct") for r in per_run]
    agg_udp_jitter = [r["summary"].get("mean_udp_jitter_ms") for r in per_run]
    agg_udp_loss = [r["summary"].get("mean_udp_loss_pct") for r in per_run]
    agg_jain_thr = [r["fairness"].get("jain_throughput") for r in per_run]
    agg_jain_gp = [r["fairness"].get("jain_http_goodput") for r in per_run]
    agg_jain_qoe = [r["fairness"].get("jain_qoe_score") for r in per_run]

    output = {
        "experiment_id": args.experiment_id,
        "vm_role": "vm2_ue_ran_client",
        "created_ts_ms": now_ms(),
        "config": {
            **vars(args),
            "ue_iface_info": iface_info,
            "manifest": manifest_res,
        },
        "per_run": per_run,
        "aggregate_stats": {
            "mean_throughput_mbps": mean_std_ci95([x for x in agg_thr if x is not None]),
            "mean_http_goodput_mbps": mean_std_ci95([x for x in agg_gp if x is not None]),
            "mean_startup_delay_s": mean_std_ci95([x for x in agg_start if x is not None]),
            "mean_stall_time_s": mean_std_ci95([x for x in agg_stall if x is not None]),
            "mean_ping_rtt_ms": mean_std_ci95([x for x in agg_rtt if x is not None]),
            "mean_ping_loss_pct": mean_std_ci95([x for x in agg_loss if x is not None]),
            "mean_udp_jitter_ms": mean_std_ci95([x for x in agg_udp_jitter if x is not None]),
            "mean_udp_loss_pct": mean_std_ci95([x for x in agg_udp_loss if x is not None]),
            "jain_throughput": mean_std_ci95([x for x in agg_jain_thr if x is not None]),
            "jain_http_goodput": mean_std_ci95([x for x in agg_jain_gp if x is not None]),
            "jain_qoe_score": mean_std_ci95([x for x in agg_jain_qoe if x is not None]),
        },
    }
    repair_output_qoe_metrics(output)

    ensure_dir(str(out_path.parent))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    csv_dir = args.csv_dir or str(out_path.with_suffix("")) + "_csv"
    csv_files = export_vm2_csv(output, csv_dir)

    print(f"[VM2][OK] Results saved to: {out_path}", flush=True)
    print(f"[VM2][OK] CSV tables saved to: {csv_dir}", flush=True)


if __name__ == "__main__":
    main()
