# CS 422 Assignment 2: iPerf, TCP Statistics, and Congestion Control

A from-scratch Python iperf3 client (TCP upload). It talks to public iperf3 servers from
[iperf3serverlist.net](https://iperf3serverlist.net/), samples `TCP_INFO` from the data socket while sending,
and compares CUBIC, Reno and BBR. One command runs everything and writes raw data, PDF plots and summary tables.

- Report: [`report.pdf`](report.pdf) (source: [`REPORT.md`](REPORT.md))
- Plain-language code walkthrough: [`CODE_EXPLAINED.md`](CODE_EXPLAINED.md)

## Files

| File | What it is |
|---|---|
| `iperf_client.py` | iperf3 protocol client plus the send loop that samples TCP_INFO (Q1a, Q2a) |
| `tcp_info.py` | Decoder for Linux `struct tcp_info` (Q2a) |
| `servers.py` | Loads the server list and picks n working destinations (Q1b) |
| `servers.csv` | Input server list (`host,port,...`, where `port` may be a range like `5201-5210`) |
| `run_experiments.py` | **Single entry point**: preflight, destination selection, all algorithms × reps, then analysis |
| `analyze.py` | All PDF plots and summary tables (Q1c, Q2b, Q3a) |
| `Dockerfile`, `run.sh` | Ubuntu 24.04 container and the wrapper that runs it with the right flags |
| `results/<run_id>/` | Raw measurements, plots and tables from each run (`20261021-082724` is the main run) |
| `results/wmem32m/` | Follow-up run with the send buffer cap raised to 32 MiB, plus its server list |

## Requirements

- **A Linux kernel** (Ubuntu 22.04+). TCP_INFO and the congestion control come from the host kernel, not the container.
  Our results came from an Ubuntu 24.04 VM (kernel `6.8.0-147-generic`, 2 vCPU, 4 GB) running under Multipass on macOS.
  Its NAT works at the packet level, so TCP runs end-to-end to the server.
  Do **not** use Docker Desktop on macOS. Its network proxy (vpnkit) ends TCP inside the VM, so cwnd and RTT would
  describe the proxy link instead of the Internet path (we saw ~1 ms srtt there).
- BBR must be available on the host:
  ```bash
  sudo modprobe tcp_bbr
  cat /proc/sys/net/ipv4/tcp_available_congestion_control   # should list: reno cubic bbr
  ```
- Docker. The container runs with `--network host --cap-add NET_ADMIN`. NET_ADMIN lets a socket pick an algorithm
  that is not in `tcp_allowed_congestion_control`.

## Reproduce

```bash
# Optional on macOS: create the Linux VM used for the report
brew install --cask multipass
multipass launch 24.04 --name cs422 --cpus 2 --memory 4G --disk 15G
multipass shell cs422
sudo apt-get update && sudo apt-get install -y docker.io git && sudo usermod -aG docker $USER && sudo modprobe tcp_bbr
# log out and back in, then:
git clone https://github.com/krash3125/cs422-assignment-2.git && cd cs422-assignment-2

# Full pipeline (this is the exact command used for the report)
./run.sh -n 10 -t 30 -i 0.2 -r 3 --seed 422

# Follow-up run used in the report: 4 destinations with a 32 MiB send buffer cap
sudo sysctl -w net.ipv4.tcp_wmem="4096 16384 33554432"
./run.sh --servers results/wmem32m/servers_control.csv --out results/wmem32m -n 4 -t 30 -i 0.2 -r 3 --seed 422
sudo sysctl -w net.ipv4.tcp_wmem="4096 16384 4194304"   # back to the Ubuntu default

# Re-make plots/tables from saved data without running new tests
./run.sh --analyze-only results/<run_id>
# ...or without Docker:
pip install -r requirements.txt && python3 analyze.py results/<run_id>

# One test by hand
python3 iperf_client.py speedtest.dal13.us.leaseweb.net -p 5207 -C bbr -t 10 -i 0.2
```

## Command-line options (`run_experiments.py`, also accepted by `run.sh`)

| Option | Default | Meaning |
|---|---|---|
| `--servers FILE` | `servers.csv` | server list file (`host,port` columns; port may be `a-b`) |
| `-n, --num-dest N` | 10 | number of destinations |
| `-t, --duration S` | 30 | seconds of sending per test |
| `-i, --interval S` | 0.2 | sampling interval for goodput and TCP_INFO |
| `-r, --reps N` | 3 | repetitions of every destination × algorithm |
| `--algos LIST` | `cubic,reno,bbr` | algorithms to compare |
| `--seed N` | time | random seed for choosing destinations |
| `--max-attempts N` | 40 | maximum probe attempts while selecting destinations |
| `--retries N` | 3 | retries per test after a transient error (busy, timeout, reset) |
| `--backoff S` | 5 | wait before a retry (grows linearly) |
| `--gap S` | 1 | pause between tests |
| `--out DIR` | `results` | where run folders are created |
| `--rep-dest HOST` | auto | destination for the Q2/Q3 detail plots |
| `--analyze-only DIR` | | skip tests and re-plot an existing run |
| `--refresh-servers` | | download a fresh list from iperf3serverlist.net first |

## Output (`results/<run_id>/`)

| File | Content |
|---|---|
| `metadata.json` | kernel release, available/allowed/default CC, qdisc, `tcp_wmem`, Python, git SHA, arguments, seed, units, TCP_INFO size |
| `destinations.json` | requested n, selected servers (host, IP, port, site), shortfall |
| `attempts.csv` | every probe and test attempt with its outcome (`ok`, `ACCESS_DENIED`, `CONNECT_TIMEOUT`, ...) |
| `tests.jsonl` | one line per test: algorithm requested/used, bytes sent/acked/received by the server, server JSON results |
| `samples.csv` | one row per sample: goodput, cwnd, srtt, rttvar, min_rtt, retransmits, lost, ca_state, pacing/delivery rate, counters |
| `plots/*.pdf` | `q1_goodput_<cc>`, `q2_timeseries`, `q2_scatter`, `q3_timeseries_grid`, `q3_scatter_grid` |
| `tables/*.{csv,md}` | `q1_goodput_summary` (min/median/mean/p95), `q1_destinations`, `q1_attempt_outcomes`, `q1_failures`, `q3_dest_summary`, `run_overview` |

To build the report: `pandoc REPORT.md -o report.pdf --pdf-engine=xelatex -V geometry:margin=0.8in`
