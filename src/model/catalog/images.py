import peewee

from src.model.base import BaseModel
from src.model.mixins import TimestampedMixin


class Image(TimestampedMixin, BaseModel):
    origin = peewee.CharField(unique=True, max_length=2048, help_text="原图路径或外部 URL")
    small = peewee.CharField(max_length=2048, null=False, help_text="缩略图路径或外部 URL")
    medium = peewee.CharField(max_length=2048, null=False, help_text="中图路径或外部 URL")
    large = peewee.CharField(max_length=2048, null=False, help_text="大图路径或外部 URL")

    class Meta:
        table_name = "image"
