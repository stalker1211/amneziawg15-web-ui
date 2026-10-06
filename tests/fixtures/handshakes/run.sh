#!/usr/bin/env bash
# Regenerates this fixture (DEVELOPMENT.md §10, 2.8): datagrams from the real daemon,
# with keys whose answers are known. One server container (AWG 3.x with header protection
# on 51820, without on 51821) and five devices pinging through it, each in its own
# container:
#   A  old S1-S4/H1-H4, trailers on, same HeaderProtectionKey -> named, S1 31, H1 500000-500999
#   B  old S1-S4/H1-H4 on 51821 (no header protection)        -> named, S1 33, H1 1500000-1500999
#   C  current S/H, an old HeaderProtectionKey                  -> nobody
#   D  a config for another server's key                        -> nobody
#   E  correct                                                   -> named at the current S1 40
# Writes capture.jsonl (every datagram to both ports), keys.json (throwaway keys, the
# devices' addresses) and conntrack.txt (`conntrack -L -p udp` mid-capture: A-D
# [UNREPLIED], E [ASSURED]). identify.py prints the answers. Needs bash 4+, local Docker
# (not the NAS context) and the image `amneziawg-web-ui:local` (./run.sh builds it).
set -euo pipefail
cd "$(dirname "$0")"
IMG=amneziawg-web-ui:local
NET=awgpoc
CASES="A B C D E"

docker rm -f awgpoc-srv $(for c in $CASES; do echo awgpoc-$c; done) >/dev/null 2>&1 || true
docker network rm $NET >/dev/null 2>&1 || true
docker network create $NET >/dev/null
for n in srv $CASES; do
  docker run -d --name awgpoc-$n --network $NET --cap-add NET_ADMIN --device /dev/net/tun \
    --entrypoint sleep $IMG infinity >/dev/null
done
ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "awgpoc-$1"; }
SRV_IP=$(ip_of srv)

genkey() { docker exec awgpoc-srv sh -c 'k=$(awg genkey); printf "%s %s" "$k" "$(printf %s "$k" | awg pubkey)"'; }
declare -A PRIV PUB
for n in srv0 srv1 other A B C D E; do read -r PRIV[$n] PUB[$n] <<<"$(genkey)"; done
K1=$(docker exec awgpoc-srv awg genkey)   # the server's HeaderProtectionKey
K2=$(docker exec awgpoc-srv awg genkey)   # an "old" one, for case C

# Current parameters on the server; "old" ones on the misconfigured devices.
CUR0="S1 = 40
S2 = 30
S3 = 50
S4 = 20
H1 = 100000-100999
H2 = 200000-200999
H3 = 300000-300999
H4 = 400000-400999"
OLD0="S1 = 31
S2 = 25
S3 = 33
S4 = 14
H1 = 500000-500999
H2 = 600000-600999
H3 = 700000-700999
H4 = 800000-800999"
CUR1="S1 = 55
S2 = 66
S3 = 40
S4 = 20
H1 = 1100000-1100999
H2 = 1200000-1200999
H3 = 1300000-1300999
H4 = 1400000-1400999"
OLD1="S1 = 33
S2 = 44
S3 = 20
S4 = 15
H1 = 1500000-1500999
H2 = 1600000-1600999
H3 = 1700000-1700999
H4 = 1800000-1800999"

put() { docker exec -i "awgpoc-$1" sh -c "cat > $2"; }
up() {  # container iface conf address
  docker exec -d "awgpoc-$1" sh -c "LOG_LEVEL=debug amneziawg-go -f $2 > /tmp/$2.log 2>&1"
  docker exec "awgpoc-$1" sh -c "for i in \$(seq 50); do awg show $2 >/dev/null 2>&1 && break; sleep 0.1; done
    awg setconf $2 $3 && ip addr add $4 dev $2 && ip link set $2 up"
}

put srv /tmp/wg0.conf <<EOF
[Interface]
PrivateKey = ${PRIV[srv0]}
ListenPort = 51820
$CUR0
HeaderProtectionKey = $K1
RandomTrailers = off

[Peer]
PublicKey = ${PUB[A]}
AllowedIPs = 10.99.0.2/32

