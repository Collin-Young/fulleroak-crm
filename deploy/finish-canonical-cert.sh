#!/bin/sh
set -eu

expected_ip="157.230.11.23"
resolved_ip="$(dig +short @1.1.1.1 crm.geoleads.land A | tail -n 1)"

if [ "$resolved_ip" != "$expected_ip" ]; then
    exit 0
fi

certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email \
    --redirect --expand \
    --cert-name fulleroak-crm.157.230.11.23.sslip.io \
    -d fulleroak-crm.157.230.11.23.sslip.io \
    -d crm.geoleads.land \
    -d fulleroakcrm.geoleads.land

systemctl disable --now fulleroak-crm-cert.timer || true
