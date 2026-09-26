#!/bin/sh
# An own certificate authority for a Myboxi server without a public certificate, e.g. only
# in the home network (docs/selbst-hosten.md, "Eigene Zertifizierungsstelle").
#   ./own-ca.sh myboxi.home.arpa [192.168.1.20]   writes into ./certs (or $CERTS_DIR)
#   ./own-ca.sh 192.168.1.20                      without a name
# Creates the CA once (certs/ca/ca.key stays here, 0600), then a server certificate for the
# name and, optionally, the IP address. Run it again to renew the server certificate
# (valid 825 days, the maximum phones accept for own CAs); the boxes keep trusting the CA.
# Give the boxes certs/myboxi-ca.pem in the setup portal, never ca.key.
set -eu

name=${1:?usage: own-ca.sh NAME [IP]}
ip=${2:-}
out=${CERTS_DIR:-./certs}
umask 077
mkdir -p "$out/ca"

if [ ! -f "$out/ca/ca.key" ]; then
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
        -subj "/CN=Myboxi CA ${name}" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -keyout "$out/ca/ca.key" -out "$out/myboxi-ca.pem" 2>/dev/null
    echo "new CA: $out/myboxi-ca.pem"
fi

case "$name" in
    *[!0-9.]*) san="DNS:${name}" ;;
    *) san="IP:${name}" ;;  # only an IPv4 address, when no name resolves in the home network
esac
[ -n "$ip" ] && san="${san},IP:${ip}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat > "$work/ext" <<EOF
subjectAltName=${san}
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=${name}" \
    -keyout "$work/key" -out "$work/csr" 2>/dev/null
openssl x509 -req -in "$work/csr" -CA "$out/myboxi-ca.pem" -CAkey "$out/ca/ca.key" \
    -CAcreateserial -CAserial "$out/ca/serial" -days 825 -extfile "$work/ext" \
    -out "$work/cert" 2>/dev/null
cat "$work/cert" "$out/myboxi-ca.pem" > "$out/fullchain.pem"
install -m 0600 "$work/key" "$out/privkey.pem"
chmod 0644 "$out/myboxi-ca.pem" "$out/fullchain.pem"
echo "server certificate for ${san}: $out/fullchain.pem, $out/privkey.pem"
