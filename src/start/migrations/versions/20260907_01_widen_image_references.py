"""Allow Image fields to store realistic third-party URLs."""

name = "20260907_01_widen_image_references"


def migrate(database) -> None:
    existing = {column.name for column in database.get_columns("image")}
    for column in ("origin", "small", "medium", "large"):
        if column not in existing:
            continue
        database.execute_sql(
            f'ALTER TABLE image ALTER COLUMN "{column}" TYPE VARCHAR(2048)'
        )
    database.execute_sql(
        "UPDATE background_task_run SET state = 'failed', mutex_key = NULL, "
        "lease_expires_at = NULL, finished_at = CURRENT_TIMESTAMP, "
        "error_message = 'catalog_image_publication_retired' "
        "WHERE task_key = 'image_publication' AND state IN ('pending', 'running')"
    )
