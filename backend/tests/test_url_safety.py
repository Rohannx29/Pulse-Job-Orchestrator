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


def test_dns_rebinding_socket_peer_blocked():
    """Controlled DNS rebinding test:
    Simulates a target where DNS pre-check returns a public IP, but when
    the TCP socket connects, the remote peer IP is a loopback or metadata IP (rebinding).
    SafeSyncBackend MUST detect the disallowed peer IP, close the socket, and raise ValueError."""
    from unittest.mock import MagicMock, patch
    from app.url_safety import SafeSyncBackend

    backend = SafeSyncBackend()

    mock_sock = MagicMock()
    mock_sock.getpeername.return_value = ("169.254.169.254", 80)

    mock_stream = MagicMock()
    mock_stream._sock = mock_sock

    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]), \
         patch("httpcore.SyncBackend.connect_tcp", return_value=mock_stream):
        with pytest.raises(ValueError, match="DNS rebinding attack blocked"):
            backend.connect_tcp("rebind.attacker.com", 80)
        assert mock_stream.close.called
