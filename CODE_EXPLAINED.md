# Code Explained, Line by Line

This file walks through every source file in the repo. Line numbers match the code as it is right now;
if you edit a file, they will shift.

- [How the pieces fit together](#how-the-pieces-fit-together)
- [Terms used below](#terms-used-below)
- [iperf_client.py](#iperf_clientpy): talks to an iperf3 server, sends data, samples TCP_INFO
- [adaptive_cc.py](#adaptive_ccpy): decides when to switch between CUBIC, Reno and BBR
- [run_tests.py](#run_testspy): runs every algorithm against many servers, saves results
- [plot.py](#plotpy): turns the results into graphs
- [run.sh](#runsh): prepares the Linux VM and runs everything

---

## How the pieces fit together

```
run.sh
 ├─ loads BBR into the kernel, allows reno/cubic/bbr, makes a Python venv
 ├─ python run_tests.py
 │    for each of 5 preselected servers (1 low, 2 mid, 2 high RTT):
 │      for 2 trials:
 │        for each algorithm (cubic, reno, bbr, adaptive), order rotated per trial:
 │          iperf_client.run_test()  ──► 30 s of sending, a sample every 0.2 s
 │               └─ if "adaptive": AdaptiveCC.update() on every sample, may switch algorithm
 │        save one CSV per run  → data/runs/
 │        save summary rows     → data/summary.json
 └─ python plot.py  → plots/*.png
```

## Terms used below

| Term | Meaning |
|---|---|
| **cwnd** | congestion window: how many segments TCP may have in flight without an ACK |
| **MSS** | maximum segment size: payload bytes per TCP segment (usually about 1448) |
| **RTT** | round-trip time: time for a segment to reach the server and its ACK to come back |
| **min RTT** | lowest RTT seen, which approximates the propagation delay with no queueing |
| **BDP** | bandwidth-delay product = bottleneck bandwidth × min RTT, the amount of data that fills the path exactly |
| **retransmission** | a segment sent again because TCP decided it was lost, used here as the loss estimate |
| **TCP_INFO** | a Linux socket option that returns the kernel's live statistics for one TCP connection |
| **TCP_CONGESTION** | a Linux socket option that reads or sets the congestion control algorithm for one socket |

---

## iperf_client.py

This is the core of the assignment. It implements the iperf3 client protocol directly on sockets (no iperf3
binary or library), sends data continuously, and reads TCP statistics from the kernel at regular intervals.

### Lines 1–10: imports

```python
import argparse
import csv
import json
import random
import socket
import string
import struct
import time

from adaptive_cc import AdaptiveCC
```

- **L1 `argparse`**: parses command-line options such as `--cc bbr --time 30`.
- **L2 `csv`**: writes the samples to a CSV file.
- **L3 `json`**: the iperf3 control channel exchanges JSON messages (test parameters, results).
- **L4 `random`**, **L6 `string`**: used together to build the random "cookie" that identifies this test to the server.
- **L5 `socket`**: the raw TCP sockets: one control connection and one data connection.
- **L7 `struct`**: converts between Python values and raw bytes. Needed for the 1-byte iperf3 state codes,
  the 4-byte JSON length headers, and decoding the binary `tcp_info` structure.
- **L8 `time`**: `time.monotonic()` for measuring intervals, `time.time()` for wall-clock timestamps.
- **L10**: imports the switching algorithm class from `adaptive_cc.py`, used when `--cc adaptive` is chosen.

### Lines 12–16: iperf3 protocol state codes

```python
# iperf3 control states
TEST_START, TEST_RUNNING, TEST_END = 1, 2, 4
PARAM_EXCHANGE, CREATE_STREAMS = 9, 10
EXCHANGE_RESULTS, DISPLAY_RESULTS, IPERF_DONE = 13, 14, 16
ACCESS_DENIED = -1
```

The iperf3 server and client move through a fixed sequence of states and tell each other the current state
by sending a single signed byte on the control connection. These are the same numbers iperf3 uses in its
source (`iperf_api.h`).

- **L13**: `TEST_START` (1) = server is ready to start, `TEST_RUNNING` (2) = data may flow now,
  `TEST_END` (4) = client says it has finished sending.
- **L14**: `PARAM_EXCHANGE` (9) = server wants the test parameters, `CREATE_STREAMS` (10) = server wants the
  client to open its data connection(s).
- **L15**: `EXCHANGE_RESULTS` (13) = swap final statistics, `DISPLAY_RESULTS` (14) = server is done reporting,
  `IPERF_DONE` (16) = client's final goodbye.
- **L16**: `ACCESS_DENIED` (-1): the server is already running a test for someone else. Public iperf3 servers
  handle only one client at a time, so this is common.

### Lines 18–20: the layout of Linux's `struct tcp_info`

```python
# struct tcp_info from linux/tcp.h up to delivery_rate, fields we don't use skipped as pad bytes (x)
TCP_INFO_FMT = "16x I 48x I 8x I 16x I 16x Q 8x 12x I 4x I Q"
TCP_INFO_FIELDS = "snd_mss rtt snd_cwnd total_retrans bytes_acked min_rtt data_segs_out delivery_rate".split()
```

When you ask the kernel for `TCP_INFO`, it hands back a block of raw bytes laid out exactly like the C struct
`tcp_info` in `linux/tcp.h`. `TCP_INFO_FMT` tells Python's `struct` module how to slice those bytes. Each field
sits at a fixed byte position, so we can't just leave out the fields we don't need: we have to skip over their
bytes. `x` means "skip one byte" and produces no value; `I` reads a 4-byte unsigned integer; `Q` reads an 8-byte
unsigned integer. Spaces are ignored and only there to make it readable.

- **L19**, read left to right (byte offsets in brackets):

  | Format | Bytes | What it covers in `struct tcp_info` |
  |---|---|---|
  | `16x` | 0–15 | skipped: 8 one-byte fields (`state` … `app_limited`), then `rto`, `ato` |
  | `I` | 16–19 | **`snd_mss`** |
  | `48x` | 20–67 | skipped: 12 four-byte fields (`rcv_mss` … `rcv_ssthresh`) |
  | `I` | 68–71 | **`rtt`** (µs) |
  | `8x` | 72–79 | skipped: `rttvar`, `snd_ssthresh` |
  | `I` | 80–83 | **`snd_cwnd`** |
  | `16x` | 84–99 | skipped: `advmss`, `reordering`, `rcv_rtt`, `rcv_space` |
  | `I` | 100–103 | **`total_retrans`** |
  | `16x` | 104–119 | skipped: `pacing_rate`, `max_pacing_rate` (8 bytes each) |
  | `Q` | 120–127 | **`bytes_acked`** |
  | `8x` | 128–135 | skipped: `bytes_received` |
  | `12x` | 136–147 | skipped: `segs_out`, `segs_in`, `notsent_bytes` |
  | `I` | 148–151 | **`min_rtt`** (µs) |
  | `4x` | 152–155 | skipped: `data_segs_in` |
  | `I` | 156–159 | **`data_segs_out`** |
  | `Q` | 160–167 | **`delivery_rate`** (bytes/s) |

  Total 168 bytes. No prefix character means native byte order (little-endian on x86/ARM Linux), the same as
  the kernel uses. The struct continues past `delivery_rate` in newer kernels, but we stop there.
- **L20**: the names of the 8 values `struct.unpack` returns, in the same order. `.split()` turns the string into a list.

To check the format, unpack the same random bytes with the full layout (`"8B24I4Q6IQ"`, all 43 fields named)
and with this one; the 8 values must match.

### Line 22: the data block

```python
BLOCK = b"\0" * 65536
```

- 64 KiB of zero bytes, created once and sent over and over. The content doesn't matter (iperf3 throws it away);
  reusing one object avoids allocating new memory on every send.

### Lines 25–27: reading TCP_INFO

```python
def tcp_info(sock):
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, struct.calcsize(TCP_INFO_FMT))
    return dict(zip(TCP_INFO_FIELDS, struct.unpack(TCP_INFO_FMT, raw)))
```

- **L26**: `getsockopt` with a buffer size asks the kernel to copy up to that many bytes of the socket's TCP
  statistics. `struct.calcsize(TCP_INFO_FMT)` is 168, exactly the part we decode.
- **L27**: `struct.unpack` turns the bytes into a tuple of 8 numbers; `zip` pairs each number with its name; `dict`
  makes it easy to read, e.g. `info["snd_cwnd"]`.

### Lines 30–35: reading and setting the congestion control algorithm

```python
def get_cc(sock):
    return sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).rstrip(b"\0").decode()


def set_cc(sock, cc):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())
```

- **L31**: asks the kernel which algorithm this socket is using. The name comes back in a 16-byte buffer
  (the kernel's max name length) padded with zero bytes; `rstrip(b"\0")` removes the padding and `decode()` turns
  bytes into a string like `"cubic"`.
- **L35**: tells the kernel to use algorithm `cc` for this socket. This works before connecting and also in
  the middle of a live connection, which is what lets the adaptive mode switch algorithms on the fly. If the
  algorithm isn't loaded the kernel raises `FileNotFoundError`; if a non-root user isn't allowed to use it, it
  raises `PermissionError`.

### Lines 38–45: reading an exact number of bytes

```python
def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("server closed connection")
        data += chunk
    return data
```

TCP is a byte stream, not a message stream: one `recv(100)` may return fewer than 100 bytes. This helper
keeps reading until it has exactly `n`.

- **L39**: start with nothing.
- **L40**: keep going until we have `n` bytes.
- **L41**: ask only for the bytes still missing.
- **L42–43**: `recv` returning empty bytes means the other side closed the connection, so raise an error
  instead of looping forever.
- **L44**: append what arrived.
- **L45**: return the full `n` bytes.

### Lines 48–53: waiting for a specific server state

```python
def expect(sock, want):
    state = struct.unpack("b", recv_exact(sock, 1))[0]
    if state == ACCESS_DENIED:
        raise ConnectionRefusedError("server busy")
    if state != want:
        raise ConnectionError(f"expected state {want}, got {state}")
```

- **L49**: read one byte and decode it as a *signed* char (`"b"`), so `-1` comes out as `-1` and not `255`.
- **L50–51**: server busy: raise a specific error so `run_tests.py` can try another port.
- **L52–53**: any other unexpected state means the protocol went wrong (for example the server reported an error),
  so stop with a clear message.

### Lines 56–63: sending and receiving JSON messages

```python
def send_json(sock, obj):
    data = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_json(sock):
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    return json.loads(recv_exact(sock, size))
```

iperf3 frames every JSON message as a 4-byte length followed by the JSON text.

- **L57**: Python dict → JSON string → bytes.
- **L58**: `"!I"` = 4-byte unsigned integer in network byte order (big-endian). Send length + body in one call;
  `sendall` keeps sending until every byte is out.
- **L62**: read the 4-byte length first.
- **L63**: read exactly that many bytes and parse them as JSON.

### Lines 66–109: the send loop and sampling (the main measurement)

```python
def send_loop(sock, duration, interval, adaptive):
    samples, sent, last_sent = [], 0, 0
    start = last_t = time.monotonic()
    last = tcp_info(sock)
    next_sample = start + interval
```

- **L66**: `sock` is the data socket, `duration` is seconds to send, `interval` is seconds between samples, `adaptive`
  is an `AdaptiveCC` object or `None`.
- **L67**: `samples` collects one dict per sample; `sent` counts every byte handed to the kernel; `last_sent` remembers
  `sent` at the previous sample so we can compute the send rate.
- **L68**: `time.monotonic()` is a clock that never jumps backwards (unlike wall-clock time), so it's the right
  clock for measuring durations. `last_t` is the time of the previous sample.
- **L69**: a starting snapshot of TCP_INFO. Every rate is computed as "now minus last snapshot".
- **L70**: when the first sample is due.

```python
    while time.monotonic() - start < duration:
        try:
            sent += sock.send(BLOCK)
        except TimeoutError:
            pass
        now = time.monotonic()
        if now < next_sample:
            continue
```

- **L72**: keep looping until `duration` seconds have passed.
- **L73–74**: hand 64 KiB to the kernel. `send` returns how many bytes the kernel actually accepted into its send
  buffer (it may be fewer than 65536 when the buffer is nearly full), and we add that to `sent`.
- **L75–76**: the socket's timeout is set to `interval` (see L130). If the send buffer stays full that long,
  `send` gives up with `TimeoutError`. We ignore it so a full buffer never delays the next sample.
- **L77–79**: if it isn't time for a sample yet, go back and send more. This keeps the pipe full: the loop
  spends almost all its time sending.

```python
        info, dt = tcp_info(sock), now - last_t
        segs = info["data_segs_out"] - last["data_segs_out"]
```

- **L81**: take a fresh TCP_INFO snapshot, and measure exactly how long it has been since the last one (`dt`).
  Using the real `dt` rather than assuming `interval` keeps the rates correct even if a sample is a bit late.
- **L82**: how many data segments were sent since the last sample (retransmissions included). This is the
  denominator for the loss rate.

```python
        sample = {
            "t": round(now - start, 3),
            ...
        }
```

**L83–98**: build one sample. Each field:

| Line | Field | How it's computed | Meaning |
|---|---|---|---|
| 84 | `t` | `now - start` | seconds since the test began (timestamp for plotting) |
| 85 | `time` | `time.time()` | wall-clock Unix timestamp, so samples can be matched to real time |
| 86 | `cc` | `get_cc(sock)` | the algorithm the kernel is *actually* using right now (confirms switches happened) |
| 87 | `cwnd` | `snd_cwnd` | congestion window in segments |
| 88 | `mss` | `snd_mss` | bytes per segment, needed to convert BDP from bytes to segments |
| 89 | `rtt_ms` | `rtt / 1000` | kernel's smoothed RTT; the kernel reports microseconds, we convert to ms |
| 90 | `min_rtt_ms` | `min_rtt / 1000` | lowest RTT the kernel has seen recently (ms) |
| 91 | `throughput_mbps` | Δ`bytes_acked` × 8 / `dt` / 10⁶ | data the receiver **confirmed** receiving, in megabits per second |
| 92 | `send_mbps` | Δ`sent` × 8 / `dt` / 10⁶ | rate our app wrote data into the socket |
| 93 | `delivery_mbps` | `delivery_rate` × 8 / 10⁶ | the kernel's own delivery-rate estimate (bytes/s → Mbps) |
| 94 | `loss_rate` | Δ`total_retrans` / `segs` | fraction of segments retransmitted in this interval; `if segs else 0` avoids dividing by zero |
| 95 | `bytes_acked` | cumulative | total bytes acknowledged so far (used for the run's average throughput) |
| 96 | `total_retrans` | cumulative | total retransmissions so far (used for the run's loss rate and by the adaptive algorithm) |
| 97 | `data_segs_out` | cumulative | total data segments sent so far (same uses) |

`× 8` converts bytes to bits; `/ 1e6` converts bits to megabits. `round(..., 3)` keeps the CSV readable.

```python
        samples.append(sample)
        print(f"{sample['t']:6.2f}s {sample['cc']:5} cwnd={sample['cwnd']:5} rtt={sample['rtt_ms']:7.2f}ms "
              f"tput={sample['throughput_mbps']:8.2f}Mbps loss={sample['loss_rate']:.2%}")
```

- **L99**: store the sample.
- **L100–101**: print a live line so you can watch the test. The format codes pad numbers to fixed widths so the
  columns line up (`:6.2f` = width 6, 2 decimals; `:.2%` prints a fraction as a percentage).

```python
        if adaptive and (cc := adaptive.update(sample)) != sample["cc"]:
            set_cc(sock, cc)
```

- **L103**: only in adaptive mode: give the new sample to the switching algorithm, which returns the algorithm
  it wants. The `:=` ("walrus") assigns that answer to `cc` inside the `if`.
- **L104**: if the answer differs from what the kernel is using, switch the live connection to it.

```python
        last_t, last, last_sent = now, info, sent
        next_sample += interval

    return samples, sent
```

- **L106**: this sample becomes the baseline for the next one.
- **L107**: schedule the next sample exactly one interval after the previous *scheduled* time (not after "now"),
  so samples stay on a regular 0.2 s grid and don't drift.
- **L109**: return all samples and the total bytes sent (the server wants that number at the end).

### Lines 112–145: one complete iperf3 test

```python
def run_test(host, port, cc="cubic", duration=30, interval=0.2):
    cookie = "".join(random.choices(string.ascii_lowercase, k=36)).encode() + b"\0"
    adaptive = AdaptiveCC() if cc == "adaptive" else None
```

- **L112**: defaults: CUBIC, 30 s, a sample every 0.2 s.
- **L113**: the cookie: 36 random lowercase letters plus a terminating zero byte = 37 bytes, the size iperf3
  expects. The client sends it on both connections so the server knows the data connection belongs to this
  control connection.
- **L114**: create the switching algorithm only when `--cc adaptive` was chosen.

```python
    with socket.create_connection((host, port), timeout=5) as ctrl:
        ctrl.sendall(cookie)
        expect(ctrl, PARAM_EXCHANGE)
        send_json(ctrl, {"tcp": True, "time": duration})
        expect(ctrl, CREATE_STREAMS)
```

- **L116**: open the **control connection**. `create_connection` resolves hostnames and tries IPv4/IPv6 addresses
  until one works. `timeout=5` limits how long connects and reads may hang. `with` closes the socket automatically.
- **L117**: identify ourselves with the cookie.
- **L118**: the server replies `PARAM_EXCHANGE` (or `-1` if busy, which raises).
- **L119**: send the test settings: TCP, for `duration` seconds. Everything else uses the server's defaults
  (one stream, client is the sender).
- **L120**: the server replies `CREATE_STREAMS`: "open your data connection now".

```python
        with socket.socket(ctrl.family, socket.SOCK_STREAM) as data:
            set_cc(data, adaptive.cc if adaptive else cc)
            data.settimeout(5)
            data.connect(ctrl.getpeername())
            data.sendall(cookie)
            expect(ctrl, TEST_START)
            expect(ctrl, TEST_RUNNING)
```

- **L122**: create the **data socket**, using the same address family (IPv4 or IPv6) as the control connection.
- **L123**: choose the algorithm *before* connecting, so the whole connection, handshake included, uses it.
  In adaptive mode we start with whatever `AdaptiveCC` starts with (CUBIC).
- **L124**: 5-second limit on the connect.
- **L125**: connect to the same IP and port as the control connection (`getpeername()` returns them). iperf3
  accepts data connections on the same port.
- **L126**: send the cookie on the data connection too, so the server can pair the two.
- **L127–128**: wait for `TEST_START`, then `TEST_RUNNING`: the server is ready to receive.

```python
            data.settimeout(interval)
            samples, sent = send_loop(data, duration, interval, adaptive)
            retrans = tcp_info(data)["total_retrans"]
```

- **L130**: shorten the data socket timeout to one sample interval (see L75–76 for why).
- **L131**: the actual test: send and sample for `duration` seconds.
- **L132**: read the final retransmission count to report to the server.

```python
            ctrl.sendall(struct.pack("b", TEST_END))
            expect(ctrl, EXCHANGE_RESULTS)
            send_json(ctrl, {
                "cpu_util_total": 0, "cpu_util_user": 0, "cpu_util_system": 0, "sender_has_retransmits": 1,
                "streams": [{"id": 1, "bytes": sent, "retransmits": retrans,
                             "jitter": 0, "errors": 0, "packets": 0}],
            })
            recv_json(ctrl)
            expect(ctrl, DISPLAY_RESULTS)
            ctrl.sendall(struct.pack("b", IPERF_DONE))

    return samples
```

- **L134**: tell the server we've finished sending (`TEST_END` as one signed byte).
- **L135**: the server replies `EXCHANGE_RESULTS`.
- **L136–140**: send our side's results. The server refuses the message if any of these keys is missing, so all
  are present:
  - CPU usage: we don't measure it, so 0.
  - `sender_has_retransmits: 1`: we do report retransmits.
  - one stream with `id` 1 (the server numbers the first stream 1), the bytes we sent, and the retransmit count.
  - `jitter`, `errors`, `packets` only matter for UDP tests, so 0.
- **L141**: read the server's results (its receive-side numbers). We don't need them, but must read them to stay
  in sync with the protocol.
- **L142**: the server says `DISPLAY_RESULTS`.
- **L143**: we answer `IPERF_DONE` so the server ends cleanly and is free for the next client.
- **L145**: both `with` blocks have closed the sockets; return the samples.

### Lines 148–152: saving samples to CSV

```python
def save_csv(samples, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=samples[0].keys())
        writer.writeheader()
        writer.writerows(samples)
```

- **L149**: open the output file. `newline=""` is what the `csv` module docs ask for (prevents blank lines on Windows).
- **L150**: column names come from the keys of the first sample.
- **L151**: write the header row.
- **L152**: write one row per sample.

### Lines 155–171: command-line entry point

```python
def main():
    parser = argparse.ArgumentParser(description="Send data to an iperf3 server and log TCP_INFO stats")
    parser.add_argument("host")
    parser.add_argument("port", type=int, nargs="?", default=5201)
    parser.add_argument("--cc", default="cubic", choices=["cubic", "reno", "bbr", "adaptive"])
    parser.add_argument("--time", type=int, default=30)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--out", help="CSV output path")
    args = parser.parse_args()

    samples = run_test(args.host, args.port, args.cc, args.time, args.interval)
    if args.out:
        save_csv(samples, args.out)


if __name__ == "__main__":
    main()
```

- **L157**: required: server hostname or IP.
- **L158**: optional port (`nargs="?"`), default 5201 (iperf3's standard port).
- **L159**: algorithm; `choices` rejects anything else with a helpful message.
- **L160**: test length in seconds (whole number, because iperf3's `time` parameter is an integer).
- **L161**: seconds between samples.
- **L162**: optional CSV path.
- **L163**: parse what the user typed.
- **L165**: run the test.
- **L166–167**: save the CSV only if `--out` was given.
- **L170–171**: run `main()` only when this file is executed directly. When `run_tests.py` imports this file,
  `main()` does not run.

Example: `python3 iperf_client.py 37.19.206.20 5201 --cc bbr --time 30 --out bbr.csv`

---

## adaptive_cc.py

The hand-written switching algorithm. Every sample goes into `AdaptiveCC.update()`, which looks at the last
2 seconds and returns which algorithm the connection should be using.

### Lines 1–9: imports and tuning constants

```python
from collections import deque
from statistics import mean

WINDOW = 2.0  # seconds of history used for each decision
HOLD = 3.0  # min seconds between switches
PERSIST = 5  # consecutive samples a new choice must hold before switching
BW_HISTORY = 10.0  # seconds of window-averaged throughput, max of it estimates bottleneck bandwidth
MIN_QUEUE_MS = 5.0  # queueing delay below this is jitter, not a standing queue
CUBIC_C, CUBIC_BETA = 0.4, 0.3  # linux cubic: scaling constant, window reduction on loss
```

- **L1 `deque`**: a list that is fast to add to on the right and remove from on the left, ideal for a sliding window.
- **L2 `mean`**: average of a sequence.
- **L4 `WINDOW`**: each decision looks at the last 2 s of samples (10 samples at 0.2 s).
- **L5 `HOLD`**: after a switch, wait at least 3 s before switching again. Because HOLD (3 s) is longer than WINDOW (2 s),
  the next decision never uses samples from the old algorithm.
- **L6 `PERSIST`**: a new choice must be the answer 5 samples in a row (1 s) before we act on it, so one noisy
  sample can't cause a switch.
- **L7 `BW_HISTORY`**: remember 10 s of 2-second-averaged throughput; the maximum of that history estimates the
  bottleneck bandwidth (the same idea BBR uses internally).
- **L8 `MIN_QUEUE_MS`**: if average RTT is less than 5 ms above min RTT, treat it as normal jitter rather than a
  queue. On very short paths, tiny absolute jitter would otherwise look like a big ratio.
- **L9**: Linux CUBIC's constants: `C = 0.4` controls how fast the cubic curve grows; `β = 0.3` means cwnd
  drops by 30 % (to 0.7×) on loss.

### Lines 12–28: `choose_cc`, the decision rules

```python
def choose_cc(cur, loss, inflation, min_rtt_ms, cwnd_ratio, bdp_pkts, utilization):
```

- **L12**: inputs: the current algorithm and six numbers computed in `update()` (explained there). Returns the
  algorithm name to use. The rules are checked top to bottom; the first match wins.

```python
    if loss > 0.01 and inflation < 1.3:
        return "bbr"  # loss without queueing = random loss, loss-based CCs overreact
```

- **L13–14**: more than 1 % of segments are being retransmitted, **but** RTT is barely above its minimum (under 1.3×).
  Congestion loss normally comes after a queue builds and raises RTT; loss *without* a queue suggests random loss
  (bad link, policer). Reno and CUBIC cut their window on every loss even when it isn't congestion; BBR doesn't
  treat loss as a signal, so it keeps its speed.

```python
    if inflation > 2.0 or (cwnd_ratio > 2.0 and inflation > 1.5):
        return "bbr"  # cwnd well past BDP and queue building up, BBR drains it
```

- **L15–16**: RTT is more than twice its minimum, or cwnd is more than 2× the BDP while RTT is 1.5× its minimum.
  Both mean a standing queue (bufferbloat): CUBIC/Reno keep filling the buffer until it overflows. BBR keeps
  roughly one BDP in flight, so the queue drains and RTT drops back toward the minimum.

```python
    if cur == "bbr" and utilization > 0.5:
        return "bbr"  # BBR keeps the queue empty, leaving it would just refill the queue
```

- **L17–18**: stickiness. Once on BBR, the inflation that triggered the switch disappears (BBR emptied the queue).
  Without this rule the algorithm would switch back to CUBIC, the queue would refill, and it would bounce every
  few seconds. So we stay on BBR as long as it still gets more than half of the best recent throughput.

```python
    # time to regrow a BDP-sized window after one loss: reno adds 1 MSS per RTT,
    # cubic's K doesn't depend on RTT, so cubic wins once the path's BDP * RTT is big
    reno_s = bdp_pkts / 2 * min_rtt_ms / 1e3
    cubic_s = (bdp_pkts * CUBIC_BETA / CUBIC_C) ** (1 / 3)
```

If we get here the path is "clean" (no unusual loss, no big queue), so the choice is CUBIC vs Reno. We compare how
long each takes to grow back to full speed after one loss.

- **L22 `reno_s`**: Reno halves its window on loss, then adds 1 segment per RTT. Getting back from BDP/2 to BDP takes
  BDP/2 round trips, i.e. `(bdp_pkts / 2) × RTT` seconds (`/ 1e3` converts ms to s). This grows with RTT.
- **L23 `cubic_s`**: CUBIC's recovery time is its constant `K = ∛(W × β / C)`, which does **not** depend on RTT.
  `** (1 / 3)` is the cube root.

Example: at 15 ms and 50 Mbps, BDP ≈ 65 segments → Reno 0.5 s vs CUBIC 3.6 s → Reno. At 150 ms and 50 Mbps,
BDP ≈ 647 → Reno 49 s vs CUBIC 7.9 s → CUBIC.

```python
    if reno_s > 1.25 * cubic_s:
        return "cubic"
    if reno_s < 0.8 * cubic_s:
        return "reno"  # cubic acts like reno here anyway, reno's 0.5 backoff keeps a smaller queue
    return "cubic" if cur == "bbr" else cur  # close call, don't flip between cubic and reno
```

- **L24–25**: Reno would be clearly slower (by more than 25 %) → CUBIC.
- **L26–27**: Reno would be clearly faster → Reno. On such short paths CUBIC behaves like Reno anyway
  ("TCP-friendly mode"), and Reno's deeper back-off (to 50 %) leaves a smaller queue, so RTT is a bit lower.
- **L28**: within the ±25 % band it's too close to call: keep the current algorithm so measurement noise doesn't
  cause back-and-forth switching. If we're leaving BBR (rule 3 failed), fall back to CUBIC, the Linux default.

### Lines 31–37: `AdaptiveCC` state

```python
class AdaptiveCC:
    def __init__(self, start="cubic"):
        self.cc = start
        self.last_switch = 0.0
        self.window = deque()
        self.rates = deque()
        self.pending, self.streak = start, 0
```

- **L33 `cc`**: the algorithm currently chosen; starts as CUBIC.
- **L34 `last_switch`**: time (seconds since test start) of the last switch. Starting at 0 means the first decision
  happens after `HOLD` seconds, once there is enough data.
- **L35 `window`**: the samples from the last `WINDOW` seconds.
- **L36 `rates`**: `(time, 2 s-average throughput)` pairs for the last `BW_HISTORY` seconds.
- **L37 `pending`, `streak`**: the most recent answer from `choose_cc` and how many samples in a row it has been the answer.

### Lines 39–50: `update`, keeping the history up to date

```python
    def update(self, sample):
        self.window.append(sample)
        while sample["t"] - self.window[0]["t"] > WINDOW:
            self.window.popleft()
```

- **L40**: add the newest sample.
- **L41–42**: drop samples older than 2 s from the left end.

```python
        # averaged over a full window first, a max over raw 0.2s samples would mostly catch ACK bursts
        tput = mean(s["throughput_mbps"] for s in self.window)
        if sample["t"] >= WINDOW:
            self.rates.append((sample["t"], tput))
        while self.rates and sample["t"] - self.rates[0][0] > BW_HISTORY:
            self.rates.popleft()
```

- **L44**: average throughput over the last 2 s. Single 0.2 s samples jump around because ACKs arrive in bursts;
  the 2 s average is much steadier.
- **L45–46**: record that average, but only once a full 2 s window exists. In the first seconds the window holds
  only one or two samples, whose noise would make the bandwidth estimate too high.
- **L47–48**: forget averages older than 10 s, so the estimate follows the path if it changes.

```python
        if sample["t"] - self.last_switch < HOLD:
            return self.cc
```

- **L49–50**: too soon after the last switch (or the test start), so keep the current algorithm. The history above was
  still updated, so it's ready when the hold period ends.

### Lines 52–61: computing the decision inputs

```python
        first = self.window[0]
        segs = sample["data_segs_out"] - first["data_segs_out"]
        loss = (sample["total_retrans"] - first["total_retrans"]) / segs if segs else 0
```

- **L52**: the oldest sample in the window.
- **L53**: data segments sent over the window.
- **L54 `loss`**: retransmissions over the window ÷ segments sent over the window. 0 if nothing was sent.

```python
        min_rtt = sample["min_rtt_ms"]
        avg_rtt = mean(s["rtt_ms"] for s in self.window)
        inflation = avg_rtt / min_rtt if avg_rtt - min_rtt > MIN_QUEUE_MS else 1.0
```

- **L55**: the path's base RTT (no queueing).
- **L56**: average RTT over the window.
- **L57 `inflation`**: how many times larger the current RTT is than the base RTT. 1.0 means no queue; 2.0 means the
  queue doubles the delay. If the extra delay is under 5 ms it's treated as jitter and set to 1.0.

```python
        btl_bw = max((rate for _, rate in self.rates), default=0)
        bdp_pkts = btl_bw * 1e6 / 8 * min_rtt / 1e3 / sample["mss"]
        cwnd_ratio = mean(s["cwnd"] for s in self.window) / bdp_pkts if bdp_pkts else 0
        utilization = tput / btl_bw if btl_bw else 0
```

- **L58 `btl_bw`**: bottleneck bandwidth estimate (Mbps) = best 2 s average throughput in the last 10 s.
  `default=0` handles an empty history.
- **L59 `bdp_pkts`**: bandwidth-delay product in segments. Step by step: Mbps × 10⁶ = bits/s; ÷ 8 = bytes/s;
  × min RTT in seconds (ms ÷ 10³) = bytes; ÷ MSS = segments. This is how many segments it takes to fill the path.
- **L60 `cwnd_ratio`**: average cwnd ÷ BDP. About 1 means the window matches the path; well above 1 means the extra
  data is sitting in a queue.
- **L61 `utilization`**: current throughput ÷ best recent throughput. Near 1 means running at full speed.

### Lines 63–68: decide, with persistence

```python
        cc = choose_cc(self.cc, loss, inflation, min_rtt, cwnd_ratio, bdp_pkts, utilization)
        self.streak = self.streak + 1 if cc == self.pending else 1
        self.pending = cc
        if cc != self.cc and self.streak >= PERSIST:
            self.cc, self.last_switch = cc, sample["t"]
        return self.cc
```

- **L63**: ask the rules what they want now. This uses all four quantities the assignment names: cwnd (via
  `cwnd_ratio`), RTT (via `inflation`, `min_rtt`), loss (`loss`) and throughput (via `btl_bw`, `bdp_pkts`, `utilization`).
- **L64**: same answer as last time → streak + 1; different answer → streak restarts at 1.
- **L65**: remember this answer for the next comparison.
- **L66–67**: switch only if the answer differs from the current algorithm **and** it has been the answer for 5 samples
  in a row. Record when the switch happened (starts the next hold period).
- **L68**: return the chosen algorithm; `send_loop` applies it with `set_cc`.

---

## run_tests.py

Runs the whole experiment: every algorithm, twice, against 5 preselected servers, and saves the results.
Total: 5 servers × 2 trials × 4 algorithms = 40 runs × ~33 s ≈ 22 minutes.

### Lines 1–25: imports, paths, settings

```python
import json
import os
import time
from statistics import mean, quantiles

from iperf_client import run_test, save_csv
```

- **L1–3**: JSON files, file paths, `sleep`.
- **L4**: `mean` for averages, `quantiles` for the 95th percentile RTT.
- **L6**: reuse the client's test function and CSV writer.

```python
DIR = os.path.dirname(os.path.abspath(__file__))
SERVERS = os.path.join(DIR, "data", "iperf3_results.json")
SUMMARY = os.path.join(DIR, "data", "summary.json")
RUNS = os.path.join(DIR, "data", "runs")
```

- **L8**: the folder this script lives in, so paths work no matter where you run it from.
- **L9**: the public server list, with a ping RTT measured for each server.
- **L10**: where one summary row per run goes.
- **L11**: where the per-run CSVs go.

```python
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
```

- **L13**: the algorithms to test.
- **L14**: how the regime labels are defined: by the ping RTT stored in the server list. The gaps (50–100,
  150–200 ms) keep the regimes clearly separate.
- **L15**: why these servers: each offers several ports (if one port is busy with another user, the next is
  tried), had a steady ping when measured, and sits on a 10 Gbit/s or faster link, so the server isn't the bottleneck.
- **L16–22**: the 5 servers to test, each as `(regime label, host)`. The host must match an `IP/HOST` entry in
  `data/iperf3_results.json`, which supplies its ports, site name and ping RTT:

  | Regime | Host | Site | Ping |
  |---|---|---|---|
  | low | chi.speedtest.clouvider.net | Chicago | 20 ms |
  | mid | lon.speedtest.clouvider.net | London | 106 ms |
  | mid | iperf-ams-nl.eranium.net | Amsterdam | 109 ms |
  | high | speedtest.uztelecom.uz | Tashkent | 205 ms |
  | high | 23.249.58.14 | Hong Kong | 205 ms |

  Only one low-RTT server: on short paths all three algorithms usually just fill your home connection, so the
  interesting differences (CUBIC vs Reno, BBR) show up on the longer paths.
- **L23**: how many times to repeat each server. Two trials give a mean and a standard deviation (the error bars).
- **L24**: seconds per test.
- **L25**: at most 5 ports to try per server (some list ranges like 5201–5240).

### Lines 28–30: expanding a port range

```python
def ports(spec):
    lo, _, hi = spec.partition("-")
    return list(range(int(lo), int(hi or lo) + 1))[:MAX_PORTS]
```

- **L29**: `"5201-5209"` → `lo="5201"`, `hi="5209"`. `"5201"` → `lo="5201"`, `hi=""`.
- **L30**: if there's no `hi`, use `lo`. `range(lo, hi + 1)` includes both ends; `[:MAX_PORTS]` keeps the first 5.
  `"5201-5209"` → `[5201, 5202, 5203, 5204, 5205]`.

### Lines 33–48: one algorithm against one server, trying several ports

```python
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
```

- **L34**: try each port in turn.
- **L36–37**: print what we're doing, then run a 30 s test.
- **L38–39**: these two errors come from `set_cc` (algorithm not loaded or not allowed). That's a setup problem,
  not a server problem, so stop immediately instead of trying every port.
- **L40–42**: anything else network-related (server busy, timeout, connection reset, bad JSON): print it and try the
  next port. `OSError` covers all socket errors; `ValueError` covers JSON decode errors.

```python
        # a userspace NAT proxy (e.g. Docker Desktop) ends TCP locally, TCP_INFO then shows the proxy hop
        if samples[-1]["min_rtt_ms"] < server["mean_rtt"] / 2:
            print(f"  warning: TCP min RTT {samples[-1]['min_rtt_ms']} ms << ping {server['mean_rtt']} ms, "
                  "connection is proxied and stats are not end-to-end")
        return samples
    return None
```

- **L44–46**: sanity check. TCP's min RTT can't really be less than half the ping RTT to the same server. If it is,
  something between us and the server (for example Docker Desktop's network proxy) is ending the TCP connection
  locally, and TCP_INFO describes that short hop instead of the real path. Print a warning so bad data is noticed.
- **L47**: success: return the samples.
- **L48**: every port failed.

### Lines 51–60: one trial = every algorithm once

```python
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
```

- **L52**: algorithm → samples.
- **L53–54**: rotate the order each trial: trial 0 = cubic, reno, bbr, adaptive; trial 1 = reno, bbr, adaptive, cubic
  (more trials would keep rotating). Network conditions drift over time; rotating stops one algorithm from
  always getting the first (or last) slot.
- **L55–57**: if any algorithm fails, the trial is incomplete and can't be compared fairly, so it is dropped.
- **L58**: store the samples.
- **L59**: pause 2 s so the server finishes cleaning up before the next test.
- **L60**: all four algorithms' samples.

### Lines 63–71: summary numbers for one run

```python
def stats(samples):
    last = samples[-1]
    return {
        "mean_mbps": round(last["bytes_acked"] * 8 / last["t"] / 1e6, 2),
        "mean_rtt_ms": round(mean(s["rtt_ms"] for s in samples), 2),
        "p95_rtt_ms": round(quantiles([s["rtt_ms"] for s in samples], n=20)[-1], 2),
        "loss_rate": round(last["total_retrans"] / last["data_segs_out"], 5) if last["data_segs_out"] else 0,
        "switches": sum(a["cc"] != b["cc"] for a, b in zip(samples, samples[1:])),
    }
```

- **L64**: the last sample holds the cumulative counters.
- **L66 `mean_mbps`**: total bytes acknowledged × 8 ÷ total seconds ÷ 10⁶. Uses totals rather than averaging the
  per-sample rates, so uneven sample spacing can't skew it.
- **L67 `mean_rtt_ms`**: average smoothed RTT across samples.
- **L68 `p95_rtt_ms`**: `quantiles(..., n=20)` splits the data into 20 equal parts and returns the 19 cut points; the
  last one is the 95th percentile, the RTT that 95 % of samples stay below. It shows queueing spikes that an average hides.
- **L69 `loss_rate`**: total retransmissions ÷ total data segments for the whole run.
- **L70 `switches`**: compare each sample with the next and count how often `cc` changed (only non-zero for adaptive).
  `True` counts as 1 in `sum`.

### Lines 74–100: the main experiment loop

```python
def main():
    os.makedirs(RUNS, exist_ok=True)
    with open(SERVERS) as f:
        by_host = {s["IP/HOST"]: s for s in json.load(f)}
```

- **L75**: make `data/runs/` if it doesn't exist.
- **L76–77**: load the full server list and index it by host name, so each target in `TARGETS` can be looked up
  directly (every host appears only once in the list).

```python
    summary = []
    for regime, host in TARGETS:
        server = by_host[host]
        print(f"[{regime}] {server['SITE']} {host} (ping {server['mean_rtt']:.0f} ms)")
```

- **L79**: all summary rows so far.
- **L80**: go through the 5 preselected servers in order.
- **L81**: fetch that server's entry (ports, site, ping RTT). A typo in `TARGETS` stops here with a `KeyError`
  naming the host.
- **L82**: progress message.

```python
        # host in the name, many servers share a SITE
        name = "_".join((regime, "".join(c for c in server["SITE"] if c.isalnum()), host))
```

- **L85**: file-name base, for example `low_Chicago_chi.speedtest.clouvider.net`. The site name is stripped of spaces
  and symbols (`"Hong Kong"` → `HongKong`). The host is included because several servers in the list share a site
  name (there are multiple "Chicago" servers); without it their files would overwrite each other.

```python
        for trial in range(TRIALS):
            results = run_trial(server, trial)
            if not results:
                break
```

- **L86–87**: run trial 0, then trial 1.
- **L88–89**: a failed trial usually means the server went busy or down, so stop wasting time on it and move to the
  next server. There's no automatic replacement: if a server keeps failing, swap its host in `TARGETS`.

```python
            for cc, samples in results.items():
                save_csv(samples, os.path.join(RUNS, f"{name}_t{trial + 1}_{cc}.csv"))
                summary.append({"regime": regime, "site": server["SITE"], "host": host,
                                "ping_ms": server["mean_rtt"], "trial": trial + 1, "cc": cc, **stats(samples)})
            # saved after every trial so a crash keeps what finished
            with open(SUMMARY, "w") as f:
                json.dump(summary, f, indent=4)
```

- **L90–91**: one CSV per algorithm, e.g. `low_Chicago_chi.speedtest.clouvider.net_t1_bbr.csv` (trials numbered from
  1 for humans).
- **L92–93**: one summary row: where, which trial, which algorithm, plus the numbers from `stats()`. `**stats(samples)`
  unpacks that dict's keys into this one.
- **L95–96**: rewrite `summary.json` after every trial, so a crash or Ctrl-C partway through keeps everything
  finished so far.
- **L99–100**: run `main()` when executed directly.

---

## plot.py

Turns the CSVs and `summary.json` into PNG graphs in `plots/`.

### Lines 1–21: imports, settings, what to plot

```python
import csv
import glob
import json
import os
from collections import defaultdict
from statistics import mean, stdev

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
```

- **L1–4**: read CSVs, find files by pattern (`glob`), read JSON, build paths.
- **L5 `defaultdict`**: a dict that creates an empty value automatically for a new key, handy for grouping.
- **L6**: `mean` and `stdev` (standard deviation) for averaging trials and drawing error bars.
- **L10**: the "Agg" backend draws straight to image files with no window, which is required in a VM with no screen.
  It must be set before `pyplot` is imported (L11).

```python
DIR = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(DIR, "data", "runs")
SUMMARY = os.path.join(DIR, "data", "summary.json")
PLOTS = os.path.join(DIR, "plots")

METRICS = [("cwnd", "cwnd (segments)"), ("rtt_ms", "RTT (ms)"), ("throughput_mbps", "Throughput (Mbps)"),
           ("loss_rate", "Retransmit rate")]
SUMMARY_METRICS = [("mean_mbps", "Mean throughput (Mbps)"), ("mean_rtt_ms", "Mean RTT (ms)"),
                   ("p95_rtt_ms", "p95 RTT (ms)")]
```

- **L13–16**: input and output folders.
- **L18–19**: for per-run graphs: which CSV column to plot and its axis label, one panel each.
- **L20–21**: for the summary graph: which summary field and its label, one panel each.

### Lines 24–26: loading a CSV

```python
def load(path):
    with open(path) as f:
        return [{k: v if k == "cc" else float(v) for k, v in row.items()} for row in csv.DictReader(f)]
```

- **L26**: `DictReader` gives each row as a dict of strings. Convert every value to a number except `cc`, which stays a
  string like `"bbr"`.

### Lines 29–56: one time-series figure per trial

```python
def plot_runs():
    groups = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(RUNS, "*.csv"))):
        name, cc = os.path.basename(path)[:-4].rsplit("_", 1)
        groups[name][cc] = load(path)
```

- **L30**: `groups[name][cc] = rows`.
- **L31**: every CSV in `data/runs/`, in a stable order.
- **L32**: `low_Chicago_chi.speedtest.clouvider.net_t1_bbr.csv` → drop `.csv` (`[:-4]`) → split at the **last** underscore →
  `name = "low_Chicago_chi.speedtest.clouvider.net_t1"`, `cc = "bbr"`. All four algorithms of one trial share a `name`.
- **L33**: load the rows into the group.

```python
    for name, runs in groups.items():
        fig, axes = plt.subplots(len(METRICS), 1, figsize=(10, 12), sharex=True)
        for cc, rows in runs.items():
            t = [r["t"] for r in rows]
            for ax, (key, label) in zip(axes, METRICS):
                ax.plot(t, [r[key] for r in rows], label=cc)
                ax.set_ylabel(label)
```

- **L35**: one figure per server + trial.
- **L36**: four panels stacked vertically, sharing the time axis.
- **L37–38**: for each algorithm, the time values.
- **L39–41**: draw one line per algorithm in each panel (cwnd, RTT, throughput, retransmit rate), labelled for the legend.

```python
        # mark where the adaptive run switched algorithms
        adaptive = runs.get("adaptive", [])
        for prev, row in zip(adaptive, adaptive[1:]):
            if row["cc"] != prev["cc"]:
                for ax in axes:
                    ax.axvline(row["t"], color="gray", linestyle=":")
                axes[0].text(row["t"], axes[0].get_ylim()[1], row["cc"], fontsize=8, va="top")
```

- **L44**: the adaptive run's rows, or an empty list if there isn't one.
- **L45–46**: walk through consecutive pairs; where `cc` changed, a switch happened.
- **L47–48**: draw a dotted vertical line at that time in every panel.
- **L49**: write the new algorithm's name at the top of the first panel.

```python
        axes[0].set_title(name)
        axes[0].legend()
        axes[-1].set_xlabel("Time (s)")
        fig.tight_layout()
        fig.savefig(os.path.join(PLOTS, f"{name}.png"))
        plt.close(fig)
```

- **L51–53**: title, legend, x-axis label (on the bottom panel only, since they share it).
- **L54**: tidy spacing so labels don't overlap.
- **L55**: save as `plots/<name>.png`.
- **L56**: close the figure to free memory (many figures are made in a loop).

### Lines 59–81: summary bar chart across all servers

```python
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
```

- **L60–61**: load all summary rows.
- **L63–65**: group rows by (server label, algorithm). The label has three lines (regime, site, host) so servers with
  the same site name stay separate. Each group holds that server+algorithm's trials.
- **L66–67**: the unique server labels and algorithms, in first-seen order (`dict.fromkeys` removes duplicates but keeps order).
- **L68**: each server gets 0.8 units of x-axis space, split evenly between its algorithm bars.

```python
    fig, axes = plt.subplots(len(SUMMARY_METRICS), 1, figsize=(12, 12))
    for ax, (key, label) in zip(axes, SUMMARY_METRICS):
        for i, cc in enumerate(ccs):
            values = [[s[key] for s in trials.get((site, cc), [])] for site in sites]
            ax.bar([x + i * width for x in range(len(sites))], [mean(v) if v else 0 for v in values], width,
                   yerr=[stdev(v) if len(v) > 1 else 0 for v in values], capsize=2, label=cc)
        ax.set_xticks([x + width * (len(ccs) - 1) / 2 for x in range(len(sites))], sites, fontsize=7)
        ax.set_ylabel(label)
```

- **L70**: three panels: mean throughput, mean RTT, p95 RTT.
- **L71**: for each panel's metric.
- **L72**: for each algorithm (`i` = its position within a group of bars).
- **L73**: for every server, the list of that metric across trials (empty if none).
- **L74–75**: draw this algorithm's bars, shifted right by `i × width` so the bars sit side by side. Bar height = mean of
  the trials; error bar (`yerr`) = standard deviation across trials (0 if only one trial); `capsize` draws little caps.
- **L76**: put each server's label under the middle of its group of bars.
- **L77**: y-axis label.

```python
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(os.path.join(PLOTS, "summary.png"))
    plt.close(fig)
```

- **L78–81**: legend on the top panel, tidy spacing, save `plots/summary.png`, free memory.

### Lines 84–87: entry point

```python
if __name__ == "__main__":
    os.makedirs(PLOTS, exist_ok=True)
    plot_runs()
    plot_summary()
```

- Make `plots/` if needed, then draw the per-run figures and the summary figure.

---

## run.sh

A shell script that prepares the Linux VM and runs the experiment plus plots.

```bash
#!/bin/bash

set -e
cd "$(dirname "$0")"
```

- **L1**: run this file with bash.
- **L3 `set -e`**: stop the whole script as soon as any command fails.
- **L4**: change to the folder this script is in, so relative paths (`requirements.txt`, `run_tests.py`) work.

```bash
if [ "$(uname)" != "Linux" ]; then
    echo "Needs Linux for TCP_INFO/TCP_CONGESTION/BBR, run inside the VM (see README)"
    exit 1
fi
```

- **L6–9**: `uname` prints the OS name. macOS doesn't support the same `TCP_INFO` layout, per-socket `TCP_CONGESTION`, or
  BBR, so refuse to run anywhere but Linux.

```bash
# enable bbr and let non-root sockets pick any of the three
sudo modprobe tcp_bbr
sudo sysctl -q net.ipv4.tcp_allowed_congestion_control="reno cubic bbr"
echo "Available: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"
```

- **L12**: load the BBR kernel module (Ubuntu ships it but doesn't load it by default).
- **L13**: normal (non-root) programs may only pick algorithms on this allowed list; add all three so `set_cc` works
  without `sudo`. `-q` = quiet.
- **L14**: print which algorithms the kernel has loaded, as a quick check that `bbr` is there.

```bash
# venv lives outside the shared mount
VENV="$HOME/.venv-cs422"
if [ ! -d "$VENV" ]; then
    echo "Making venv"
    python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"
```

- **L17**: the Python virtual environment lives in the VM's home folder, not in the project folder shared with the Mac.
  A Linux venv inside the shared folder would mix Linux-only files into your Mac project, and the shared mount is slower.
- **L18–21**: create the venv only the first time.
- **L22**: activate it, so `python` and `pip` below use it.

```bash
echo "Installing requirements.txt"
pip install -r requirements.txt > /dev/null

echo "----------------------------------------------"

python run_tests.py
python plot.py
```

- **L25**: install matplotlib (the only dependency); `> /dev/null` hides pip's progress output.
- **L27**: a separator line in the output.
- **L29**: run the experiment (about 22 minutes).
- **L30**: draw the graphs.
