"""Unit tests for pgAdmin 4 layout discovery and subprocess runner."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import types

import pytest

from pgadmin_schemadiff.config import (
    Config,
    DatabaseConfig,
    IgnoreOptions,
    OutputFilterOptions,
)
from pgadmin_schemadiff.exceptions import PgAdminRuntimeError
from pgadmin_schemadiff.pgadmin_runner import resolve_pgadmin_layout, run_pgadmin_diff
from pgadmin_schemadiff.script_generator import DiffStatus


def _sample_config(pgadmin_path: Path) -> Config:
    return Config(
        pgadmin_path=pgadmin_path,
        old_db=DatabaseConfig(
            host="old-host",
            port=5432,
            database="old_db",
            user="old_user",
            password="pw_old",
            search_path="old_sch",
        ),
        new_db=DatabaseConfig(
            host="new-host",
            port=5433,
            database="new_db",
            user="new_user",
            password="pw_new",
            search_path="new_sch",
        ),
        ignore=IgnoreOptions(owner=True, whitespace=True, tablespace=False, grants=True),
        output_filter=OutputFilterOptions(),
        timeout_seconds=45,
    )


def test_resolve_pgadmin_layout_variants(tmp_path: Path) -> None:
    win_root = tmp_path / "win_pgadmin"
    (win_root / "web").mkdir(parents=True)
    (win_root / "web" / "pgAdmin4.py").write_text("", encoding="utf-8")
    (win_root / "python").mkdir(parents=True)
    (win_root / "python" / "python.exe").write_text("", encoding="utf-8")
    (win_root / "runtime").mkdir(parents=True)

    win_layout = resolve_pgadmin_layout(win_root)
    assert win_layout.web_dir == win_root / "web"
    assert win_layout.python_exe == win_root / "python" / "python.exe"
    assert win_layout.runtime_dir == win_root / "runtime"

    linux_root = tmp_path / "linux_pgadmin"
    (linux_root / "web").mkdir(parents=True)
    (linux_root / "web" / "pgAdmin4.py").write_text("", encoding="utf-8")
    (linux_root / "venv" / "bin").mkdir(parents=True)
    (linux_root / "venv" / "bin" / "python3").write_text("", encoding="utf-8")

    linux_layout = resolve_pgadmin_layout(linux_root)
    assert linux_layout.python_exe == linux_root / "venv" / "bin" / "python3"
    assert linux_layout.runtime_dir is None

    mac_root = tmp_path / "pgAdmin 4.app"
    (mac_root / "Contents" / "Resources" / "web").mkdir(parents=True)
    (mac_root / "Contents" / "Resources" / "web" / "pgAdmin4.py").write_text("", encoding="utf-8")
    mac_py = (
        mac_root
        / "Contents"
        / "Frameworks"
        / "Python.framework"
        / "Versions"
        / "Current"
        / "bin"
        / "python3"
    )
    mac_py.parent.mkdir(parents=True)
    mac_py.write_text("", encoding="utf-8")

    mac_layout = resolve_pgadmin_layout(mac_root)
    assert mac_layout.web_dir == mac_root / "Contents" / "Resources" / "web"
    assert mac_layout.python_exe == mac_py


def test_resolve_pgadmin_layout_errors(tmp_path: Path) -> None:
    with pytest.raises(PgAdminRuntimeError, match="does not exist"):
        resolve_pgadmin_layout(tmp_path / "missing")

    no_web = tmp_path / "no_web"
    no_web.mkdir()
    with pytest.raises(PgAdminRuntimeError, match="pgAdmin4.py"):
        resolve_pgadmin_layout(no_web)

    no_py = tmp_path / "no_py"
    (no_py / "web").mkdir(parents=True)
    (no_py / "web" / "pgAdmin4.py").write_text("", encoding="utf-8")
    with pytest.raises(PgAdminRuntimeError, match="Python executable"):
        resolve_pgadmin_layout(no_py)


def test_resolve_local_pgadmin_smoke() -> None:
    local_install = Path(r"C:\Program Files\PostgreSQL\17\pgAdmin 4")
    if not local_install.exists():
        pytest.skip("pgAdmin 4 is not installed at default path")
    layout = resolve_pgadmin_layout(local_install)
    assert layout.python_exe.is_file()
    assert (layout.web_dir / "pgAdmin4.py").is_file()


def _make_fake_pgadmin_dir(tmp_path: Path) -> Path:
    root = tmp_path / "pgadmin4"
    (root / "web").mkdir(parents=True)
    (root / "web" / "pgAdmin4.py").write_text("", encoding="utf-8")
    (root / "python").mkdir(parents=True)
    (root / "python" / "python.exe").write_text("", encoding="utf-8")
    (root / "runtime").mkdir(parents=True)
    return root


def test_run_pgadmin_diff_success_and_payload_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgadmin_dir = _make_fake_pgadmin_dir(tmp_path)
    cfg = _sample_config(pgadmin_dir)
    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        captured["cmd"] = cmd
        captured["payload"] = json.loads(str(kwargs["input"]))
        captured["env"] = kwargs["env"]
        resp_path = Path(cmd[cmd.index("--response") + 1])
        resp_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "items": [
                        {
                            "id": 1,
                            "type": "table",
                            "label": "Tables",
                            "title": "users",
                            "oid": 42,
                            "status": "Source Only",
                            "source_ddl": "CREATE TABLE users();",
                            "target_ddl": "",
                            "diff_ddl": "CREATE TABLE users();",
                            "dependencies": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    items = run_pgadmin_diff(cfg, include_identical_ddl=True)

    assert len(items) == 1
    assert items[0].status == DiffStatus.SOURCE_ONLY
    assert items[0].title == "users"

    payload = captured["payload"]
    assert isinstance(payload, dict)
    # Source must be New DB and Target must be Old DB so diff_ddl updates Old -> New
    assert payload["source"]["database"] == "new_db"
    assert payload["source"]["schema"] == "new_sch"
    assert payload["target"]["database"] == "old_db"
    assert payload["target"]["schema"] == "old_sch"
    assert payload["ignore"] == {
        "owner": True,
        "whitespace": True,
        "tablespace": False,
        "grants": True,
    }
    assert payload["include_identical_ddl"] is True


def test_run_pgadmin_diff_error_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgadmin_dir = _make_fake_pgadmin_dir(tmp_path)
    cfg = _sample_config(pgadmin_dir)

    def timeout_run(cmd: list[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=cfg.timeout_seconds)

    monkeypatch.setattr(subprocess, "run", timeout_run)
    with pytest.raises(PgAdminRuntimeError, match="timed out"):
        run_pgadmin_diff(cfg)

    def oserr_run(cmd: list[str], **kwargs: object) -> None:
        raise OSError("cannot execute")

    monkeypatch.setattr(subprocess, "run", oserr_run)
    with pytest.raises(PgAdminRuntimeError, match="Failed to launch"):
        run_pgadmin_diff(cfg)

    def no_response_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", no_response_run)
    with pytest.raises(PgAdminRuntimeError, match="without writing a response"):
        run_pgadmin_diff(cfg)

    def bad_json_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        resp_path = Path(cmd[cmd.index("--response") + 1])
        resp_path.write_text("{not json", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", bad_json_run)
    with pytest.raises(PgAdminRuntimeError, match="parse pgAdmin 4 worker response JSON"):
        run_pgadmin_diff(cfg)

    def worker_failed_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        resp_path = Path(cmd[cmd.index("--response") + 1])
        resp_path.write_text(json.dumps({"ok": False, "error": "version mismatch"}), encoding="utf-8")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", worker_failed_run)
    with pytest.raises(PgAdminRuntimeError, match="version mismatch"):
        run_pgadmin_diff(cfg)

    def nonzero_exit_ok_json(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        resp_path = Path(cmd[cmd.index("--response") + 1])
        resp_path.write_text(json.dumps({"ok": True, "items": []}), encoding="utf-8")
        return types.SimpleNamespace(returncode=2, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", nonzero_exit_ok_json)
    with pytest.raises(PgAdminRuntimeError, match="non-zero code 2"):
        run_pgadmin_diff(cfg)

    def invalid_items_run(cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
        resp_path = Path(cmd[cmd.index("--response") + 1])
        resp_path.write_text(json.dumps({"ok": True, "items": "not-a-list"}), encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", invalid_items_run)
    with pytest.raises(PgAdminRuntimeError, match="invalid 'items'"):
        run_pgadmin_diff(cfg)
