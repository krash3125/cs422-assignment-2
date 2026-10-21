"""Turn one run's raw data into PDF plots and summary tables for Q1, Q2 and Q3."""
import argparse
import json
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ALGO_ORDER = ["cubic", "reno", "bbr"]
CC_COLORS = {"cubic": "#2a78d6", "reno": "#eb6834", "bbr": "#1baf7a"}
REP_STYLES = ["-", "--", ":", "-."]
INK, MUTED, GRID, SHADE = "#0b0b0b", "#52514e", "#e4e3df", "#f3d9cf"

plt.rcParams.update({
    "figure.facecolor": "#fcfcfb", "axes.facecolor": "#fcfcfb", "savefig.facecolor": "#fcfcfb",
    "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 9, "axes.titlesize": 9, "lines.linewidth": 1.4,
    "legend.frameon": False,
})


def load(run_dir):
    samples = pd.read_csv(os.path.join(run_dir, "samples.csv"))
    tests = pd.read_json(os.path.join(run_dir, "tests.jsonl"), lines=True)
    ok_ids = set(tests.loc[tests["status"] == "ok", "test_id"])
    samples = samples[samples["test_id"].isin(ok_ids)].copy()
    samples["cc"] = samples["cc_used"]
    samples["goodput_mbps"] = samples["goodput_bps"] / 1e6
    return samples, tests


def algos_in(df):
    return [a for a in ALGO_ORDER if a in set(df["cc"])] + sorted(set(df["cc"]) - set(ALGO_ORDER))


