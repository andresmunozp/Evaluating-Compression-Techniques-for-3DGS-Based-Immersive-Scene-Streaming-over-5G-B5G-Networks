#!/usr/bin/env python3
"""
servidor_final_v2.py

Framework tipo “paper 3” (Mininet grid-mesh + emulación 6G + métricas red/QoE)
con mejoras de reproducibilidad y escenarios de descarga:

✅ Mejoras clave:
1) Inicio sincronizado de tráfico para los hosts activos (Barrier).
2) Multi-run + seed reproducible.
3) TCP congestion control configurable (tcp_cc).
4) TC shaping configurable:
   - core (switch-switch) opcional
   - access (host-switch) opcional
5) HTTP multihilo en el server (ThreadedHTTPServer).
6) Escenarios de descarga bajo mismas condiciones iniciales:
   - one: descarga 1 host
   - half: descarga la mitad
   - all: descargan todos
   + Registro de timeline (start_ms/end_ms) por host al descargar.

Salida:
- JSON con lista per_run (cada run incluye scenario, active_downloaders, per_client, fairness, summary)
- aggregate_stats (mean/std/CI95) sobre métricas globales por run

Requisitos:
- Mininet instalado
- OVS (openvswitch-switch)
- iperf3 instalado
- curl instalado
- python3
"""

import argparse
import json
import os
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict
from math import sqrt
from typing import Dict, List, Optional, Tuple

from mininet.net import Mininet
from mininet.node import OVSSwitch, NullController
from mininet.link import TCLink
from mininet.log import setLogLevel, info


