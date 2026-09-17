#!/usr/bin/env bash
# Generate local-only RSA key and X.509 certificate for OAuth2 SAML Bearer.
set -euo pipefail
umask 077

if [[ $# -lt 1 || $# -gt 3 ]]; then
  echo "Usage: $0 <company_id> [technical_user_CN] [validity_days]" >&2
  exit 2
fi
company_id="$1"
common_name="${2:-APIUSER}"
validity_days="${3:-730}"
if [[ ! "$company_id" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] ||
   [[ ! "$common_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$ ]] ||
   [[ ! "$validity_days" =~ ^[1-9][0-9]{0,3}$ ]]; then
  echo "Invalid company ID, common name, or validity (1-9999 days)." >&2
  exit 2
fi

# Reserve a new directory atomically; existing material is never overwritten.
mkdir -p secrets
output="secrets/keypair_${company_id}"
if ! mkdir "$output"; then
  echo "Output already exists or cannot be created; choose another directory." >&2
  exit 1
fi
trap 'rm -f "$output/private_key.pem" "$output/certificate.crt"; rmdir "$output"' ERR
openssl genrsa -out "$output/private_key.pem" 2048 2>/dev/null
openssl req -new -x509 -sha256 -key "$output/private_key.pem" \
  -out "$output/certificate.crt" -days "$validity_days" -subj "/CN=$common_name" 2>/dev/null
trap - ERR
printf 'Created private key: %s/private_key.pem\nCertificate: %s/certificate.crt\n' "$output" "$output"
echo "Upload the certificate to your SuccessFactors OAuth client; keep both files out of Git."
