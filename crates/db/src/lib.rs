//! Shared Postgres pool, migration and transaction infrastructure.

use sqlx::{
    Acquire, AssertSqlSafe, PgConnection, PgPool, Postgres, Transaction, migrate::Migrator,
    postgres::PgPoolOptions,
};

/// A pool for one service-owned database.
#[derive(Clone, Debug)]
pub struct Database {
    pool: PgPool,
}

impl Database {
    /// Connect to a service database with a bounded pool.
    pub async fn connect(url: &str, max_connections: u32) -> Result<Self, sqlx::Error> {
        let pool = PgPoolOptions::new()
            .max_connections(max_connections)
            .connect(url)
            .await?;
        Ok(Self { pool })
    }

    /// Wrap an existing pool, including a test pool.
    pub fn from_pool(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Borrow the pool for storage reads and migrations.
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Begin one unit of work shared across storage crates.
    pub async fn begin(&self) -> Result<UnitOfWork<'static>, sqlx::Error> {
        Ok(UnitOfWork {
            transaction: self.pool.begin().await?,
        })
    }

    /// Run a service's embedded migrations at startup.
    pub async fn migrate(&self, migrations: &Migrator) -> Result<(), sqlx::migrate::MigrateError> {
        migrations.run(&self.pool).await
    }

    /// Run library migrations in a schema on a connection excluded from the pool.
    pub async fn migrate_in_schema(
        &self,
        schema: &str,
        migrations: &Migrator,
    ) -> anyhow::Result<()> {
        let mut connection = self.pool.acquire().await?.detach();
        let lock_name = format!("concurro:migrate:{schema}");
        sqlx::query("SELECT pg_advisory_lock(hashtextextended($1, 0))")
            .bind(lock_name)
            .execute(&mut connection)
            .await?;
        let quoted = format!("\"{}\"", schema.replace('"', "\"\""));
        let create = format!("CREATE SCHEMA IF NOT EXISTS {quoted}");
        sqlx::query(AssertSqlSafe(create.as_str()))
            .execute(&mut connection)
            .await?;
        let search_path = format!("SET search_path TO {quoted}, public");
        sqlx::query(AssertSqlSafe(search_path.as_str()))
            .execute(&mut connection)
            .await?;
        migrations.run(&mut connection).await?;
        Ok(())
    }
}

/// The sole commit owner for writes in one Postgres transaction.
pub struct UnitOfWork<'a> {
    transaction: Transaction<'a, Postgres>,
}

impl<'a> UnitOfWork<'a> {
    /// Begin a unit of work on a dedicated connection held by the caller.
    pub async fn begin_on(connection: &'a mut PgConnection) -> Result<Self, sqlx::Error> {
        Ok(Self {
            transaction: connection.begin().await?,
        })
    }

    /// Borrow the connection for a storage implementation's typed operation.
    pub fn connection(&mut self) -> &mut PgConnection {
        &mut self.transaction
    }

    /// Start a nested transaction that rolls back to a savepoint on drop.
    pub async fn savepoint(&mut self) -> Result<UnitOfWork<'_>, sqlx::Error> {
        Ok(UnitOfWork {
            transaction: self.transaction.begin().await?,
        })
    }

    /// Commit all participating storage operations together.
    pub async fn commit(self) -> Result<(), sqlx::Error> {
        self.transaction.commit().await
    }

    /// Undo all participating storage operations.
    pub async fn rollback(self) -> Result<(), sqlx::Error> {
        self.transaction.rollback().await
    }
}
