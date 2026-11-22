#!/usr/bin/env bash
# Break-glass check: an admin creates a recovery key; then, with the legacy unauthenticated
# generate-root endpoint enabled temporarily on the listener, the key alone yields a root
# token. The endpoint is disabled again afterwards. Disposable spike only.
set -eu
B() { docker compose exec -T -e BAO_ADDR=http://127.0.0.1:8200 ${TOKEN:+-e BAO_TOKEN=$TOKEN} openbao bao "$@"; }
ADDR=http://127.0.0.1:18200
ADMIN=$(B login -method=userpass -field=token username=admin password=spike-admin)
KEY=$(TOKEN=$ADMIN B operator rotate-keys -target=recovery -init -key-shares=1 -key-threshold=1 -format=json \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("keys_base64") or d["keys"])[0])')
echo "recovery key created by admin"

restart() { docker compose restart openbao >/dev/null 2>&1; sleep 5; }
cp server.hcl server.hcl.bak
trap 'mv server.hcl.bak server.hcl; restart' EXIT
sed -i.tmp 's/^  tls_disable = true$/  tls_disable = true\n  disable_unauthed_generate_root_endpoints = false/' server.hcl && rm -f server.hcl.tmp
restart

# The CLI's -generate-otp reads the newer authenticated status endpoint, so the OTP is made here
# from the legacy endpoint's otp_length.
LENGTH=$(curl -sf "$ADDR/v1/sys/generate-root/attempt" | python3 -c 'import sys,json; print(json.load(sys.stdin)["otp_length"])')
OTP=$(python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range($LENGTH)))")
NONCE=$(curl -sf -X PUT "$ADDR/v1/sys/generate-root/attempt" -d "{\"otp\":\"$OTP\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["nonce"])')
ENCODED=$(curl -sf -X PUT "$ADDR/v1/sys/generate-root/update" -d "{\"key\":\"$KEY\",\"nonce\":\"$NONCE\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["encoded_token"])')
# -decode also reads the authenticated status endpoint; the encoding is the token XOR the OTP.
ROOT=$(python3 -c "import base64,sys; e=base64.b64decode(sys.argv[1]+'=='); o=sys.argv[2].encode(); print(bytes(a^b for a,b in zip(e,o)).decode())" "$ENCODED" "$OTP")
POLICIES=$(TOKEN=$ROOT B token lookup -format=json | python3 -c 'import sys,json; print(",".join(json.load(sys.stdin)["data"]["policies"]))')
echo "PASS root token from the recovery key alone, policies=$POLICIES"
TOKEN=$ROOT B token revoke -self && echo "root token revoked"
