"""Minimal iperf3-compatible TCP client (sender side) with per-interval TCP_INFO sampling."""
import argparse
import json
import math
import os
import secrets
import select
import socket
import struct
import time
from dataclasses import dataclass, field

import tcp_info

# Control-channel states from iperf3 src/iperf_api.h
TEST_START = 1
TEST_RUNNING = 2
TEST_END = 4
PARAM_EXCHANGE = 9
CREATE_STREAMS = 10
SERVER_TERMINATE = 11
EXCHANGE_RESULTS = 13
DISPLAY_RESULTS = 14
IPERF_DONE = 16
ACCESS_DENIED = -1
SERVER_ERROR = -2

COOKIE_SIZE = 37
BLOCK_SIZE = 128 * 1024
TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)

# Server-side i_errno values (iperf_api.h) that mean "your parameters were rejected".
PARAM_REJECT_CODES = {5: "duration too long", 27: "bitrate over server limit",
                      35: "auth users file", 37: "duration over server maximum",
                      142: "authentication required"}
OTHER_SERVER_CODES = {120: "server terminated", 121: "server busy", 134: "cannot set congestion",
                      144: "no message received", 160: "server test duration expired"}


class IperfError(Exception):
    def __init__(self, kind, message):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


@dataclass
class TestResult:
    host: str
    port: int
    cc_requested: str
    duration_s: float
    interval_s: float
    ip: str = ""
    cc_used: str = ""
    status: str = "ok"
    error: str = ""
    complete: bool = False
    start_time: float = 0.0
    elapsed_s: float = 0.0
    bytes_written: int = 0
    bytes_acked: int = 0
    server_bytes: int = 0
    tcp_info_len: int = 0
    server_results: dict = field(default_factory=dict)
    samples: list = field(default_factory=list)


def make_cookie():
    chars = "abcdefghijklmnopqrstuvwxyz234567"
    return ("".join(secrets.choice(chars) for _ in range(COOKIE_SIZE - 1)) + "\0").encode()


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        try:
            chunk = sock.recv(n - len(data))
        except socket.timeout:
            raise IperfError("CTRL_TIMEOUT", "server did not respond on control connection")
        except OSError as e:
            raise IperfError("PREMATURE_CLOSE", f"control connection error: {e}")
        if not chunk:
            raise IperfError("PREMATURE_CLOSE", "server closed control connection")
        data += chunk
    return data


def read_state(ctrl):
    state = struct.unpack("b", recv_exact(ctrl, 1))[0]
    if state == ACCESS_DENIED:
        raise IperfError("ACCESS_DENIED", "server is busy running another test")
    if state == SERVER_ERROR:
        code, err = struct.unpack("!ii", recv_exact(ctrl, 8))
        if code in PARAM_REJECT_CODES:
            raise IperfError("PARAM_REJECTED", f"i_errno={code} ({PARAM_REJECT_CODES[code]})")
        if code == 121:
            raise IperfError("ACCESS_DENIED", "server is busy running another test")
        raise IperfError("SERVER_ERROR", f"i_errno={code} ({OTHER_SERVER_CODES.get(code, 'unknown')}) errno={err}")
    if state == SERVER_TERMINATE:
        raise IperfError("SERVER_TERMINATE", "server terminated the test")
    return state


def expect_state(ctrl, wanted):
    state = read_state(ctrl)
    if state != wanted:
        raise IperfError("PROTOCOL_ERROR", f"expected state {wanted}, got {state}")


def send_json(ctrl, obj):
    data = json.dumps(obj, separators=(",", ":")).encode()
    ctrl.sendall(struct.pack("!I", len(data)) + data)


def recv_json(ctrl):
    size = struct.unpack("!I", recv_exact(ctrl, 4))[0]
    if size == 0 or size > 10_000_000:
        raise IperfError("PROTOCOL_ERROR", f"bad JSON length {size}")
    return json.loads(recv_exact(ctrl, size).decode(errors="replace"))


def connect(ip, port, timeout, cc=None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if cc:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, cc.encode())  # Must be set before connect().
        except OSError as e:
            sock.close()
            raise IperfError("CC_UNAVAILABLE", f"cannot set {cc}: {e}")
    sock.settimeout(timeout)
    try:
        sock.connect((ip, port))
    except socket.timeout:
        sock.close()
        raise IperfError("CONNECT_TIMEOUT", f"no answer from {ip}:{port} within {timeout}s")
    except ConnectionRefusedError:
        sock.close()
        raise IperfError("CONNECT_REFUSED", f"{ip}:{port} refused the connection")
    except OSError as e:
        sock.close()
        raise IperfError("CONNECT_ERROR", str(e))
    return sock


