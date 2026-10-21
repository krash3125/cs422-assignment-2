import csv
import glob
import json
import os
from collections import defaultdict
from statistics import mean, stdev

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DIR = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(DIR, "data", "runs")
SUMMARY = os.path.join(DIR, "data", "summary.json")
PLOTS = os.path.join(DIR, "plots")

METRICS = [("cwnd", "cwnd (segments)"), ("rtt_ms", "RTT (ms)"), ("throughput_mbps", "Throughput (Mbps)"),
           ("loss_rate", "Retransmit rate")]
SUMMARY_METRICS = [("mean_mbps", "Mean throughput (Mbps)"), ("mean_rtt_ms", "Mean RTT (ms)"),
                   ("p95_rtt_ms", "p95 RTT (ms)")]


def load(path):
    with open(path) as f:
        return [{k: v if k == "cc" else float(v) for k, v in row.items()} for row in csv.DictReader(f)]


def plot_runs():
    groups = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(RUNS, "*.csv"))):
        name, cc = os.path.basename(path)[:-4].rsplit("_", 1)
        groups[name][cc] = load(path)

    for name, runs in groups.items():
        fig, axes = plt.subplots(len(METRICS), 1, figsize=(10, 12), sharex=True)
        for cc, rows in runs.items():
            t = [r["t"] for r in rows]
            for ax, (key, label) in zip(axes, METRICS):
                ax.plot(t, [r[key] for r in rows], label=cc)
                ax.set_ylabel(label)

        # mark where the adaptive run switched algorithms
        adaptive = runs.get("adaptive", [])
        for prev, row in zip(adaptive, adaptive[1:]):
            if row["cc"] != prev["cc"]:
                for ax in axes:
                    ax.axvline(row["t"], color="gray", linestyle=":")
                axes[0].text(row["t"], axes[0].get_ylim()[1], row["cc"], fontsize=8, va="top")

        axes[0].set_title(name)
        axes[0].legend()
        axes[-1].set_xlabel("Time (s)")
        fig.tight_layout()
        fig.savefig(os.path.join(PLOTS, f"{name}.png"))
        plt.close(fig)


def plot_summary():
    with open(SUMMARY) as f:
        summary = json.load(f)
    # one bar per (server, cc), averaged over trials
    trials = defaultdict(list)
    for s in summary:
        trials[(f"{s['regime']}\n{s['site']}\n{s['host']}", s["cc"])].append(s)
    sites = list(dict.fromkeys(site for site, _ in trials))
    ccs = list(dict.fromkeys(cc for _, cc in trials))
    width = 0.8 / len(ccs)

    fig, axes = plt.subplots(len(SUMMARY_METRICS), 1, figsize=(12, 12))
    for ax, (key, label) in zip(axes, SUMMARY_METRICS):
        for i, cc in enumerate(ccs):
            values = [[s[key] for s in trials.get((site, cc), [])] for site in sites]
            ax.bar([x + i * width for x in range(len(sites))], [mean(v) if v else 0 for v in values], width,
                   yerr=[stdev(v) if len(v) > 1 else 0 for v in values], capsize=2, label=cc)
        ax.set_xticks([x + width * (len(ccs) - 1) / 2 for x in range(len(sites))], sites, fontsize=7)
        ax.set_ylabel(label)
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "summary.png"))
    plt.close(fig)


if __name__ == "__main__":
    os.makedirs(PLOTS, exist_ok=True)
    plot_runs()
    plot_summary()
