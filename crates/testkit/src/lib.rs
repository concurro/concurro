//! Local Postgres and NATS containers for integration tests.

use std::error::Error;
use std::time::Duration;

use concurro_db::{Database, UnitOfWork};
use sqlx::Executor;
use testcontainers::{
    ContainerAsync, GenericImage, ImageExt,
    core::{IntoContainerPort, WaitFor},
    runners::AsyncRunner,
};

/// A disposable Postgres server and its connection URL.
pub struct PostgresContainer {
    /// Keep the container alive until the test ends.
    _container: ContainerAsync<GenericImage>,
    /// Host-side connection URL.
    url: String,
}

/// A disposable JetStream server for transport integration tests.
pub struct NatsContainer {
    /// Keep the container alive until the test ends.
    _container: ContainerAsync<GenericImage>,
    /// Host-side NATS URL.
    url: String,
}

impl NatsContainer {
    /// Start the pinned development NATS image with JetStream enabled.
    pub async fn start() -> Result<Self, Box<dyn Error + Send + Sync>> {
        let container = GenericImage::new("nats", "2.15.3")
            .with_exposed_port(4222.tcp())
            .with_wait_for(WaitFor::message_on_stderr("Server is ready"))
            .with_cmd(["-js"])
            .start()
            .await?;
        let port = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if let Ok(port) = container.get_host_port_ipv4(4222).await {
                    break port;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await?;
        Ok(Self {
            _container: container,
            url: format!("nats://127.0.0.1:{port}"),
        })
    }

    /// URL for a client in this test process.
    pub fn url(&self) -> &str {
        &self.url
    }
}

impl PostgresContainer {
    /// Start the pinned Postgres image with one test database.
    pub async fn start() -> Result<Self, Box<dyn Error + Send + Sync>> {
        let container = GenericImage::new("postgres", "18.7")
            .with_exposed_port(5432.tcp())
            .with_wait_for(WaitFor::message_on_stdout(
                "PostgreSQL init process complete; ready for start up.",
            ))
            .with_env_var("POSTGRES_PASSWORD", "test")
            .with_env_var("POSTGRES_DB", "concurro_test")
            .start()
            .await?;
        let port = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                if let Ok(port) = container.get_host_port_ipv4(5432).await {
                    break port;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        })
        .await?;
        let url =
            format!("postgres://postgres:test@127.0.0.1:{port}/concurro_test?sslmode=disable");
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        loop {
            let error = match Database::connect(&url, 1).await {
                Ok(database) => match sqlx::query_scalar::<_, i32>("SELECT 1")
                    .fetch_one(database.pool())
                    .await
                {
                    Ok(1) => {
                        return Ok(Self {
                            _container: container,
                            url,
                        });
                    }
                    Ok(_) => unreachable!("SELECT 1 must return one"),
                    Err(error) => error,
                },
                Err(error) => error,
            };
            if tokio::time::Instant::now() >= deadline {
                return Err(error.into());
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    /// URL for a client in this test process.
    pub fn url(&self) -> &str {
        &self.url
    }
}

/// A state table used to prove cross-crate transaction atomicity.
pub struct TestState;

impl TestState {
    /// Install the test producer's table.
    pub async fn migrate(database: &Database) -> Result<(), sqlx::Error> {
        database
            .pool()
            .execute("CREATE TABLE IF NOT EXISTS test_state (id bigint PRIMARY KEY)")
            .await?;
        Ok(())
    }

    /// Write a state row through the outbox's unit of work.
    pub async fn insert(uow: &mut UnitOfWork<'_>, id: i64) -> Result<(), sqlx::Error> {
        sqlx::query("INSERT INTO test_state (id) VALUES ($1)")
            .bind(id)
            .execute(uow.connection())
            .await?;
        Ok(())
    }
}