def make_sample(info, prev, t, sent_bytes, prev_sent):
    """Build one log row from the current and previous TCP_INFO snapshots."""
    dt = t - prev["t"]
    mss = info["snd_mss"]
    d_sent = info.get("bytes_sent", 0) - prev["info"].get("bytes_sent", 0)
    d_retrans_bytes = info.get("bytes_retrans", 0) - prev["info"].get("bytes_retrans", 0)
    d_busy = info.get("busy_time", 0) - prev["info"].get("busy_time", 0)
    row = {
        "t_s": round(t, 4),
        "interval_s": round(dt, 4),
        "goodput_bps": (info["bytes_acked"] - prev["info"]["bytes_acked"]) * 8 / dt,
        "send_rate_bps": (sent_bytes - prev_sent) * 8 / dt,
        "cwnd_pkts": info["snd_cwnd"],
        "cwnd_bytes": info["snd_cwnd"] * mss,
        "ssthresh_pkts": info["snd_ssthresh"],
        "mss_bytes": mss,
        "srtt_ms": info["rtt"] / 1000,
        "rttvar_ms": info["rttvar"] / 1000,
        "min_rtt_ms": info.get("min_rtt", 0) / 1000,
        "retrans_pkts_delta": info["total_retrans"] - prev["info"]["total_retrans"],
        "retrans_bytes_delta": d_retrans_bytes,
        "retrans_ratio": d_retrans_bytes / d_sent if d_sent > 0 else 0.0,
        "rto_events_delta": info["total_rto"] - prev["info"]["total_rto"] if "total_rto" in info else None,
        "lost_pkts": info["lost"],
        "unacked_pkts": info["unacked"],
        "ca_state": info["ca_state"],
        "pacing_rate_bps": info["pacing_rate"] * 8,
        "delivery_rate_bps": info.get("delivery_rate", 0) * 8,
        "app_limited": info["flags"] & 1,
        "rwnd_limited_frac": (info.get("rwnd_limited", 0) - prev["info"].get("rwnd_limited", 0)) / d_busy if d_busy > 0 else 0.0,
        "sndbuf_limited_frac": (info.get("sndbuf_limited", 0) - prev["info"].get("sndbuf_limited", 0)) / d_busy if d_busy > 0 else 0.0,
        "snd_wnd_bytes": info.get("snd_wnd"),
        "notsent_bytes": info.get("notsent_bytes"),
        "bytes_acked": info["bytes_acked"],
        "bytes_sent": info.get("bytes_sent"),
        "bytes_retrans": info.get("bytes_retrans"),
        "total_retrans_pkts": info["total_retrans"],
    }
    return row


def send_loop(ctrl, data, result, duration, interval):
    """Send data until `duration` passes, sampling TCP_INFO every `interval` seconds."""
    buf = os.urandom(BLOCK_SIZE)
    data.setblocking(False)
    start = time.monotonic()
    info, result.tcp_info_len = tcp_info.read(data)
    prev = {"t": 0.0, "info": info}
    prev_sent = 0
    sent = 0
    next_sample = start + interval
    deadline = start + duration
    try:
        while True:
            now = time.monotonic()
            t = now - start
            if now >= deadline and t - prev["t"] < interval / 2:
                break  # Skip a tiny final interval; it would give a noisy goodput value.
            if now >= next_sample or now >= deadline:
                info, _ = tcp_info.read(data)
                result.samples.append(make_sample(info, prev, t, sent, prev_sent))
                prev = {"t": t, "info": info}
                prev_sent = sent
                next_sample = max(next_sample + interval, now + interval / 2)
                if now >= deadline:
                    break
            wait = max(0.0, min(next_sample, deadline) - time.monotonic())
            readable, writable, _ = select.select([ctrl], [data], [], wait)
            if readable:
                read_state(ctrl)  # Any message mid-test is an error/termination; read_state raises for those.
                raise IperfError("PROTOCOL_ERROR", "unexpected control message during test")
            if writable:
                try:
                    sent += data.send(buf)
                except BlockingIOError:
                    pass
                except OSError as e:
                    raise IperfError("PREMATURE_CLOSE", f"data connection failed: {e}")
    finally:
        result.bytes_written = sent
        result.elapsed_s = time.monotonic() - start
        result.bytes_acked = prev["info"]["bytes_acked"]


