FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/mpl

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-numpy python3-pandas python3-matplotlib ca-certificates iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py servers.csv ./

# TCP behaviour comes from the HOST kernel; run with --network host --cap-add NET_ADMIN (see run.sh).
ENTRYPOINT ["python3", "run_experiments.py"]
