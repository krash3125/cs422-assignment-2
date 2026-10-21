"""Single entry point: pick destinations, run every CC algorithm, save raw data, then make plots and tables."""
import argparse
import csv
import json
import os
import platform
import random
import socket
import sys
import time
from datetime import datetime

import analyze
import servers as server_list
from iperf_client import TCP_CONGESTION, run_test

RETRYABLE = {"ACCESS_DENIED", "CONNECT_REFUSED", "CONNECT_TIMEOUT", "CONNECT_ERROR",
             "CTRL_TIMEOUT", "PREMATURE_CLOSE", "SERVER_ERROR", "SERVER_TERMINATE", "PROTOCOL_ERROR"}

UNITS = {
    "t_s": "seconds since data started", "interval_s": "seconds since previous sample",
    "goodput_bps": "bits/s, delta tcpi_bytes_acked * 8 / interval", "send_rate_bps": "bits/s written by the app",
    "cwnd_pkts": "segments (tcpi_snd_cwnd)", "cwnd_bytes": "bytes (cwnd * snd_mss)", "srtt_ms": "ms (tcpi_rtt)",
    "rttvar_ms": "ms", "min_rtt_ms": "ms", "retrans_pkts_delta": "segments retransmitted this interval",
    "retrans_bytes_delta": "bytes retransmitted this interval", "retrans_ratio": "retransmitted / sent bytes",
    "rto_events_delta": "retransmission timeouts this interval (kernel >= 6.7)", "lost_pkts": "segments currently marked lost",
    "ca_state": "0 Open, 1 Disorder, 2 CWR, 3 Recovery, 4 Loss", "pacing_rate_bps": "bits/s",
    "delivery_rate_bps": "bits/s (kernel delivery-rate estimate)", "rwnd_limited_frac": "fraction of busy time limited by receiver window",
}


def read_proc(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def preflight(algos):
    """Record host/kernel facts and check every algorithm can be set on a socket."""
    meta = {
        "kernel_release": platform.release(),
        "kernel_version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "os_release": read_proc("/etc/os-release").split("\n")[0],
        "available_cc": read_proc("/proc/sys/net/ipv4/tcp_available_congestion_control"),
        "allowed_cc": read_proc("/proc/sys/net/ipv4/tcp_allowed_congestion_control"),
        "default_cc": read_proc("/proc/sys/net/ipv4/tcp_congestion_control"),
        "default_qdisc": read_proc("/proc/sys/net/core/default_qdisc"),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "cc_check": {},
    }
    for cc in algos:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, cc.encode())
            meta["cc_check"][cc] = "ok"
        except OSError as e:
            meta["cc_check"][cc] = str(e)
        finally:
            s.close()
    return meta


def append_csv(path, rows):
    if not rows:
        return
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def run_with_retries(dest, cc, args, rng, log):
    """Run one test; on a transient failure wait and retry, trying another port of the same host."""
    result = None
    for attempt in range(args.retries + 1):
        port = dest["port"] if attempt == 0 else rng.choice(dest["ports"])
        result = run_test(dest["host"], port, cc, args.duration, args.interval)
        log(result, attempt)
        if result.status == "ok" or result.status not in RETRYABLE:
            return result
        time.sleep(args.backoff * (attempt + 1))
    return result


