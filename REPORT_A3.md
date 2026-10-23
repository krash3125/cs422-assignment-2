# CS 422 Assignment 3: TCP Congestion Control Implementation
**Option 1: Kernel Module Implementation**
**Name:** Kush Khanna
**Repository:** https://github.com/krash3125/cs422-assignment-2 (Branch: `assignment-3`)

---

## 1. QAIMD Implementation & Congestion Window Update Logic

**Algorithm Overview:** `tcp_qaimd.c` is a loadable Linux kernel congestion control module implementing the Queue-Aware AIMD with a BDP Anchor (QAIMD) algorithm designed in Assignment 2. Rather than relying purely on buffer overflow (like CUBIC and Reno) or ignoring packet loss entirely (like BBR v1), QAIMD anchors its congestion window (`snd_cwnd`) to the estimated Bandwidth-Delay Product ($\text{BDP} = \text{btl\_bw} \times \text{min\_rtt}$) and regulates standing queues using per-round minimum RTT measurements.

**How `cwnd` Updates Using RTT, Loss Proxy, and Throughput:**
1. **Per-ACK Signal Collection (`qaimd_main`):**
   * **RTT:** On every non-retransmitted ACK, `qaimd` updates `rtt_round_min_us` (the minimum RTT observed within the current round trip) and `min_rtt_us` (windowed minimum RTT over `10,000 ms`). Measuring standing queue via $q = \text{rtt\_round\_min\_us} - \text{min\_rtt\_us}$ filters out transient Wi-Fi delay spikes that often prematurely trip CUBIC's HyStart.
   * **Throughput / Delivery Rate:** When an ACK is not app-limited, the module computes the instantaneous delivery rate in bytes/sec. It tracks the peak rate in `btl_bw` (with a 0.5% per-ACK decay to expire stale peaks) and computes $\text{bdp} = \max\left(\frac{\text{btl\_bw} \times \text{min\_rtt\_us}}{\text{MSS}}, 4\right)$.
   * **Loss Proxy:** Across each round trip, `qaimd` tracks $\Delta \text{retrans}$ and $\Delta \text{delivered}$ directly from the kernel's segment counters. Loss is classified as **congestive loss** only if $\Delta \text{retrans} > 0$ *and* either $q > q_{\text{thr}}$ or the loss ratio exceeds 2.0%. Random non-congestive losses without standing queues are ignored.
2. **Per-Round Window Update (`qaimd_update_Round`):**
   * **Startup:** `snd_cwnd` doubles every round until (a) congestive loss occurs, (b) $q > 2 q_{\text{thr}}$ for 2 consecutive rounds, or (c) `btl_bw` grows by $< 25\%$ for 3 rounds. Upon exiting `STARTUP`, `snd_cwnd` drops directly to $\max(\text{bdp}, 4)$ to drain startup overshoot immediately.
   * **Congestion Avoidance:**
     * *Congestive Loss:* Multiplicative decrease $\text{cwnd} \leftarrow \max(0.7 \times \text{cwnd}, 4)$.
     * *Queue > Threshold:* Proportional delay backoff $\text{cwnd} \leftarrow \max\left(\text{cwnd} \times \left(1 - 0.5 \frac{q - q_{\text{thr}}}{\text{srtt}}\right), 4\right)$.
     * *Sub-BDP Catch-Up ($\text{cwnd} < \text{bdp}$):* Rapid catch-up $\text{cwnd} \leftarrow \text{cwnd} + \max(1, (\text{bdp} - \text{cwnd})/4)$ so long-RTT paths recover in a few RTTs rather than hundreds of linear Reno steps.
     * *Probing ($\text{cwnd} \ge \text{bdp}$ and $q \le q_{\text{thr}}$):* Additive increase $\text{cwnd} \leftarrow \text{cwnd} + 1\text{ MSS}$.

---

## 2. Initial State, Parameters, and Deviations from Pseudocode

