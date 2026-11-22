# OpenBao for Concurro: PostgreSQL storage, the static seal and declarative self-init.
# Paths are inside the container. See docs/platform.md#openbao.
#
# Self-init runs only at the first boot of an empty database. Later changes to the
# policies, roles or users are made by an `admin` identity with the `bao` CLI and
# recorded with the deployment records; editing the `initialize` blocks applies nothing.

ui       = false
api_addr = "http://concurro-openbao:8200"

# The container joins only its private network, so this listener is reachable only by
# OpenBao's clients. Recovery adds `disable_unauthed_generate_root_endpoints = false`
# here temporarily (docs/platform.md#openbao, "Recovery").
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

# BAO_PG_CONNECTION_URL names OpenBao's own database and role.
storage "postgresql" {
  ha_enabled          = "false"
  max_connect_retries = 0
}

seal "static" {
  current_key_id = "seal-1"
  current_key    = "file:///openbao/secrets/seal.key"
}

audit "file" "audit" {
  options {
    file_path = "/openbao/logs/audit.log"
  }
}

initialize "auth" {
  request "userpass" {
    operation = "update"
    path      = "sys/auth/userpass"
    data      = { type = "userpass" }
  }
  request "approle" {
    operation = "update"
    path      = "sys/auth/approle"
    data      = { type = "approle" }
  }
}

# One KV v2 mount. Create-only for personal versions comes from the accounts policy, not
# from a mount-wide cas_required, which would make routine rotation name a version.
initialize "kv" {
  request "concurro" {
    operation = "update"
    path      = "sys/mounts/concurro"
    data      = { type = "kv", options = { version = "2" } }
  }
}

initialize "policies" {
  request "accounts" {
    operation = "update"
    path      = "sys/policies/acl/accounts"
    data = {
      policy = <<-EOT
        path "concurro/data/personal/*"     { capabilities = ["create"] }
        path "concurro/metadata/personal/*" { capabilities = ["read", "delete"] }
        path "concurro/destroy/personal/*"  { capabilities = ["update"] }
        path "concurro/data/ops/accounts/*" { capabilities = ["read"] }
      EOT
    }
  }
  request "gateway" {
    operation = "update"
    path      = "sys/policies/acl/gateway"
    data = {
      policy = <<-EOT
        path "concurro/data/personal/*"    { capabilities = ["read"] }
        path "concurro/data/ops/gateway/*" { capabilities = ["read"] }
      EOT
    }
  }
  request "exchange" {
    operation = "update"
    path      = "sys/policies/acl/exchange"
    data = {
      policy = <<-EOT
        path "concurro/data/ops/exchange/*" { capabilities = ["read"] }
      EOT
    }
  }
  # For the phase 5 import only. Its AppRole is created for the import, then deleted and
  # its tokens revoked (docs/platform.md#openbao, "The phase 5 import").
  request "migrator" {
    operation = "update"
    path      = "sys/policies/acl/migrator"
    data = {
      policy = <<-EOT
        path "concurro/data/personal/*"     { capabilities = ["create", "read"] }
        path "concurro/metadata/personal/*" { capabilities = ["read"] }
      EOT
    }
  }
  request "operator" {
    operation = "update"
    path      = "sys/policies/acl/operator"
    data = {
      policy = <<-EOT
        path "concurro/data/ops/*"     { capabilities = ["create", "update", "patch", "read"] }
        path "concurro/metadata/ops/*" { capabilities = ["read", "list"] }
      EOT
    }
  }
  request "admin" {
    operation = "update"
    path      = "sys/policies/acl/admin"
    data = {
      policy = <<-EOT
        path "sys/policies/acl"            { capabilities = ["list"] }
        path "sys/policies/acl/*"          { capabilities = ["create", "read", "update", "delete", "list"] }
        path "sys/auth"                    { capabilities = ["read"] }
        path "sys/auth/*"                  { capabilities = ["create", "read", "update", "delete", "sudo"] }
        path "auth/approle/role"           { capabilities = ["list"] }
        path "auth/approle/role/*"         { capabilities = ["create", "read", "update", "delete", "list"] }
        path "auth/userpass/users"         { capabilities = ["list"] }
        path "auth/userpass/users/*"       { capabilities = ["create", "read", "update", "delete", "list"] }
        path "auth/token/revoke-accessor"  { capabilities = ["update"] }
        path "sys/leases/revoke-prefix/auth/approle/login"   { capabilities = ["update", "sudo"] }
        path "sys/leases/revoke-prefix/auth/approle/login/*" { capabilities = ["update", "sudo"] }
        path "sys/mounts"                  { capabilities = ["read"] }
        path "sys/mounts/*"                { capabilities = ["read", "update"] }
        path "concurro/config"             { capabilities = ["read", "update"] }
        path "sys/audit"                   { capabilities = ["read", "sudo"] }
        path "sys/rotate/recovery*"        { capabilities = ["create", "read", "update", "delete", "sudo"] }
        path "sys/seal-status"             { capabilities = ["read"] }
      EOT
    }
  }
}

# Service AppRoles. Each service's secret ID is issued by an admin after first boot and
# kept in a file only that service reads.
initialize "approles" {
  request "accounts" {
    operation = "update"
    path      = "auth/approle/role/accounts"
    data      = { token_policies = "accounts", token_ttl = "1h", token_max_ttl = "24h" }
  }
  request "gateway" {
    operation = "update"
    path      = "auth/approle/role/gateway"
    data      = { token_policies = "gateway", token_ttl = "1h", token_max_ttl = "24h" }
  }
  request "exchange" {
    operation = "update"
    path      = "auth/approle/role/exchange"
    data      = { token_policies = "exchange", token_ttl = "1h", token_max_ttl = "24h" }
  }
}

# People. Initial passwords come from the environment at first boot only.
initialize "users" {
  request "admin" {
    operation = "update"
    path      = "auth/userpass/users/admin"
    data = {
      password = {
        eval_type       = "string"
        eval_source     = "env"
        env_var         = "INITIAL_ADMIN_PASSWORD"
        require_present = true
      }
      token_policies = "admin"
      token_ttl      = "1h"
    }
  }
  request "admin-recovery" {
    operation = "update"
    path      = "auth/userpass/users/admin-recovery"
    data = {
      password = {
        eval_type       = "string"
        eval_source     = "env"
        env_var         = "INITIAL_ADMIN_RECOVERY_PASSWORD"
        require_present = true
      }
      token_policies = "admin"
      token_ttl      = "1h"
    }
  }
  request "operator" {
    operation = "update"
    path      = "auth/userpass/users/operator"
    data = {
      password = {
        eval_type       = "string"
        eval_source     = "env"
        env_var         = "INITIAL_OPERATOR_PASSWORD"
        require_present = true
      }
      token_policies = "operator"
      token_ttl      = "1h"
    }
  }
}
