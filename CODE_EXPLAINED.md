# How the code works (plain-language guide)

This guide explains every file, in the order data flows through them:

```
servers.csv ──> servers.py ──> run_experiments.py ──> iperf_client.py ──> tcp_info.py
 (server list)  (pick n good   (main loop: every     (talks iperf3,     (decodes kernel
                 servers)       rep x dest x algo)    sends, samples)    TCP stats)
                                      │
                                      ▼
                     results/<run_id>/ samples.csv, tests.jsonl, ...
                                      │
                                      ▼
                                 analyze.py ──> plots/*.pdf, tables/*.md|csv
```

---

## `tcp_info.py`: reading the kernel's TCP statistics

**Why:** Linux keeps a `struct tcp_info` for each TCP socket, with fields like cwnd, RTT and the number of bytes
acknowledged. `getsockopt(IPPROTO_TCP, TCP_INFO)` hands it to us as raw bytes, and we have to decode them.

**How:**
- `FIELDS` lists every field in the order the kernel lays them out (copied from `include/uapi/linux/tcp.h`),
  each with its C size: `B` = 1 byte, `H` = 2, `I` = 4, `Q` = 8.
- `decode(raw)` walks that list and unpacks each field with `struct.unpack_from` until it runs out of bytes.
  Older kernels return a shorter struct, so fields they don't have are simply missing. This means one decoder works on any kernel.
- `read(sock)` asks the kernel for up to 256 bytes and returns `(dict, length)`. On our 6.8 kernel the length is 248, which is the full struct.

Units to remember: `rtt`, `rttvar` and `min_rtt` are **microseconds**. `snd_cwnd` is in **segments** (multiply by
`snd_mss` to get bytes). `pacing_rate` and `delivery_rate` are **bytes per second**. `bytes_acked`, `bytes_sent` and
`bytes_retrans` are running totals since the connection opened.

---

## `iperf_client.py`: the iperf3 client

iperf3 uses **two TCP connections** to the same server port:
1. a **control** connection that carries one-byte "state" messages and JSON, and
2. a **data** connection that we fill with bytes as fast as TCP allows.

### Protocol steps (function `run_test`)
| Step | What we do | iperf3 state |
|---|---|---|
| (i) control connection | `connect()` to the server, then send a random 37-byte **cookie** (36 letters + `\0`) that names this test | |
| (ii) parameter exchange | wait for `PARAM_EXCHANGE (9)`, then send JSON such as `{"tcp":true,"time":30,"parallel":1,"len":131072,...}` with a 4-byte length in front | 9 |
| (iii) data connection | wait for `CREATE_STREAMS (10)`, open a 2nd socket, **set the congestion control before connecting**, send the same cookie, then read back which algorithm the kernel actually uses | 10 |
| start | wait for `TEST_START (1)` and `TEST_RUNNING (2)` | 1, 2 |
| (iv) transmit | `send_loop()` sends for `duration` seconds | |
| (v) terminate | send `TEST_END (4)`. When the server says `EXCHANGE_RESULTS (13)`, send our results JSON and read theirs. When it says `DISPLAY_RESULTS (14)`, reply `IPERF_DONE (16)` and close | 4, 13, 14, 16 |

### Small helper functions
- `make_cookie()` builds the 37-byte test ID from the same alphabet real iperf3 uses.
- `recv_exact(sock, n)` keeps reading until it has exactly `n` bytes. A timeout or a closed socket becomes an `IperfError`.
- `read_state(ctrl)` reads one signed byte. It turns the server's error states into clear errors:
  - `ACCESS_DENIED (-1)`: the server is busy with someone else.
  - `SERVER_ERROR (-2)`: two 4-byte numbers follow, the server's error code and errno. Codes such as 37
    ("duration over server maximum") or 27 ("bitrate over limit") mean the server **rejected our parameters**.
  - `SERVER_TERMINATE (11)`: the server stopped the test.
