import logging
import hashlib
import os
import re
import shutil
from abc import ABC, abstractmethod
from typing import BinaryIO, Dict, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from open_webui.config import (
    AZURE_STORAGE_CONTAINER_NAME,
    AZURE_STORAGE_ENDPOINT,
    AZURE_STORAGE_KEY,
    GCS_BUCKET_NAME,
    GOOGLE_APPLICATION_CREDENTIALS_JSON,
    S3_ACCESS_KEY_ID,
    S3_ADDRESSING_STYLE,
    S3_BUCKET_NAME,
    S3_ENABLE_TAGGING,
    S3_ENDPOINT_URL,
    S3_KEY_PREFIX,
    S3_REGION_NAME,
    S3_SECRET_ACCESS_KEY,
    S3_USE_ACCELERATE_ENDPOINT,
    STORAGE_PROVIDER,
    UPLOAD_DIR,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.utils.json_codec import JSONCodec

from open_webui.env import USE_SLIM

if not USE_SLIM:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient
    from google.cloud import storage
    from google.cloud.exceptions import GoogleCloudError, NotFound

log = logging.getLogger(__name__)


def _prepared_target(provider: str, name: str, primary_path: str, **identity) -> dict:
    """Create a JSON-serializable target without touching local or remote storage."""
    if not name or name in ('.', '..') or os.path.basename(name) != name or not re.fullmatch(r'[A-Za-z0-9._-]{1,100}', name):
        raise ValueError('Invalid prepared storage name')
    return {
        'version': 1, 'provider': provider, 'storage_name': name,
        'primary_path': primary_path, 'local_path': os.path.join(UPLOAD_DIR, name), **identity,
    }


def _check_prepared(actual: dict, expected: dict) -> None:
    # Includes the configured endpoint/bucket identity: never redirect deletion
    # to a newly configured provider with a coincidentally identical name.
    if not isinstance(actual, dict) or actual != expected:
        raise ValueError('Prepared upload target does not match current storage configuration')


def _endpoint_identity(endpoint: str | None) -> str:
    return hashlib.sha256((endpoint or '').encode()).hexdigest()


def _remove_prepared_cache(target: dict) -> None:
    try:
        os.remove(target['local_path'])
    except FileNotFoundError:
        pass


class StorageProvider(ABC):
    def prepare_upload(self, storage_name: str) -> dict:
        raise NotImplementedError('Prepared uploads are not supported by this provider')

    def upload_prepared(self, file: BinaryIO, target: dict, tags: Dict[str, str]) -> Tuple[bytes, str]:
        raise NotImplementedError('Prepared uploads are not supported by this provider')

    def delete_prepared(self, target: dict) -> None:
        raise NotImplementedError('Prepared uploads are not supported by this provider')

    @abstractmethod
    def get_file(self, file_path: str) -> str:
        pass

    @abstractmethod
    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        pass

    @abstractmethod
    def delete_all_files(self) -> None:
        pass

    @abstractmethod
    def delete_file(self, file_path: str) -> None:
        pass


class LocalStorageProvider(StorageProvider):
    def prepare_upload(self, storage_name: str) -> dict:
        return _prepared_target('local', storage_name, os.path.join(UPLOAD_DIR, storage_name))

    def upload_prepared(self, file: BinaryIO, target: dict, tags: Dict[str, str]) -> Tuple[bytes, str]:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        contents, path = self.upload_file(file, target['storage_name'], tags)
        if path != target['primary_path']:
            raise RuntimeError('Prepared upload returned a different target')
        return contents, path

    def delete_prepared(self, target: dict) -> None:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        _remove_prepared_cache(target)

    @staticmethod
    def upload_file(file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        contents = file.read()
        if not contents:
            raise ValueError(ERROR_MESSAGES.EMPTY_CONTENT)
        file_path = os.path.join(UPLOAD_DIR, filename)
        with open(file_path, 'wb') as f:
            f.write(contents)
        return contents, file_path

    @staticmethod
    def get_file(file_path: str) -> str:
        """Handles downloading of the file from local storage."""
        return file_path

    @staticmethod
    def delete_file(file_path: str) -> None:
        """Handles deletion of the file from local storage."""
        filename = os.path.basename(file_path)
        file_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.isfile(file_path):
            os.remove(file_path)
        else:
            log.warning(f'File {file_path} not found in local storage.')

    @staticmethod
    def delete_all_files() -> None:
        """Handles deletion of all files from local storage."""
        if os.path.exists(UPLOAD_DIR):
            for filename in os.listdir(UPLOAD_DIR):
                file_path = os.path.join(UPLOAD_DIR, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)  # Remove the file or link
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)  # Remove the directory
                except Exception as e:
                    log.exception(f'Failed to delete {file_path}. Reason: {e}')
        else:
            log.warning(f'Directory {UPLOAD_DIR} not found in local storage.')


class S3StorageProvider(StorageProvider):
    def __init__(self):
        config = Config(
            s3={
                'use_accelerate_endpoint': S3_USE_ACCELERATE_ENDPOINT,
                'addressing_style': S3_ADDRESSING_STYLE,
            },
            # KIT change - see https://github.com/boto/boto3/issues/4400#issuecomment-2600742103∆
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        )

        # If access key and secret are provided, use them for authentication
        if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
            self.s3_client = boto3.client(
                's3',
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                config=config,
            )
        else:
            # If no explicit credentials are provided, fall back to default AWS credentials
            # This supports workload identity (IAM roles for EC2, EKS, etc.)
            self.s3_client = boto3.client(
                's3',
                region_name=S3_REGION_NAME,
                endpoint_url=S3_ENDPOINT_URL,
                config=config,
            )

        self.bucket_name = S3_BUCKET_NAME
        self.key_prefix = S3_KEY_PREFIX if S3_KEY_PREFIX else ''

    def prepare_upload(self, storage_name: str) -> dict:
        key = os.path.join(self.key_prefix, storage_name)
        return _prepared_target('s3', storage_name, f's3://{self.bucket_name}/{key}',
                                bucket=self.bucket_name, key=key, endpoint_identity=_endpoint_identity(S3_ENDPOINT_URL))

    def upload_prepared(self, file: BinaryIO, target: dict, tags: Dict[str, str]) -> Tuple[bytes, str]:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        contents, path = self.upload_file(file, target['storage_name'], tags)
        if path != target['primary_path']:
            raise RuntimeError('Prepared upload returned a different target')
        return contents, path

    def delete_prepared(self, target: dict) -> None:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        self.s3_client.delete_object(Bucket=target['bucket'], Key=target['key'])
        _remove_prepared_cache(target)

    @staticmethod
    def sanitize_tag_value(s: str) -> str:
        """Only include S3 allowed characters."""
        return re.sub(r'[^a-zA-Z0-9 äöüÄÖÜß\+\-=\._:/@]', '', s)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to S3 storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        s3_key = os.path.join(self.key_prefix, filename)
        try:
            self.s3_client.upload_file(file_path, self.bucket_name, s3_key)
            if S3_ENABLE_TAGGING and tags:
                sanitized_tags = {self.sanitize_tag_value(k): self.sanitize_tag_value(v) for k, v in tags.items()}
                tagging = {'TagSet': [{'Key': k, 'Value': v} for k, v in sanitized_tags.items()]}
                self.s3_client.put_object_tagging(
                    Bucket=self.bucket_name,
                    Key=s3_key,
                    Tagging=tagging,
                )
            return (
                contents,
                f's3://{self.bucket_name}/{s3_key}',
            )
        except ClientError as e:
            raise RuntimeError(f'Error uploading file to S3: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            local_file_path = self._get_local_file_path(s3_key)
            # download_file's temp name caps characters, not bytes, so non-ASCII names can exceed NAME_MAX
            with open(local_file_path, 'wb') as local_file:
                self.s3_client.download_fileobj(self.bucket_name, s3_key, local_file)
            return local_file_path
        except ClientError as e:
            raise RuntimeError(f'Error downloading file from S3: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from S3 storage."""
        try:
            s3_key = self._extract_s3_key(file_path)
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)
        except ClientError as e:
            raise RuntimeError(f'Error deleting file from S3: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from S3 storage."""
        try:
            response = self.s3_client.list_objects_v2(Bucket=self.bucket_name)
            if 'Contents' in response:
                for content in response['Contents']:
                    # Skip objects that were not uploaded from open-webui in the first place
                    if not content['Key'].startswith(self.key_prefix):
                        continue

                    self.s3_client.delete_object(Bucket=self.bucket_name, Key=content['Key'])
        except ClientError as e:
            raise RuntimeError(f'Error deleting all files from S3: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()

    # The s3 key is the name assigned to an object. It excludes the bucket name, but includes the internal path and the file name.
    def _extract_s3_key(self, full_file_path: str) -> str:
        return '/'.join(full_file_path.split('//')[1].split('/')[1:])

    def _get_local_file_path(self, s3_key: str) -> str:
        return os.path.join(UPLOAD_DIR, s3_key.split('/')[-1])


class GCSStorageProvider(StorageProvider):
    def __init__(self):
        self.bucket_name = GCS_BUCKET_NAME

        if GOOGLE_APPLICATION_CREDENTIALS_JSON:
            self.gcs_client = storage.Client.from_service_account_info(
                info=JSONCodec.loads(GOOGLE_APPLICATION_CREDENTIALS_JSON)
            )
        else:
            # if no credentials json is provided, credentials will be picked up from the environment
            # if running on local environment, credentials would be user credentials
            # if running on a Compute Engine instance, credentials would be from Google Metadata server
            self.gcs_client = storage.Client()
        self.bucket = self.gcs_client.bucket(GCS_BUCKET_NAME)

    def prepare_upload(self, storage_name: str) -> dict:
        return _prepared_target('gcs', storage_name, f'gs://{self.bucket_name}/{storage_name}', bucket=self.bucket_name)

    def upload_prepared(self, file: BinaryIO, target: dict, tags: Dict[str, str]) -> Tuple[bytes, str]:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        contents, path = self.upload_file(file, target['storage_name'], tags)
        if path != target['primary_path']:
            raise RuntimeError('Prepared upload returned a different target')
        return contents, path

    def delete_prepared(self, target: dict) -> None:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        try:
            self.bucket.blob(target['storage_name']).delete()
        except NotFound:
            pass
        _remove_prepared_cache(target)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to GCS storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob = self.bucket.blob(filename)
            blob.upload_from_filename(file_path)
            return contents, 'gs://' + self.bucket_name + '/' + filename
        except GoogleCloudError as e:
            raise RuntimeError(f'Error uploading file to GCS: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from GCS storage."""
        try:
            filename = file_path.removeprefix('gs://').split('/')[1]
            local_file_path = os.path.join(UPLOAD_DIR, filename)
            blob = self.bucket.get_blob(filename)
            blob.download_to_filename(local_file_path)

            return local_file_path
        except NotFound as e:
            raise RuntimeError(f'Error downloading file from GCS: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from GCS storage."""
        try:
            filename = file_path.removeprefix('gs://').split('/')[1]
            blob = self.bucket.get_blob(filename)
            blob.delete()
        except NotFound as e:
            raise RuntimeError(f'Error deleting file from GCS: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from GCS storage."""
        try:
            blobs = self.bucket.list_blobs()

            for blob in blobs:
                blob.delete()

        except NotFound as e:
            raise RuntimeError(f'Error deleting all files from GCS: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


class AzureStorageProvider(StorageProvider):
    def __init__(self):
        self.endpoint = AZURE_STORAGE_ENDPOINT
        self.container_name = AZURE_STORAGE_CONTAINER_NAME
        storage_key = AZURE_STORAGE_KEY

        if storage_key:
            # Configure using the Azure Storage Account Endpoint and Key
            self.blob_service_client = BlobServiceClient(account_url=self.endpoint, credential=storage_key)
        else:
            # Configure using the Azure Storage Account Endpoint and DefaultAzureCredential
            # If the key is not configured, then the DefaultAzureCredential will be used to support Managed Identity authentication
            self.blob_service_client = BlobServiceClient(account_url=self.endpoint, credential=DefaultAzureCredential())
        self.container_client = self.blob_service_client.get_container_client(self.container_name)

    def prepare_upload(self, storage_name: str) -> dict:
        from urllib.parse import urlsplit
        endpoint = urlsplit(self.endpoint)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('Prepared Azure endpoint must not contain credentials')
        return _prepared_target('azure', storage_name, f'{self.endpoint}/{self.container_name}/{storage_name}',
                                container=self.container_name, endpoint_identity=_endpoint_identity(self.endpoint))

    def upload_prepared(self, file: BinaryIO, target: dict, tags: Dict[str, str]) -> Tuple[bytes, str]:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        contents, path = self.upload_file(file, target['storage_name'], tags)
        if path != target['primary_path']:
            raise RuntimeError('Prepared upload returned a different target')
        return contents, path

    def delete_prepared(self, target: dict) -> None:
        _check_prepared(target, self.prepare_upload(target['storage_name']))
        try:
            self.container_client.get_blob_client(target['storage_name']).delete_blob()
        except ResourceNotFoundError:
            pass
        _remove_prepared_cache(target)

    def upload_file(self, file: BinaryIO, filename: str, tags: Dict[str, str]) -> Tuple[bytes, str]:
        """Handles uploading of the file to Azure Blob Storage."""
        contents, file_path = LocalStorageProvider.upload_file(file, filename, tags)
        try:
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.upload_blob(contents, overwrite=True)
            return contents, f'{self.endpoint}/{self.container_name}/{filename}'
        except Exception as e:
            raise RuntimeError(f'Error uploading file to Azure Blob Storage: {e}')

    def get_file(self, file_path: str) -> str:
        """Handles downloading of the file from Azure Blob Storage."""
        try:
            filename = file_path.split('/')[-1]
            local_file_path = os.path.join(UPLOAD_DIR, filename)
            blob_client = self.container_client.get_blob_client(filename)
            with open(local_file_path, 'wb') as download_file:
                download_file.write(blob_client.download_blob().readall())
            return local_file_path
        except ResourceNotFoundError as e:
            raise RuntimeError(f'Error downloading file from Azure Blob Storage: {e}')

    def delete_file(self, file_path: str) -> None:
        """Handles deletion of the file from Azure Blob Storage."""
        try:
            filename = file_path.split('/')[-1]
            blob_client = self.container_client.get_blob_client(filename)
            blob_client.delete_blob()
        except ResourceNotFoundError as e:
            raise RuntimeError(f'Error deleting file from Azure Blob Storage: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_file(file_path)

    def delete_all_files(self) -> None:
        """Handles deletion of all files from Azure Blob Storage."""
        try:
            blobs = self.container_client.list_blobs()
            for blob in blobs:
                self.container_client.delete_blob(blob.name)
        except Exception as e:
            raise RuntimeError(f'Error deleting all files from Azure Blob Storage: {e}')

        # Always delete from local storage
        LocalStorageProvider.delete_all_files()


def get_storage_provider(storage_provider: str):
    if USE_SLIM and storage_provider not in ('local', 's3'):
        raise RuntimeError(
            'Slim supports local and S3 file storage. Set STORAGE_PROVIDER=local or s3, '
            'or use the standard image for other storage providers.'
        )
    if storage_provider == 'local':
        Storage = LocalStorageProvider()
    elif storage_provider == 's3':
        Storage = S3StorageProvider()
    elif storage_provider == 'gcs':
        Storage = GCSStorageProvider()
    elif storage_provider == 'azure':
        Storage = AzureStorageProvider()
    else:
        raise RuntimeError(f'Unsupported storage provider: {storage_provider}')
    return Storage


Storage = get_storage_provider(STORAGE_PROVIDER)
