#!/bin/bash
# Generuje self-signed TLS certifikát pro Graphene Intel Dashboard
# Platnost: 3650 dní (10 let)
# Výstup: /etc/ssl/graphene/{cert.pem,key.pem}

set -e
mkdir -p /etc/ssl/graphene

openssl req -x509 -nodes -days 3650 \
  -newkey rsa:2048 \
  -keyout /etc/ssl/graphene/key.pem \
  -out    /etc/ssl/graphene/cert.pem \
  -subj   "/C=CZ/ST=Czech Republic/O=Graphene Intel/CN=graphene-dashboard"

chmod 600 /etc/ssl/graphene/key.pem
echo "Certifikát vygenerován: /etc/ssl/graphene/cert.pem"