- `send_json` and `recv_json` implement iperf3's framing: a 4-byte big-endian length, then the JSON text.
- `connect(ip, port, timeout, cc)` opens a socket, optionally sets `TCP_CONGESTION`, and connects. It turns timeouts and
  refusals into named errors.

### The send loop (`send_loop`)
One thread, no busy waiting:
1. Make the data socket non-blocking and take a starting TCP_INFO snapshot.
2. Loop. Each time, call `select()` and wait until **either** the data socket can take more bytes **or** the control socket has
   a message, but never past the next sample time.
   - Data writable: `send()` a 128 KiB block and add the bytes accepted to `sent`.
   - Control readable: in the middle of a test this only happens on an error or termination, so stop.
   - Sample time reached: read TCP_INFO and record one row with `make_sample()`.
3. Stop at `duration`.

### One sample (`make_sample`)
It compares the current snapshot with the previous one:
- **goodput** = (change in `bytes_acked`) × 8 / (actual seconds between the two snapshots). Because this counts bytes the
  *receiver acknowledged*, it is real delivered throughput, not just bytes we wrote into a buffer.
- **loss signals**: change in `total_retrans` (segments retransmitted this interval), change in `bytes_retrans`,
  their share of bytes sent, the current `lost` count, `ca_state` (3 = Recovery after fast retransmit, 4 = Loss
  after a timeout), and the change in `total_rto` (timeouts; kernel 6.7+).
- also cwnd (segments and bytes), srtt, rttvar, min_rtt, pacing rate, delivery rate, the share of time limited by the
  receiver window or the send buffer, and the running counters.

### Errors and robustness
Every failure becomes a `TestResult` whose `status` says what happened, and nothing crashes:
`DNS_FAIL`, `CONNECT_TIMEOUT`, `CONNECT_REFUSED`, `ACCESS_DENIED`, `PARAM_REJECTED`, `SERVER_ERROR`, `CTRL_TIMEOUT`
(server stopped answering), `PREMATURE_CLOSE` (connection dropped mid-test), `SERVER_TERMINATE`,
`PROTOCOL_ERROR`, `CC_UNAVAILABLE`. Timeouts are 5 s for connecting and 15 s for each control message.

You can run it by hand: `python3 iperf_client.py <host> -p <port> -C bbr -t 10`.

---

## `servers.py`: the server list and choosing destinations

- `parse_ports("5201-5210")` turns a port range into a list. Many public servers run several iperf3 daemons on a range of ports.
- `load_servers(path)` reads `servers.csv` into `Server` objects.
- `refresh_from_web(path)` downloads the live list from the site's JSON export and saves it as CSV.
- `select_destinations(...)`:
  1. shuffles the list with a seeded random generator (the same seed gives the same order),
  2. runs a **3-second probe test** on each candidate,
  3. keeps the server if the probe succeeds; if the port was busy or refused, tries a second random port of the same
     host; any other failure moves on to a replacement server,
  4. stops after `n` successes or `max_attempts` probes, and returns the **shortfall** if fewer than `n` worked.

Every probe is logged, so the report can show which servers failed and why.

---

## `run_experiments.py`: the single entry point

`main()` reads the command-line options and calls `run_all()`, then `analyze.run()`.

`run_all()` does five things:
1. **Preflight** (`preflight`): records the kernel version, the available/allowed/default congestion control, the qdisc,
   `tcp_wmem` (the send buffer limits), the Python version and the git SHA. It also tries to set each requested algorithm on a throwaway socket, and stops with a
   hint (`modprobe tcp_bbr`, `--cap-add NET_ADMIN`) if one fails.
2. **Select destinations** with `servers.select_destinations` and save `destinations.json`.
3. **Main loop**: for each repetition, for each destination, run each algorithm back to back. The algorithm order
   **rotates every rep** (rep 1: cubic, reno, bbr; rep 2: reno, bbr, cubic; rep 3: bbr, cubic, reno), so no algorithm
   always goes first. Runs of the same destination sit close together in time, so they see similar network conditions.