def run_all(args, out_dir):
    algos = [a.strip() for a in args.algos.split(",") if a.strip()]
    meta = preflight(algos)
    bad = {cc: msg for cc, msg in meta["cc_check"].items() if msg != "ok"}
    if bad:
        print(f"Cannot use these congestion control algorithms: {bad}")
        print("Fix on the host: sudo modprobe tcp_bbr; run docker with --network host --cap-add NET_ADMIN")
        sys.exit(1)

    run_id = os.path.basename(out_dir)
    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(seed)
    meta.update({"run_id": run_id, "started": datetime.now().isoformat(timespec="seconds"),
                 "args": vars(args), "seed": seed, "units": UNITS})
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)  # Saved now and again at the end, so a crashed run still has it.
    attempts_path = os.path.join(out_dir, "attempts.csv")

    def log_select(row):
        row = {"time": datetime.now().isoformat(timespec="seconds"), "rep": 0, "seq": 0, **row}
        append_csv(attempts_path, [row])
        print(f"  probe {row['host']}:{row['port']} -> {row['status']} {row['error']}")

    if args.refresh_servers:
        print(f"Refreshed {server_list.refresh_from_web(args.servers)} servers into {args.servers}")
    servers = server_list.load_servers(args.servers)
    print(f"Selecting {args.num_dest} destinations from {len(servers)} servers (seed={seed})")
    dests, shortfall = server_list.select_destinations(servers, args.num_dest, args.max_attempts, rng, log_select)
    with open(os.path.join(out_dir, "destinations.json"), "w") as f:
        json.dump({"requested": args.num_dest, "selected": dests, "shortfall": shortfall,
                   "max_attempts": args.max_attempts, "seed": seed}, f, indent=2)
    if shortfall:
        print(f"WARNING: only found {len(dests)} of {args.num_dest} destinations (shortfall {shortfall})")
    if not dests:
        sys.exit("No working destinations found.")

    seq = 0
    total = args.reps * len(dests) * len(algos)
    for rep in range(1, args.reps + 1):
        shift = (rep - 1) % len(algos)
        order = algos[shift:] + algos[:shift]  # Rotate the order each rep so no algorithm always goes first.
        for dest in dests:
            for cc in order:
                seq += 1

                def log_main(r, attempt):
                    append_csv(attempts_path, [{"time": datetime.now().isoformat(timespec="seconds"), "rep": rep,
                                                "seq": seq, "phase": f"main#{attempt}", "host": r.host,
                                                "port": r.port, "cc": r.cc_requested, "status": r.status,
                                                "error": r.error}])

                r = run_with_retries(dest, cc, args, rng, log_main)
                test_id = f"{run_id}-r{rep}-s{seq:03d}"
                info = {"run_id": run_id, "test_id": test_id, "rep": rep, "seq": seq, "host": r.host,
                        "ip": r.ip, "port": r.port, "cc_requested": r.cc_requested, "cc_used": r.cc_used,
                        "complete": r.complete}
                append_csv(os.path.join(out_dir, "samples.csv"), [{**info, **s} for s in r.samples])
                summary = {**info, "status": r.status, "error": r.error, "start_time": r.start_time,
                           "elapsed_s": r.elapsed_s, "bytes_written": r.bytes_written, "bytes_acked": r.bytes_acked,
                           "server_bytes": r.server_bytes, "tcp_info_len": r.tcp_info_len,
                           "n_samples": len(r.samples), "server_results": r.server_results}
                with open(os.path.join(out_dir, "tests.jsonl"), "a") as f:
                    f.write(json.dumps(summary) + "\n")
                meta["tcp_info_len"] = r.tcp_info_len or meta.get("tcp_info_len", 0)
                mean = sum(s["goodput_bps"] for s in r.samples) / len(r.samples) / 1e6 if r.samples else 0
                print(f"[{seq}/{total}] rep {rep} {r.host}:{r.port} {cc}->{r.cc_used or '?'} "
                      f"{r.status} mean goodput {mean:.1f} Mbps {r.error}")
                time.sleep(args.gap)

    meta["finished"] = datetime.now().isoformat(timespec="seconds")
    meta["test_order"] = "for rep: for destination: algorithms in rotated order"
    with open(os.path.join(out_dir, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Run the full CS422 A2 experiment pipeline.")
    parser.add_argument("--servers", default="servers.csv", help="CSV with host,port columns (port may be a range)")
    parser.add_argument("-n", "--num-dest", type=int, default=10, help="number of destinations")
    parser.add_argument("-t", "--duration", type=float, default=30, help="seconds per test")
    parser.add_argument("-i", "--interval", type=float, default=0.2, help="sampling interval in seconds")
    parser.add_argument("-r", "--reps", type=int, default=3, help="repetitions of every destination x algorithm")
    parser.add_argument("--algos", default="cubic,reno,bbr", help="comma-separated congestion control list")
    parser.add_argument("--seed", type=int, default=None, help="random seed for destination choice")
    parser.add_argument("--max-attempts", type=int, default=40, help="max probe attempts while selecting destinations")
    parser.add_argument("--retries", type=int, default=3, help="retries per test on transient errors")
    parser.add_argument("--backoff", type=float, default=5, help="seconds to wait before retry (grows each retry)")
    parser.add_argument("--gap", type=float, default=1, help="seconds to pause between tests")
    parser.add_argument("--out", default="results", help="parent folder for run output")
    parser.add_argument("--rep-dest", default=None, help="host to use for the Q2/Q3 detail plots")
    parser.add_argument("--analyze-only", default=None, metavar="RUN_DIR", help="skip tests, re-plot RUN_DIR")
    parser.add_argument("--refresh-servers", action="store_true", help="download a fresh server list first")
    args = parser.parse_args()

    if args.analyze_only:
        out_dir = args.analyze_only
    else:
        out_dir = os.path.join(args.out, datetime.now().strftime("%Y%m%d-%H%M%S"))
        run_all(args, out_dir)
    analyze.run(out_dir, args.rep_dest)
    print(f"Done. Results in {out_dir}")


if __name__ == "__main__":
    main()
