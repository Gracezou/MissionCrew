import socket

import pytest

from missioncrew.core import seed as seed_mod
from missioncrew.core.store import Store


@pytest.fixture(autouse=True)
def _isolated_mc_home(tmp_path, monkeypatch):
    # 默认数据目录是 ~/.missioncrew;测试一律指向临时目录,不碰真实主目录
    monkeypatch.setenv("MISSIONCREW_HOME", str(tmp_path / "mc_home"))


@pytest.fixture(autouse=True)
def _reject_external_sockets(monkeypatch):
    """测试进程只能连接本机或 Unix socket。"""
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo
    local_hosts = {"127.0.0.1", "::1", "localhost"}

    def check(sock, address):
        if sock.family != socket.AF_UNIX and address[0] not in local_hosts:
            pytest.fail(f"测试禁止连接非本机地址: {address!r}")

    def local_connect(sock, address):
        check(sock, address)
        return connect(sock, address)

    def local_connect_ex(sock, address):
        check(sock, address)
        return connect_ex(sock, address)

    def local_getaddrinfo(host, *args, **kwargs):
        if host not in (None, "", *local_hosts):
            pytest.fail(f"测试禁止解析非本机地址: {host!r}")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", local_getaddrinfo)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    # 工作区与数据库都落在临时目录,互不干扰
    monkeypatch.setenv("MISSIONCREW_HOME", str(tmp_path / "mc_home"))
    return Store(tmp_path / "mc_home" / "db.sqlite3")


@pytest.fixture()
def seeded(store):
    seed_mod.seed(store)
    return store
