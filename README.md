## Setup (once, on the Mac)

TCP_INFO, per-socket TCP_CONGESTION and BBR are Linux-only, so tests run in an Ubuntu VM.
Multipass uses NAT at the IP level, so the VM's TCP stack talks to the iperf3 servers end-to-end.
Don't use Docker Desktop: its network proxy ends TCP inside the VM, so TCP_INFO only sees a
0.2 ms hop to the proxy. `run_tests.py` prints a warning when TCP RTT is far below the ping RTT.

```bash
brew install --cask multipass
multipass launch 24.04 -n cs422 -c 2 -m 2G
multipass mount ~/Downloads/cs422/cs422-assignment-2 cs422:/home/ubuntu/a2
multipass exec cs422 -- sudo apt-get install -y python3-venv
```

## How to Run

```bash
multipass shell cs422
cd ~/a2
./run.sh
```

Runs 5 preselected servers (1 low, 2 mid, 2 high RTT, see `TARGETS` in `run_tests.py`), 2 trials per server,
30 s per algorithm (about 22 min). The algorithm order rotates every trial.

Single test:

```bash
python3 iperf_client.py 37.19.206.20 5201 --cc bbr --time 30 --interval 0.2 --out bbr.csv
```

`--cc` takes `cubic`, `reno`, `bbr` or `adaptive` (switches live using `adaptive_cc.py`).

## Output

- `data/runs/{regime}_{site}_{host}_t{trial}_{cc}.csv`: one row per sample. `t` / `time` (offset, wall clock),
  `cc`, `cwnd`, `rtt_ms`, `min_rtt_ms`, `throughput_mbps` (bytes acked), `send_mbps` (bytes written),
  `delivery_mbps` (kernel estimate), `loss_rate` (retransmits / data segments sent)
- `data/summary.json`: one row per run, saved after every trial
- `plots/`: cwnd / RTT / throughput / retransmits per run, and `summary.png` (mean over trials, error bar = stdev)

## Files

- `iperf_client.py`: iperf3 protocol client, sends data and samples TCP_INFO
- `adaptive_cc.py`: rule-based switching between CUBIC, RENO and BBR
- `run_tests.py`: runs every algorithm against low, mid and high RTT servers
- `plot.py`: plots in `plots/`
- `REPORT.md`: regimes, why each algorithm wins there, and the switching algorithm