# -------------------------
# Utils sistema
# -------------------------
def run_host(cmd: str, timeout: int = 30) -> Tuple[int, str]:
    """Run command on the real host (VM), return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + "\n[TIMEOUT]\n"


def sh_ok(cmd: str, timeout: int = 30) -> None:
    rc, out = run_host(cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}): {cmd}\n{out}")


def mn_cleanup() -> None:
    run_host("mn -c >/dev/null 2>&1 || true", timeout=30)
    # Limpieza extra de bridges s1..s99 por si queda algo raro
    for i in range(1, 100):
        run_host(f"ovs-vsctl --if-exists del-br s{i} >/dev/null 2>&1 || true", timeout=5)


def now_ms() -> int:
    return int(time.time() * 1000)


def jains_fairness(values: List[float]) -> Optional[float]:
    vals = [v for v in values if v is not None and v >= 0]
    if not vals:
        return None
    s = sum(vals)
    if s == 0:
        return 0.0
    return (s * s) / (len(vals) * sum(v * v for v in vals))


def mean_std_ci95(xs: List[float]) -> Dict[str, Optional[float]]:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"mean": None, "std": None, "ci95_halfwidth": None, "n": 0}
    n = len(xs)
    mu = statistics.mean(xs)
    std = statistics.pstdev(xs) if n > 1 else 0.0
    ci = 1.96 * (std / sqrt(n)) if n > 1 else 0.0  # aprox normal
    return {"mean": mu, "std": std, "ci95_halfwidth": ci, "n": n}


def set_tcp_cc(host, cc: str) -> None:
    """
    Intenta setear congestion control dentro del namespace del host Mininet.
    Si falla, no rompe el experimento.
    """
    if not cc:
        return
    out = host.cmd("sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null || true")
    if out and cc not in out.strip().split():
        info(f"[WARN] {host.name}: CC '{cc}' no aparece en tcp_available_congestion_control: {out.strip()}\n")
    host.cmd(f"sysctl -w net.ipv4.tcp_congestion_control={cc} >/dev/null 2>&1 || true")


# -------------------------
# Parse helpers
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
        d.update(
            {
                "rtt_ms": {
                    "min": float(m2.group("min")),
                    "avg": float(m2.group("avg")),
                    "max": float(m2.group("max")),
                    "mdev": float(m2.group("mdev")),
                }
            }
        )
    if "rtt_ms" in d:
        d["jitter_ms_est"] = d["rtt_ms"]["mdev"]
    return d


def safe_json_load(s: str) -> Optional[dict]:
    try:
        return json.loads(s)
    except Exception:
        return None


# -------------------------
# QoE (modelo simple tipo DASH / paper3)
# -------------------------
@dataclass
class SegmentResult:
    idx: int
    quality: int
    bytes: int
    download_time_s: float
    goodput_mbps: float
    stall_s: float


def simulate_qoe_from_segments(
    segments: List[SegmentResult],
    playback_rate_segments_per_s: float,
    initial_buffer_s: float,
) -> Dict:
    """
    Modelo simple de buffer:
    - cada segmento representa 1/playback_rate segundos de reproducción.
    - buffer aumenta cuando llega un segmento, disminuye con el tiempo de descarga.
    - si buffer llega a 0 durante descarga -> stall.
    Además: asigna stall_s a cada segmento.
    """
    if not segments:
        return {
            "startup_delay_s": None,
            "stall_count": 0,
            "stall_time_s": 0.0,
            "quality_switches": 0,
            "avg_quality_level": None,
        }

    seg_play_s = 1.0 / max(1e-9, playback_rate_segments_per_s)
    buffer_s = float(initial_buffer_s)

    startup_delay_s = 0.0
    stall_count = 0
    stall_time_s = 0.0
    buffer_reached = False

    for seg in segments:
        t = float(seg.download_time_s)
        stall = 0.0

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
        seg.stall_s = stall

        if not buffer_reached:
            startup_delay_s += t
            if buffer_s >= initial_buffer_s:
                buffer_reached = True

    qualities = [s.quality for s in segments]
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
    """Score simple para fairness: premia avg_quality, penaliza startup y stalls."""
    if not qoe or qoe.get("startup_delay_s") is None:
        return None
    startup = float(qoe.get("startup_delay_s", 0.0))
    stalls = float(qoe.get("stall_time_s", 0.0))
    avgq = float(qoe.get("avg_quality_level", 0.0))
    return max(0.0, (avgq + 1.0) * 10.0 - (startup * 2.0) - (stalls * 5.0))


# -------------------------
# TC shaping (tbf + netem)
# -------------------------
def apply_tc(intf: str, bw_mbps: float, delay_ms: float, jitter_ms: float, loss_pct: float) -> Dict:
    """
    Aplica:
      root: tbf (bw)
      child: netem (delay/jitter/loss)
    """
    run_host(f"tc qdisc del dev {intf} root >/dev/null 2>&1 || true")

    sh_ok(
        f"tc qdisc add dev {intf} root handle 1: "
        f"tbf rate {bw_mbps}mbit burst 15400 latency 50ms",
        timeout=10,
    )

    if jitter_ms and jitter_ms > 0:
        delay_part = f"delay {delay_ms}ms {jitter_ms}ms distribution normal"
    else:
        delay_part = f"delay {delay_ms}ms"

    sh_ok(
        f"tc qdisc add dev {intf} parent 1:1 handle 10: "
        f"netem {delay_part} loss {loss_pct}%",
        timeout=10,
    )

    _, q = run_host(f"tc qdisc show dev {intf}", timeout=5)
    return {
        "intf": intf,
        "bw_mbps": bw_mbps,
        "delay_ms": delay_ms,
        "jitter_ms": jitter_ms,
        "loss_pct": loss_pct,
        "tc_qdisc": q.strip(),
    }


# -------------------------
# Topología: grid mesh de switches
# -------------------------
def build_grid_mesh(
    n_side: int,
    n_clients: int,
    cpu: float,
    stp: bool,
    stp_wait_s: int,
) -> Tuple[Mininet, Dict]:
    """
    Crea una malla 2D (grid) de switches.
    Conecta:
      - hS al switch (0,0)
      - clientes distribuidos en switches restantes (round-robin)
    """
    net = Mininet(controller=None, link=TCLink, autoSetMacs=True, autoStaticArp=True)
    net.addController("c0", controller=NullController)

    switches = {}
    idx = 1
    for r in range(n_side):
        for c in range(n_side):
            name = f"s{idx}"
            sw = net.addSwitch(name, cls=OVSSwitch, failMode="standalone")
            switches[(r, c)] = sw
            idx += 1

    # enlaces grid (derecha + abajo)
    for r in range(n_side):
        for c in range(n_side):
            if c + 1 < n_side:
                net.addLink(switches[(r, c)], switches[(r, c + 1)])
            if r + 1 < n_side:
                net.addLink(switches[(r, c)], switches[(r + 1, c)])

    # host server
    hS = net.addHost("hS", cpu=cpu)
    net.addLink(hS, switches[(0, 0)])

    # clients
    clients = []
    switch_positions = [(r, c) for r in range(n_side) for c in range(n_side)]
    attach_positions = [p for p in switch_positions if p != (0, 0)] or [(0, 0)]

    for i in range(n_clients):
        h = net.addHost(f"hC{i+1}", cpu=cpu)
        pos = attach_positions[i % len(attach_positions)]
        net.addLink(h, switches[pos])
        clients.append({"name": h.name, "pos": pos})

    net.start()

    # activar STP (si se pide)
    if stp:
        for sw in switches.values():
            run_host(f"ovs-vsctl set Bridge {sw.name} stp_enable=true >/dev/null 2>&1 || true")
        if stp_wait_s > 0:
            info(f"*** Esperando convergencia STP (~{stp_wait_s}s)...\n")
            time.sleep(stp_wait_s)

    topo_info = {
        "type": "grid_mesh",
        "n_side": n_side,
        "switches": [sw.name for sw in switches.values()],
        "server_switch": switches[(0, 0)].name,
        "clients": clients,
        "stp": stp,
        "stp_wait_s": stp_wait_s,
    }
    return net, topo_info


def gather_link_intfs(net: Mininet) -> Dict[str, List[str]]:
    """
    Retorna {'core': [...], 'access': [...]} con nombres de interfaces.
    - core: switch-switch
    - access: host-switch (incluye hS y hC*)
    """
    core_intfs: List[str] = []
    access_intfs: List[str] = []

    for lk in net.links:
        n1 = lk.intf1.node
        n2 = lk.intf2.node
        if isinstance(n1, OVSSwitch) and isinstance(n2, OVSSwitch):
            core_intfs.append(lk.intf1.name)
            core_intfs.append(lk.intf2.name)
        else:
            if isinstance(n1, OVSSwitch) or isinstance(n2, OVSSwitch):
                access_intfs.append(lk.intf1.name)
                access_intfs.append(lk.intf2.name)

    def dedup(xs):
        seen = set()
        out = []
        for x in xs:
            if x not in seen:
                out.append(x)
                seen.add(x)
        return out

    return {"core": dedup(core_intfs), "access": dedup(access_intfs)}


# -------------------------
# Tráfico: iperf3 + ping + HTTP “segmentos”
# -------------------------
def start_iperf_servers(hS, base_port: int, count: int) -> List[int]:
    """Start multiple iperf3 server instances on consecutive ports (one per client)."""
    hS.cmd("pkill -f 'iperf3 -s' >/dev/null 2>&1 || true")
    time.sleep(0.3)
    ports = []
    for i in range(count):
        p = base_port + i
        hS.cmd(f"iperf3 -s -p {p} -D")
        ports.append(p)
    time.sleep(0.3)
    return ports


def stop_iperf_servers(hS) -> None:
    hS.cmd("pkill -f 'iperf3 -s' >/dev/null 2>&1 || true")


def run_iperf_client(hC, server_ip: str, seconds: int, port: int) -> Dict:
    out = hC.cmd(f"iperf3 -c {server_ip} -t {seconds} -p {port} -J")
    js = safe_json_load(out)
    if js is None:
        return {"ok": False, "raw": out}
    return {"ok": True, "json": js}


def run_ping_cmd(hC, server_ip: str, count: int, interval_s: float = 0.2) -> Dict:
    out = hC.cmd(f"ping -c {count} -i {interval_s} {server_ip}")
    return parse_ping(out)


def prepare_http_content(src_dir: str, dst_dir: str, max_files: int) -> List[str]:
    if not os.path.isdir(src_dir):
        raise FileNotFoundError(f"--http_src_dir no existe: {src_dir}")
    os.makedirs(dst_dir, exist_ok=True)

    for name in os.listdir(dst_dir):
        p = os.path.join(dst_dir, name)
        if os.path.isfile(p):
            os.remove(p)

    files = []
    for root, _, fnames in os.walk(src_dir):
        for f in fnames:
            files.append(os.path.join(root, f))
    files.sort()

    chosen = files[:max_files] if max_files > 0 else files
    outnames = []
    for i, fpath in enumerate(chosen):
        base = os.path.basename(fpath)
        outname = f"{i:05d}_{base}"
        shutil.copy2(fpath, os.path.join(dst_dir, outname))
        outnames.append(outname)
    return outnames


def start_http_server_threaded(hS, serve_dir: str, port: int) -> None:
    """Servidor HTTP multihilo para soportar descargas concurrentes."""
    hS.cmd("pkill -f 'threaded_http_srv' >/dev/null 2>&1 || true")
    hS.cmd("pkill -f 'python3 -m http.server' >/dev/null 2>&1 || true")

    hS.cmd(
        f"cat > /tmp/threaded_http_srv.py << 'PYEOF'\n"
        f"import os\n"
        f"from http.server import HTTPServer, SimpleHTTPRequestHandler\n"
        f"from socketserver import ThreadingMixIn\n"
        f"class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):\n"
        f"    daemon_threads = True\n"
        f"os.chdir('{serve_dir}')\n"
        f"ThreadedHTTPServer(('', {port}), SimpleHTTPRequestHandler).serve_forever()\n"
        f"PYEOF"
    )
    hS.cmd(f"nohup python3 /tmp/threaded_http_srv.py >/tmp/http_{port}.log 2>&1 &")
    time.sleep(0.3)


def stop_http_server(hS) -> None:
    hS.cmd("pkill -f 'threaded_http_srv' >/dev/null 2>&1 || true")
    hS.cmd("pkill -f 'python3 -m http.server' >/dev/null 2>&1 || true")


def http_download_segments(
    hC,
    server_ip: str,
    port: int,
    filenames: List[str],
    quality_schedule: List[int],
    playback_rate_segments_per_s: float,
    initial_buffer_s: float,
) -> Dict:
    seg_results: List[SegmentResult] = []
    hC.cmd("rm -rf /tmp/dl && mkdir -p /tmp/dl")

    for i, fname in enumerate(filenames):
        q = quality_schedule[i] if i < len(quality_schedule) else quality_schedule[-1]
        url = f"http://{server_ip}:{port}/{fname}"
        out = hC.cmd(f"curl -L -s -o /tmp/dl/{fname} -w '%{{size_download}} %{{time_total}}' {url}")

        m = re.search(r"(\d+)\s+([\d.]+)", out.strip())
        if m:
            b = int(m.group(1))
            dt = float(m.group(2))
        else:
            b = 0
            dt = 0.000001

        goodput_mbps = (b * 8.0) / (dt * 1e6) if dt > 0 else 0.0
        seg_results.append(
            SegmentResult(
                idx=i,
                quality=q,
                bytes=b,
                download_time_s=dt,
                goodput_mbps=goodput_mbps,
                stall_s=0.0,
            )
        )

    qoe = simulate_qoe_from_segments(seg_results, playback_rate_segments_per_s, initial_buffer_s)

    total_bytes = sum(s.bytes for s in seg_results)
    total_time = sum(s.download_time_s for s in seg_results)
    total_goodput_mbps = (total_bytes * 8.0) / (total_time * 1e6) if total_time > 0 else 0.0

    return {
        "segments": [asdict(s) for s in seg_results],
        "http_total": {"bytes": total_bytes, "time_s": total_time, "goodput_mbps": total_goodput_mbps},
        "qoe": qoe,
        "enabled": True,
    }


# -------------------------
# Escenarios de descarga
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
        k = max(1, (n + 1) // 2)  # ceil(n/2)
    else:  # "one"
        k = 1

    if select == "random":
        rng = random.Random(seed if seed is not None else None)
        shuffled = list(client_names)
        rng.shuffle(shuffled)
        return shuffled[:k]

    # "first"
    return list(client_names)[:k]


# -------------------------
# Un run completo (1 experimento) para un scenario
# -------------------------
def run_experiment_once(args, run_idx: int, scenario: str) -> Dict:
    # Limpieza previa
    mn_cleanup()

    # Seed reproducible por run
    seed = args.seed + run_idx if args.seed is not None else None
    if seed is not None:
        random.seed(seed)

    # Build net
    net, topo_info = build_grid_mesh(args.n_side, args.clients, args.cpu, stp=args.stp, stp_wait_s=args.stp_wait_s)
    hS = net.get("hS")
    server_ip = hS.IP()

    # TCP CC
    if args.tcp_cc:
        set_tcp_cc(hS, args.tcp_cc)
        for i in range(args.clients):
            set_tcp_cc(net.get(f"hC{i+1}"), args.tcp_cc)

    # Aplicar TC
    tc_applied = []
    link_intfs = gather_link_intfs(net)
    try:
        if args.tc_core:
            for intf in link_intfs["core"]:
                tc_applied.append(apply_tc(intf, args.bw, args.delay, args.jitter, args.loss))
        if args.tc_access:
            for intf in link_intfs["access"]:
                tc_applied.append(apply_tc(intf, args.bw_access, args.delay_access, args.jitter_access, args.loss_access))
    except Exception as e:
        info(f"[WARN] Falló apply_tc en alguna interfaz: {e}\n")

    # HTTP setup
    http_info = {"enabled": False}
    filenames = []
    serve_dir = "/tmp/serve_paper3"
    if args.http_src_dir:
        filenames = prepare_http_content(args.http_src_dir, serve_dir, args.max_files)
        start_http_server_threaded(hS, serve_dir, args.http_port)
        http_info = {"enabled": True, "serve_dir": serve_dir, "port": args.http_port, "file_count": len(filenames)}
    else:
        info("[WARN] --http_src_dir no especificado: métricas HTTP/QoE no se calcularán.\n")

    # Services iperf per client
    iperf_ports = start_iperf_servers(hS, args.iperf_base_port, args.clients)

    clients = [net.get(f"hC{i+1}") for i in range(args.clients)]
    client_names = [h.name for h in clients]

    active_client_names = choose_active_clients(
        client_names=client_names,
        scenario=scenario,
        select=args.scenario_select,
        seed=seed,
    )
    active_set = set(active_client_names)
    active_count = max(1, len(active_client_names))
    barrier = threading.Barrier(active_count)

    per_client: Dict[str, Dict] = {}
    lock = threading.Lock()

    def _test_single_client(hC, iperf_port: int):
        name = hC.name
        is_active = (name in active_set)

        # schedule calidad (solo se necesita si este host descarga)
        schedule = []
        if is_active and http_info["enabled"] and filenames:
            K = max(1, args.quality_levels)
            if args.quality_mode == "fixed":
                schedule = [K - 1] * len(filenames)
            elif args.quality_mode == "random":
                schedule = [random.randint(0, K - 1) for _ in filenames]
            else:  # roundrobin
                schedule = [(i % K) for i in range(len(filenames))]

        # sincroniza inicio SOLO para activos (para que “one/half/all” sea comparable)
        if is_active:
            barrier.wait()

        # Ping
        ping_res = run_ping_cmd(hC, server_ip, args.ping_count)

        # iperf
        iperf_res = run_iperf_client(hC, server_ip, args.iperf_sec, port=iperf_port)
        iperf_kpis = {"ok": False}
        if iperf_res.get("ok"):
            js = iperf_res["json"]
            try:
                bps = js["end"]["sum_received"]["bits_per_second"]
                thr_mbps = float(bps) / 1e6
                iperf_kpis = {
                    "ok": True,
                    "throughput_mbps": thr_mbps,
                    "retransmits": js["end"].get("sum_sent", {}).get("retransmits"),
                    "seconds": js["end"]["sum_received"].get("seconds"),
                    "cpu_util": js["end"].get("cpu_utilization_percent"),
                    "tcp_mss_default": js["start"].get("tcp_mss_default"),
                    "snd_cwnd_bytes_last": js["end"].get("streams", [{}])[0].get("sender", {}).get("snd_cwnd"),
                    "rtt_ms_last": js["end"].get("streams", [{}])[0].get("sender", {}).get("rtt"),
                    "rttvar_ms_last": js["end"].get("streams", [{}])[0].get("sender", {}).get("rttvar"),
                }
            except Exception:
                iperf_kpis = {"ok": True, "note": "No se pudieron extraer KPIs; ver raw json", "raw": js}

        # HTTP/QoE + timeline (solo si es active downloader)
        http_res = {"enabled": False}
        download_timeline = {"enabled": False}
        if is_active and http_info["enabled"] and filenames:
            download_timeline = {"enabled": True, "start_ms": now_ms()}
            http_res = http_download_segments(
                hC=hC,
                server_ip=server_ip,
                port=args.http_port,
                filenames=filenames,
                quality_schedule=schedule,
                playback_rate_segments_per_s=args.playback_rate,
                initial_buffer_s=args.initial_buffer,
            )
            download_timeline["end_ms"] = now_ms()

        with lock:
            per_client[name] = {
                "client_ip": hC.IP(),
                "is_active_downloader": is_active,
                "download_timeline": download_timeline,
                "ping": ping_res,
                "iperf3": {
                    "kpis": iperf_kpis,
                    "raw": iperf_res.get("json") if iperf_res.get("ok") else iperf_res.get("raw"),
                },
                "http_qoe": http_res,
            }

    # Lanzar threads (todos hacen ping/iperf; solo algunos descargan)
    threads = []
    for i, hC in enumerate(clients):
        t = threading.Thread(target=_test_single_client, args=(hC, iperf_ports[i]), name=f"{scenario}-run{run_idx}-{hC.name}")
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # Stop services
    stop_iperf_servers(hS)
    if http_info["enabled"]:
        stop_http_server(hS)

    # Fairness + summary
    thr_vals = []
    gp_vals = []
    qoe_vals = []

    for cdata in per_client.values():
        thr = cdata["iperf3"]["kpis"].get("throughput_mbps")
        if thr is not None:
            thr_vals.append(float(thr))

        if cdata.get("http_qoe", {}).get("enabled"):
            gp = cdata["http_qoe"].get("http_total", {}).get("goodput_mbps")
            if gp is not None:
                gp_vals.append(float(gp))
            score = qoe_score_simple(cdata["http_qoe"].get("qoe", {}))
            if score is not None:
                qoe_vals.append(float(score))

    fairness = {
        "jain_throughput": jains_fairness(thr_vals),
        "jain_http_goodput": jains_fairness(gp_vals),
        "jain_qoe_score": jains_fairness(qoe_vals),
        "inputs": {"throughput_mbps": thr_vals, "http_goodput_mbps": gp_vals, "qoe_score_simple": qoe_vals},
    }

    # Promedios globales (HTTP/QoE solo sobre activos que descargaron)
    startup_list = []
    stall_list = []
    for c in per_client.values():
        if c.get("http_qoe", {}).get("enabled"):
            q = c["http_qoe"].get("qoe", {})
            if q.get("startup_delay_s") is not None:
                startup_list.append(float(q["startup_delay_s"]))
            stall_list.append(float(q.get("stall_time_s", 0.0)))

    summary = {
        "clients": args.clients,
        "active_downloaders_count": len(active_client_names),
        "success_ping_clients": sum(
            1
            for c in per_client.values()
            if c["ping"].get("loss_pct") is not None and c["ping"]["loss_pct"] < 100.0
        ),
        "success_iperf_clients": sum(1 for c in per_client.values() if c["iperf3"]["kpis"].get("ok") is True),
        "mean_throughput_mbps": statistics.mean(thr_vals) if thr_vals else None,
        "mean_http_goodput_mbps": statistics.mean(gp_vals) if gp_vals else None,
        "mean_startup_delay_s": statistics.mean(startup_list) if startup_list else None,
        "mean_stall_time_s": statistics.mean(stall_list) if stall_list else None,
    }

    result = {
        "meta": {
            "ts_ms": now_ms(),
            "script": os.path.basename(__file__),
            "run_idx": run_idx,
            "seed": seed,
            "scenario": scenario,
            "active_downloaders": sorted(list(active_client_names)),
            "notes": "MJ v3: escenarios one/half/all + timeline descarga + reproducibilidad + TCP CC + TC core/access.",
        },
        "config": {
            "topology": topo_info,
            "profile_6g_core": {"bw_mbps": args.bw, "delay_ms": args.delay, "jitter_ms": args.jitter, "loss_pct": args.loss}
            if args.tc_core
            else None,
            "profile_6g_access": {
                "bw_mbps": args.bw_access,
                "delay_ms": args.delay_access,
                "jitter_ms": args.jitter_access,
                "loss_pct": args.loss_access,
            }
            if args.tc_access
            else None,
            "tests": {"ping_count": args.ping_count, "iperf_sec": args.iperf_sec},
            "http": http_info,
            "qoe_model": {"playback_rate_segments_per_s": args.playback_rate, "initial_buffer_s": args.initial_buffer},
            "quality": {"levels": args.quality_levels, "mode": args.quality_mode},
            "tcp_cc": args.tcp_cc,
            "tc_core": args.tc_core,
            "tc_access": args.tc_access,
            "download_scenario": scenario,
            "scenario_select": args.scenario_select,
        },
        "tc_applied": tc_applied,
        "per_client": per_client,
        "fairness": fairness,
        "summary": summary,
    }

    # Stop net + cleanup
    try:
        net.stop()
    except Exception:
        pass
    mn_cleanup()

    return result


# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()

    # Topología
    ap.add_argument("--n_side", type=int, default=3, help="Lado del grid de switches (3 => 9 switches, ...)")
    ap.add_argument("--clients", type=int, default=6, help="Número de clientes")
    ap.add_argument("--cpu", type=float, default=0.5, help="CPU share por host (Mininet)")
    ap.add_argument("--stp", action="store_true", help="Activar STP en OVS bridges")
    ap.add_argument("--stp_wait_s", type=int, default=30, help="Segundos a esperar por convergencia STP")

    # TC core (switch-switch)
    ap.add_argument("--tc_core", action="store_true", help="Aplicar TC shaping en enlaces core (switch-switch)")
    ap.add_argument("--bw", type=float, default=100000.0, help="BW Mbps (core)")
    ap.add_argument("--delay", type=float, default=2.0, help="Delay ms (core)")
    ap.add_argument("--jitter", type=float, default=0.2, help="Jitter ms (core)")
    ap.add_argument("--loss", type=float, default=0.01, help="Loss pct (core)")

    # TC access (host-switch)
    ap.add_argument("--tc_access", action="store_true", help="Aplicar TC shaping en enlaces access (host-switch)")
    ap.add_argument("--bw_access", type=float, default=100000.0, help="BW Mbps (access)")
    ap.add_argument("--delay_access", type=float, default=2.0, help="Delay ms (access)")
    ap.add_argument("--jitter_access", type=float, default=0.2, help="Jitter ms (access)")
    ap.add_argument("--loss_access", type=float, default=0.01, help="Loss pct (access)")

    # Tests
    ap.add_argument("--ping_count", type=int, default=20)
    ap.add_argument("--iperf_sec", type=int, default=10)
    ap.add_argument("--iperf_base_port", type=int, default=5201)

    # HTTP/QoE
    ap.add_argument("--http_src_dir", type=str, default="", help="Directorio con archivos para servir por HTTP")
    ap.add_argument("--http_port", type=int, default=8000)
    ap.add_argument("--max_files", type=int, default=1, help="Máximo de archivos/segmentos a servir y descargar")
    ap.add_argument("--playback_rate", type=float, default=1.0, help="Segmentos por segundo (modelo QoE)")
    ap.add_argument("--initial_buffer", type=float, default=2.0, help="Buffer inicial en segundos (modelo QoE)")

    # Calidad
    ap.add_argument("--quality_levels", type=int, default=3)
    ap.add_argument("--quality_mode", type=str, default="roundrobin", choices=["fixed", "roundrobin", "random"])

    # Reproducibilidad + multi-run
    ap.add_argument("--runs", type=int, default=1, help="Número de repeticiones por escenario")
    ap.add_argument("--seed", type=int, default=2000, help="Seed base (se incrementa por run)")
    ap.add_argument("--tcp_cc", type=str, default="", help="Congestion control (cubic, bbr, reno...). Vacío = no cambia")

    # Escenarios de descarga
    ap.add_argument(
        "--download_scenarios",
        type=str,
        default="all",
        help="Escenarios HTTP: lista separada por coma: one,half,all (ej: one,half,all)",
    )
    ap.add_argument(
        "--scenario_select",
        type=str,
        default="first",
        choices=["first", "random"],
        help="Selección de hosts activos en one/half: first (determinístico) o random (usa seed).",
    )

    # Output
    ap.add_argument("--out", type=str, default="results_mj_v3.json", help="Archivo JSON de salida")

    args = ap.parse_args()
    setLogLevel("info")

    # Ctrl+C robusto
    def _sigint(_signo, _frame):
        try:
            mn_cleanup()
        finally:
            sys.exit(130)

    signal.signal(signal.SIGINT, _sigint)

    scenarios = parse_scenarios(args.download_scenarios)

    per_run: List[Dict] = []
    for sc in scenarios:
        for r in range(args.runs):
            info(f"\n=== SCENARIO {sc.upper()} | RUN {r+1}/{args.runs} ===\n")
            per_run.append(run_experiment_once(args, r, scenario=sc))

    # Aggregate stats (sobre métricas globales por run)
    agg_thr = [run["summary"].get("mean_throughput_mbps") for run in per_run]
    agg_gp = [run["summary"].get("mean_http_goodput_mbps") for run in per_run]
    agg_start = [run["summary"].get("mean_startup_delay_s") for run in per_run]
    agg_stall = [run["summary"].get("mean_stall_time_s") for run in per_run]
    agg_jain_thr = [run["fairness"].get("jain_throughput") for run in per_run]
    agg_jain_gp = [run["fairness"].get("jain_http_goodput") for run in per_run]
    agg_jain_qoe = [run["fairness"].get("jain_qoe_score") for run in per_run]

    aggregate_stats = {
        "mean_throughput_mbps": mean_std_ci95([x for x in agg_thr if x is not None]),
        "mean_http_goodput_mbps": mean_std_ci95([x for x in agg_gp if x is not None]),
        "mean_startup_delay_s": mean_std_ci95([x for x in agg_start if x is not None]),
        "mean_stall_time_s": mean_std_ci95([x for x in agg_stall if x is not None]),
        "jain_throughput": mean_std_ci95([x for x in agg_jain_thr if x is not None]),
        "jain_http_goodput": mean_std_ci95([x for x in agg_jain_gp if x is not None]),
        "jain_qoe_score": mean_std_ci95([x for x in agg_jain_qoe if x is not None]),
    }

    output = {
        "meta": {
            "ts_ms": now_ms(),
            "script": os.path.basename(__file__),
            "notes": "MJ v3 aggregate: escenarios one/half/all + timeline + multi-run + CI95.",
        },
        "config": {
            "n_side": args.n_side,
            "clients": args.clients,
            "cpu": args.cpu,
            "stp": args.stp,
            "stp_wait_s": args.stp_wait_s,
            "tc_core": args.tc_core,
            "tc_access": args.tc_access,
            "core_profile": {"bw_mbps": args.bw, "delay_ms": args.delay, "jitter_ms": args.jitter, "loss_pct": args.loss}
            if args.tc_core
            else None,
            "access_profile": {
                "bw_mbps": args.bw_access,
                "delay_ms": args.delay_access,
                "jitter_ms": args.jitter_access,
                "loss_pct": args.loss_access,
            }
            if args.tc_access
            else None,
            "tests": {"ping_count": args.ping_count, "iperf_sec": args.iperf_sec, "iperf_base_port": args.iperf_base_port},
            "http": {"src_dir": args.http_src_dir, "port": args.http_port, "max_files": args.max_files},
            "qoe_model": {"playback_rate_segments_per_s": args.playback_rate, "initial_buffer_s": args.initial_buffer},
            "quality": {"levels": args.quality_levels, "mode": args.quality_mode},
            "runs_per_scenario": args.runs,
            "seed": args.seed,
            "tcp_cc": args.tcp_cc,
            "download_scenarios": scenarios,
            "scenario_select": args.scenario_select,
        },
        "per_run": per_run,
        "aggregate_stats": aggregate_stats,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    info(f"\n[OK] Resultados guardados en: {args.out}\n")


if __name__ == "__main__":
    try:
        main()
    finally:
        mn_cleanup()