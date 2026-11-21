//! Authentication regression for services using the shared broker connector.

use std::{fs, time::Duration};

use concurro_events_nats::connect;
use concurro_testkit::NatsContainer;

/// Production brokers reject anonymous clients and accept file-backed credentials.
#[tokio::test]
async fn connects_with_private_credentials() {
    let broker = NatsContainer::start_authenticated("reader", "test-password")
        .await
        .expect("broker");
    assert!(connect(broker.url(), None).await.is_err());
    let path = std::env::temp_dir().join(format!("concurro-nats-auth-{}.json", std::process::id()));
    fs::write(&path, r#"{"user":"reader","password":"test-password"}"#).expect("test credentials");
    let result = connect(broker.url(), path.to_str()).await;
    fs::remove_file(path).expect("remove test credentials");
    let client = result.expect("authenticated connection");
    tokio::time::timeout(Duration::from_secs(5), client.flush())
        .await
        .expect("flush deadline")
        .expect("broker round trip");
}
