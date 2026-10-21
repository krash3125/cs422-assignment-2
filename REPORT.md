# CS422 Assignment 2: CUBIC vs Reno vs BBR

> Sections marked **TBD** need numbers from `data/summary.json` and the plots in `plots/` after `./run.sh`.
> Sections marked *Expected* are predictions from how the algorithms work. Check each one against the data
> and rewrite it if the measurements disagree.

## 1. Setup

- **Client**: `iperf_client.py` speaks the iperf3 control protocol over plain sockets (cookie, JSON
  parameter exchange, state machine through `IPERF_DONE`). It opens one TCP data stream and writes
  64 KiB blocks continuously for 30 s.
- **Host**: Ubuntu 24.04 VM (Multipass, IP-level NAT), kernel **TBD** (`uname -r`). Congestion control is set
  per socket with `setsockopt(IPPROTO_TCP, TCP_CONGESTION)`.
- **Servers**: 5 picked from the public iperf3 list (`data/iperf3_results.json`), labelled by ping RTT
  (low < 50 ms, mid 100–150 ms, high ≥ 200 ms). Picked for several ports, a steady ping and 10 Gbit/s+ links:

  | Regime | Host | Site | Ping |
  |---|---|---|---|
  | low | chi.speedtest.clouvider.net | Chicago | 20 ms |
  | mid | lon.speedtest.clouvider.net | London | 106 ms |
  | mid | iperf-ams-nl.eranium.net | Amsterdam | 109 ms |
  | high | speedtest.uztelecom.uz | Tashkent | 205 ms |
  | high | 23.249.58.14 | Hong Kong | 205 ms |

  2 trials per server, 30 s per algorithm. The algorithm order rotates every trial so no algorithm always runs first.
- **Sampling**: every 200 ms, `getsockopt(TCP_INFO)` is unpacked with
  `TCP_INFO_FMT` (the `struct tcp_info` layout from `linux/tcp.h` up to `tcpi_delivery_rate`, with unused
  fields skipped as pad bytes).

## 2. Metrics

| Column | Source | Meaning |
|---|---|---|
| `t`, `time` | `time.monotonic()`, `time.time()` | offset from test start, wall clock |
| `cwnd` | `tcpi_snd_cwnd` | congestion window (segments) |
| `rtt_ms` | `tcpi_rtt` (µs) | kernel smoothed RTT |
| `min_rtt_ms` | `tcpi_min_rtt` (µs) | lowest RTT seen (propagation delay estimate) |
| `throughput_mbps` | Δ`tcpi_bytes_acked`·8 / Δt | data the receiver acknowledged (goodput) |
| `send_mbps` | Δ bytes returned by `send()` ·8 / Δt | rate the app writes into the socket buffer |
| `delivery_mbps` | `tcpi_delivery_rate` | kernel per-ACK delivery rate estimate |
| `loss_rate` | Δ`tcpi_total_retrans` / Δ`tcpi_data_segs_out` | retransmission rate, used as the loss estimate |

Per run (`summary.json`): `mean_mbps` = total bytes acked / duration, mean and p95 RTT, overall retransmission
rate, and the number of algorithm switches (adaptive runs).

## 3. Results — TBD

| Regime | Server (host) | Ping (ms) | Algorithm | Throughput Mbps (mean ± sd) | Mean RTT (ms) | p95 RTT (ms) | Retrans % |
|---|---|---|---|---|---|---|---|
| low | TBD | | cubic / reno / bbr / adaptive | | | | |
| mid | TBD | | | | | | |
| high | TBD | | | | | | |

Figures: `plots/summary.png`, and one `plots/<regime>_<site>_<host>_t<trial>.png` per trial
(cwnd, RTT, throughput and retransmits over time; dotted lines mark adaptive switches).

## 4. Regimes: which algorithm wins and why

### How each algorithm works

