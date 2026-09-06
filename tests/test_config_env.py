from src.config.config import Settings


class temporary_config_path:
    def __init__(self, monkeypatch, config_path):
        self.monkeypatch = monkeypatch
        self.config_path = config_path
        self.original_config_path = Settings.model_config["toml_file"]

    def __enter__(self):
        self.monkeypatch.setitem(Settings.model_config, "toml_file", self.config_path)

    def __exit__(self, *args):
        Settings.model_config["toml_file"] = self.original_config_path


def test_database_url_env_without_prefix_overrides_toml(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[database]\nurl = "postgresql://from-file:pass@postgres:5432/filedb"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE__URL", "postgresql://from-env:pass@postgres:5432/envdb")
    monkeypatch.delenv("SAKURAMEDIA_DATABASE__URL", raising=False)

    with temporary_config_path(monkeypatch, config_path):
        settings = Settings()

    assert settings.database.url == "postgresql://from-env:pass@postgres:5432/envdb"


def test_storage_webdav_env_without_prefix_overrides_toml(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[storage]",
                'backend = "local"',
                'webdav_base_url = "https://dav.example/from-file"',
                'username = "file-user"',
                'password = "file-pass"',
                'root_prefix = "file-prefix"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("STORAGE__BACKEND", "webdav")
    monkeypatch.setenv("STORAGE__WEBDAV_BASE_URL", "https://dav.example/from-env")
    monkeypatch.setenv("STORAGE__USERNAME", "env-user")
    monkeypatch.setenv("STORAGE__PASSWORD", "env-pass")
    monkeypatch.setenv("STORAGE__ROOT_PREFIX", "env-prefix")
    for name in (
        "SAKURAMEDIA_STORAGE__BACKEND",
        "SAKURAMEDIA_STORAGE__WEBDAV_BASE_URL",
        "SAKURAMEDIA_STORAGE__USERNAME",
        "SAKURAMEDIA_STORAGE__PASSWORD",
        "SAKURAMEDIA_STORAGE__ROOT_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)

    with temporary_config_path(monkeypatch, config_path):
        settings = Settings()

    assert settings.storage.backend == "webdav"
    assert settings.storage.webdav_base_url == "https://dav.example/from-env"
    assert settings.storage.username == "env-user"
    assert settings.storage.password == "env-pass"
    assert settings.storage.root_prefix == "env-prefix"


def test_image_pipeline_concurrency_and_staging_support_environment_overrides(monkeypatch, tmp_path):
    from src.config.config import Settings

    monkeypatch.setenv("METADATA__IMAGE_DOWNLOAD_MAX_WORKERS", "5")
    monkeypatch.setenv("STORAGE__WEBDAV_PUBLICATION_MAX_WORKERS", "4")
    monkeypatch.setenv("STORAGE__IMAGE_PUBLICATION_STAGING_ROOT", "/data/custom-images")

    with temporary_config_path(monkeypatch, tmp_path / "missing.toml"):
        configured = Settings()

    assert configured.metadata.image_download_max_workers == 5
    assert configured.storage.webdav_publication_max_workers == 4
    assert configured.storage.image_publication_staging_root == "/data/custom-images"
