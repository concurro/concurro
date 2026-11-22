//! S0 spike: the `concurro-secrets` operations S2 specifies, through `vaultrs` 0.8,
//! against the throwaway OpenBao in `../compose.yaml`.
//!
//! Usage: s0-openbao-client <addr> <accounts role_id> <accounts secret_id>
//!        <gateway role_id> <gateway secret_id>

use std::collections::HashMap;

use anyhow::{Context, Result, bail};
use vaultrs::{
    api::kv2::requests::SetSecretRequestOptions,
    auth::approle,
    client::{Client, VaultClient, VaultClientSettingsBuilder},
    kv2,
};

async fn login(addr: &str, role_id: &str, secret_id: &str) -> Result<VaultClient> {
    let mut client = VaultClient::new(VaultClientSettingsBuilder::default().address(addr).build()?)?;
    let auth = approle::login(&client, "approle", role_id, secret_id).await?;
    client.set_token(&auth.client_token);
    Ok(client)
}

fn check(name: &str, passed: bool) -> Result<()> {
    println!("{} {name}", if passed { "PASS" } else { "FAIL" });
    if passed { Ok(()) } else { bail!("{name}") }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let [addr, acc_role, acc_secret, gw_role, gw_secret] = args.as_slice() else {
        bail!("usage: <addr> <accounts role_id> <secret_id> <gateway role_id> <secret_id>");
    };
    let accounts = login(addr, acc_role, acc_secret).await.context("accounts login")?;
    let gateway = login(addr, gw_role, gw_secret).await.context("gateway login")?;
    let path = format!("personal/{}", "a".repeat(64));
    let value = HashMap::from([("secret", "sk-spike-value"), ("account", "acct-1")]);
    let create = SetSecretRequestOptions { cas: 0 };

    let first = kv2::set_with_options(&accounts, "concurro", &path, &value, create.clone()).await;
    check("accounts create with cas=0", first.is_ok())?;
    let second = kv2::set_with_options(&accounts, "concurro", &path, &value, create.clone()).await;
    check("accounts second create refused", second.is_err())?;
    let read: Result<HashMap<String, String>, _> = kv2::read(&accounts, "concurro", &path).await;
    check("accounts data read refused", read.is_err())?;
    let meta = kv2::read_metadata(&accounts, "concurro", &path).await;
    check("accounts metadata read", meta.as_ref().is_ok_and(|m| m.current_version == 1))?;
    let seen: HashMap<String, String> = kv2::read(&gateway, "concurro", &path).await?;
    check("gateway reads the exact version", seen.get("secret").map(String::as_str) == Some("sk-spike-value"))?;
    let gw_write = kv2::set_with_options(&gateway, "concurro", &format!("{path}x"), &value, create).await;
    check("gateway write refused", gw_write.is_err())?;
    check("accounts destroy", kv2::destroy_versions(&accounts, "concurro", &path, vec![1]).await.is_ok())?;
    let gone: Result<HashMap<String, String>, _> = kv2::read(&gateway, "concurro", &path).await;
    check("gateway read of a destroyed version fails", gone.is_err())?;
    check("accounts metadata delete", kv2::delete_metadata(&accounts, "concurro", &path).await.is_ok())?;
    let absent = kv2::read_metadata(&accounts, "concurro", &path).await;
    check("path absent after metadata delete", absent.is_err())?;
    // Errors must not echo the value.
    let message = format!("{:?}", second.err().map(|e| e.to_string()));
    check("refusal error carries no secret", !message.contains("sk-spike-value"))?;
    Ok(())
}
