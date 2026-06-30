"""Tests RemoteLinuxBackend — discovery Linux tách rời môi trường (EPIC 2).

Cùng một backend chạy trên EC2/bare-metal/OrbStack; chỉ transport khác. Test dùng
FakeTransport (canned ss/systemctl/nginx output) để kiểm parsing THẬT mà không phụ
thuộc môi trường — transport là seam tới thế giới thật (giống resolver injection).
"""
from __future__ import annotations

from aoip.remote_linux_backend import RemoteLinuxBackend


class FakeTransport:
    target = "ec2-test"

    def __init__(self, responses: dict[str, tuple[str, int]]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def run(self, argv, *, timeout: float = 15.0):
        joined = " ".join(argv)
        self.calls.append(joined)
        for key, resp in self._responses.items():
            if key in joined:
                return resp
        return "", 1


_SS = (
    "LISTEN 0 4096 0.0.0.0:3306 0.0.0.0:* users:((\"mariadbd\",pid=1,fd=20))\n"
    "LISTEN 0 511 0.0.0.0:80 0.0.0.0:* users:((\"nginx\",pid=2,fd=6))\n"
    "LISTEN 0 128 0.0.0.0:2049 0.0.0.0:*\n"  # NFS, không có process → port_owner
)
_ENV = "payment-api DB_HOST=cust-db\npayment-api REDIS_HOST=cust-db\n"
_NGINX = "proxy_pass http://10.0.0.5:8080\n"


def _backend() -> tuple[RemoteLinuxBackend, FakeTransport]:
    t = FakeTransport({
        "ss -Htlnp": (_SS, 0),
        "Environment=": (_ENV, 0),
        "proxy_pass": (_NGINX, 0),
        "/dev/tcp/127.0.0.1/3306": ("OPEN", 0),
    })
    return RemoteLinuxBackend(t), t


async def test_discover_parses_services_and_unowned_ports():
    backend, _ = _backend()
    inv = await backend.discover("ec2-test")
    names = {s["name"] for s in inv["services"]}
    assert names == {"mariadbd", "nginx"}
    assert "port_owner:2049" in inv["unknowns"]


async def test_discover_extracts_real_topology_relationships():
    backend, _ = _backend()
    inv = await backend.discover("ec2-test")
    rels = inv["relationships"]
    assert any(r["relation"] == "depends_on" and r["target"] == "cust-db" for r in rels)
    assert any(r["relation"] == "proxies_to" and "10.0.0.5:8080" in r["target"] for r in rels)


async def test_probe_port_uses_transport_dev_tcp():
    backend, t = _backend()
    assert await backend.probe_port("ec2-test", 3306) is True
    assert await backend.probe_port("ec2-test", 9999) is False
    assert any("/dev/tcp/127.0.0.1/3306" in c for c in t.calls)


async def test_backend_is_environment_agnostic():
    # Backend KHÔNG nhúng môi trường: không gọi lệnh 'orb -m', không import/khởi tạo
    # transport cụ thể — chỉ dùng transport.run được tiêm vào.
    import aoip.remote_linux_backend as mod
    src = open(mod.__file__).read()
    assert "orb -m" not in src
    assert "OrbTransport" not in src
    assert "SSHTransport" not in src
    assert "subprocess" not in src  # mọi I/O qua transport, không tự exec