- **Reno** (AIMD): cwnd grows by 1 MSS per RTT and halves on loss. Growth per second scales with 1/RTT, and
  throughput is roughly `(MSS/RTT)·1.22/√p` (Mathis model).
- **CUBIC** (Linux default): `W(t) = C·(t−K)³ + W_max`, `K = ∛(W_max·β/C)` with C = 0.4 and β = 0.3
  (cwnd drops to 0.7·W on loss). Growth depends on time since the last loss, not on RTT, so it recovers
  quickly on long, fat paths. When the BDP is small it runs in "TCP-friendly" mode and behaves like Reno.
- **BBR** (v1, mainline Linux): builds a model of the path instead of reacting to loss. It estimates the
  bottleneck bandwidth (max delivery rate over about 10 RTTs) and the propagation RTT (min RTT over 10 s),
  paces at the bandwidth estimate and caps data in flight at 2·BDP. It cycles its pacing gain through
  1.25 / 0.75 / 1 to probe for bandwidth, and drops to 4 packets every 10 s (ProbeRTT) to re-measure min RTT.

### A. Low RTT, clean path (low regime)
*Expected*: all three fill the bottleneck, which is probably the home access link, so throughput is close.
CUBIC ≈ Reno because CUBIC is in TCP-friendly mode. On RTT, BBR stays lowest if the bottleneck buffer is
deep, and Reno queues less than CUBIC on average because it backs off by 50 % instead of 30 %.
*Observed*: **TBD**

### B. High RTT / high BDP (mid and high regimes)
*Expected*: **CUBIC > Reno**, and BBR at or above CUBIC. At 100 Mbps and 200 ms the BDP is about 1727 packets.
After one loss, Reno needs about 864 RTTs (≈ 173 s) to grow back, longer than the whole test. CUBIC needs
K = ∛(1727·0.3/0.4) ≈ 10.9 s whatever the RTT. BBR doesn't need loss-free stretches at all: it finds the
bottleneck rate within a few RTTs and holds it.
*Observed*: **TBD**

### C. Lossy path (random, non-congestive loss)
*Expected*: **BBR ≫ CUBIC > Reno.** Loss-based algorithms treat every loss as congestion. With the Mathis model,
1 % loss at 100 ms RTT limits Reno to about 1.4 Mbps no matter how fast the link is. BBR v1 ignores loss
until it is very high (around 15–20 %). In the data this looks like a retransmission rate above 1 % while
`rtt_ms` stays close to `min_rtt_ms`.
*Observed*: **TBD**

### D. Deep buffers / bufferbloat
*Expected*: throughput is similar, but **BBR wins on RTT**. CUBIC and Reno keep growing until the buffer
overflows, so `rtt_ms` can climb to several times `min_rtt_ms` and cwnd goes well past the BDP. BBR keeps data
in flight near the BDP, so RTT stays near `min_rtt_ms`.
*Observed*: **TBD**

### E. Shallow buffers
*Expected*: BBR v1 still gets high throughput but with many retransmissions, because its 2·BDP cap and 1.25×
probing overflow a small buffer. Reno needs a buffer of about 1 BDP to keep the link full after halving, and
CUBIC needs about 0.43 BDP, so both leave the link idle after each loss.
*Observed*: **TBD**

### What each regime looks like in the samples

| Regime | `min_rtt_ms` | `rtt_ms / min_rtt_ms` | Retrans rate | cwnd vs BDP | Best (throughput / RTT) |
|---|---|---|---|---|---|
| Low RTT, clean | small | ≈ 1 | low | ≈ 1 | Reno ≈ CUBIC ≈ BBR / BBR or Reno |
| High BDP, clean | large | ≈ 1 | low | ≈ 1 | CUBIC, BBR / BBR |
| Random loss | any | < 1.3 | > 1 % | < 1 | BBR / BBR |
| Bufferbloat | any | > 1.5–2 | low | > 2 | similar / BBR |

## 5. Switching algorithm (`adaptive_cc.py`)

Each decision uses the last 2 s of samples:

