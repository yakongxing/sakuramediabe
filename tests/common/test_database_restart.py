"""Opt-in real restart test; owns its cluster and never stops an existing server.

Run with SAKURAMEDIA_TEST_PG_BIN=/usr/lib/postgresql/15/bin.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config.config import Database
from src.model.base import create_database


@pytest.mark.skipif(
    not os.environ.get("SAKURAMEDIA_TEST_PG_BIN"),
    reason="requires PostgreSQL binaries for an isolated restart test",
)
def test_backend_recovers_after_postgres_restart():
    binaries = Path(os.environ["SAKURAMEDIA_TEST_PG_BIN"])
    with TemporaryDirectory(prefix="sakura-reconnect-") as directory:
        data = str(Path(directory) / "data")

        def run(binary, *args):
            subprocess.run(
                [str(binaries / binary), *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )

        run(
            "initdb",
            "-D",
            data,
            "-A",
            "trust",
            "-U",
            "reconnect_test",
            "--encoding=UTF8",
            "--no-locale",
        )

        def start():
            # Unix socket in a private directory: no shared TCP port or server.
            run(
                "pg_ctl",
                "-D",
                data,
                "-l",
                str(Path(directory) / "server.log"),
                "-o",
                f"-h '' -k {directory}",
                "-w",
                "start",
            )

        start()
        try:
            database = create_database(
                Database(url="postgresql://reconnect_test@localhost/postgres")
            )
            # Host already has a dedicated argument in create_database().
            # Set the test socket directly, without changing production URL parsing.
            database.connect_params["host"] = directory
            with ThreadPoolExecutor(max_workers=1) as executor:
                app = create_app()

                @app.get("/__recovery_test")
                def read():
                    return executor.submit(
                        lambda: database.execute_sql("SELECT 42").fetchone()[0]
                    ).result(timeout=10)

                # No lifespan: avoid touching the application's configured database.
                client = TestClient(app)
                try:
                    assert client.get("/__recovery_test").json() == 42
                    first_pid = executor.submit(
                        lambda: database.connection().get_backend_pid()
                    ).result()
                    run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
                    for _ in range(2):
                        response = client.get("/__recovery_test")
                        assert response.status_code == 503
                        assert response.json() == {
                            "error": {
                                "code": "database_unavailable",
                                "message": "Database temporarily unavailable",
                                "details": None,
                            }
                        }
                    start()
                    response = client.get("/__recovery_test")
                    assert response.status_code == 200
                    assert response.json() == 42
                    assert (
                        executor.submit(
                            lambda: database.connection().get_backend_pid()
                        ).result()
                        != first_pid
                    )
                finally:
                    client.close()
                    executor.submit(database.close).result()
        finally:
            if (Path(data) / "postmaster.pid").exists():
                run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