| Parameter / State | Value in `tcp_qaimd.c` | Purpose |
| :--- | :--- | :--- |
| Initial `state` | `QAIMD_STARTUP` (0) | Exponential bandwidth search at flow start |
| Initial `snd_cwnd` | `10 MSS` (Kernel default) | Baseline starting window |
| `QAIMD_BETA` | `700 / 1000` ($0.7$) | Multiplicative decrease factor on congestive loss |
| `QAIMD_LOSS_TOL` | `20 / 1000` ($2.0\%$) | Loss ratio threshold when $q \le q_{\text{thr}}$ |
| `QAIMD_MIN_CWND` | `4 MSS` | Minimum congestion window floor |
| `QAIMD_PROBE_RTT_MS`| `10,000 ms` ($10\text{ s}$) | Window for `min_rtt_us` refresh / drain |
| `q_thr` | `clamp(0.15 * min_rtt, 3ms, 15ms)` | Target maximum standing queue delay |
| `QAIMD_PACE_GAIN` | `110 / 100` ($1.1\times$) | Pacing rate multiplier |

**Changes from Assignment 2 Pseudocode:**
1. **Fixed-Point Integer Arithmetic:** The Linux kernel forbids floating-point instructions in socket contexts. All floating-point operations (0.7, 0.15, 1.1, 0.5, 0.02) were converted to 64-bit/32-bit fixed-point integer arithmetic in microsecond and byte-per-second units.
2. **Packet-Count Loss Proxy:** In `struct tcp_sock`, `tp->total_retrans` and `tp->delivered` are maintained natively in segments. Because MSS is constant, $\frac{\Delta \text{total\_retrans}}{\Delta \text{delivered}}$ is mathematically identical to $\frac{\text{retrans\_bytes}}{\text{delivered\_bytes}}$ while safely fitting in 32-bit arithmetic.
3. **Exponential Decay Filter for `btl_bw`:** Instead of storing an explicit 10-element ring buffer inside the strict 104-byte `ICSK_CA_PRIV_SIZE` socket limit, `btl_bw` uses a peak tracker with a $0.995\times$ per-ACK decay to gracefully expire stale peaks.
4. **Capping Proportional Delay Backoff:** If a sudden Wi-Fi spike pushes $q - q_{\text{thr}}$ close to `srtt`, the raw delay backoff formula could cut `cwnd` by more than 30%. The per-round delay reduction is now capped at 30% so it is never harsher than a standard congestive-loss cut.

---

## 3. Kernel Environment & Reproduction Instructions