| Signal | Formula |
|---|---|
| loss | Δ`total_retrans` / Δ`data_segs_out` over the window |
| inflation | mean `rtt_ms` / `min_rtt_ms` (treated as 1 when the queueing delay is under 5 ms, which is just jitter) |
| btl_bw | max of the 2 s-averaged throughput over the last 10 s (like BBR's max filter) |
| bdp_pkts | btl_bw · min_rtt / MSS |
| cwnd_ratio | mean cwnd / bdp_pkts |
| utilization | 2 s-averaged throughput / btl_bw |

```
if loss > 1% and inflation < 1.3:                     BBR    # loss with no queue = random loss       (C)
elif inflation > 2 or (cwnd_ratio > 2 and inflation > 1.5):  BBR    # cwnd past BDP, queue building  (D)
elif current == BBR and utilization > 0.5:           BBR    # BBR keeps the queue empty, stay       (D)
else:                                                       # clean path, pick by regrowth time    (A/B)
    reno_s  = bdp_pkts / 2 * min_rtt                        # Reno: +1 MSS per RTT after halving
    cubic_s = cbrt(bdp_pkts * 0.3 / 0.4)                    # CUBIC: K, doesn't depend on RTT
    reno_s > 1.25 * cubic_s  ->  CUBIC
    reno_s < 0.8  * cubic_s  ->  RENO
    otherwise                ->  keep current (CUBIC if leaving BBR)
switch only if: >= 3 s since the last switch AND the same choice for 5 samples in a row (1 s)
```

Why it is built this way:

- **Rule 1** separates random loss from congestion loss by checking whether the RTT went up. Congestion loss
  comes after a queue builds; random loss doesn't.
- **Rule 2** catches a standing queue: cwnd well past the BDP while RTT climbs.
- **Rule 3** stops flip-flopping. BBR empties the queue, which removes the inflation that caused the switch.
  Without this rule the algorithm would switch back to CUBIC, the queue would refill, and it would bounce
  every few seconds. It leaves BBR only if BBR's throughput drops below half of the best recent rate
  (for example, starved by competing loss-based flows).
- **CUBIC vs Reno** compares how long each takes to regrow the window after a loss, using the formulas in
  Section 4. Examples: 15 ms, 50 Mbps → Reno (0.5 s vs 3.6 s). 150 ms, 50 Mbps → CUBIC (49 s vs 7.9 s).
  The ±25 % band keeps noise from flipping it back and forth.
- **Inputs**: the rules use all four quantities the assignment asks for (cwnd, RTT, loss rate, throughput).
  The 1 %, 1.3, 1.5, 2 and 0.5 thresholds are starting points. Adjust them against the regimes measured in
  Section 3: **TBD**.

Checks done so far: a simulation with 7 path scenarios × 200 noisy seeds settles on the expected algorithm
with at most one switch each, and a live run on Linux switches CUBIC → Reno through `setsockopt` mid-connection.

*Adaptive vs the best fixed algorithm per server*: **TBD**.

## 6. Limitations

- Public internet paths: unknown cross-traffic, server load and route changes. 2 trials × 30 s on 5 servers is a small sample.
- For low-RTT servers the bottleneck is probably the local access link (Wi-Fi or ISP), which makes the
  algorithms look alike.
- Retransmission rate is not the same as loss rate: it includes spurious retransmits and only sees the
  sending side.
- When switching away from BBR, the kernel keeps pacing the connection (BBR turns pacing on and never turns
  it off), so CUBIC or Reno after a switch is paced. Expect smoother behaviour than a fresh CUBIC connection.
- Linux keeps cwnd across a switch, so the new algorithm starts from the old one's window, not from slow start.
  Switching into BBR restarts BBR's STARTUP phase from that window.
- The VM must use IP-level NAT. Docker Desktop's proxy showed a 0.2 ms TCP RTT to a server 20 ms away, so
  its TCP_INFO data is useless for this assignment.
