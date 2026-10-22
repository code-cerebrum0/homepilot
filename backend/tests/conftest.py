import os
import tempfile

_dir = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_dir}/test.db"
os.environ["SECRET_KEY"] = "t" * 48
os.environ["COOKIE_SECURE"] = "true"
os.environ["AUTO_CREATE_TABLES"] = "true"

import pytest
from fastapi.testclient import TestClient

from app.main import app


def make_client() -> TestClient:
    return TestClient(app, base_url="https://testserver")


@pytest.fixture()
def client():
    with make_client() as c:
        yield c


def csrf(c: TestClient) -> dict:
    return {"X-CSRF-Token": c.cookies.get("csrf_token")}


_n = 0


def register(c: TestClient, name="Asha") -> dict:
    global _n
    _n += 1
    r = c.post("/api/v1/auth/register", json={"name": name, "email": f"user{_n}@example.com", "password": "correct-horse-1"})
    assert r.status_code == 201, r.text
    return r.json()
