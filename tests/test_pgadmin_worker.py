"""Unit tests for the internal headless pgAdmin 4 worker module."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import types

import pytest

from pgadmin_schemadiff import _pgadmin_worker as worker


def test_write_temp_config_distro_and_configure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = worker._write_temp_config_distro(str(tmp_path))
    content = Path(cfg_file).read_text(encoding="utf-8")
    assert "SERVER_MODE = False" in content
    assert "AUTO_DISCOVER_SERVERS = False" in content
    assert "CONSOLE_LOG_LEVEL = 50" in content

    pgadmin_dir = tmp_path / "pgadmin4"
    (pgadmin_dir / "web" / "pgadmin").mkdir(parents=True)
    (pgadmin_dir / "runtime").mkdir(parents=True)
    (pgadmin_dir / "python").mkdir(parents=True)

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("PATH", "")
    added_dlls: list[str] = []
    monkeypatch.setattr(os, "add_dll_directory", lambda p: added_dlls.append(p), raising=False)

    worker._configure_runtime_paths(str(pgadmin_dir))
    assert str(pgadmin_dir / "web") == sys.path[0]
    assert str(pgadmin_dir / "runtime") in added_dlls
    assert str(pgadmin_dir / "python") in added_dlls


def test_connect_server_and_db_and_resolve_schema() -> None:
    class FakeConn:
        def __init__(self, *, connect_ok: bool = True, already_connected: bool = False, oid: int | None = 16384) -> None:
            self._connect_ok = connect_ok
            self._already = already_connected
            self._oid = oid

        def connect(self) -> tuple[bool, str | None]:
            return (True, None) if self._connect_ok else (False, "auth failed")

        def connected(self) -> bool:
            return self._already

        def execute_scalar(self, _sql: str) -> tuple[bool, int | None]:
            return (True, self._oid) if self._oid is not None else (False, None)

    class FakeMgr:
        def __init__(self, maint: FakeConn, db: FakeConn) -> None:
            self._maint = maint
            self._db = db
            self.released = False

        def connection(self, did: int | None = None) -> FakeConn:
            return self._maint if did is None else self._db

        def release(self) -> None:
            self.released = True

    class FakeDriver:
        def __init__(self, mgr: FakeMgr | None) -> None:
            self._mgr = mgr

        def connection_manager(self, _sid: int) -> FakeMgr | None:
            return self._mgr

    mgr = FakeMgr(FakeConn(), FakeConn(already_connected=True))
    out_mgr, did = worker._connect_server_and_db(FakeDriver(mgr), 1, "db_a")
    assert out_mgr is mgr
    assert did == 16384

    with pytest.raises(RuntimeError, match="connection manager"):
        worker._connect_server_and_db(FakeDriver(None), 1, "db_a")
    with pytest.raises(RuntimeError, match="auth failed"):
        worker._connect_server_and_db(
            FakeDriver(FakeMgr(FakeConn(connect_ok=False), FakeConn())), 1, "db_a"
        )
    with pytest.raises(RuntimeError, match="database OID"):
        worker._connect_server_and_db(
            FakeDriver(FakeMgr(FakeConn(oid=None), FakeConn())), 1, "db_a"
        )
    with pytest.raises(RuntimeError, match="oid=16384"):
        worker._connect_server_and_db(
            FakeDriver(FakeMgr(FakeConn(), FakeConn(connect_ok=False, already_connected=False))),
            1,
            "db_a",
        )

    assert worker._resolve_schema_oid(
        lambda _s, _d: [{"_id": 2200, "label": "public"}], 1, 16384, "public"
    ) == 2200
    with pytest.raises(RuntimeError, match="Failed to retrieve schemas"):
        worker._resolve_schema_oid(lambda _s, _d: None, 1, 16384, "public")
    with pytest.raises(RuntimeError, match="was not found"):
        worker._resolve_schema_oid(
            lambda _s, _d: [{"_id": 2200, "label": "public"}], 1, 16384, "app"
        )


def _install_fake_pgadmin_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compat_result: tuple[bool, str | None] = (True, None),
    compare_items: list[dict] | None = None,
    compare_error_obj: object = None,
    user_exists: bool = True,
    has_server_group: bool = True,
) -> dict[str, object]:
    state: dict[str, object] = {"released": 0, "disposed": False, "logged_in": None}

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self._next_id = 1

        def add(self, obj: object) -> None:
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id  # type: ignore[attr-defined]
                self._next_id += 1
            self.added.append(obj)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def remove(self) -> None:
            return None

    class FakeEngine:
        def dispose(self) -> None:
            state["disposed"] = True

    fake_session = FakeSession()
    fake_db = types.SimpleNamespace(session=fake_session, engine=FakeEngine())

    class FakeQuery:
        def __init__(self, result: object) -> None:
            self._result = result

        def filter_by(self, **_kwargs: object) -> FakeQuery:
            return self

        def first(self) -> object:
            return self._result

    class FakeUser:
        id = 1
        query = FakeQuery(types.SimpleNamespace(id=1) if user_exists else None)

    class FakeServerGroup:
        query = FakeQuery(types.SimpleNamespace(id=10) if has_server_group else None)

        def __init__(self, **kwargs: object) -> None:
            self.id = 11
            self.__dict__.update(kwargs)

    class FakeServer:
        def __init__(self, **kwargs: object) -> None:
            self.id = None
            self.__dict__.update(kwargs)

    class FakeApp:
        def app_context(self) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

        def test_request_context(self) -> contextlib.AbstractContextManager[None]:
            return contextlib.nullcontext()

    class FakeConn:
        def __init__(self, oid: int) -> None:
            self._oid = oid

        def connect(self) -> tuple[bool, None]:
            return True, None

        def connected(self) -> bool:
            return True

        def execute_scalar(self, _sql: str) -> tuple[bool, int]:
            return True, self._oid

    class FakeMgr:
        def __init__(self, sid: int) -> None:
            self._sid = sid

        def connection(self, did: int | None = None) -> FakeConn:
            return FakeConn(100 + self._sid)

        def release(self) -> None:
            state["released"] = int(state["released"]) + 1

    class FakeDriver:
        def connection_manager(self, sid: int) -> FakeMgr:
            return FakeMgr(sid)

    class FakeTableView:
        def compare(self, **kwargs: object) -> object:
            state["compare_kwargs"] = kwargs
            if compare_error_obj is not None:
                return compare_error_obj
            return compare_items if compare_items is not None else [
                {
                    "id": 1,
                    "type": "table",
                    "label": "Tables",
                    "title": "t1",
                    "oid": 500,
                    "status": "Identical",
                    "group_name": "Schema Objects",
                    "dependencies": [],
                }
            ]

        def ddl_compare(self, **_kwargs: object) -> dict[str, str]:
            return {"source_ddl": "CREATE TABLE t1();", "target_ddl": "CREATE TABLE t1();", "diff_ddl": ""}

    class FakeRegistry:
        @classmethod
        def set_schema_diff_compare_mode(cls, _mode: str) -> None:
            return None

        @classmethod
        def get_registered_nodes(cls) -> dict[str, object]:
            return {"schema": object(), "table": object(), "view": object()}

        @classmethod
        def get_node_view(cls, name: str) -> object:
            if name == "schema":
                return object()  # No compare() method
            if name == "table":
                return FakeTableView()
            return types.SimpleNamespace(compare=lambda **_kw: None)

    config_mod = types.ModuleType("config")
    config_mod.DESKTOP_USER = "pgadmin4@pgadmin.org"  # type: ignore[attr-defined]
    config_mod.PG_DEFAULT_DRIVER = "psycopg3"  # type: ignore[attr-defined]

    flask_sec_pkg = types.ModuleType("flask_security")
    flask_sec_utils = types.ModuleType("flask_security.utils")
    flask_sec_utils.login_user = lambda u: state.__setitem__("logged_in", u.id)  # type: ignore[attr-defined]

    pgadmin_pkg = types.ModuleType("pgadmin")
    pgadmin_pkg.create_app = lambda: FakeApp()  # type: ignore[attr-defined]

    model_mod = types.ModuleType("pgadmin.model")
    model_mod.db = fake_db  # type: ignore[attr-defined]
    model_mod.User = FakeUser  # type: ignore[attr-defined]
    model_mod.ServerGroup = FakeServerGroup  # type: ignore[attr-defined]
    model_mod.Server = FakeServer  # type: ignore[attr-defined]

    tools_pkg = types.ModuleType("pgadmin.tools")
    sd_mod = types.ModuleType("pgadmin.tools.schema_diff")
    sd_mod.SCH_OBJ_STR = "Schema Objects"  # type: ignore[attr-defined]
    sd_mod.check_version_compatibility = lambda _s, _t: compat_result  # type: ignore[attr-defined]
    sd_mod.get_schemas = lambda sid, _did: [{"_id": 2200 + sid, "label": "app"}]  # type: ignore[attr-defined]

    reg_mod = types.ModuleType("pgadmin.tools.schema_diff.node_registry")
    reg_mod.SchemaDiffRegistry = FakeRegistry  # type: ignore[attr-defined]

    utils_pkg = types.ModuleType("pgadmin.utils")
    crypto_mod = types.ModuleType("pgadmin.utils.crypto")
    crypto_mod.encrypt = lambda pw, _key: f"enc:{pw}".encode("utf-8")  # type: ignore[attr-defined]
    driver_mod = types.ModuleType("pgadmin.utils.driver")
    driver_mod.get_driver = lambda _name: FakeDriver()  # type: ignore[attr-defined]
    mp_mod = types.ModuleType("pgadmin.utils.master_password")
    mp_mod.get_crypt_key = lambda: (True, b"key")  # type: ignore[attr-defined]

    modules = {
        "config": config_mod,
        "flask_security": flask_sec_pkg,
        "flask_security.utils": flask_sec_utils,
        "pgadmin": pgadmin_pkg,
        "pgadmin.model": model_mod,
        "pgadmin.tools": tools_pkg,
        "pgadmin.tools.schema_diff": sd_mod,
        "pgadmin.tools.schema_diff.node_registry": reg_mod,
        "pgadmin.utils": utils_pkg,
        "pgadmin.utils.crypto": crypto_mod,
        "pgadmin.utils.driver": driver_mod,
        "pgadmin.utils.master_password": mp_mod,
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    return state


def _sample_payload(tmp_path: Path) -> dict[str, object]:
    pg_dir = tmp_path / "pgadmin"
    (pg_dir / "web" / "pgadmin").mkdir(parents=True, exist_ok=True)
    return {
        "pgadmin_path": str(pg_dir),
        "source": {
            "host": "localhost",
            "port": 5432,
            "database": "new_db",
            "user": "postgres",
            "password": "pw",
            "schema": "app",
            "sslmode": "prefer",
        },
        "target": {
            "host": "localhost",
            "port": 5432,
            "database": "old_db",
            "user": "postgres",
            "password": "",
            "schema": "app",
            "sslmode": "prefer",
        },
        "ignore": {"owner": True, "whitespace": False, "tablespace": True, "grants": False},
        "include_identical_ddl": True,
    }


def test_run_worker_request_success_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_pgadmin_modules(monkeypatch, has_server_group=False)
    items = worker.run_worker_request(_sample_payload(tmp_path))

    assert len(items) == 1
    assert items[0]["source_ddl"] == "CREATE TABLE t1();"
    assert items[0]["target_ddl"] == "CREATE TABLE t1();"
    assert state["logged_in"] == 1
    assert state["released"] == 2
    assert state["disposed"] is True
    assert state["compare_kwargs"]["ignore_owner"] is True
    assert state["compare_kwargs"]["ignore_tablespace"] is True


def test_run_worker_request_failure_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_pgadmin_modules(monkeypatch, user_exists=False)
    with pytest.raises(RuntimeError, match="desktop user"):
        worker.run_worker_request(_sample_payload(tmp_path))

    _install_fake_pgadmin_modules(
        monkeypatch, compat_result=(False, "Source and Target must match major version")
    )
    with pytest.raises(RuntimeError, match="major version"):
        worker.run_worker_request(_sample_payload(tmp_path))

    fake_resp = types.SimpleNamespace(get_data=lambda as_text=True: "query error")
    _install_fake_pgadmin_modules(monkeypatch, compare_error_obj=fake_resp)
    with pytest.raises(RuntimeError, match="query error"):
        worker.run_worker_request(_sample_payload(tmp_path))


def test_worker_main_writes_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp_file = tmp_path / "resp.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"x": 1})))
    monkeypatch.setattr(worker, "run_worker_request", lambda _p: [{"id": 1}])

    rc = worker.main(["--response", str(resp_file)])
    assert rc == 0
    assert json.loads(resp_file.read_text(encoding="utf-8")) == {
        "ok": True,
        "items": [{"id": 1}],
    }

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"x": 1})))

    def boom(_p: object) -> list[dict]:
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(worker, "run_worker_request", boom)
    rc_err = worker.main(["--response", str(resp_file)])
    assert rc_err == 1
    err_data = json.loads(resp_file.read_text(encoding="utf-8"))
    assert err_data["ok"] is False
    assert "worker crashed" in err_data["error"]