* **Host Environment:** Windows WSL2 (Ubuntu 24.04), Linux kernel `6.18.54.1-microsoft-standard-WSL2`.
* **Kernel Requirements:** Built against Microsoft WSL2 Kernel Source using `KBUILD_MODPOST_WARN=1`. Requires `CONFIG_TCP_CONG_ADVANCED=y` and internal TCP pacing (`cmpxchg(&sk->sk_pacing_status, SK_PACING_NONE, SK_PACING_NEEDED)`).
* **Execution:** All data collection is fully automated.
  ```bash
  git clone [https://github.com/krash3125/cs422-assignment-2.git](https://github.com/krash3125/cs422-assignment-2.git)
  git checkout assignment-3
  sudo ./run_a3.sh




  ## 4. Experimental Methodology & Comparative Analysis

**Methodology & Test Parameters**
*   **Destinations:** The pipeline tested geographically diverse public `iperf3` servers from Assignment 2. 7 out of 10 destinations successfully completed all tests (the remaining 3 were skipped due to being busy or connection resets during the selection phase).
*   **Duration & Sampling:** Every test ran for exactly **10.0 seconds**, with TCP statistics sampled every **200 ms**.
*   **Algorithms Tested:** `qaimd` (our custom kernel module), `cubic` (Linux default), `reno`, and `bbr` (Linux v1).
*   **Repetitions & Test Order:** Each destination $\times$ algorithm pair was tested for **3 repetitions**. To prevent temporal bias from network conditions, the order was rotated for each repetition:
    *   *Rep 1:* `qaimd` $\rightarrow$ `cubic` $\rightarrow$ `reno` $\rightarrow$ `bbr`
    *   *Rep 2:* `cubic` $\rightarrow$ `reno` $\rightarrow$ `bbr` $\rightarrow$ `qaimd`
    *   *Rep 3:* `reno` $\rightarrow$ `bbr` $\rightarrow$ `qaimd` $\rightarrow$ `cubic`
*   **Representative Destination:** `84.17.41.11` (Seattle, US; `min_rtt` $\approx 53\text{ ms}$).

### Operating Regimes & Algorithm Comparison

**1. Network-Limited Regime with Deep Queues (Short Paths)**
*Path Example: Washington DC (`speedtest.wdc2.us.leaseweb.net`, min_rtt $\approx 26\text{ ms}$)*
On short paths, the bandwidth-delay product (BDP) is small. Loss-based algorithms (`cubic` and `reno`) aggressively overfill the bottleneck buffer until a packet drops. 
*   **CUBIC & Reno:** Achieved high throughput (224 Mbps and 205 Mbps) but caused severe bufferbloat. Reno inflated the RTT by a massive **3.05$\times$** (median `srtt` of 87.8 ms), keeping `cwnd_over_bdp` at $4.75$. CUBIC inflated RTT by **2.32$\times$**.
*   **BBR:** Achieved 213.79 Mbps with a more constrained RTT inflation of $1.71\times$.
*   **QAIMD:** Effectively eliminated bufferbloat. By using proportional delay backoff anchored to the BDP, `qaimd` kept its median `srtt` at 30.4 ms—an RTT inflation of only **1.17$\times$**. It successfully held `cwnd` near the BDP (`cwnd_over_bdp` = $1.16$, $0.62\text{ MB}$ in flight vs CUBIC's $2.28\text{ MB}$). Because it yields to standing queues, it sacrificed some throughput (140.6 Mbps) to achieve this vastly superior latency.

**2. Sender-Limited / Startup Dominance (Long Paths)**
*Path Example: Sydney (`speedtest.syd12.au.leaseweb.net`, min_rtt $\approx 177\text{ ms}$)*
Over a short 10-second test on a long-distance path, startup behavior dictates the final mean throughput. 
*   **CUBIC:** Suffered heavily, averaging only **44.15 Mbps** with a massive standard deviation ($\pm34.75\text{ Mbps}$). The Wi-Fi access link's delay jitter frequently triggered CUBIC's HyStart to exit slow-start prematurely. With only 10 seconds total, it never reached the bottleneck capacity.
*   **QAIMD:** Outperformed CUBIC, averaging **78.82 Mbps** with much lower variance ($\pm15.29\text{ Mbps}$). Because `qaimd`'s slow-start exit requires 2 consecutive round trips of standing queue *ignoring transient spikes* (`rtt_round_min_us`), it did not fall victim to false HyStart exits. 
*   **Reno & BBR:** Averaged $\sim 99\text{ Mbps}$. Reno performed well here strictly because it never exited slow start before filling the pipe, avoiding its notoriously slow additive increase phase.

**3. The Representative Destination (Seattle, `84.17.41.11`)**
At $\sim 53\text{ ms}$ RTT, Seattle serves as a middle ground. `cubic` and `reno` reached 171–190 Mbps but operated with $1.50\text{--}1.70\times$ RTT inflation (median `srtt` 80–92 ms). `qaimd` sacrificed a portion of throughput (averaging 130.3 Mbps) to keep the queue nearly empty, resulting in a remarkable RTT inflation of just **1.09$\times$** (median `srtt` 58 ms).

### Variability and Limitations
*   **Variability Across Runs:** Throughput varied between repetitions due to uncontrolled background traffic on the shared internet paths and the Wi-Fi link. `cubic` saw extreme variability (up to $\pm78.4\text{ Mbps}$ on `iperf.worldstream.nl`) entirely dependent on when HyStart triggered. `qaimd` showed much higher stability on long paths. 
*   **Limitations of Public Paths:** We lack visibility into the exact router buffer depths and cross-traffic of the public `iperf3` servers. Furthermore, `qaimd` acts as a "delay-sensitive" algorithm. If it were run concurrently against `cubic` on the same link, `cubic` would fill the buffer, trigger `qaimd`'s delay backoff, and starve it. 
*   **Test Duration:** 10 seconds is relatively short for 150