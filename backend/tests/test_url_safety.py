import pytest

from app.url_safety import validate_public_url


def test_blocks_loopback():
    with pytest.raises(ValueError, match="non-public address"):
        validate_public_url("http://127.0.0.1/admin")


def test_blocks_localhost_hostname():
    with pytest.raises(ValueError, match="non-public address"):
        validate_public_url("http://localhost:8000/internal")


def test_blocks_link_local_cloud_metadata():
    with pytest.raises(ValueError, match="non-public address"):
        validate_public_url("http://169.254.169.254/latest/meta-data/")


def test_blocks_private_rfc1918_ranges():
    for host in ("http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/"):
        with pytest.raises(ValueError, match="non-public address"):
            validate_public_url(host)


def test_blocks_disallowed_scheme():
    with pytest.raises(ValueError, match="scheme"):
        validate_public_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="scheme"):
        validate_public_url("ftp://example.com/file")


def test_allows_public_host():
    # api.github.com resolves to a real public IP — should not raise
    validate_public_url("https://api.github.com/zen")


def test_rejects_unresolvable_hostname():
    with pytest.raises(ValueError, match="Could not resolve"):
        validate_public_url("http://this-domain-should-not-exist-pulse-test.invalid/")
