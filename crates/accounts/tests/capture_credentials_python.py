"""Capture synthetic owner-vault wire fixtures through the pre-retirement Python adapter.

Run from the repository root at the pre-retirement baseline (or a checkout of
that baseline) with backoffice/tests and gateway/tests on PYTHONPATH. Redirect
stdout to credentials_python.json. Requires the pinned Backoffice test packages.
"""

import copy
import json
import httpx
from backoffice.credentials import Vault, VaultError
from test_backoffice import settings

raw_entry = {
    "entry_id": "e" * 32, "provider": "openai", "route": "openai-api-key",
    "kind": "api_key", "name": "Credits", "enabled": True, "roles": ["triage"],
    "profiles": [{"target": "acme/demo", "profile": "sonnet55-high"}],
    "revision": 4, "removed": None, "created": "2026-11-17T00:00:00+00:00",
    "record": {"status": "valid", "status_reason": "stored", "fingerprint": "sha256:fixture",
        "version": 2, "updated": "2026-11-17T00:01:00+00:00", "last_used": None,
        "expires_at": None, "expiry_known": False, "account_label": None,
        "verified_account": False, "custody": "vault", "relay_id": None},
}
removed_entry = copy.deepcopy(raw_entry)
removed_entry["removed"] = "2026-11-17T00:02:00+00:00"
removed_entry["revision"] = 5
removed_entry["record"]["status"] = "revoked"
removed_entry["record"]["status_reason"] = "revoked"
relay_entry = copy.deepcopy(raw_entry)
relay_entry["route"] = "chatgpt-subscription"
relay_entry["kind"] = "subscription"
relay_entry["record"].update({"custody": "relay", "relay_id": "relay-1", "account_label": "work", "verified_account": True})
second_relay_entry = copy.deepcopy(relay_entry)
second_relay_entry["entry_id"] = "f" * 32
second_relay_entry["provider"] = "anthropic"
second_relay_entry["route"] = "claude-subscription"
raw_group = {"provider": "openai", "revision": 4, "entries": [raw_entry]}
raw_listing = {"providers": [{"provider": "anthropic", "revision": 0, "entries": []}, raw_group],
    "types": [
      {"route":"anthropic-api-key", "provider":"anthropic", "kind":"api_key", "label":"Anthropic API key"},
      {"route":"claude-subscription", "provider":"anthropic", "kind":"subscription", "label":"Claude subscription"},
      {"route":"openai-api-key", "provider":"openai", "kind":"api_key", "label":"OpenAI API key"},
      {"route":"chatgpt-subscription", "provider":"openai", "kind":"subscription", "label":"ChatGPT subscription"},
    ]}
raw_status = {"service":"available", "started":"2026-11-17T00:00:00+00:00", "checked":"2026-11-17T00:01:00+00:00",
    "relays":[{"relay":"relay-1", "idle":1, "last_seen":"2026-11-17T00:01:00+00:00",
      "active":[{"slot":1,"entry_id":"e"*32,"route":"chatgpt-subscription", "version":2,
        "since":"2026-11-17T00:00:00+00:00","task":{"repository":"acme/demo","issue":4}}]}]}
reply = None
observed = []
def capture(request):
    observed.append({"method": request.method, "path": request.url.path,
        "query": dict(request.url.params), "authorization": request.headers.get("authorization"),
        "body": json.loads(request.content) if request.content else None,
        "timeout": request.extensions.get("timeout")})
    return reply
vault = Vault("http://vault.invalid", httpx.Client(timeout=30, transport=httpx.MockTransport(capture)))
cases = []
def case(name, status, upstream, invoke):
    global reply
    reply = httpx.Response(status, json=upstream)
    observed.clear()
    try:
        result = invoke()
        outcome = {"status": 200, "body": result}
    except VaultError as error:
        outcome = {"status": error.status, "body": {"detail": error.reason}}
    cases.append({"name": name, "vault_response": {"status": status, "body": upstream},
        "vault_request": observed[0] if observed else None, "browser_response": outcome})

TOKEN = "fixture-user-token"
case("list",200,raw_listing,lambda:vault.listing(TOKEN))
case("add",200,raw_entry,lambda:vault.add(TOKEN,"openai_api",{"name":"Credits","secret":"synthetic-secret","mutation_id":"m"*16,"roles":["triage"],"profiles":raw_entry["profiles"]}))
case("edit",200,raw_entry,lambda:vault.edit(TOKEN,"e"*32,{"revision":3,"mutation_id":"n"*16,"name":"Credits","enabled":True}))
case("replace_secret",200,raw_entry,lambda:vault.replace(TOKEN,"e"*32,{"revision":3,"mutation_id":"p"*16,"secret":"synthetic-replacement"}))
case("reorder",200,raw_group,lambda:vault.reorder(TOKEN,"openai",{"revision":3,"entries":["e"*32],"mutation_id":"o"*16}))
case("remove",200,removed_entry,lambda:vault.remove(TOKEN,"e"*32,4))
case("relay_pending",200,{"name":"studio","routes":["chatgpt-subscription","claude-subscription"],"expires_at":None},lambda:vault.pending_relay(TOKEN,"BCDF-GHJK"))
case("relay_add",200,relay_entry,lambda:vault.add_relay(TOKEN,{"code":"BCDF-GHJK","routes":["chatgpt-subscription"],"name":"Studio","roles":[],"profiles":[],"mutation_id":"a"*16}))
case("relay_add_two_routes",200,{"entries":{"chatgpt-subscription":relay_entry,"claude-subscription":second_relay_entry}},lambda:vault.add_relay(TOKEN,{"code":"BCDF-GHJK","routes":["chatgpt-subscription","claude-subscription"],"name":"Studio","roles":[],"profiles":[],"mutation_id":"b"*16}))
case("relay_replace_device",200,{"entries":{"chatgpt-subscription":relay_entry}},lambda:vault.replace_device(TOKEN,{"code":"LMNP-QRST","entries":[{"id":"e"*32,"revision":4}],"mutation_id":"d"*16}))
case("relay_status",200,raw_status,lambda:vault.relay_status(TOKEN))
case("relay_status_unavailable",503,{"error":"vault down"},lambda:vault.relay_status(TOKEN))
for status,message in [(409,"stale revision"),(410,"entry removed"),(422,"verifier refused this code"),(401,"bad credential")]:
    case("error_"+str(status),status,{"error":message},lambda:vault.replace(TOKEN,"e"*32,{"revision":3,"mutation_id":"p"*16,"secret":"synthetic-replacement"}))
configured = settings(public_url="http://testserver", relay_server="relay.invalid:7443", relay_download_url="https://example.invalid/relay")
ipv6 = settings(public_url="http://testserver", relay_server="[::1]:80", relay_download_url="https://example.invalid/relay")
fixture = {"provenance": "backoffice.credentials.Vault, group, entry and task; relay add response branches mirror gateway/vault/vault.py approve_relay; inputs mirror backoffice/tests/test_portal_lifecycle.py and gateway/tests/vault_fixtures.py; generated with pinned backoffice requirements", "cases": cases,
    "relay_setup": {"server": configured.relay_server, "download": configured.relay_download_url,
        "command": configured.relay_command(), "login": configured.relay_command("login")},
    "relay_setup_ipv6": {"server": ipv6.relay_server, "download": ipv6.relay_download_url,
        "command": ipv6.relay_command(), "login": ipv6.relay_command("login")}}
print(json.dumps(fixture, indent=2, sort_keys=True))
