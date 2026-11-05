FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf
COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

# Placeholder matches nothing so Squid starts before the controller writes the real list.
RUN install -d -m 0755 /etc/squid/allowlist \
    && echo ".invalid" > /etc/squid/allowlist/allowlist.txt \
    && install -d -o proxy -g proxy /var/log/squid /run/squid \
    && squid -k parse -f /etc/squid/squid.conf

EXPOSE 3128
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
