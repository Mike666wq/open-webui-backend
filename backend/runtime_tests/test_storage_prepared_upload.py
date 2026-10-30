"""Prepared targets must be durable and exact before import storage writes."""
import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from open_webui.storage import provider


class PreparedUploadTests(unittest.TestCase):
    def test_local_prepare_has_no_io_and_deletion_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'UPLOAD_DIR', directory):
            storage = provider.LocalStorageProvider()
            with patch('builtins.open', side_effect=AssertionError('prepare performed I/O')):
                target = storage.prepare_upload('safe-id.png')
            self.assertEqual(target['storage_name'], 'safe-id.png')
            self.assertEqual(target['primary_path'], os.path.join(directory, 'safe-id.png'))
            self.assertFalse(os.path.exists(target['primary_path']))
            contents, path = storage.upload_prepared(io.BytesIO(b'png'), target, {})
            self.assertEqual((contents, path), (b'png', target['primary_path']))
            storage.delete_prepared(target)
            storage.delete_prepared(target)
            self.assertFalse(os.path.exists(path))

    def test_s3_target_survives_tag_failure_and_removes_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'UPLOAD_DIR', directory), patch.object(provider, 'S3_ENABLE_TAGGING', True):
            storage = provider.S3StorageProvider.__new__(provider.S3StorageProvider)
            storage.bucket_name = 'test-bucket'
            storage.key_prefix = 'prefix'
            storage.s3_client = MagicMock()
            storage.s3_client.put_object_tagging.side_effect = ClientError({'Error': {'Code': 'Denied'}}, 'PutObjectTagging')
            target = storage.prepare_upload('safe-id.png')
            self.assertEqual(target['primary_path'], 's3://test-bucket/prefix/safe-id.png')
            storage.s3_client.upload_file.assert_not_called()
            with self.assertRaises(Exception):
                storage.upload_prepared(io.BytesIO(b'png'), target, {'id': 'x'})
            self.assertTrue(os.path.exists(target['local_path']))
            storage.delete_prepared(target)
            storage.s3_client.delete_object.assert_called_once_with(Bucket='test-bucket', Key='prefix/safe-id.png')
            self.assertFalse(os.path.exists(target['local_path']))

    def test_gcs_preparation_upload_and_missing_delete(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'UPLOAD_DIR', directory):
            storage = provider.GCSStorageProvider.__new__(provider.GCSStorageProvider)
            storage.bucket_name = 'test-bucket'
            storage.bucket = MagicMock()
            target = storage.prepare_upload('safe-id.png')
            self.assertEqual(target['primary_path'], 'gs://test-bucket/safe-id.png')
            storage.bucket.blob.assert_not_called()
            _, path = storage.upload_prepared(io.BytesIO(b'png'), target, {})
            self.assertEqual(path, target['primary_path'])
            storage.bucket.blob.return_value.delete.side_effect = provider.NotFound('missing')
            storage.delete_prepared(target)
            self.assertFalse(os.path.exists(target['local_path']))

    def test_azure_preparation_upload_and_missing_delete(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(provider, 'UPLOAD_DIR', directory):
            storage = provider.AzureStorageProvider.__new__(provider.AzureStorageProvider)
            storage.endpoint = 'https://storage.example.test'
            storage.container_name = 'container'
            storage.container_client = MagicMock()
            target = storage.prepare_upload('safe-id.png')
            self.assertEqual(target['primary_path'], 'https://storage.example.test/container/safe-id.png')
            storage.container_client.get_blob_client.assert_not_called()
            _, path = storage.upload_prepared(io.BytesIO(b'png'), target, {})
            self.assertEqual(path, target['primary_path'])
            storage.container_client.get_blob_client.return_value.delete_blob.side_effect = provider.ResourceNotFoundError('missing')
            storage.delete_prepared(target)
            self.assertFalse(os.path.exists(target['local_path']))

    def test_configuration_drift_does_not_delete_other_bucket(self):
        storage = provider.S3StorageProvider.__new__(provider.S3StorageProvider)
        storage.bucket_name = 'first'
        storage.key_prefix = 'prefix'
        storage.s3_client = MagicMock()
        target = storage.prepare_upload('safe-id.png')
        storage.bucket_name = 'second'
        with self.assertRaises(Exception):
            storage.delete_prepared(target)
        storage.s3_client.delete_object.assert_not_called()


if __name__ == '__main__':
    unittest.main()
