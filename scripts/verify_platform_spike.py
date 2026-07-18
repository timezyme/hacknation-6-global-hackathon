"""Deploy and verify the Phase 2B Databricks Apps and Lakebase spike."""

from __future__ import annotations

import base64
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment, AppDeploymentMode
from databricks.sdk.service.workspace import ImportFormat

APP_NAME = "trustdesk-spike"
ARTIFACT_PATH = Path("artifacts/platform-spike.json")
SOURCE_PATH = "/Workspace/Shared/trustdesk-spike"
VERIFICATION_PROFILE = "trustdesk-spike"
SOURCE_FILES = (
    Path("app.yaml"),
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("app/main.py"),
    Path("src/trustdesk/__init__.py"),
)
REQUEST_TIMEOUT_SECONDS = 20
START_TIMEOUT = timedelta(minutes=10)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_artifact(artifact: dict[str, Any]) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")


def require_source_files() -> None:
    missing = [str(path) for path in SOURCE_FILES if not path.is_file()]
    if missing:
        raise FileNotFoundError("required deployment files are missing")


def upload_source(workspace: WorkspaceClient, source_path: str) -> None:
    root = Path.cwd()
    for relative_path in SOURCE_FILES:
        destination = f"{source_path.rstrip('/')}/{relative_path.as_posix()}"
        workspace.workspace.mkdirs(destination.rsplit("/", 1)[0])
        content = base64.b64encode((root / relative_path).read_bytes()).decode("ascii")
        workspace.workspace.import_(
            destination,
            content=content,
            format=ImportFormat.RAW,
            overwrite=True,
        )


def request_json(
    url: str,
    path: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
) -> tuple[int, dict[str, Any]]:
    request_headers = {**headers, "Accept": "application/json"}
    data = None
    if method == "POST":
        data = b""
        request_headers["Content-Type"] = "application/json"
    request = Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read())
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            return response.status, payload
    except HTTPError as error:
        return error.code, {}


def wait_for_health(url: str, headers: dict[str, str], timeout_seconds: int = 300) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, _ = request_json(url, "/api/health", headers)
            if status == 200:
                return status
        except (TimeoutError, URLError):
            pass
        time.sleep(2)
    raise TimeoutError("health endpoint did not become ready")


def main() -> int:
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "status": "fail",
        "started_at": utc_now(),
        "completed_at": None,
        "deployment": {
            "completed": False,
            "duration_seconds": None,
            "cold_start_duration_seconds": None,
            "cold_start_health_200": False,
        },
        "resource_binding": {
            "postgres_present": False,
            "can_connect_and_create": False,
        },
        "permission_checks": {
            "source_access": False,
            "service_principal_database_access": False,
        },
        "probe": {
            "write": False,
            "read_before_restart": False,
            "read_after_restart": False,
        },
        "restart": {"completed": False, "duration_seconds": None},
        "failure": None,
    }
    stage = "preflight"

    try:
        require_source_files()
        workspace = WorkspaceClient(profile=VERIFICATION_PROFILE)
        app = workspace.apps.get(APP_NAME)
        postgres_resources = [resource for resource in app.resources or [] if resource.postgres]
        artifact["resource_binding"] = {
            "postgres_present": len(postgres_resources) == 1,
            "can_connect_and_create": (
                len(postgres_resources) == 1
                and postgres_resources[0].postgres is not None
                and postgres_resources[0].postgres.permission is not None
                and postgres_resources[0].postgres.permission.value == "CAN_CONNECT_AND_CREATE"
            ),
        }
        if artifact["resource_binding"] != {
            "postgres_present": True,
            "can_connect_and_create": True,
        }:
            raise RuntimeError("expected one writable Postgres resource")
        stage = "upload"
        upload_source(workspace, SOURCE_PATH)

        stage = "deployment"
        deployment_started = time.monotonic()
        compute_state = getattr(app.compute_status, "state", None)
        if compute_state is None or compute_state.value != "ACTIVE":
            workspace.apps.start(APP_NAME).result(timeout=START_TIMEOUT)
        workspace.apps.deploy(
            APP_NAME,
            AppDeployment(
                source_code_path=SOURCE_PATH,
                mode=AppDeploymentMode.SNAPSHOT,
            ),
        ).result(timeout=START_TIMEOUT)
        deployment_seconds = round(time.monotonic() - deployment_started, 3)
        artifact["deployment"] = {
            "completed": True,
            "duration_seconds": deployment_seconds,
            "cold_start_duration_seconds": None,
            "cold_start_health_200": False,
        }
        permission_checks = artifact["permission_checks"]
        assert isinstance(permission_checks, dict)
        permission_checks["source_access"] = True

        deployed_app = workspace.apps.get(APP_NAME)
        if not deployed_app.url:
            raise RuntimeError("deployed app has no URL")
        headers = workspace.config.authenticate()

        stage = "cold_start_health"
        cold_start_started = time.monotonic()
        workspace.apps.stop(APP_NAME).result(timeout=START_TIMEOUT)
        workspace.apps.start(APP_NAME).result(timeout=START_TIMEOUT)
        health_status = wait_for_health(deployed_app.url, headers)
        cold_start_seconds = round(time.monotonic() - cold_start_started, 3)
        deployment = artifact["deployment"]
        assert isinstance(deployment, dict)
        deployment["cold_start_health_200"] = health_status == 200
        deployment["cold_start_duration_seconds"] = cold_start_seconds

        stage = "probe_write"
        write_status, write_payload = request_json(
            deployed_app.url,
            "/api/probe",
            headers,
            method="POST",
        )
        probe_id = write_payload.get("probe_id")
        if write_status != 201 or not isinstance(probe_id, str):
            raise RuntimeError("probe write failed")
        probe = artifact["probe"]
        assert isinstance(probe, dict)
        probe["write"] = True
        permission_checks["service_principal_database_access"] = True

        stage = "probe_read"
        read_status, read_payload = request_json(
            deployed_app.url,
            f"/api/probe/{probe_id}",
            headers,
        )
        if read_status != 200 or read_payload.get("found") is not True:
            raise RuntimeError("probe read failed")
        probe["read_before_restart"] = True

        stage = "restart"
        restart_started = time.monotonic()
        workspace.apps.stop(APP_NAME).result(timeout=START_TIMEOUT)
        workspace.apps.start(APP_NAME).result(timeout=START_TIMEOUT)
        wait_for_health(deployed_app.url, headers)
        restart_seconds = round(time.monotonic() - restart_started, 3)
        artifact["restart"] = {"completed": True, "duration_seconds": restart_seconds}

        stage = "post_restart_read"
        final_status, final_payload = request_json(
            deployed_app.url,
            f"/api/probe/{probe_id}",
            headers,
        )
        if final_status != 200 or final_payload.get("found") is not True:
            raise RuntimeError("post-restart probe read failed")
        probe["read_after_restart"] = True

        artifact["status"] = "pass"
        artifact["completed_at"] = utc_now()
        write_artifact(artifact)
        print("platform spike: pass")
        return 0
    except Exception as error:
        artifact["completed_at"] = utc_now()
        artifact["failure"] = {"stage": stage, "error_type": type(error).__name__}
        write_artifact(artifact)
        print(f"platform spike: fail ({stage}, {type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
