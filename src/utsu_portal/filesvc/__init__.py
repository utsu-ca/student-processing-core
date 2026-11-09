"""Cloud-agnostic file storage abstraction.

Supports multiple storage backends with a unified interface:
- Local filesystem (development)
- FTP (legacy systems)
- AWS S3 / MinIO
- Google Cloud Storage
- Azure Blob Storage

All backends are configured via environment variables and are vendor-agnostic.

Usage:
    from utsu_portal.filesvc import FileHandler
    
    handler = FileHandler.from_env()
    
    # Upload
    with open("report.pdf", "rb") as f:
        file_key = handler.upload(f, "reports/semester-2024-fall")
    
    # Download
    with handler.download(file_key) as f:
        content = f.read()
    
    # Delete
    handler.delete(file_key)
"""

import os
import io
import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import BinaryIO, Optional, Iterator
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class FileStorageBackend(ABC):
    """Abstract base class for file storage backends."""

    @abstractmethod
    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """
        Upload a file.
        
        Args:
            file_obj: File-like object to upload
            path: Storage path (e.g., "reports/2024/semester-fall")
        
        Returns:
            File key/URL for later retrieval
        """
        pass

    @abstractmethod
    def download(self, file_key: str) -> Iterator[bytes]:
        """
        Download a file by key.
        
        Args:
            file_key: File key returned from upload()
        
        Yields:
            Chunks of file data
        """
        pass

    @abstractmethod
    def delete(self, file_key: str) -> None:
        """Delete a file by key."""
        pass

    @abstractmethod
    def exists(self, file_key: str) -> bool:
        """Check if a file exists."""
        pass

    @abstractmethod
    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """
        Get a signed/authenticated URL for direct access.
        
        Args:
            file_key: File key
            expires_in: URL expiration time in seconds
        
        Returns:
            Signed URL
        """
        pass


class LocalFileStorage(FileStorageBackend):
    """File storage on local filesystem (development/testing)."""

    def __init__(self, base_path: str = "/tmp/utsu-files"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalFileStorage initialized at {self.base_path}")

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload file to local filesystem."""
        full_path = self.base_path / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(full_path, "wb") as f:
            f.write(file_obj.read())
        
        logger.info(f"File uploaded to {full_path}")
        return str(full_path.relative_to(self.base_path))

    def download(self, file_key: str) -> Iterator[bytes]:
        """Download file from local filesystem."""
        full_path = self.base_path / file_key
        
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {file_key}")
        
        with open(full_path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk

    def delete(self, file_key: str) -> None:
        """Delete file from local filesystem."""
        full_path = self.base_path / file_key
        if full_path.exists():
            full_path.unlink()
            logger.info(f"File deleted: {file_key}")

    def exists(self, file_key: str) -> bool:
        """Check if file exists."""
        return (self.base_path / file_key).exists()

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """For local storage, just return the relative path."""
        return f"/files/{file_key}"


class S3FileStorage(FileStorageBackend):
    """AWS S3 or MinIO compatible storage."""

    def __init__(
        self,
        bucket_name: str,
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        import boto3
        
        self.bucket_name = bucket_name
        
        session_kwargs = {}
        if access_key and secret_key:
            session_kwargs = {
                "aws_access_key_id": access_key,
                "aws_secret_access_key": secret_key,
            }
        
        self.s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            **session_kwargs,
        )
        logger.info(f"S3FileStorage initialized for bucket: {bucket_name}")

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload file to S3."""
        self.s3.put_object(
            Bucket=self.bucket_name,
            Key=path,
            Body=file_obj.read(),
        )
        logger.info(f"File uploaded to S3: {path}")
        return path

    def download(self, file_key: str) -> Iterator[bytes]:
        """Download file from S3."""
        response = self.s3.get_object(Bucket=self.bucket_name, Key=file_key)
        for chunk in response["Body"].iter_chunks(chunk_size=8192):
            yield chunk

    def delete(self, file_key: str) -> None:
        """Delete file from S3."""
        self.s3.delete_object(Bucket=self.bucket_name, Key=file_key)
        logger.info(f"File deleted from S3: {file_key}")

    def exists(self, file_key: str) -> bool:
        """Check if file exists in S3."""
        try:
            self.s3.head_object(Bucket=self.bucket_name, Key=file_key)
            return True
        except Exception:
            return False

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """Get signed S3 URL."""
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": file_key},
            ExpiresIn=expires_in,
        )


