"""测试夹具：每个测试使用独立的临时数据目录。"""

from __future__ import annotations

import logging

import pytest

from app.api import create_app
from app.storage import LocalStore


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def store(data_dir):
    s = LocalStore(data_dir)
    s.setup()
    return s


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    app = create_app(data_dir)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _decision_log(caplog):
    """以 DEBUG 级别捕获判定日志，单测中可打印输入/版本/依据。"""
    caplog.set_level(logging.DEBUG)
    yield
