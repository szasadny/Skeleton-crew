#!/usr/bin/env bash
set -euo pipefail

install -d -o proxy -g proxy /var/log/squid /run/squid
for log_file in access.log cache.log; do
    install -m 0640 -o proxy -g proxy /dev/null "/var/log/squid/${log_file}"
done

# Squid workers cannot open the container's stdout pipe after dropping to the proxy user.
tail -q -n 0 -F /var/log/squid/access.log /var/log/squid/cache.log &

exec squid -N -f /etc/squid/squid.conf