def run_test(host, port, cc="cubic", duration=30, interval=0.2,
             connect_timeout=5.0, ctrl_timeout=15.0):
    """Run one iperf3 upload test against host:port. Always returns a TestResult (status says what happened)."""
    result = TestResult(host=host, port=port, cc_requested=cc, duration_s=duration,
                        interval_s=interval, start_time=time.time())
    ctrl = None
    data = None
    try:
        try:
            result.ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except socket.gaierror as e:
            raise IperfError("DNS_FAIL", str(e))

        # (i) Control connection: connect, then send the cookie that identifies this test.
        cookie = make_cookie()
        ctrl = connect(result.ip, port, connect_timeout)
        ctrl.settimeout(ctrl_timeout)
        ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ctrl.sendall(cookie)

        # (ii) Parameter exchange as length-prefixed JSON.
        expect_state(ctrl, PARAM_EXCHANGE)
        send_json(ctrl, {"tcp": True, "omit": 0, "time": math.ceil(duration), "num": 0, "blockcount": 0,
                         "parallel": 1, "len": BLOCK_SIZE, "pacing_timer": 1000,
                         "client_version": "3.17"})

        # (iii) Data connection: pick the congestion control before connecting, then send the same cookie.
        expect_state(ctrl, CREATE_STREAMS)
        data = connect(result.ip, port, connect_timeout, cc)
        data.sendall(cookie)
        result.cc_used = data.getsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, 16).split(b"\0")[0].decode()

        expect_state(ctrl, TEST_START)
        expect_state(ctrl, TEST_RUNNING)

        # (iv) Send continuously for the requested duration.
        cpu_start = os.times()
        send_loop(ctrl, data, result, duration, interval)

        # (v) Terminate: TEST_END, swap results, wait for DISPLAY_RESULTS, then IPERF_DONE.
        ctrl.setblocking(True)
        ctrl.settimeout(ctrl_timeout)
        ctrl.sendall(struct.pack("b", TEST_END))
        expect_state(ctrl, EXCHANGE_RESULTS)
        info, _ = tcp_info.read(data)
        result.bytes_acked = info["bytes_acked"]  # Includes data still in flight when sending stopped.
        cpu_end = os.times()
        user_pct = 100 * (cpu_end.user - cpu_start.user) / max(result.elapsed_s, 1e-6)
        system_pct = 100 * (cpu_end.system - cpu_start.system) / max(result.elapsed_s, 1e-6)
        send_json(ctrl, {
            "cpu_util_total": user_pct + system_pct, "cpu_util_user": user_pct,
            "cpu_util_system": system_pct,
            "sender_has_retransmits": 1, "congestion_used": result.cc_used,
            "streams": [{"id": 1, "bytes": result.bytes_written, "retransmits": info["total_retrans"],
                         "jitter": 0, "errors": 0, "omitted_errors": 0, "packets": 0,
                         "omitted_packets": 0, "start_time": 0, "end_time": result.elapsed_s}],
        })
        result.server_results = recv_json(ctrl)
        streams = result.server_results.get("streams") or [{}]
        result.server_bytes = int(streams[0].get("bytes", 0))
        expect_state(ctrl, DISPLAY_RESULTS)
        ctrl.sendall(struct.pack("b", IPERF_DONE))
        result.complete = True
    except IperfError as e:
        result.status, result.error = e.kind, e.message
    except OSError as e:
        result.status, result.error = "PREMATURE_CLOSE", str(e)
    finally:
        for sock in (data, ctrl):
            if sock is not None:
                sock.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="Run a single iperf3 upload test.")
    parser.add_argument("host")
    parser.add_argument("-p", "--port", type=int, default=5201)
    parser.add_argument("-C", "--cc", default="cubic")
    parser.add_argument("-t", "--duration", type=float, default=10)
    parser.add_argument("-i", "--interval", type=float, default=0.2)
    args = parser.parse_args()

    r = run_test(args.host, args.port, args.cc, args.duration, args.interval)
    for s in r.samples:
        print(f"t={s['t_s']:6.2f}s goodput={s['goodput_bps']/1e6:8.2f} Mbps cwnd={s['cwnd_pkts']:5d} "
              f"srtt={s['srtt_ms']:7.2f} ms retrans+={s['retrans_pkts_delta']}")
    print(f"status={r.status} {r.error} cc_used={r.cc_used} sent={r.bytes_written} "
          f"acked={r.bytes_acked} server_received={r.server_bytes} tcp_info_len={r.tcp_info_len}")


if __name__ == "__main__":
    main()
