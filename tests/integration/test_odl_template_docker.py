"""Docker test of the odl-generator-from-text template: live writes on a Linux bind mount.

On a Linux host, ``./data`` keeps the host user's ownership inside the
container (typically UID 1000, mode 755). The image must run as a user that
can write there, or a ``--live`` work order fails with ``ConnectorLockedError``.

The data directory is simulated with a named volume owned by ``4321:4321``
(mode 755), so the test behaves the same under Docker Desktop, whose bind
mounts hide ownership. The IDs differ from the Dockerfile's default of 1000,
so the test fails if Compose stops passing them as build arguments. The probe performs the write ``agent.py --live`` makes
(``ExcelCsvConnector.create_work_order`` on ``data/workorders.xlsx``) without
an LLM.

Opt-in: building the image installs ``machina-ai`` from PyPI. Run with
``MACHINA_DOCKER_TESTS=1 pytest tests/integration/test_odl_template_docker.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "odl-generator-from-text"
HOST_UID = 4321
HOST_GID = 4321

PROBE = """
import asyncio
import yaml
from machina.connectors.docs.excel import ExcelCsvConnector
from machina.domain.work_order import Priority, WorkOrder, WorkOrderStatus, WorkOrderType

settings = yaml.safe_load(open("config.yaml"))["connectors"]["registry"]["settings"]

async def main():
    conn = ExcelCsvConnector(**settings)
    await conn.connect()
    await conn.create_work_order(
        WorkOrder(
            id="WO-DOCKER-1",
            type=WorkOrderType.CORRECTIVE,
            priority=Priority.HIGH,
            status=WorkOrderStatus.CREATED,
            asset_id="P-201",
            description="pompa P-201 perde acqua",
        )
    )
    print("WROTE", [wo.id for wo in await conn.read_work_orders()])

asyncio.run(main())
"""

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("MACHINA_DOCKER_TESTS") != "1",
        reason="set MACHINA_DOCKER_TESTS=1 to build and run the template image",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]


def _docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=900, **kwargs)


@pytest.fixture
def linux_owned_data_volume() -> Iterator[str]:
    """A copy of the template's ``data/`` owned by the host IDs, mode 755, as on a Linux host."""
    name = f"machina-odl-data-{uuid.uuid4().hex[:8]}"
    created = _docker("volume", "create", name)
    assert created.returncode == 0, created.stderr
    try:
        seeded = _docker(
            "run", "--rm",
            "-v", f"{TEMPLATE / 'data'}:/src:ro",
            "-v", f"{name}:/data",
            "busybox",
            "sh", "-c",
            f"cp -r /src/. /data/ && rm -f /data/workorders.xlsx "
            f"&& chown -R {HOST_UID}:{HOST_GID} /data && chmod 755 /data",
        )  # fmt: skip
        assert seeded.returncode == 0, seeded.stderr
        yield name
    finally:
        _docker("volume", "rm", "-f", name)


def test_live_write_succeeds_on_host_owned_data_dir(linux_owned_data_volume: str) -> None:
    project = f"machina-odl-{uuid.uuid4().hex[:8]}"
    env = {**os.environ, "MACHINA_UID": str(HOST_UID), "MACHINA_GID": str(HOST_GID)}
    env_file = TEMPLATE / ".env"
    created_env_file = not env_file.exists()
    if created_env_file:
        env_file.write_text("", encoding="utf-8")
    try:
        built = _docker("compose", "-p", project, "build", cwd=TEMPLATE, env=env)
        assert built.returncode == 0, built.stderr[-2000:]
        image = f"{project}-machina"
        try:
            run = _docker(
                "run", "--rm",
                "-v", f"{linux_owned_data_volume}:/home/machina/app/data",
                "--entrypoint", "python",
                image, "-c", PROBE,
            )  # fmt: skip
        finally:
            _docker("image", "rm", "-f", image)
    finally:
        if created_env_file:
            env_file.unlink()

    assert run.returncode == 0, run.stdout + run.stderr
    assert "WO-DOCKER-1" in run.stdout
