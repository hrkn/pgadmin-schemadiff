"""Resolve pgAdmin 4 installation layout and run headless Schema Diff comparisons."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from pgadmin_schemadiff.config import Config
from pgadmin_schemadiff.exceptions import PgAdminRuntimeError
from pgadmin_schemadiff.script_generator import DiffItem


@dataclass(frozen=True)
class PgAdminLayout:
    """Discovered paths inside a pgAdmin 4 installation."""

    root: Path
    python_exe: Path
    web_dir: Path
    runtime_dir: Path | None


def resolve_pgadmin_layout(pgadmin_path: Path) -> PgAdminLayout:
    """Locate pgAdmin 4's ``web`` directory, bundled Python executable, and ``runtime`` directory."""
    root = Path(pgadmin_path)
    if not root.exists():
        raise PgAdminRuntimeError(
            f"pgAdmin 4 installation path does not exist: '{root}'"
        )

    web_candidates = (
        root / "web",
        root / "Contents" / "Resources" / "web",
        root,
    )
    web_dir = next(
        (candidate for candidate in web_candidates if (candidate / "pgAdmin4.py").is_file()),
        None,
    )
    if web_dir is None:
        raise PgAdminRuntimeError(
            f"Could not locate pgAdmin 4 'web' directory containing 'pgAdmin4.py' under '{root}'."
        )

    python_candidates = (
        root / "python" / "python.exe",
        root / "python" / "bin" / "python3",
        root / "python" / "bin" / "python",
        root / "venv" / "bin" / "python3",
        root / "venv" / "bin" / "python",
        root / "Contents" / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3",
    )
    python_exe = next(
        (candidate for candidate in python_candidates if candidate.is_file()),
        None,
    )
    if python_exe is None:
        raise PgAdminRuntimeError(
            f"Could not locate pgAdmin 4 Python executable under '{root}'."
        )

    runtime_candidate = root / "runtime"
    runtime_dir = runtime_candidate if runtime_candidate.is_dir() else None

    return PgAdminLayout(
        root=root,
        python_exe=python_exe,
        web_dir=web_dir,
        runtime_dir=runtime_dir,
    )


def _build_worker_payload(
    layout: PgAdminLayout,
    config: Config,
    *,
    include_identical_ddl: bool,
) -> dict[str, Any]:
    # In pgAdmin 4 Schema Diff, diff_ddl updates Target -> Source.
    # To transform Old -> New, we map Source = New DB and Target = Old DB.
    return {
        "pgadmin_path": str(layout.root),
        "source": {
            "host": config.new_db.host,
            "port": config.new_db.port,
            "database": config.new_db.database,
            "user": config.new_db.user,
            "password": config.new_db.password,
            "schema": config.new_db.primary_schema,
            "sslmode": config.new_db.sslmode,
        },
        "target": {
            "host": config.old_db.host,
            "port": config.old_db.port,
            "database": config.old_db.database,
            "user": config.old_db.user,
            "password": config.old_db.password,
            "schema": config.old_db.primary_schema,
            "sslmode": config.old_db.sslmode,
        },
        "ignore": {
            "owner": config.ignore.owner,
            "whitespace": config.ignore.whitespace,
            "tablespace": config.ignore.tablespace,
            "grants": config.ignore.grants,
        },
        "include_identical_ddl": include_identical_ddl,
    }


def _build_worker_env(layout: PgAdminLayout) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    extra_dirs = [str(layout.python_exe.parent)]
    if layout.runtime_dir is not None:
        extra_dirs.insert(0, str(layout.runtime_dir))
    existing_path = env.get("PATH", "")
    prefix = os.pathsep.join(extra_dirs)
    env["PATH"] = f"{prefix}{os.pathsep}{existing_path}" if existing_path else prefix
    return env


def run_pgadmin_diff(
    config: Config,
    *,
    include_identical_ddl: bool = False,
) -> list[DiffItem]:
    """Execute pgAdmin 4's Schema Diff engine in an isolated subprocess."""
    layout = resolve_pgadmin_layout(config.pgadmin_path)
    payload = _build_worker_payload(
        layout, config, include_identical_ddl=include_identical_ddl
    )
    worker_path = Path(__file__).with_name("_pgadmin_worker.py")
    env = _build_worker_env(layout)

    with tempfile.TemporaryDirectory(prefix="pgadmin_sd_ipc_") as ipc_dir:
        response_file = Path(ipc_dir) / "response.json"
        cmd = [
            str(layout.python_exe),
            str(worker_path),
            "--response",
            str(response_file),
        ]

        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                encoding="utf-8",
                timeout=config.timeout_seconds,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PgAdminRuntimeError(
                f"pgAdmin 4 Schema Diff timed out after {config.timeout_seconds} second(s)."
            ) from exc
        except OSError as exc:
            raise PgAdminRuntimeError(
                f"Failed to launch pgAdmin 4 Python worker '{layout.python_exe}': {exc}"
            ) from exc

        if not response_file.is_file():
            stderr_msg = proc.stderr.strip() or proc.stdout.strip() or "no output"
            raise PgAdminRuntimeError(
                f"pgAdmin 4 worker exited with code {proc.returncode} without writing a response: {stderr_msg}"
            )

        try:
            response = json.loads(response_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PgAdminRuntimeError(
                f"Failed to parse pgAdmin 4 worker response JSON: {exc}"
            ) from exc

        if not response.get("ok"):
            err = response.get("error") or proc.stderr.strip() or "Unknown pgAdmin 4 worker error"
            raise PgAdminRuntimeError(f"pgAdmin 4 Schema Diff failed: {err}")

        if proc.returncode != 0:
            raise PgAdminRuntimeError(
                f"pgAdmin 4 worker exited with non-zero code {proc.returncode}."
            )

        raw_items = response.get("items")
        if not isinstance(raw_items, list):
            raise PgAdminRuntimeError("pgAdmin 4 worker returned an invalid 'items' payload.")

        return [DiffItem.from_pgadmin_dict(raw) for raw in raw_items]
