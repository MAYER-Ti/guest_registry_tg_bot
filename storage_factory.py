"""Select storage explicitly; a cloud failure never becomes an empty database."""
from config import Config, ConfigError
from sheets_store import SheetsStore
from storage import Store


def open_store(config: Config):
    if config.storage_backend == "google_sheets":
        return SheetsStore(config.sheets_endpoint, config.sheets_secret,
                           expected_owner_email=config.google_owner_email,
                           expected_admin_id=config.admin_user_id)
    if config.storage_backend != "sqlite":
        raise ConfigError("Неизвестный тип хранилища.")
    return Store(config.db_path, create=config.allow_new_database)
