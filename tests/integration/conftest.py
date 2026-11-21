"""Real SSH servers in Docker. Run with: uv run pytest -m integration"""

import os
import shutil
import socket
import subprocess
import time
import uuid

import paramiko
import pytest

PASSWORD = "s3cret-pw"
# the docker CLI's own environment (HOME: its context), before tests isolate HOME
DOCKER_ENV = dict(os.environ)
PASSPHRASE = "key-pass"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ssh(port: int, timeout: float = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.settimeout(3)
                if s.recv(64).startswith(b"SSH-"):
                    time.sleep(1)
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(f"ssh server on {port} did not start")


def docker_run(args: list[str]) -> str:
    name = f"shipr-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, *args], check=True,
                   capture_output=True, env=DOCKER_ENV)
    return name


@pytest.fixture(scope="session")
def keypair(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(d / "id_rsa"), password=PASSPHRASE)
    return d / "id_rsa", f"{key.get_name()} {key.get_base64()}"


@pytest.fixture(scope="session")
def ssh_server(keypair):
    """Full SSH (shell + SFTP). Writable home: /config. /xfs is a separate filesystem (tmpfs),
    for state on another filesystem than the site (P-1)."""
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    port = free_port()
    name = docker_run(["-p", f"127.0.0.1:{port}:2222", "-e", "PUID=1000", "-e", "PGID=1000",
                       "--tmpfs", "/xfs:uid=1000,gid=1000,mode=0755",
                       "-e", "USER_NAME=deploy", "-e", f"USER_PASSWORD={PASSWORD}",
                       "-e", "PASSWORD_ACCESS=true", "-e", f"PUBLIC_KEY={keypair[1]}",
                       "lscr.io/linuxserver/openssh-server:latest"])
    try:
        wait_ssh(port)
        yield {"port": port, "user": "deploy", "root": "/config", "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=DOCKER_ENV)


@pytest.fixture(scope="session")
def sftp_only_server():
    """SFTP-only, chrooted (like shared hosting). Writable dir: /upload. /xfs is a separate
    filesystem (tmpfs)."""
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    port = free_port()
    name = docker_run(["-p", f"127.0.0.1:{port}:22",
                       "--tmpfs", "/home/deploy/xfs:uid=1001,gid=100,mode=0755",
                       "atmoz/sftp:alpine",
                       f"deploy:{PASSWORD}:1001::upload"])
    try:
        wait_ssh(port)
        yield {"port": port, "user": "deploy", "root": "/upload", "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, env=DOCKER_ENV)