class GCSFileStorage(FileStorageBackend):
    """Google Cloud Storage backend."""

    def __init__(self, bucket_name: str, project_id: Optional[str] = None):
        from google.cloud import storage
        
        self.bucket_name = bucket_name
        self.client = storage.Client(project=project_id)
        self.bucket = self.client.bucket(bucket_name)
        logger.info(f"GCSFileStorage initialized for bucket: {bucket_name}")

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload file to GCS."""
        blob = self.bucket.blob(path)
        blob.upload_from_file(file_obj)
        logger.info(f"File uploaded to GCS: {path}")
        return path

    def download(self, file_key: str) -> Iterator[bytes]:
        """Download file from GCS."""
        blob = self.bucket.blob(file_key)
        for chunk in blob.download_as_bytes(chunk_size=8192):
            yield chunk

    def delete(self, file_key: str) -> None:
        """Delete file from GCS."""
        self.bucket.delete_blob(file_key)
        logger.info(f"File deleted from GCS: {file_key}")

    def exists(self, file_key: str) -> bool:
        """Check if file exists in GCS."""
        blob = self.bucket.blob(file_key)
        return blob.exists()

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """Get signed GCS URL."""
        blob = self.bucket.blob(file_key)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=expires_in),
            method="GET",
        )


class AzureBlobStorage(FileStorageBackend):
    """Azure Blob Storage backend."""

    def __init__(
        self,
        container_name: str,
        account_name: Optional[str] = None,
        account_key: Optional[str] = None,
        connection_string: Optional[str] = None,
    ):
        from azure.storage.blob import BlobServiceClient
        
        self.container_name = container_name
        
        if connection_string:
            self.blob_service = BlobServiceClient.from_connection_string(connection_string)
        else:
            self.blob_service = BlobServiceClient(
                account_url=f"https://{account_name}.blob.core.windows.net",
                credential=account_key,
            )
        
        self.container_client = self.blob_service.get_container_client(container_name)
        logger.info(f"AzureBlobStorage initialized for container: {container_name}")

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload file to Azure Blob Storage."""
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name,
            blob=path,
        )
        blob_client.upload_blob(file_obj, overwrite=True)
        logger.info(f"File uploaded to Azure Blob: {path}")
        return path

    def download(self, file_key: str) -> Iterator[bytes]:
        """Download file from Azure Blob Storage."""
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name,
            blob=file_key,
        )
        for chunk in blob_client.download_blob().chunks():
            yield chunk

    def delete(self, file_key: str) -> None:
        """Delete file from Azure Blob Storage."""
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name,
            blob=file_key,
        )
        blob_client.delete_blob()
        logger.info(f"File deleted from Azure Blob: {file_key}")

    def exists(self, file_key: str) -> bool:
        """Check if file exists in Azure Blob Storage."""
        blob_client = self.blob_service.get_blob_client(
            container=self.container_name,
            blob=file_key,
        )
        return blob_client.exists()

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """Get signed Azure Blob URL."""
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions
        
        sas_token = generate_blob_sas(
            account_name=self.blob_service.account_name,
            container_name=self.container_name,
            blob_name=file_key,
            account_key=self.blob_service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(seconds=expires_in),
        )
        
        blob_url = self.blob_service.get_blob_client(
            container=self.container_name,
            blob=file_key,
        ).url
        return f"{blob_url}?{sas_token}"


class FTPFileStorage(FileStorageBackend):
    """FTP-based file storage (for legacy systems)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        base_path: str = "/",
        port: int = 21,
    ):
        from ftplib import FTP
        
        self.host = host
        self.username = username
        self.password = password
        self.base_path = base_path
        self.port = port
        self.ftp = None
        self._connect()
        logger.info(f"FTPFileStorage initialized for {host}:{port}")

    def _connect(self):
        """Connect to FTP server."""
        from ftplib import FTP
        self.ftp = FTP()
        self.ftp.connect(self.host, self.port)
        self.ftp.login(self.username, self.password)

    def _ensure_connected(self):
        """Ensure FTP connection is active."""
        try:
            self.ftp.noop()
        except Exception:
            self._connect()

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload file via FTP."""
        self._ensure_connected()
        full_path = f"{self.base_path}/{path}".lstrip("/")
        
        # Create directories if needed
        dirs = full_path.rsplit("/", 1)[0]
        for d in dirs.split("/"):
            if d:
                try:
                    self.ftp.cwd(d)
                except Exception:
                    self.ftp.mkd(d)
                    self.ftp.cwd(d)
        
        self.ftp.storbinary(f"STOR {path.split('/')[-1]}", file_obj)
        logger.info(f"File uploaded via FTP: {full_path}")
        return full_path

    def download(self, file_key: str) -> Iterator[bytes]:
        """Download file via FTP."""
        self._ensure_connected()
        
        buffer = io.BytesIO()
        self.ftp.retrbinary(f"RETR {file_key}", buffer.write)
        
        buffer.seek(0)
        while True:
            chunk = buffer.read(8192)
            if not chunk:
                break
            yield chunk

    def delete(self, file_key: str) -> None:
        """Delete file via FTP."""
        self._ensure_connected()
        self.ftp.delete(file_key)
        logger.info(f"File deleted via FTP: {file_key}")

    def exists(self, file_key: str) -> bool:
        """Check if file exists via FTP."""
        self._ensure_connected()
        try:
            self.ftp.size(file_key)
            return True
        except Exception:
            return False

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """FTP doesn't support signed URLs; return a placeholder."""
        return f"ftp://{self.host}/{file_key}"