[Peer]
PublicKey = ${PUB[C]}
AllowedIPs = 10.99.0.3/32

[Peer]
PublicKey = ${PUB[E]}
AllowedIPs = 10.99.0.4/32

[Peer]
PublicKey = ${PUB[D]}
AllowedIPs = 10.99.0.5/32
EOF
put srv /tmp/wg1.conf <<EOF
[Interface]
PrivateKey = ${PRIV[srv1]}
ListenPort = 51821
$CUR1

[Peer]
PublicKey = ${PUB[B]}
AllowedIPs = 10.98.0.2/32
EOF
docker exec awgpoc-srv iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE  # conntrack on, as in the panel
docker exec awgpoc-srv apk add --no-cache -q conntrack-tools >/dev/null
up srv wg0 /tmp/wg0.conf 10.99.0.1/24
up srv wg1 /tmp/wg1.conf 10.98.0.1/24
docker cp cap.py awgpoc-srv:/tmp/cap.py >/dev/null

client() {  # case params hpk_line trailers server_pub port addr net
  put "$1" /tmp/wg0.conf <<EOF
[Interface]
PrivateKey = ${PRIV[$1]}
Jc = 4
Jmin = 40
Jmax = 80
$2
$3
RandomTrailers = $4
I1 = <r 60>

[Peer]
PublicKey = $5
Endpoint = $SRV_IP:$6
AllowedIPs = $8
EOF
  up "$1" wg0 /tmp/wg0.conf "$7"
}
client A "$OLD0" "HeaderProtectionKey = $K1" on  "${PUB[srv0]}"  51820 10.99.0.2/24 10.99.0.0/24
client B "$OLD1" ""                          off "${PUB[srv1]}"  51821 10.98.0.2/24 10.98.0.0/24
client C "$CUR0" "HeaderProtectionKey = $K2" off "${PUB[srv0]}"  51820 10.99.0.3/24 10.99.0.0/24
client D "$CUR0" "HeaderProtectionKey = $K1" off "${PUB[other]}" 51820 10.99.0.5/24 10.99.0.0/24
client E "$CUR0" "HeaderProtectionKey = $K1" off "${PUB[srv0]}"  51820 10.99.0.4/24 10.99.0.0/24

# Traffic makes each device start handshakes; it begins 2 s into a 24 s capture.
for c in A C D E; do docker exec -d "awgpoc-$c" sh -c 'sleep 2; ping -c 40 -i 0.5 10.99.0.1 >/dev/null 2>&1'; done
docker exec -d awgpoc-B sh -c 'sleep 2; ping -c 40 -i 0.5 10.98.0.1 >/dev/null 2>&1'
docker exec -d awgpoc-srv sh -c 'sleep 12; conntrack -L -p udp > /tmp/conntrack.txt 2>/dev/null'
docker exec awgpoc-srv python3 /tmp/cap.py 24

docker cp awgpoc-srv:/tmp/capture.jsonl ./capture.jsonl >/dev/null
docker cp awgpoc-srv:/tmp/conntrack.txt ./conntrack.txt >/dev/null
{
  printf '{"servers": {"51820": {"priv": "%s", "hpk": "%s"}, "51821": {"priv": "%s"}},\n' \
    "${PRIV[srv0]}" "$K1" "${PRIV[srv1]}"
  printf ' "clients": {'; sep=""
  for c in $CASES; do printf '%s"%s": "%s"' "$sep" "$c" "${PUB[$c]}"; sep=", "; done
  printf '},\n "sources": {'; sep=""
  for c in $CASES; do printf '%s"%s": "%s"' "$sep" "$c" "$(ip_of $c)"; sep=", "; done
  printf '},\n "srv_pub": {"srv0": "%s", "other": "%s"}}\n' "${PUB[srv0]}" "${PUB[other]}"
} > keys.json
docker rm -f awgpoc-srv $(for c in $CASES; do echo awgpoc-$c; done) >/dev/null
docker network rm $NET >/dev/null
echo "fixture written; check: uv run --no-project --with cryptography python identify.py keys.json capture.jsonl"
