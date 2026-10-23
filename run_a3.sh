#!/usr/bin/env bash
set -euo pipefail

# 1. Build + load tcp_qaimd.ko
make clean && make load

# 2. Run 10-second experiments across all 10 destinations, 3 repetitions, 4 algorithms
python3 run_experiments.py \
    --servers servers_a3.csv \
    -n 10 \
    -t 10 \
    -i 0.2 \
    -r 3 \
    --algos qaimd,cubic,reno,bbr \
    --rep-dest iperf-ams-nl.eranium.net \
    --seed 422 \
    --out results_a3