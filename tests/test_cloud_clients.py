"""Tests for cloud storage clients.

Uses unittest.mock for mocking cloud SDK operations.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from orf.cloud.s3_client import S3ResourceClient
from orf.cloud.azure_blob_client import AzureBlobResourceClient


class TestS3ResourceClient:
    """Test suite for S3ResourceClient."""

    @pytest.fixture
    def mock_boto_session(self):
        """Create mocked boto3 session and client."""
        import sys
        from unittest.mock import MagicMock

        mock_boto3 = MagicMock()

        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name

        mock_boto3.ClientError = MockClientError

        mock_botocore = MagicMock()
        mock_botocore.exceptions = MagicMock()
        mock_botocore.exceptions.ClientError = MockClientError

        mock_session = MagicMock()
        mock_client = MagicMock()
        mock_session.client.return_value = mock_client
        mock_boto3.Session.return_value = mock_session

        # Evict cached botocore.* submodules so they re-import the
        # mocked exceptions module instead of keeping a stale
        # ClientError class reference after the fixture teardown.
        saved = {mod: sys.modules.get(mod) for mod in ("boto3", "botocore", "botocore.exceptions")}
        saved_dependents = {
            name: sys.modules[name]
            for name in list(sys.modules)
            if name == "botocore.client" or name.startswith("botocore.")
        }
        for name in saved_dependents:
            sys.modules.pop(name, None)

        sys.modules["boto3"] = mock_boto3
        sys.modules["botocore"] = mock_botocore
        sys.modules["botocore.exceptions"] = mock_botocore.exceptions
        try:
            yield mock_session, mock_client
        finally:
            for mod in ("boto3", "botocore", "botocore.exceptions"):
                sys.modules.pop(mod, None)
            for name, original in saved_dependents.items():
                if original is not None:
                    sys.modules[name] = original
            for mod, original in saved.items():
                if original is not None:
                    sys.modules[mod] = original

    @pytest.fixture
    def s3_client(self, mock_boto_session):
        """Create S3ResourceClient with mocked boto3."""
        mock_session, mock_client = mock_boto_session
        client = S3ResourceClient(bucket="test-bucket", prefix="test/prefix")
        return client, mock_client

    def test_full_key_with_prefix(self, s3_client):
        """Test _full_key combines prefix and key correctly."""
        client, _ = s3_client
        assert client._full_key("file.txt") == "test/prefix/file.txt"
        assert client._full_key("/file.txt") == "test/prefix/file.txt"
        assert client._full_key("sub/path/file.txt") == "test/prefix/sub/path/file.txt"

    def test_full_key_without_prefix(self, s3_client):
        """Test _full_key returns key as-is when no prefix configured."""
        _, mock_client = s3_client
        client = S3ResourceClient(bucket="test-bucket")
        assert client._full_key("file.txt") == "file.txt"
        assert client._full_key("/file.txt") == "file.txt"

    def test_download_success(self, s3_client):
        """Test successful file download."""
        client, mock_client = s3_client
        mock_client.download_file.return_value = None

        result = client.download("test-key", Path("/tmp/dest"))

        assert result is True
        # The client forwards str(dest); the platform renders the same
        # logical path with its own separators ("\\tmp\\dest" on Windows),
        # so derive the expectation from Path instead of a literal.
        mock_client.download_file.assert_called_once_with(
            "test-bucket", "test/prefix/test-key", str(Path("/tmp/dest"))
        )

    def test_download_failure(self, s3_client):
        """Test download returns False on ClientError."""
        import sys
        from unittest.mock import MagicMock
        mock_boto3 = MagicMock()
        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name
        mock_boto3.ClientError = MockClientError
        sys.modules["botocore.exceptions"].ClientError = MockClientError

        client, mock_client = s3_client
        mock_client.download_file.side_effect = MockClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "GetObject",
        )

        result = client.download("test-key", Path("/tmp/dest"))

        assert result is False

    def test_upload_success(self, s3_client, tmp_path):
        """Test successful file upload."""
        client, mock_client = s3_client
        mock_client.upload_file.return_value = None

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        result = client.upload(test_file, "test-key")

        assert result is True
        mock_client.upload_file.assert_called_once_with(
            str(test_file), "test-bucket", "test/prefix/test-key"
        )

    def test_upload_file_not_found(self, s3_client):
        """Test upload returns False when source file doesn't exist."""
        client, mock_client = s3_client

        result = client.upload(Path("/nonexistent/file.txt"), "test-key")

        assert result is False
        mock_client.upload_file.assert_not_called()

    def test_upload_failure(self, s3_client, tmp_path):
        """Test upload returns False on ClientError."""
        import sys
        from unittest.mock import MagicMock
        mock_boto3 = MagicMock()
        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name
        mock_boto3.ClientError = MockClientError
        sys.modules["botocore.exceptions"].ClientError = MockClientError

        client, mock_client = s3_client
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")
        mock_client.upload_file.side_effect = MockClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "PutObject",
        )

        result = client.upload(test_file, "test-key")

        assert result is False

    def test_exists_true(self, s3_client):
        """Test exists returns True when object is found."""
        client, mock_client = s3_client
        mock_client.head_object.return_value = {"ContentLength": 100}

        result = client.exists("test-key")

        assert result is True
        mock_client.head_object.assert_called_once_with(
            Bucket="test-bucket", Key="test/prefix/test-key"
        )

    def test_exists_false(self, s3_client):
        """Test exists returns False when object is 404."""
        import sys
        from unittest.mock import MagicMock
        mock_boto3 = MagicMock()
        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name
        mock_boto3.ClientError = MockClientError
        sys.modules["botocore.exceptions"].ClientError = MockClientError

        client, mock_client = s3_client
        mock_client.head_object.side_effect = MockClientError(
            {"Error": {"Code": "404", "Message": "Not found"}},
            "HeadObject",
        )

        result = client.exists("test-key")

        assert result is False

    def test_exists_error(self, s3_client):
        """Test exists returns False on non-404 error."""
        import sys
        from unittest.mock import MagicMock
        mock_boto3 = MagicMock()
        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name
        mock_boto3.ClientError = MockClientError
        sys.modules["botocore.exceptions"].ClientError = MockClientError

        client, mock_client = s3_client
        mock_client.head_object.side_effect = MockClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "HeadObject",
        )

        result = client.exists("test-key")

        assert result is False

    def test_list_resources_success(self, s3_client):
        """Test list_resources returns list of keys."""
        client, mock_client = s3_client
        mock_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "test/prefix/file1.txt"},
                {"Key": "test/prefix/file2.txt"},
            ]
        }

        result = client.list_resources("subdir/")

        assert result == ["test/prefix/file1.txt", "test/prefix/file2.txt"]
        mock_client.list_objects_v2.assert_called_once_with(
            Bucket="test-bucket", Prefix="test/prefix/subdir/"
        )

    def test_list_resources_empty(self, s3_client):
        """Test list_resources returns empty list when no objects."""
        client, mock_client = s3_client
        mock_client.list_objects_v2.return_value = {}

        result = client.list_resources()

        assert result == []

    def test_list_resources_failure(self, s3_client):
        """Test list_resources returns empty list on error."""
        import sys
        from unittest.mock import MagicMock
        mock_boto3 = MagicMock()
        class MockClientError(Exception):
            def __init__(self, response, operation_name):
                self.response = response
                self.operation_name = operation_name
        mock_boto3.ClientError = MockClientError
        sys.modules["botocore.exceptions"].ClientError = MockClientError

        client, mock_client = s3_client
        mock_client.list_objects_v2.side_effect = MockClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "ListObjectsV2",
        )

        result = client.list_resources()

        assert result == []


