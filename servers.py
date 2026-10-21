"""Load the public iperf3 server list and pick n working destinations."""
import csv
import json
import urllib.request
from dataclasses import dataclass

from iperf_client import run_test

LIST_URL = "https://export.iperf3serverlist.net/listed_iperf3_servers.json"
CSV_COLUMNS = ["host", "port", "continent", "country", "site", "provider", "options"]


@dataclass
class Server:
    host: str
    ports: list
    continent: str = ""
    country: str = ""
    site: str = ""
    provider: str = ""


def parse_ports(text):
    """'5201' -> [5201], '5201-5205' -> [5201, ..., 5205]."""
    text = text.strip()
    if "-" in text:
        low, high = text.split("-")
        return list(range(int(low), int(high) + 1))
    return [int(text)]


def load_servers(path):
    servers = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            servers.append(Server(row["host"].strip(), parse_ports(row["port"]), row.get("continent", ""),
                                  row.get("country", ""), row.get("site", ""), row.get("provider", "")))
    return servers


def refresh_from_web(path):
    """Download the current list from iperf3serverlist.net and save it as CSV."""
    with urllib.request.urlopen(LIST_URL, timeout=30) as resp:
        entries = json.load(resp)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for e in entries:
            writer.writerow([e["IP/HOST"], e["PORT"], e["CONTINENT"], e["COUNTRY"],
                             e["SITE"], e["PROVIDER"], e["OPTIONS"]])
    return len(entries)


def select_destinations(servers, n, max_attempts, rng, log, probe_seconds=3, ports_per_server=2):
    """Shuffle the list and keep the first n servers that pass a short probe; returns (selected, shortfall)."""
    candidates = servers[:]
    rng.shuffle(candidates)
    selected = []
    attempts = 0
    for server in candidates:
        if len(selected) >= n or attempts >= max_attempts:
            break
        ports = rng.sample(server.ports, min(ports_per_server, len(server.ports)))
        for port in ports:
            if attempts >= max_attempts:
                break
            attempts += 1
            r = run_test(server.host, port, "cubic", duration=probe_seconds, interval=1.0)
            log({"phase": "select", "host": server.host, "port": port, "cc": "cubic",
                 "status": r.status, "error": r.error})
            if r.status == "ok":
                selected.append({"host": server.host, "ip": r.ip, "port": port, "ports": server.ports,
                                 "continent": server.continent, "country": server.country,
                                 "site": server.site, "provider": server.provider})
                break
            if r.status not in ("ACCESS_DENIED", "CONNECT_REFUSED"):
                break  # Only a busy/closed port is worth retrying on another port of the same host.
    return selected, max(0, n - len(selected))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh servers.csv from iperf3serverlist.net")
    parser.add_argument("--out", default="servers.csv")
    count = refresh_from_web(parser.parse_args().out)
    print(f"saved {count} servers")
