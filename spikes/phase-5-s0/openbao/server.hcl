ui = false
api_addr = "http://127.0.0.1:8200"

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

storage "postgresql" {
  max_connect_retries = 0
}

seal "static" {
  current_key_id = "spike-1"
  current_key    = "file:///openbao/secrets/seal.key"
}

audit "file" "to-stdout" {
  options {
    file_path = "stdout"
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

initialize "kv" {
  request "mount" {
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
        path "concurro/data/personal/*"    { capabilities = ["create"] }
        path "concurro/metadata/personal/*" { capabilities = ["read", "delete"] }
        path "concurro/destroy/personal/*" { capabilities = ["update"] }
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
  request "operator" {
    operation = "update"
    path      = "sys/policies/acl/operator"
    data = {
      policy = <<-EOT
        path "concurro/data/ops/*" { capabilities = ["create", "update", "patch", "read"] }
        path "concurro/metadata/*" { capabilities = ["read", "list"] }
      EOT
    }
  }
  request "admin" {
    operation = "update"
    path      = "sys/policies/acl/admin"
    data = {
      policy = <<-EOT
        path "sys/policies/acl/*"  { capabilities = ["create", "read", "update", "delete", "list"] }
        path "sys/auth"            { capabilities = ["read"] }
        path "sys/auth/*"          { capabilities = ["create", "read", "update", "delete", "sudo"] }
        path "auth/approle/role/*" { capabilities = ["create", "read", "update", "delete", "list"] }
        path "auth/userpass/users/*" { capabilities = ["create", "read", "update", "delete", "list"] }
        path "sys/mounts/*"        { capabilities = ["read", "update"] }
        path "concurro/config"     { capabilities = ["read", "update"] }
        path "sys/rotate/recovery*" { capabilities = ["create", "read", "update", "delete", "sudo"] }
        path "sys/rekey-recovery-key/*" { capabilities = ["create", "read", "update", "delete", "sudo"] }
        path "sys/seal-status"     { capabilities = ["read"] }
        path "auth/token/revoke-accessor" { capabilities = ["update"] }
      EOT
    }
  }
}

initialize "identities" {
  request "accounts-role" {
    operation = "update"
    path      = "auth/approle/role/accounts"
    data      = { token_policies = "accounts", token_ttl = "1h", token_max_ttl = "24h" }
  }
  request "gateway-role" {
    operation = "update"
    path      = "auth/approle/role/gateway"
    data      = { token_policies = "gateway", token_ttl = "1h", token_max_ttl = "24h" }
  }
  request "admin-user" {
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
    }
  }
  request "operator-user" {
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
    }
  }
}
