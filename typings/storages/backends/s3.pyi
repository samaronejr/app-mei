# Verified against django-storages 1.14.6 and its real-adapter archive tests.

from boto3.s3.transfer import TransferConfig
from django.core.files.storage import Storage
from mypy_boto3_s3.service_resource import Bucket

class S3Storage(Storage):
    location: str
    transfer_config: TransferConfig
    max_memory_size: int
    _bucket: Bucket | None
    def __init__(self, **settings: object) -> None: ...
    @property
    def bucket(self) -> Bucket: ...
