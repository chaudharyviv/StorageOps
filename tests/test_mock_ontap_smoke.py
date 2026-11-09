"""Start the real mock server on a free local port, call each endpoint once, shut it down."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from contracts import Variant
from simulator.engine import simulate
from simulator.scenario import find_scenario
from simulator.store import write_run

pytestmark = pytest.mark.smoke  # skip with: pytest -m "not smoke"

REPO = Path(__file__).resolve().parents[1]
MOCK_NOW = "2026-01-04T12:00:00Z"
ENDPOINTS = [
    "/api/cluster",
    "/api/storage/aggregates",
    "/api/storage/volumes",
    "/api/snapmirror/relationships",
    "/api/storage/qos/policies",
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local URL
            return response.status, json.load(response)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    workdir = tmp_path_factory.mktemp("smoke")
    db = workdir / "metrics.duckdb"
    write_run(db, simulate(find_scenario("snapmirror_lag_creep").variant(Variant.MILD), seed=1))
    port = free_port()
    log_path = workdir / "server.log"
    env = {**os.environ, "MOCK_ONTAP_DB": str(db), "MOCK_NOW": MOCK_NOW}
    command = [
        sys.executable, "-m", "uvicorn", "simulator.mock_ontap_api.app:create_app", "--factory",
        "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
    ]  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    pytest.fail(f"server exited early:\n{log_path.read_text()}")
                try:
                    if fetch(base + "/api/cluster")[0] == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    pytest.fail(f"server did not start in 30s:\n{log_path.read_text()}")
                time.sleep(0.2)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.mark.parametrize("path", ENDPOINTS)
def test_each_endpoint_answers_over_real_http(server: str, path: str) -> None:
    status, body = fetch(server + path)
    assert status == 200
    if path == "/api/cluster":
        assert body["version"]["major"] == 19 and body["name"]
    else:
        assert body["num_records"] == len(body["records"]) > 0
        assert body["_links"]["self"]["href"] == path


def test_the_mock_now_setting_reaches_the_real_server(server: str) -> None:
    _, body = fetch(server + "/api/storage/volumes?fields=metric.*")
    assert body["records"][0]["metric"]["timestamp"] == "2026-01-04 12:00:00 +0000"
    _, overridden = fetch(
        server + "/api/storage/volumes?fields=metric.*&mock_now=2026-01-02T00:00:00Z"
    )
    assert overridden["records"][0]["metric"]["timestamp"] == "2026-01-02 00:00:00 +0000"


def test_writes_are_refused_over_real_http(server: str) -> None:
    request = urllib.request.Request(server + "/api/storage/volumes", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)  # noqa: S310 - local URL
    assert caught.value.code == 405