4. **Retries** (`run_with_retries`): a busy server or a timeout gets up to `--retries` more tries on the same destination
   (another port of the same host if it has a range), with a growing wait. We don't swap destinations here, so all
   three algorithms are always measured on the same set.
5. **Save as it goes** (`append_csv`), so a crash keeps the data already collected:
   - `samples.csv`: one row per 200 ms sample,
   - `tests.jsonl`: one line per test, including the server's own byte count,
   - `attempts.csv`: every attempt and its outcome.

---

## `analyze.py`: plots and tables

`run(run_dir)` loads `samples.csv` and `tests.jsonl`, keeps only successful tests, and then:

| Function | Output | Question |
|---|---|---|
| `q1_goodput_plots` | `q1_goodput_<cc>.pdf`: one panel per destination, one line per rep | Q1c |
| `q1_summary` | `q1_goodput_summary`: min / median / mean / p95 goodput per destination × algorithm | Q1c |
| `selection_tables` | `q1_destinations`, `q1_attempt_outcomes`, `q1_failures` | Q1b |
| `pick_rep_dest` | picks the destination that has every run and median CUBIC goodput | Q2/Q3 |
| `representative_run` | for that destination and algorithm, picks the run with median mean goodput | Q2/Q3 |
| `q2_timeseries` / `draw_timeseries` | cwnd, RTT (srtt ± rttvar, min_rtt), retransmits per interval (Recovery/Loss shaded), goodput | Q2b(i) |
| `q2_scatter` | cwnd, RTT and retransmits vs goodput, with Spearman correlation | Q2b(ii) |
| `q3_timeseries_grid` / `_overlay` | the same time series for all three algorithms, **shared y-axes** | Q3a |
| `q3_scatter_grid` | the same scatters for all three algorithms, shared axes | Q3a |
| `per_test_metrics` | one row per run: mean goodput, median srtt, min_rtt, RTT inflation, retransmit %, mean cwnd, **BDP**, cwnd/BDP, in-flight MB, cwnd-limited % | Q3a |
| `q3_dest_summary` | mean ± std over reps per destination × algorithm, plus the winner for throughput and for RTT | Q3a |
| `q3_variability` | dot per run + mean bar, per destination and algorithm | Q3a |
| `q3_tradeoff` | goodput vs RTT inflation and vs retransmit % for every run | Q3a |

Definitions used in the tables:
- **RTT inflation** = median srtt / min_rtt. A value of 1 means no queueing; 2 means the queue doubled the delay.
- **BDP** = (highest delivery rate seen) × min_rtt. This is how much data the path can hold with no queue.
- **cwnd / BDP**: below 1 means the window is too small to fill the pipe; above 1 means the extra data is sitting in a queue.
- **in-flight MB** = median of unacked segments × MSS after the first 5 s. This is the data actually on the wire,
  which can be much smaller than cwnd when the send buffer is the limit.
- **cwnd-limited %** = share of samples where unacked ≥ cwnd − 2, meaning the congestion window really is the limit.

Colours: cubic = blue, reno = orange, bbr = aqua. They stay the same in every plot.

---

## `Dockerfile` and `run.sh`

- `Dockerfile`: `FROM ubuntu:24.04`, installs Python, numpy, pandas, matplotlib and iproute2 from apt, copies the code,
  and starts `run_experiments.py`.
- `run.sh`: builds the image and runs it with:
  - `--network host`: use the host's network stack, so there's no extra NAT layer and the host's congestion control settings apply,
  - `--cap-add NET_ADMIN`: lets the program choose BBR per socket,
  - `-v results:/app/results`: the output lands on the host,
  - `GIT_SHA`: the commit is recorded in `metadata.json`.

The container fixes the Python version and libraries, but **TCP itself always runs in the host kernel**. That is why
the report records the host kernel version.