class FileHandler:
    """Unified file handler that dispatches to appropriate backend."""

    def __init__(self, backend: FileStorageBackend):
        self.backend = backend

    @classmethod
    def from_env(cls) -> "FileHandler":
        """
        Create FileHandler from environment variables.
        
        Environment variables:
        - FILE_STORAGE: Backend type (local, s3, gcs, azure, ftp)
        - FILE_STORAGE_PATH: Local filesystem path (for 'local' backend)
        - AWS_S3_BUCKET: S3 bucket name
        - AWS_S3_REGION: S3 region
        - AWS_S3_ENDPOINT: S3 endpoint (for MinIO)
        - GCS_BUCKET: GCS bucket name
        - GCS_PROJECT: GCS project ID
        - AZURE_CONTAINER: Azure container name
        - AZURE_CONNECTION_STRING: Azure connection string
        - FTP_HOST, FTP_USERNAME, FTP_PASSWORD, FTP_PORT
        """
        storage_type = os.getenv("FILE_STORAGE", "local").lower()
        
        if storage_type == "local":
            backend = LocalFileStorage(
                os.getenv("FILE_STORAGE_PATH", "/tmp/utsu-files")
            )
        elif storage_type == "s3":
            backend = S3FileStorage(
                bucket_name=os.getenv("AWS_S3_BUCKET"),
                region=os.getenv("AWS_S3_REGION", "us-east-1"),
                endpoint_url=os.getenv("AWS_S3_ENDPOINT"),
            )
        elif storage_type == "gcs":
            backend = GCSFileStorage(
                bucket_name=os.getenv("GCS_BUCKET"),
                project_id=os.getenv("GCS_PROJECT"),
            )
        elif storage_type == "azure":
            backend = AzureBlobStorage(
                container_name=os.getenv("AZURE_CONTAINER"),
                connection_string=os.getenv("AZURE_CONNECTION_STRING"),
            )
        elif storage_type == "ftp":
            backend = FTPFileStorage(
                host=os.getenv("FTP_HOST"),
                username=os.getenv("FTP_USERNAME"),
                password=os.getenv("FTP_PASSWORD"),
                port=int(os.getenv("FTP_PORT", "21")),
            )
        else:
            raise ValueError(f"Unknown storage type: {storage_type}")
        
        logger.info(f"FileHandler initialized with {storage_type} backend")
        return cls(backend)

    @contextmanager
    def download(self, file_key: str):
        """Download a file with context manager."""
        chunks = []
        for chunk in self.backend.download(file_key):
            chunks.append(chunk)
        
        yield io.BytesIO(b"".join(chunks))

    def upload(self, file_obj: BinaryIO, path: str) -> str:
        """Upload a file."""
        return self.backend.upload(file_obj, path)

    def delete(self, file_key: str) -> None:
        """Delete a file."""
        self.backend.delete(file_key)

    def exists(self, file_key: str) -> bool:
        """Check if a file exists."""
        return self.backend.exists(file_key)

    def get_signed_url(self, file_key: str, expires_in: int = 3600) -> str:
        """Get a signed URL for direct access."""
        return self.backend.get_signed_url(file_key, expires_in)


__all__ = [
    "FileHandler",
    "FileStorageBackend",
    "LocalFileStorage",
    "S3FileStorage",
    "GCSFileStorage",
    "AzureBlobStorage",
    "FTPFileStorage",
]