class TestAzureBlobResourceClient:
    """Test suite for AzureBlobResourceClient."""

    @pytest.fixture
    def mock_azure_blob_service(self):
        """Create mocked Azure BlobServiceClient."""
        import sys
        from unittest.mock import MagicMock

        class MockResourceNotFoundError(Exception):
            pass

        mock_blob_module = MagicMock()
        mock_blob_module.BlobServiceClient.from_connection_string.return_value = MagicMock()
        mock_blob_module.ResourceNotFoundError = MockResourceNotFoundError
        mock_azure_module = MagicMock()
        mock_azure_module.storage.blob = mock_blob_module
        mock_azure_module.core.exceptions = mock_blob_module
        sys.modules["azure"] = mock_azure_module
        sys.modules["azure.storage"] = MagicMock()
        sys.modules["azure.storage.blob"] = mock_blob_module
        sys.modules["azure.core"] = MagicMock()
        sys.modules["azure.core.exceptions"] = mock_blob_module
        try:
            yield mock_blob_module.BlobServiceClient.from_connection_string.return_value
        finally:
            for mod in ["azure", "azure.storage", "azure.storage.blob", "azure.core", "azure.core.exceptions"]:
                if mod in sys.modules:
                    del sys.modules[mod]

    @pytest.fixture
    def azure_client(self, mock_azure_blob_service):
        """Create AzureBlobResourceClient with mocked Azure SDK."""
        mock_client = mock_azure_blob_service
        client = AzureBlobResourceClient(
            connection_string="DefaultEndpointsProtocol=https;AccountName=test;AccountKey=xxx;EndpointSuffix=core.windows.net",
            container="test-container",
            prefix="test/prefix",
        )
        return client, mock_client

    def test_full_key_with_prefix(self, azure_client):
        """Test _full_key combines prefix and key correctly."""
        client, _ = azure_client
        assert client._full_key("file.txt") == "test/prefix/file.txt"
        assert client._full_key("/file.txt") == "test/prefix/file.txt"
        assert client._full_key("sub/path/file.txt") == "test/prefix/sub/path/file.txt"

    def test_full_key_without_prefix(self, azure_client):
        """Test _full_key returns key as-is when no prefix configured."""
        _, mock_client = azure_client
        client = AzureBlobResourceClient(
            connection_string="DefaultEndpointsProtocol=https;AccountName=test;AccountKey=xxx;EndpointSuffix=core.windows.net",
            container="test-container",
        )
        assert client._full_key("file.txt") == "file.txt"
        assert client._full_key("/file.txt") == "file.txt"

    def test_download_success(self, azure_client, tmp_path):
        """Test successful blob download."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_download_stream = MagicMock()
        mock_download_stream.readall.return_value = b"test content"
        mock_blob_client.download_blob.return_value = mock_download_stream

        dest = tmp_path / "dest.txt"
        result = client.download("test-key", dest)

        assert result is True
        assert dest.read_bytes() == b"test content"

    def test_download_not_found(self, azure_client, tmp_path):
        """Test download returns False when blob not found."""
        import sys
        class MockResourceNotFoundError(Exception):
            pass
        sys.modules["azure.core.exceptions"].ResourceNotFoundError = MockResourceNotFoundError

        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.download_blob.side_effect = MockResourceNotFoundError(
            "Blob not found"
        )

        dest = tmp_path / "dest.txt"
        result = client.download("test-key", dest)

        assert result is False

    def test_download_failure(self, azure_client, tmp_path):
        """Test download returns False on generic error."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.download_blob.side_effect = Exception("Network error")

        dest = tmp_path / "dest.txt"
        result = client.download("test-key", dest)

        assert result is False

    def test_upload_success(self, azure_client, tmp_path):
        """Test successful file upload."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        result = client.upload(test_file, "test-key")

        assert result is True
        mock_blob_client.upload_blob.assert_called_once()

    def test_upload_file_not_found(self, azure_client):
        """Test upload returns False when source file doesn't exist."""
        client, mock_client = azure_client

        result = client.upload(Path("/nonexistent/file.txt"), "test-key")

        assert result is False
        mock_client.get_blob_client.assert_not_called()

    def test_upload_failure(self, azure_client, tmp_path):
        """Test upload returns False on error."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.upload_blob.side_effect = Exception("Upload failed")

        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        result = client.upload(test_file, "test-key")

        assert result is False

    def test_exists_true(self, azure_client):
        """Test exists returns True when blob is found."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.get_blob_properties.return_value = MagicMock()

        result = client.exists("test-key")

        assert result is True

    def test_exists_false(self, azure_client):
        """Test exists returns False when blob is not found."""
        import sys
        class MockResourceNotFoundError(Exception):
            pass
        sys.modules["azure.core.exceptions"].ResourceNotFoundError = MockResourceNotFoundError

        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.get_blob_properties.side_effect = MockResourceNotFoundError(
            "Blob not found"
        )

        result = client.exists("test-key")

        assert result is False

    def test_exists_error(self, azure_client):
        """Test exists returns False on non-404 error."""
        client, mock_client = azure_client
        mock_blob_client = MagicMock()
        mock_client.get_blob_client.return_value = mock_blob_client
        mock_blob_client.get_blob_properties.side_effect = Exception("Unknown error")

        result = client.exists("test-key")

        assert result is False

    def test_list_resources_success(self, azure_client):
        """Test list_resources returns list of blob paths."""
        client, mock_client = azure_client
        mock_container_client = MagicMock()
        mock_client.get_container_client.return_value = mock_container_client
        mock_blob1 = MagicMock()
        mock_blob1.name = "test/prefix/file1.txt"
        mock_blob2 = MagicMock()
        mock_blob2.name = "test/prefix/file2.txt"
        mock_container_client.list_blobs.return_value = [mock_blob1, mock_blob2]

        result = client.list_resources("subdir/")

        assert result == ["test/prefix/file1.txt", "test/prefix/file2.txt"]

    def test_list_resources_empty(self, azure_client):
        """Test list_resources returns empty list when no blobs."""
        client, mock_client = azure_client
        mock_container_client = MagicMock()
        mock_client.get_container_client.return_value = mock_container_client
        mock_container_client.list_blobs.return_value = []

        result = client.list_resources()

        assert result == []

    def test_list_resources_failure(self, azure_client):
        """Test list_resources returns empty list on error."""
        client, mock_client = azure_client
        mock_container_client = MagicMock()
        mock_client.get_container_client.return_value = mock_container_client
        mock_container_client.list_blobs.side_effect = Exception("List failed")

        result = client.list_resources()

        assert result == []