def to_markdown(df):
    def fmt(v):
        return f"{v:.2f}" if isinstance(v, (float, np.floating)) else str(v)
    lines = ["| " + " | ".join(df.columns) + " |", "|" + "---|" * len(df.columns)]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(fmt(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def save_table(df, tables_dir, name):
    df.to_csv(os.path.join(tables_dir, name + ".csv"), index=False)
    with open(os.path.join(tables_dir, name + ".md"), "w") as f:
        f.write(to_markdown(df))


def short(host):
    return host if len(host) <= 28 else host[:26] + ".."


# ---------- Q1: goodput of every destination ----------

def q1_goodput_plots(samples, plots_dir):
    hosts = sorted(samples["host"].unique())
    for cc in algos_in(samples):
        ncols = 2
        nrows = math.ceil(len(hosts) / ncols)
        fig, axes = plt.subplots(nrows, ncols, figsize=(7.5, 1.7 * nrows), sharex=True, squeeze=False)
        for ax, host in zip(axes.flat, hosts):
            d = samples[(samples["host"] == host) & (samples["cc"] == cc)]
            for i, (rep, run) in enumerate(d.groupby("rep")):
                ax.plot(run["t_s"], run["goodput_mbps"], color=CC_COLORS.get(cc, INK),
                        linestyle=REP_STYLES[i % len(REP_STYLES)], label=f"rep {rep}")
            ax.set_title(short(host), loc="left")
            ax.set_ylabel("Mbps")
        for ax in axes.flat[len(hosts):]:
            ax.set_visible(False)
        floor_at_zero(axes)
        for ax in axes[-1]:
            ax.set_xlabel("time (s)")
        fig.suptitle(f"Q1: goodput over time, all destinations ({cc})", x=0.01, ha="left")
        fig.tight_layout()
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper right", ncol=3, fontsize=8, frameon=False)
        fig.savefig(os.path.join(plots_dir, f"q1_goodput_{cc}.pdf"))
        plt.close(fig)


def q1_summary(samples):
    g = samples.groupby(["host", "cc"])["goodput_mbps"]
    table = pd.DataFrame({
        "min_mbps": g.min(), "median_mbps": g.median(), "mean_mbps": g.mean(),
        "p95_mbps": g.quantile(0.95), "samples": g.size(),
        "runs": samples.groupby(["host", "cc"])["test_id"].nunique(),
    }).reset_index()
    table["cc"] = pd.Categorical(table["cc"], algos_in(samples))
    return table.sort_values(["host", "cc"])


# ---------- picking the representative destination and run ----------

def pick_rep_dest(samples):
    """Host with every algorithm x rep present whose CUBIC mean goodput is the median of such hosts."""
    algos = algos_in(samples)
    runs = samples.groupby(["host", "cc"])["test_id"].nunique().unstack(fill_value=0)
    full = runs[(runs[algos] == runs.values.max()).all(axis=1)].index.tolist()
    if not full:
        full = runs.sum(axis=1).sort_values(ascending=False).index[:1].tolist()
    base = "cubic" if "cubic" in algos else algos[0]
    means = samples[(samples["host"].isin(full)) & (samples["cc"] == base)].groupby("host")["goodput_mbps"].mean()
    means = means.sort_values()
    return means.index[len(means) // 2]


def representative_run(samples, host, cc):
    """Samples of the run whose mean goodput is the median among this host/algorithm's runs."""
    d = samples[(samples["host"] == host) & (samples["cc"] == cc)]
    means = d.groupby("test_id")["goodput_mbps"].mean().sort_values()
    return d[d["test_id"] == means.index[len(means) // 2]]


# ---------- Q2 / Q3 detail plots ----------

def shade_recovery(ax, run):
    """Shade the intervals where the kernel was in Recovery or Loss state."""
    for _, row in run[run["ca_state"] >= 3].iterrows():
        ax.axvspan(row["t_s"] - row["interval_s"], row["t_s"], color=SHADE, linewidth=0)


def draw_timeseries(axes, run, color):
    """Fill four stacked axes: cwnd, RTT, loss proxy, goodput."""
    axes[0].plot(run["t_s"], run["cwnd_pkts"], color=color)
    axes[0].set_ylabel("cwnd (segments)")
    axes[1].plot(run["t_s"], run["srtt_ms"], color=color, label="srtt")
    axes[1].fill_between(run["t_s"], run["srtt_ms"] - run["rttvar_ms"], run["srtt_ms"] + run["rttvar_ms"],
                         color=color, alpha=0.15, linewidth=0, label="± rttvar")
    axes[1].plot(run["t_s"], run["min_rtt_ms"], color=MUTED, linestyle="--", linewidth=1, label="min_rtt")
    axes[1].set_ylabel("RTT (ms)")
    shade_recovery(axes[2], run)
    axes[2].bar(run["t_s"], run["retrans_pkts_delta"], width=run["interval_s"].median() * 0.8, color=color)
    axes[2].set_ylabel("retrans segs\n/ interval")
    axes[3].plot(run["t_s"], run["goodput_mbps"], color=color)
    axes[3].set_ylabel("goodput (Mbps)")
    axes[3].set_xlabel("time (s)")


def floor_at_zero(axes):
    """Call after all data is drawn; earlier calls would freeze shared axes at the first panel's range."""
    for ax in np.ravel(axes):
        ax.set_ylim(bottom=0)


def q2_timeseries(run, path):
    cc, host = run["cc"].iloc[0], run["host"].iloc[0]
    fig, axes = plt.subplots(4, 1, figsize=(7.5, 7.5), sharex=True)
    draw_timeseries(axes, run, CC_COLORS.get(cc, INK))
    floor_at_zero(axes)
    axes[1].legend(fontsize=7, loc="upper right")
    axes[2].set_title("shaded = ca_state Recovery/Loss", loc="right", color=MUTED, fontsize=7)
    fig.suptitle(f"Q2: TCP stats over time, {host}, cc={cc}, run {run['test_id'].iloc[0]}", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


SCATTERS = [("cwnd_pkts", "cwnd (segments)"), ("srtt_ms", "srtt (ms)"), ("retrans_pkts_delta", "retrans segs / interval")]


def corr_text(x, y):
    if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
        return ""
    return f"Spearman ρ = {x.corr(y, method='spearman'):.2f}"


def q2_scatter(d, path):
    cc, host = d["cc"].iloc[0], d["host"].iloc[0]
    fig, axes = plt.subplots(1, 3, figsize=(7.5, 2.8), sharey=True)
    for ax, (col, label) in zip(axes, SCATTERS):
        ax.scatter(d[col], d["goodput_mbps"], s=10, color=CC_COLORS.get(cc, INK), alpha=0.5, linewidths=0)
        ax.set_xlabel(label)
        ax.set_title(corr_text(d[col], d["goodput_mbps"]), loc="left", fontsize=7, color=MUTED)
    axes[0].set_ylabel("goodput (Mbps)")
    fig.suptitle(f"Q2: TCP stats vs goodput, {host}, cc={cc}, all reps", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def q3_timeseries_grid(samples, host, path):
    algos = algos_in(samples)
    fig, axes = plt.subplots(4, len(algos), figsize=(7.5, 7.5), sharex=True, sharey="row", squeeze=False)
    for j, cc in enumerate(algos):
        run = representative_run(samples, host, cc)
        draw_timeseries(axes[:, j], run, CC_COLORS.get(cc, INK))
        axes[0, j].set_title(f"{cc} ({run['test_id'].iloc[0].split('-', 2)[-1]})", color=CC_COLORS.get(cc, INK))
        if j > 0:
            for ax in axes[:, j]:
                ax.set_ylabel("")
    floor_at_zero(axes)
    fig.suptitle(f"Q3: CUBIC vs Reno vs BBR over time, {host} (shared y-axis per row)", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def q3_timeseries_overlay(samples, host, path):
    cols = [("cwnd_pkts", "cwnd (segments)"), ("srtt_ms", "srtt (ms)"),
            ("retrans_pkts_delta", "retrans segs / interval"), ("goodput_mbps", "goodput (Mbps)")]
    fig, axes = plt.subplots(4, 1, figsize=(7.5, 7.5), sharex=True)
    for cc in algos_in(samples):
        run = representative_run(samples, host, cc)
        for ax, (col, label) in zip(axes, cols):
            ax.plot(run["t_s"], run[col], color=CC_COLORS.get(cc, INK), label=cc)
            ax.set_ylabel(label)
    floor_at_zero(axes)
    axes[0].legend(ncol=3, fontsize=8, loc="upper right")
    axes[-1].set_xlabel("time (s)")
    fig.suptitle(f"Q3: CUBIC vs Reno vs BBR overlaid, {host}", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def q3_scatter_grid(samples, host, path):
    algos = algos_in(samples)
    d_host = samples[samples["host"] == host]
    fig, axes = plt.subplots(3, len(algos), figsize=(7.5, 7), sharey=True, sharex="row", squeeze=False)
    for j, cc in enumerate(algos):
        d = d_host[d_host["cc"] == cc]
        for i, (col, label) in enumerate(SCATTERS):
            ax = axes[i, j]
            ax.scatter(d[col], d["goodput_mbps"], s=9, color=CC_COLORS.get(cc, INK), alpha=0.5, linewidths=0)
            ax.set_xlabel(label)
            ax.text(0.02, 0.97, corr_text(d[col], d["goodput_mbps"]), transform=ax.transAxes,
                    va="top", fontsize=7, color=MUTED)
            if j == 0:
                ax.set_ylabel("goodput (Mbps)")
        axes[0, j].set_title(cc, color=CC_COLORS.get(cc, INK))
    fig.suptitle(f"Q3: TCP stats vs goodput per algorithm, {host}, all reps (shared axes)", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------- Q3: per-test regime metrics across all destinations ----------

def per_test_metrics(samples):
    rows = []
    for test_id, d in samples.groupby("test_id"):
        min_rtt = d.loc[d["min_rtt_ms"] > 0, "min_rtt_ms"].min()
        bdp_bytes = d["delivery_rate_bps"].max() / 8 * min_rtt / 1000
        sent = d["bytes_sent"].max()
        steady = d[d["t_s"] > 5]  # Skip slow start for the window-limit columns.
        rows.append({
            "test_id": test_id, "host": d["host"].iloc[0], "cc": d["cc"].iloc[0], "rep": d["rep"].iloc[0],
            "seq": d["seq"].iloc[0], "goodput_mbps": d["goodput_mbps"].mean(),
            "srtt_median_ms": d["srtt_ms"].median(), "min_rtt_ms": min_rtt,
            "rtt_inflation": d["srtt_ms"].median() / min_rtt if min_rtt else float("nan"),
            "retrans_pct": 100 * d["bytes_retrans"].max() / sent if sent else 0.0,
            "rto_events": d["rto_events_delta"].sum() if "rto_events_delta" in d else float("nan"),
            "cwnd_mean_kb": d["cwnd_bytes"].mean() / 1000, "bdp_kb": bdp_bytes / 1000,
            "cwnd_over_bdp": d["cwnd_bytes"].mean() / bdp_bytes if bdp_bytes else float("nan"),
            "rwnd_limited_pct": 100 * d["rwnd_limited_frac"].mean(),
            "inflight_mb": (steady["unacked_pkts"] * steady["mss_bytes"]).median() / 1e6,
            "cwnd_limited_pct": 100 * (steady["unacked_pkts"] >= steady["cwnd_pkts"] - 2).mean(),
        })
    return pd.DataFrame(rows).sort_values(["host", "cc", "rep"])


def q3_dest_summary(per_test):
    g = per_test.groupby(["host", "cc"])
    table = pd.DataFrame({
        "runs": g.size(), "goodput_mean_mbps": g["goodput_mbps"].mean(), "goodput_std_mbps": g["goodput_mbps"].std(),
        "srtt_median_ms": g["srtt_median_ms"].mean(), "min_rtt_ms": g["min_rtt_ms"].min(),
        "rtt_inflation": g["rtt_inflation"].mean(), "retrans_pct": g["retrans_pct"].mean(),
        "cwnd_over_bdp": g["cwnd_over_bdp"].mean(), "rwnd_limited_pct": g["rwnd_limited_pct"].mean(),
        "inflight_mb": g["inflight_mb"].mean(), "cwnd_limited_pct": g["cwnd_limited_pct"].mean(),
    }).reset_index()
    winners = []
    for host, d in table.groupby("host"):
        winners.append({"host": host, "min_rtt_ms": d["min_rtt_ms"].min(),
                        "best_goodput": d.loc[d["goodput_mean_mbps"].idxmax(), "cc"],
                        "best_goodput_mbps": d["goodput_mean_mbps"].max(),
                        "lowest_rtt": d.loc[d["srtt_median_ms"].idxmin(), "cc"],
                        "lowest_srtt_ms": d["srtt_median_ms"].min()})
    return table, pd.DataFrame(winners).sort_values("min_rtt_ms")


def q3_variability(per_test, path):
    hosts = per_test.groupby("host")["min_rtt_ms"].min().sort_values().index.tolist()
    algos = algos_in(per_test)
    fig, axes = plt.subplots(2, 1, figsize=(7.5, 5.5), sharex=True)
    for j, cc in enumerate(algos):
        offset = (j - (len(algos) - 1) / 2) * 0.22
        for i, host in enumerate(hosts):
            d = per_test[(per_test["host"] == host) & (per_test["cc"] == cc)]
            for ax, col in zip(axes, ["goodput_mbps", "srtt_median_ms"]):
                ax.scatter([i + offset] * len(d), d[col], s=14, color=CC_COLORS.get(cc, INK), alpha=0.8,
                           linewidths=0, label=cc if i == 0 else None)
                if len(d):
                    ax.hlines(d[col].mean(), i + offset - 0.09, i + offset + 0.09, color=CC_COLORS.get(cc, INK))
    axes[0].set_ylabel("mean goodput per run (Mbps)")
    axes[1].set_ylabel("median srtt per run (ms)")
    axes[0].legend(ncol=3, fontsize=8, loc="upper right")
    axes[1].set_xticks(range(len(hosts)))
    axes[1].set_xticklabels([short(h) for h in hosts], rotation=35, ha="right", fontsize=7)
    floor_at_zero(axes)
    fig.suptitle("Q3: run-to-run variability (dot = one run, bar = mean; hosts sorted by min RTT)", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def q3_tradeoff(per_test, path):
    fig, axes = plt.subplots(1, 2, figsize=(7.5, 3.2), sharey=True)
    for cc in algos_in(per_test):
        d = per_test[per_test["cc"] == cc]
        axes[0].scatter(d["rtt_inflation"], d["goodput_mbps"], s=16, color=CC_COLORS.get(cc, INK), alpha=0.8,
                        linewidths=0, label=cc)
        axes[1].scatter(d["retrans_pct"], d["goodput_mbps"], s=16, color=CC_COLORS.get(cc, INK), alpha=0.8,
                        linewidths=0, label=cc)
    axes[0].set_xlabel("RTT inflation (median srtt / min_rtt)")
    axes[1].set_xlabel("retransmitted bytes (%)")
    axes[1].set_xscale("symlog", linthresh=0.01)
    axes[0].set_ylabel("mean goodput per run (Mbps)")
    axes[0].set_yscale("log")
    axes[0].legend(fontsize=8)
    fig.suptitle("Q3: throughput vs queueing delay and loss, every run", x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ---------- destination selection summary (Q1b) ----------

def selection_tables(run_dir, tables_dir):
    attempts = pd.read_csv(os.path.join(run_dir, "attempts.csv"))
    with open(os.path.join(run_dir, "destinations.json")) as f:
        dest = json.load(f)
    picked = pd.DataFrame(dest["selected"])[["host", "ip", "port", "country", "site", "provider"]]
    save_table(picked, tables_dir, "q1_destinations")
    counts = attempts.groupby(["phase", "status"]).size().reset_index(name="count")
    counts["phase"] = counts["phase"].str.replace(r"#\d+", "", regex=True)
    counts = counts.groupby(["phase", "status"])["count"].sum().reset_index()
    save_table(counts, tables_dir, "q1_attempt_outcomes")
    failures = attempts[attempts["status"] != "ok"][["phase", "host", "port", "cc", "status", "error"]]
    save_table(failures, tables_dir, "q1_failures")
    return dest


def run(run_dir, rep_dest=None):
    plots_dir = os.path.join(run_dir, "plots")
    tables_dir = os.path.join(run_dir, "tables")
    os.makedirs(plots_dir, exist_ok=True)
    os.makedirs(tables_dir, exist_ok=True)
    samples, tests = load(run_dir)
    if samples.empty:
        print("No successful tests to analyze.")
        return

    dest = selection_tables(run_dir, tables_dir)
    q1_goodput_plots(samples, plots_dir)
    save_table(q1_summary(samples), tables_dir, "q1_goodput_summary")

    host = rep_dest or pick_rep_dest(samples)
    base = "cubic" if "cubic" in algos_in(samples) else algos_in(samples)[0]
    q2_timeseries(representative_run(samples, host, base), os.path.join(plots_dir, "q2_timeseries.pdf"))
    q2_scatter(samples[(samples["host"] == host) & (samples["cc"] == base)], os.path.join(plots_dir, "q2_scatter.pdf"))

    q3_timeseries_grid(samples, host, os.path.join(plots_dir, "q3_timeseries_grid.pdf"))
    q3_timeseries_overlay(samples, host, os.path.join(plots_dir, "q3_timeseries_overlay.pdf"))
    q3_scatter_grid(samples, host, os.path.join(plots_dir, "q3_scatter_grid.pdf"))
    per_test = per_test_metrics(samples)
    save_table(per_test, tables_dir, "q3_per_run")
    summary, winners = q3_dest_summary(per_test)
    save_table(summary, tables_dir, "q3_dest_summary")
    save_table(winners, tables_dir, "q3_winners")
    q3_variability(per_test, os.path.join(plots_dir, "q3_variability.pdf"))
    q3_tradeoff(per_test, os.path.join(plots_dir, "q3_tradeoff.pdf"))

    ok = (tests["status"] == "ok").sum()
    check = tests[tests["status"] == "ok"]
    byte_gap = (100 * (check["bytes_acked"] - check["server_bytes"]).abs() / check["bytes_acked"]).median()
    with open(os.path.join(tables_dir, "run_overview.md"), "w") as f:
        f.write(f"- representative destination: {host}\n")
        f.write(f"- destinations requested/selected/shortfall: {dest['requested']}/{len(dest['selected'])}/{dest['shortfall']}\n")
        f.write(f"- tests ok: {ok} of {len(tests)}\n")
        f.write(f"- cc requested == cc used in every ok test: {bool((check['cc_requested'] == check['cc_used']).all())}\n")
        f.write(f"- median |bytes_acked - server_bytes| / bytes_acked: {byte_gap:.2f}%\n")
        f.write(f"- median sample interval: {samples['interval_s'].median():.3f} s\n")
    print(f"Plots in {plots_dir}, tables in {tables_dir}, representative destination {host}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Make plots and tables for one run folder.")
    parser.add_argument("run_dir")
    parser.add_argument("--rep-dest", default=None)
    args = parser.parse_args()
    run(args.run_dir, args.rep_dest)
