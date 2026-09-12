from .keys import normalize_storage_key
from .types import ObjectStat, StorageNotFound


def asset_storage():
    from .factory import asset_storage as factory
    return factory()


def clip_storage():
    from .factory import clip_storage as factory
    return factory()


def subtitle_storage():
    from .factory import subtitle_storage as factory
    return factory()


def reset_storage_backends():
    from .factory import reset_storage_backends as reset
    return reset()

__all__ = ["ObjectStat", "StorageNotFound", "asset_storage", "clip_storage", "normalize_storage_key", "reset_storage_backends", "subtitle_storage"]
