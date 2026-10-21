from collections import deque
from statistics import mean

WINDOW = 2.0  # seconds of history used for each decision
HOLD = 3.0  # min seconds between switches
PERSIST = 5  # consecutive samples a new choice must hold before switching
BW_HISTORY = 10.0  # seconds of window-averaged throughput, max of it estimates bottleneck bandwidth
MIN_QUEUE_MS = 5.0  # queueing delay below this is jitter, not a standing queue
CUBIC_C, CUBIC_BETA = 0.4, 0.3  # linux cubic: scaling constant, window reduction on loss


def choose_cc(cur, loss, inflation, min_rtt_ms, cwnd_ratio, bdp_pkts, utilization):
    if loss > 0.01 and inflation < 1.3:
        return "bbr"  # loss without queueing = random loss, loss-based CCs overreact
    if inflation > 2.0 or (cwnd_ratio > 2.0 and inflation > 1.5):
        return "bbr"  # cwnd well past BDP and queue building up, BBR drains it
    if cur == "bbr" and utilization > 0.5:
        return "bbr"  # BBR keeps the queue empty, leaving it would just refill the queue

    # time to regrow a BDP-sized window after one loss: reno adds 1 MSS per RTT,
    # cubic's K doesn't depend on RTT, so cubic wins once the path's BDP * RTT is big
    reno_s = bdp_pkts / 2 * min_rtt_ms / 1e3
    cubic_s = (bdp_pkts * CUBIC_BETA / CUBIC_C) ** (1 / 3)
    if reno_s > 1.25 * cubic_s:
        return "cubic"
    if reno_s < 0.8 * cubic_s:
        return "reno"  # cubic acts like reno here anyway, reno's 0.5 backoff keeps a smaller queue
    return "cubic" if cur == "bbr" else cur  # close call, don't flip between cubic and reno


class AdaptiveCC:
    def __init__(self, start="cubic"):
        self.cc = start
        self.last_switch = 0.0
        self.window = deque()
        self.rates = deque()
        self.pending, self.streak = start, 0

    def update(self, sample):
        self.window.append(sample)
        while sample["t"] - self.window[0]["t"] > WINDOW:
            self.window.popleft()
        # averaged over a full window first, a max over raw 0.2s samples would mostly catch ACK bursts
        tput = mean(s["throughput_mbps"] for s in self.window)
        if sample["t"] >= WINDOW:
            self.rates.append((sample["t"], tput))
        while self.rates and sample["t"] - self.rates[0][0] > BW_HISTORY:
            self.rates.popleft()
        if sample["t"] - self.last_switch < HOLD:
            return self.cc

        first = self.window[0]
        segs = sample["data_segs_out"] - first["data_segs_out"]
        loss = (sample["total_retrans"] - first["total_retrans"]) / segs if segs else 0
        min_rtt = sample["min_rtt_ms"]
        avg_rtt = mean(s["rtt_ms"] for s in self.window)
        inflation = avg_rtt / min_rtt if avg_rtt - min_rtt > MIN_QUEUE_MS else 1.0
        btl_bw = max((rate for _, rate in self.rates), default=0)
        bdp_pkts = btl_bw * 1e6 / 8 * min_rtt / 1e3 / sample["mss"]
        cwnd_ratio = mean(s["cwnd"] for s in self.window) / bdp_pkts if bdp_pkts else 0
        utilization = tput / btl_bw if btl_bw else 0

        cc = choose_cc(self.cc, loss, inflation, min_rtt, cwnd_ratio, bdp_pkts, utilization)
        self.streak = self.streak + 1 if cc == self.pending else 1
        self.pending = cc
        if cc != self.cc and self.streak >= PERSIST:
            self.cc, self.last_switch = cc, sample["t"]
        return self.cc
