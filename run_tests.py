import json
import os
import time
from statistics import mean, quantiles

from iperf_client import run_test, save_csv

DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS = os.path.join(DIR, "data", "iperf3_results.json")
SUMMARY = os.path.join(DIR, "data", "summary.json")
RUNS = os.path.join(DIR, "data", "runs")

CCS = ["cubic", "reno", "bbr", "adaptive"]
# regime by ping RTT: low < 50 ms, mid 100-150 ms, high >= 200 ms
# picked for several ports (fallback when one is busy), stable ping and 10G+ links
TARGETS = [
    ("low", "chi.speedtest.clouvider.net"),
    ("mid", "lon.speedtest.clouvider.net"),
    ("mid", "iperf-ams-nl.eranium.net"),
    ("high", "speedtest.uztelecom.uz"),
    ("high", "23.249.58.14"),
]
TRIALS = 2  # CC order rotates every trial so no algorithm always runs first
DURATION = 30
MAX_PORTS = 5


def ports(spec):
    lo, _, hi = spec.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))[:MAX_PORTS]


def try_run(server, cc):
    for port in ports(server["PORT"]):
        try:
            print(f"  {cc} -> {server['IP/HOST']}:{port}")
            samples = run_test(server["IP/HOST"], port, cc, DURATION)
        except (PermissionError, FileNotFoundError):
            raise  # cc not allowed / not loaded, every port would fail the same way
        except (OSError, ValueError) as e:
            print(f"  failed: {e}")
            continue
        # a userspace NAT proxy (e.g. Docker Desktop) ends TCP locally, TCP_INFO then shows the proxy hop
        if samples[-1]["min_rtt_ms"] < server["mean_rtt"] / 2:
            print(f"  warning: TCP min RTT {samples[-1]['min_rtt_ms']} ms << ping {server['mean_rtt']} ms, "
                  "connection is proxied and stats are not end-to-end")
        return samples
    return None


def run_trial(server, trial):
    results = {}
    k = trial % len(CCS)
    for cc in CCS[k:] + CCS[:k]:
        samples = try_run(server, cc)
        if not samples:
            return None
        results[cc] = samples
        time.sleep(2)
    return results


def stats(samples):
    last = samples[-1]
    return {
        "mean_mbps": round(last["bytes_acked"] * 8 / last["t"] / 1e6, 2),
        "mean_rtt_ms": round(mean(s["rtt_ms"] for s in samples), 2),
        "p95_rtt_ms": round(quantiles([s["rtt_ms"] for s in samples], n=20)[-1], 2),
        "loss_rate": round(last["total_retrans"] / last["data_segs_out"], 5) if last["data_segs_out"] else 0,
        "switches": sum(a["cc"] != b["cc"] for a, b in zip(samples, samples[1:])),
    }


def main():
    os.makedirs(RUNS, exist_ok=True)
    with open(SERVERS) as f:
        by_host = {s["IP/HOST"]: s for s in json.load(f)}

    summary = []
    for regime, host in TARGETS:
        server = by_host[host]
        print(f"[{regime}] {server['SITE']} {host} (ping {server['mean_rtt']:.0f} ms)")

        # host in the name, many servers share a SITE
        name = "_".join((regime, "".join(c for c in server["SITE"] if c.isalnum()), host))
        for trial in range(TRIALS):
            results = run_trial(server, trial)
            if not results:
                break
            for cc, samples in results.items():
                save_csv(samples, os.path.join(RUNS, f"{name}_t{trial + 1}_{cc}.csv"))
                summary.append({"regime": regime, "site": server["SITE"], "host": host,
                                "ping_ms": server["mean_rtt"], "trial": trial + 1, "cc": cc, **stats(samples)})
            # saved after every trial so a crash keeps what finished
            with open(SUMMARY, "w") as f:
                json.dump(summary, f, indent=4)


if __name__ == "__main__":
    main()
