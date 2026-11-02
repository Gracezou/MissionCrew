"""测试进程不得意外访问外网。"""
import socket

import pytest


def test_external_connections_and_dns_are_blocked():
    with socket.socket() as sock, pytest.raises(pytest.fail.Exception, match="非本机"):
        sock.connect(("192.0.2.1", 443))
    with pytest.raises(pytest.fail.Exception, match="非本机"):
        socket.getaddrinfo("example.com", 443)
