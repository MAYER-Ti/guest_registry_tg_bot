"""Select storage explicitly; a cloud failure never becomes an empty database."""
from config import Config, ConfigError
from storage import Store


def open_store(config: Config):
    if config.storage_backend == "postgres":
        from postgres_store import PostgresStore
        return PostgresStore(config.database_url, expected_registry_id=config.registry_id,
                             expected_admin_id=config.admin_user_id, sslmode=config.db_sslmode,
                             sslrootcert=config.db_sslrootcert or None)
    if config.storage_backend != "sqlite":
        raise ConfigError("Неизвестный тип хранилища.")
    return Store(config.db_path, create=config.allow_new_database)
