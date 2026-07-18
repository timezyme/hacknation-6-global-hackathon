"""Minimal Databricks Apps and Lakebase deployment spike."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, status
from psycopg import Connection
from pydantic import BaseModel

LOGGER = logging.getLogger("trustdesk.platform_spike")
PROBE_MARKER = "phase-2b"

CREATE_APP_SCHEMA = "CREATE SCHEMA IF NOT EXISTS trustdesk"
CREATE_PROBE_TABLE = """
CREATE TABLE IF NOT EXISTS trustdesk.platform_probe (
    probe_id UUID PRIMARY KEY,
    marker TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
INSERT_PROBE = """
INSERT INTO trustdesk.platform_probe (probe_id, marker)
VALUES (%s, %s)
ON CONFLICT (probe_id) DO NOTHING
"""
SELECT_PROBE = """
SELECT 1
FROM trustdesk.platform_probe
WHERE probe_id = %s AND marker = %s
"""


@dataclass(frozen=True)
class DatabaseSettings:
    endpoint: str
    host: str
    database: str
    user: str
    port: int
    sslmode: str
    application_name: str


class HealthResponse(BaseModel):
    status: str


class ProbeWriteResponse(BaseModel):
    probe_id: UUID


class ProbeReadResponse(BaseModel):
    found: bool


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        endpoint=required_environment("TRUSTDESK_POSTGRES_ENDPOINT"),
        host=required_environment("PGHOST"),
        database=required_environment("PGDATABASE"),
        user=required_environment("PGUSER"),
        port=int(required_environment("PGPORT")),
        sslmode=required_environment("PGSSLMODE"),
        application_name=os.environ.get("PGAPPNAME", "trustdesk-spike"),
    )


@contextmanager
def database_connection() -> Iterator[Connection[Any]]:
    """Open one connection with one fresh OAuth credential, then close both after the request."""
    settings = database_settings()
    credential = WorkspaceClient().postgres.generate_database_credential(endpoint=settings.endpoint)
    if not credential.token:
        raise RuntimeError("database credential was unavailable")

    with psycopg.connect(
        host=settings.host,
        dbname=settings.database,
        user=settings.user,
        password=credential.token,
        port=settings.port,
        sslmode=settings.sslmode,
        application_name=settings.application_name,
        connect_timeout=10,
    ) as connection:
        yield connection


def unavailable(operation: str, error: Exception) -> HTTPException:
    LOGGER.error("Lakebase %s failed (%s)", operation, type(error).__name__)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Platform probe unavailable.",
    )


app = FastAPI(title="Trust Desk platform spike", docs_url=None, redoc_url=None)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post("/api/probe", response_model=ProbeWriteResponse, status_code=status.HTTP_201_CREATED)
def write_probe() -> ProbeWriteResponse:
    probe_id = uuid4()
    try:
        with database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(CREATE_APP_SCHEMA)
            cursor.execute(CREATE_PROBE_TABLE)
            cursor.execute(INSERT_PROBE, (probe_id, PROBE_MARKER))
            connection.commit()
    except Exception as error:
        raise unavailable("write", error) from None
    return ProbeWriteResponse(probe_id=probe_id)


@app.get("/api/probe/{probe_id}", response_model=ProbeReadResponse)
def read_probe(probe_id: UUID) -> ProbeReadResponse:
    try:
        with database_connection() as connection, connection.cursor() as cursor:
            cursor.execute(SELECT_PROBE, (probe_id, PROBE_MARKER))
            found = cursor.fetchone() is not None
    except Exception as error:
        raise unavailable("read", error) from None
    return ProbeReadResponse(found=found)
