#!/usr/bin/env bash
# Exercise the S2 policies against the running spike. Prints PASS/FAIL per check.
set -u
failures=0
B() { docker compose exec -T -e BAO_ADDR=http://127.0.0.1:8200 ${TOKEN:+-e BAO_TOKEN=$TOKEN} openbao bao "$@"; }
expect() { local want=$1; shift; if out=$("$@" 2>&1); then got=ok; else got=refused; fi
  if [ "$got" = "$want" ]; then echo "PASS $want: $*"; else echo "FAIL want $want got $got: $*"; echo "$out" | tail -3; failures=$((failures + 1)); fi; }

ADMIN=$(TOKEN= B login -method=userpass -field=token username=admin password=spike-admin)
OPER=$(TOKEN= B login -method=userpass -field=token username=operator password=spike-operator)
TOKEN=$ADMIN
for role in accounts gateway; do
  rid=$(B read -field=role_id auth/approle/role/$role/role-id)
  sid=$(B write -f -field=secret_id auth/approle/role/$role/secret-id)
  declare "${role^^}=$(TOKEN= B write -field=token auth/approle/login role_id=$rid secret_id=$sid)"
done

echo "== accounts (create-only)"
TOKEN=$ACCOUNTS
expect ok      B kv put -mount=concurro -cas=0 personal/r1 secret=sk-test-1
expect refused B kv put -mount=concurro -cas=0 personal/r1 secret=sk-test-2
expect refused B kv put -mount=concurro -cas=1 personal/r1 secret=sk-test-2
expect refused B kv get -mount=concurro personal/r1
expect ok      B kv metadata get -mount=concurro personal/r1
expect ok      B kv put -mount=concurro -cas=0 personal/r2 secret=sk-test-r2
expect refused B kv put -mount=concurro personal/r2 secret=overwrite-without-cas
expect ok      B kv destroy -mount=concurro -versions=1 personal/r1
expect ok      B kv metadata delete -mount=concurro personal/r1
expect refused B kv get -mount=concurro ops/gateway/providers

echo "== operator (ops only)"
TOKEN=$OPER
expect ok      B kv put -mount=concurro -cas=0 ops/gateway/providers chatgpt_access_token=tok-1 openai_api_key=key-1
expect ok      B kv patch -mount=concurro ops/gateway/providers chatgpt_access_token=tok-2
expect ok      B kv get -mount=concurro -field=chatgpt_access_token ops/gateway/providers
expect refused B kv get -mount=concurro personal/r2
expect refused B kv put -mount=concurro -cas=0 personal/r9 secret=x

echo "== gateway (read personal and its ops path)"
TOKEN=$GATEWAY
expect ok      B kv get -mount=concurro personal/r2
expect ok      B kv get -mount=concurro ops/gateway/providers
expect refused B kv put -mount=concurro -cas=0 personal/r4 secret=x
expect refused B kv destroy -mount=concurro -versions=1 personal/r2

echo "== admin (no personal data)"
TOKEN=$ADMIN
expect refused B kv get -mount=concurro personal/r2
expect ok      B write -f auth/approle/role/accounts/secret-id
expect ok      B policy read accounts

echo "== $failures failed"
exit $((failures > 0))
