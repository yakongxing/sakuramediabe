from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY
from src.start.recovery import HOUSEKEEPING_RECOVERY_TASK_KEYS


def test_catalog_image_publication_task_is_retired():
    assert "image_publication" not in QUEUE_TASK_REGISTRY
    assert "image_publication" not in HOUSEKEEPING_RECOVERY_TASK_KEYS
