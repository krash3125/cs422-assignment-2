import argparse
import csv
import json
import random
import socket
import string
import struct
import time

from adaptive_cc import AdaptiveCC

# iperf3 control states
TEST_START, TEST_RUNNING, TEST_END = 1, 2, 4
PARAM_EXCHANGE, CREATE_STREAMS = 9, 10
EXCHANGE_RESULTS, DISPLAY_RESULTS, IPERF_DONE = 13, 14, 16
ACCESS_DENIED = -1

# struct tcp_info from linux/tcp.h up to delivery_rate, fields we don't use skipped as pad bytes (x)
TCP_INFO_FMT = "16x I 48x I 8x I 16x I 16x Q 8x 12x I 4x I Q"
TCP_INFO_FIELDS = "snd_mss rtt snd_cwnd total_retrans bytes_acked min_rtt data_segs_out delivery_rate".split()

BLOCK = b"\0" * 65536


def tcp_info(sock):
    raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, struct.calcsize(TCP_INFO_FMT))
    return dict(zip(TCP_INFO_FIELDS, struct.unpack(TCP_INFO_FMT, raw)))


def get_cc(sock):
    return sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16).rstrip(b"\0").decode()


def set_cc(sock, cc):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, cc.encode())


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("server closed connection")
        data += chunk
    return data


def expect(sock, want):
    state = struct.unpack("b", recv_exact(sock, 1))[0]
    if state == ACCESS_DENIED:
        raise ConnectionRefusedError("server busy")
    if state != want:
        raise ConnectionError(f"expected state {want}, got {state}")


def send_json(sock, obj):
    data = json.dumps(obj).encode()
    sock.sendall(struct.pack("!I", len(data)) + data)


def recv_json(sock):
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    return json.loads(recv_exact(sock, size))


def send_loop(sock, duration, interval, adaptive):
    samples, sent, last_sent = [], 0, 0
    start = last_t = time.monotonic()
    last = tcp_info(sock)
    next_sample = start + interval

    while time.monotonic() - start < duration:
        try:
            sent += sock.send(BLOCK)
        except TimeoutError:
            pass
        now = time.monotonic()
        if now < next_sample:
            continue

        info, dt = tcp_info(sock), now - last_t
        segs = info["data_segs_out"] - last["data_segs_out"]
        sample = {
            "t": round(now - start, 3),
            "time": round(time.time(), 3),
            "cc": get_cc(sock),
            "cwnd": info["snd_cwnd"],
            "mss": info["snd_mss"],
            "rtt_ms": info["rtt"] / 1000,
            "min_rtt_ms": info["min_rtt"] / 1000,
            "throughput_mbps": round((info["bytes_acked"] - last["bytes_acked"]) * 8 / dt / 1e6, 3),
            "send_mbps": round((sent - last_sent) * 8 / dt / 1e6, 3),
            "delivery_mbps": round(info["delivery_rate"] * 8 / 1e6, 3),
            "loss_rate": round((info["total_retrans"] - last["total_retrans"]) / segs, 5) if segs else 0,
            "bytes_acked": info["bytes_acked"],
            "total_retrans": info["total_retrans"],
            "data_segs_out": info["data_segs_out"],
        }
        samples.append(sample)
        print(f"{sample['t']:6.2f}s {sample['cc']:5} cwnd={sample['cwnd']:5} rtt={sample['rtt_ms']:7.2f}ms "
              f"tput={sample['throughput_mbps']:8.2f}Mbps loss={sample['loss_rate']:.2%}")

        if adaptive and (cc := adaptive.update(sample)) != sample["cc"]:
            set_cc(sock, cc)

        last_t, last, last_sent = now, info, sent
        next_sample += interval

    return samples, sent


def run_test(host, port, cc="cubic", duration=30, interval=0.2):
    cookie = "".join(random.choices(string.ascii_lowercase, k=36)).encode() + b"\0"
    adaptive = AdaptiveCC() if cc == "adaptive" else None

    with socket.create_connection((host, port), timeout=5) as ctrl:
        ctrl.sendall(cookie)
        expect(ctrl, PARAM_EXCHANGE)
        send_json(ctrl, {"tcp": True, "time": duration})
        expect(ctrl, CREATE_STREAMS)

        with socket.socket(ctrl.family, socket.SOCK_STREAM) as data:
            set_cc(data, adaptive.cc if adaptive else cc)
            data.settimeout(5)
            data.connect(ctrl.getpeername())
            data.sendall(cookie)
            expect(ctrl, TEST_START)
            expect(ctrl, TEST_RUNNING)

            data.settimeout(interval)
            samples, sent = send_loop(data, duration, interval, adaptive)
            retrans = tcp_info(data)["total_retrans"]

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


def save_csv(samples, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=samples[0].keys())
        writer.writeheader()
        writer.writerows(samples)


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
