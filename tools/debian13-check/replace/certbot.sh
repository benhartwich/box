# No public DNS in the test: a self-signed certificate where certbot would put its files.
mkdir -p "/etc/letsencrypt/live/${BOX_DOMAIN}"
openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=${BOX_DOMAIN}" \
    -keyout "/etc/letsencrypt/live/${BOX_DOMAIN}/privkey.pem" \
    -out "/etc/letsencrypt/live/${BOX_DOMAIN}/fullchain.pem"
