//! Local Postgres, NATS and OpenBao containers for integration tests.

use std::error::Error;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use concurro_db::{Database, UnitOfWork};
use sqlx::Executor;
use testcontainers::{
    ContainerAsync, GenericImage, ImageExt,
    core::{ExecCommand, IntoContainerPort, Mount, WaitFor},
    runners::AsyncRunner,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

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
        Self::start_with_args(vec!["-js"]).await
    }

    /// Start a broker that requires the supplied user and password.
    pub async fn start_authenticated(
        user: &str,
        password: &str,
    ) -> Result<Self, Box<dyn Error + Send + Sync>> {
        Self::start_with_args(vec!["-js", "--user", user, "--pass", password]).await
    }

    /// Start the pinned server with the requested authentication options.
    async fn start_with_args(args: Vec<&str>) -> Result<Self, Box<dyn Error + Send + Sync>> {
        let container = GenericImage::new("nats", "2.15.3")
            .with_exposed_port(4222.tcp())
            .with_wait_for(WaitFor::message_on_stderr("Server is ready"))
            .with_cmd(args)
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

/// A disposable OpenBao with its own PostgreSQL storage, both on a private network,
/// started from a server configuration file such as `deploy/openbao/openbao.hcl`.
pub struct OpenBaoContainer {
    /// OpenBao, stopped before its storage on restart.
    openbao: ContainerAsync<GenericImage>,
    /// OpenBao's PostgreSQL storage.
    postgres: ContainerAsync<GenericImage>,
    /// Holds the generated seal key; removed on drop.
    directory: PathBuf,
    /// Host-side address.
    url: String,
}

/// Distinguishes the networks and names of containers started by one test process.
static OPENBAO_INSTANCE: AtomicU64 = AtomicU64::new(0);

impl OpenBaoContainer {
    /// Start PostgreSQL, then OpenBao 2.7.3 with `config` mounted as its server
    /// configuration, a fresh static seal key, and `env` (such as self-init passwords).
    pub async fn start(
        config: &Path,
        env: &[(&str, &str)],
    ) -> Result<Self, Box<dyn Error + Send + Sync>> {
        let name = format!(
            "concurro-test-openbao-{}-{}",
            std::process::id(),
            OPENBAO_INSTANCE.fetch_add(1, Ordering::SeqCst)
        );
        let directory = std::env::temp_dir().join(&name);
        std::fs::create_dir_all(&directory)?;
        let mut key = [0_u8; 32];
        std::fs::File::open("/dev/urandom")?.read_exact(&mut key)?;
        std::fs::write(directory.join("seal.key"), key)?;
        let postgres = GenericImage::new("postgres", "18.7")
            .with_wait_for(WaitFor::message_on_stdout(
                "PostgreSQL init process complete; ready for start up.",
            ))
            .with_network(&name)
            .with_container_name(format!("{name}-postgres"))
            .with_env_var("POSTGRES_PASSWORD", "test")
            .with_env_var("POSTGRES_DB", "openbao")
            .start()
            .await?;
        let mut request = GenericImage::new("openbao/openbao", "2.7.3")
            .with_exposed_port(8200.tcp())
            .with_wait_for(WaitFor::message_on_either_std("OpenBao server started"))
            .with_network(&name)
            .with_container_name(&name)
            .with_env_var(
                "BAO_PG_CONNECTION_URL",
                format!("postgres://postgres:test@{name}-postgres:5432/openbao?sslmode=disable"),
            )
            .with_mount(Mount::bind_mount(
                std::fs::canonicalize(config)?.to_string_lossy(),
                "/openbao/config/openbao.hcl",
            ))
            .with_mount(Mount::bind_mount(
                directory.join("seal.key").to_string_lossy(),
                "/openbao/secrets/seal.key",
            ))
            .with_cmd(["server", "-config=/openbao/config/openbao.hcl"]);
        for (variable, value) in env {
            request = request.with_env_var(*variable, *value);
        }
        let openbao = request.start().await?;
        let url = openbao_url(&openbao).await?;
        wait_unsealed(&url).await?;
        Ok(Self {
            openbao,
            postgres,
            directory,
            url,
        })
    }

    /// OpenBao's address for a client in this test process.
    pub fn url(&self) -> &str {
        &self.url
    }

    /// Stop OpenBao and then PostgreSQL, start PostgreSQL and then OpenBao, and wait until
    /// OpenBao reports itself unsealed. The address may change.
    pub async fn restart(&mut self) -> Result<(), Box<dyn Error + Send + Sync>> {
        self.openbao.stop().await?;
        self.postgres.stop().await?;
        self.postgres.start().await?;
        self.openbao.start().await?;
        self.url = openbao_url(&self.openbao).await?;
        wait_unsealed(&self.url).await
    }

    /// Run a command in the OpenBao container and return its standard output.
    pub async fn exec(&self, command: &[&str]) -> Result<Vec<u8>, Box<dyn Error + Send + Sync>> {
        let mut result = self
            .openbao
            .exec(ExecCommand::new(command.iter().copied()))
            .await?;
        Ok(result.stdout_to_vec().await?)
    }
}

impl Drop for OpenBaoContainer {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

/// The host address of a running OpenBao container.
async fn openbao_url(
    container: &ContainerAsync<GenericImage>,
) -> Result<String, Box<dyn Error + Send + Sync>> {
    let port = tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if let Ok(port) = container.get_host_port_ipv4(8200).await {
                break port;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await?;
    Ok(format!("http://127.0.0.1:{port}"))
}

/// Wait until `sys/health` answers 200: initialized, unsealed and active.
async fn wait_unsealed(url: &str) -> Result<(), Box<dyn Error + Send + Sync>> {
    let address = url.trim_start_matches("http://").to_owned();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
    loop {
        if let Ok(mut stream) = tokio::net::TcpStream::connect(&address).await {
            let request = format!("GET /v1/sys/health HTTP/1.0\r\nHost: {address}\r\n\r\n");
            if stream.write_all(request.as_bytes()).await.is_ok() {
                let mut status = [0_u8; 12];
                if stream.read_exact(&mut status).await.is_ok() && status.ends_with(b"200") {
                    return Ok(());
                }
            }
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("OpenBao did not report itself unsealed".into());
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
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
