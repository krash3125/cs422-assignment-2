"""Decode Linux `struct tcp_info` returned by getsockopt(TCP_INFO)."""
import socket
import struct

TCP_INFO = getattr(socket, "TCP_INFO", 11)

# Field order and types copied from include/uapi/linux/tcp.h (B=u8, H=u16, I=u32, Q=u64).
FIELDS = [
    ("state", "B"), ("ca_state", "B"), ("retransmits", "B"), ("probes", "B"),
    ("backoff", "B"), ("options", "B"), ("wscale", "B"), ("flags", "B"),
    ("rto", "I"), ("ato", "I"), ("snd_mss", "I"), ("rcv_mss", "I"),
    ("unacked", "I"), ("sacked", "I"), ("lost", "I"), ("retrans", "I"), ("fackets", "I"),
    ("last_data_sent", "I"), ("last_ack_sent", "I"), ("last_data_recv", "I"), ("last_ack_recv", "I"),
    ("pmtu", "I"), ("rcv_ssthresh", "I"), ("rtt", "I"), ("rttvar", "I"),
    ("snd_ssthresh", "I"), ("snd_cwnd", "I"), ("advmss", "I"), ("reordering", "I"),
    ("rcv_rtt", "I"), ("rcv_space", "I"), ("total_retrans", "I"),
    ("pacing_rate", "Q"), ("max_pacing_rate", "Q"), ("bytes_acked", "Q"), ("bytes_received", "Q"),
    ("segs_out", "I"), ("segs_in", "I"), ("notsent_bytes", "I"), ("min_rtt", "I"),
    ("data_segs_in", "I"), ("data_segs_out", "I"),
    ("delivery_rate", "Q"),
    ("busy_time", "Q"), ("rwnd_limited", "Q"), ("sndbuf_limited", "Q"),
    ("delivered", "I"), ("delivered_ce", "I"),
    ("bytes_sent", "Q"), ("bytes_retrans", "Q"),
    ("dsack_dups", "I"), ("reord_seen", "I"), ("rcv_ooopack", "I"), ("snd_wnd", "I"),
    ("rcv_wnd", "I"), ("rehash", "I"),
    ("total_rto", "H"), ("total_rto_recoveries", "H"), ("total_rto_time", "I"),
]

CA_STATES = {0: "Open", 1: "Disorder", 2: "CWR", 3: "Recovery", 4: "Loss"}


def decode(raw):
    """Turn raw bytes into a dict. Fields newer than the running kernel are left out."""
    info = {}
    offset = 0
    for name, code in FIELDS:
        size = struct.calcsize("=" + code)
        if offset + size > len(raw):
            break
        info[name] = struct.unpack_from("=" + code, raw, offset)[0]
        offset += size
    return info


def read(sock):
    """Read TCP_INFO from a live socket. Returns (fields dict, number of bytes the kernel returned)."""
    raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, 256)
    return decode(raw), len(raw)
