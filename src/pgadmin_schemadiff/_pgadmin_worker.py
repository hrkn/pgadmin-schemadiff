"""Headless worker executed under pgAdmin 4's Python interpreter.

This module intentionally uses only the Python standard library at import time
so it can be executed directly by pgAdmin 4's bundled Python runtime.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
from typing import Any


def _write_temp_config_distro(work_dir: str) -> str:
    cfg_path = os.path.join(work_dir, "config_distro.py")
    content = (
        "SERVER_MODE = False\n"
        f"DATA_DIR = {work_dir!r}\n"
        "MASTER_PASSWORD_REQUIRED = False\n"
        "UPGRADE_CHECK_ENABLED = False\n"
        "AUTO_DISCOVER_SERVERS = False\n"
        "USE_OS_SECRET_STORAGE = False\n"
        "CONSOLE_LOG_LEVEL = 50\n"
        "FILE_LOG_LEVEL = 50\n"
    )
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return cfg_path


def _configure_runtime_paths(pgadmin_path: str) -> None:
    root = Path(pgadmin_path)
    web_candidates = [
        root / "web",
        root / "Contents" / "Resources" / "web",
        root,
    ]
    web_dir = next(
        (candidate for candidate in web_candidates if (candidate / "pgadmin").is_dir()),
        root / "web",
    )
    web_str = str(web_dir)
    if web_str not in sys.path:
        sys.path.insert(0, web_str)

    dll_dirs = [root / "runtime", root / "python"]
    existing_dirs = [str(d) for d in dll_dirs if d.is_dir()]
    if existing_dirs:
        current_path = os.environ.get("PATH", "")
        path_parts = [p for p in current_path.split(os.pathsep) if p]
        for dir_str in reversed(existing_dirs):
            if dir_str not in path_parts:
                path_parts.insert(0, dir_str)
        os.environ["PATH"] = os.pathsep.join(path_parts)

        add_dll_dir = getattr(os, "add_dll_directory", None)
        if add_dll_dir is not None:
            for dir_str in existing_dirs:
                add_dll_dir(dir_str)


def _connect_server_and_db(
    driver: Any,
    server_id: int,
    database_name: str,
) -> tuple[Any, int]:
    manager = driver.connection_manager(server_id)
    if manager is None:
        raise RuntimeError(f"Failed to acquire connection manager for server id={server_id}.")

    maint_conn = manager.connection()
    ok, err = maint_conn.connect()
    if not ok:
        raise RuntimeError(
            f"Failed to connect to PostgreSQL database '{database_name}': {err}"
        )

    ok, did = maint_conn.execute_scalar(
        "SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()"
    )
    if not ok or did is None:
        raise RuntimeError(
            f"Failed to resolve database OID for '{database_name}': {did}"
        )

    db_conn = manager.connection(did=int(did))
    if not db_conn.connected():
        ok, err = db_conn.connect()
        if not ok:
            raise RuntimeError(
                f"Failed to connect to PostgreSQL database '{database_name}' (oid={did}): {err}"
            )

    return manager, int(did)


def _resolve_schema_oid(
    get_schemas_fn: Any,
    sid: int,
    did: int,
    schema_name: str,
) -> int:
    schemas = get_schemas_fn(sid, did)
    if schemas is None:
        raise RuntimeError(
            f"Failed to retrieve schemas for server id={sid}, database oid={did}."
        )
    for item in schemas:
        if item.get("label") == schema_name:
            return int(item["_id"])
    raise RuntimeError(
        f"Schema '{schema_name}' was not found in target database (did={did})."
    )


def _extract_error_message(response_obj: Any) -> str:
    if hasattr(response_obj, "get_data"):
        return str(response_obj.get_data(as_text=True))
    if hasattr(response_obj, "data"):
        data = response_obj.data
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return str(response_obj)


def _create_temp_server(
    server_cls: Any,
    *,
    user_id: int,
    servergroup_id: int,
    name: str,
    entry: dict[str, Any],
    encrypt_fn: Any,
    crypt_key: Any,
) -> Any:
    password = entry.get("password") or ""
    enc_password = encrypt_fn(password, crypt_key) if password else None
    return server_cls(
        user_id=user_id,
        servergroup_id=servergroup_id,
        name=name,
        host=entry["host"],
        port=int(entry["port"]),
        maintenance_db=entry["database"],
        username=entry["user"],
        password=enc_password,
        save_password=1,
        use_ssh_tunnel=0,
        tunnel_authentication=0,
        tunnel_prompt_password=0,
        shared=False,
        kerberos_conn=False,
        cloud_status=0,
        is_adhoc=0,
        connection_params={"sslmode": entry.get("sslmode", "prefer")},
    )


def run_worker_request(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Run pgAdmin 4's Schema Diff comparison in an isolated desktop context."""
    work_dir = tempfile.mkdtemp(prefix="pgadmin_sd_")
    prev_config_distro = os.environ.get("CONFIG_DISTRO_FILE_PATH")
    os.environ["CONFIG_DISTRO_FILE_PATH"] = _write_temp_config_distro(work_dir)
    _configure_runtime_paths(payload["pgadmin_path"])

    src_mgr = None
    tgt_mgr = None
    db_module = None

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import config  # type: ignore[import-not-found]
            from flask_security.utils import login_user  # type: ignore[import-not-found]
            from pgadmin import create_app  # type: ignore[import-not-found]
            from pgadmin.model import Server, ServerGroup, User, db  # type: ignore[import-not-found]
            from pgadmin.tools.schema_diff import (  # type: ignore[import-not-found]
                SCH_OBJ_STR,
                check_version_compatibility,
                get_schemas,
            )
            from pgadmin.tools.schema_diff.node_registry import SchemaDiffRegistry  # type: ignore[import-not-found]
            from pgadmin.utils.crypto import encrypt  # type: ignore[import-not-found]
            from pgadmin.utils.driver import get_driver  # type: ignore[import-not-found]
            from pgadmin.utils.master_password import get_crypt_key  # type: ignore[import-not-found]

            db_module = db
            app = create_app()

            with app.app_context(), app.test_request_context():
                try:
                    user = User.query.filter_by(email=config.DESKTOP_USER).first()
                    if user is None:
                        raise RuntimeError(
                            f"pgAdmin desktop user '{config.DESKTOP_USER}' was not found."
                        )
                    login_user(user)

                    server_group = ServerGroup.query.filter_by(user_id=user.id).first()
                    if server_group is None:
                        server_group = ServerGroup(user_id=user.id, name="Servers")
                        db.session.add(server_group)
                        db.session.flush()

                    key_ok, crypt_key = get_crypt_key()
                    if not key_ok:
                        raise RuntimeError("Failed to obtain pgAdmin encryption key.")

                    source_cfg = payload["source"]
                    target_cfg = payload["target"]

                    source_server = _create_temp_server(
                        Server,
                        user_id=user.id,
                        servergroup_id=server_group.id,
                        name="schema_diff_source_new",
                        entry=source_cfg,
                        encrypt_fn=encrypt,
                        crypt_key=crypt_key,
                    )
                    target_server = _create_temp_server(
                        Server,
                        user_id=user.id,
                        servergroup_id=server_group.id,
                        name="schema_diff_target_old",
                        entry=target_cfg,
                        encrypt_fn=encrypt,
                        crypt_key=crypt_key,
                    )
                    db.session.add(source_server)
                    db.session.add(target_server)
                    db.session.commit()

                    driver = get_driver(config.PG_DEFAULT_DRIVER)
                    src_mgr, source_did = _connect_server_and_db(
                        driver, source_server.id, source_cfg["database"]
                    )
                    tgt_mgr, target_did = _connect_server_and_db(
                        driver, target_server.id, target_cfg["database"]
                    )

                    compat_ok, compat_msg = check_version_compatibility(
                        source_server.id, target_server.id
                    )
                    if not compat_ok:
                        raise RuntimeError(str(compat_msg))

                    source_scid = _resolve_schema_oid(
                        get_schemas,
                        source_server.id,
                        source_did,
                        source_cfg["schema"],
                    )
                    target_scid = _resolve_schema_oid(
                        get_schemas,
                        target_server.id,
                        target_did,
                        target_cfg["schema"],
                    )

                    ignore_opts = payload.get("ignore", {})
                    ignore_owner = bool(ignore_opts.get("owner", False))
                    ignore_whitespaces = bool(ignore_opts.get("whitespace", False))
                    ignore_tablespace = bool(ignore_opts.get("tablespace", False))
                    ignore_grants = bool(ignore_opts.get("grants", False))

                    SchemaDiffRegistry.set_schema_diff_compare_mode(SCH_OBJ_STR)
                    registered_nodes = SchemaDiffRegistry.get_registered_nodes()

                    comparison_result: list[dict[str, Any]] = []
                    for node_name in registered_nodes:
                        view = SchemaDiffRegistry.get_node_view(node_name)
                        if not hasattr(view, "compare"):
                            continue

                        res = view.compare(
                            source_sid=source_server.id,
                            source_did=source_did,
                            source_scid=source_scid,
                            target_sid=target_server.id,
                            target_did=target_did,
                            target_scid=target_scid,
                            group_name=SCH_OBJ_STR,
                            source_schema_name=None,
                            ignore_owner=ignore_owner,
                            ignore_whitespaces=ignore_whitespaces,
                            ignore_tablespace=ignore_tablespace,
                            ignore_grants=ignore_grants,
                        )
                        if res is None:
                            continue
                        if isinstance(res, list):
                            comparison_result.extend(res)
                        else:
                            raise RuntimeError(
                                f"pgAdmin compare failed for '{node_name}': "
                                f"{_extract_error_message(res)}"
                            )

                    if payload.get("include_identical_ddl"):
                        for item in comparison_result:
                            if item.get("status") != "Identical":
                                continue
                            view = SchemaDiffRegistry.get_node_view(item.get("type"))
                            if view and hasattr(view, "ddl_compare"):
                                ddl_info = view.ddl_compare(
                                    source_sid=source_server.id,
                                    source_did=source_did,
                                    source_scid=source_scid,
                                    target_sid=target_server.id,
                                    target_did=target_did,
                                    target_scid=target_scid,
                                    source_oid=item.get("source_oid") or item.get("oid"),
                                    target_oid=item.get("target_oid") or item.get("oid"),
                                    comp_status=item["status"],
                                )
                                item["source_ddl"] = ddl_info.get("source_ddl") or ""
                                item["target_ddl"] = ddl_info.get("target_ddl") or ""
                                item["diff_ddl"] = ddl_info.get("diff_ddl") or ""

                    return comparison_result
                finally:
                    for mgr in (src_mgr, tgt_mgr):
                        if mgr is not None:
                            try:
                                mgr.release()
                            except Exception:
                                pass
                    if db_module is not None:
                        try:
                            db_module.session.remove()
                            db_module.engine.dispose()
                        except Exception:
                            pass
    finally:
        if prev_config_distro is None:
            os.environ.pop("CONFIG_DISTRO_FILE_PATH", None)
        else:
            os.environ["CONFIG_DISTRO_FILE_PATH"] = prev_config_distro
        logging.shutdown()
        shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint invoked by :mod:`pgadmin_schemadiff.pgadmin_runner`."""
    parser = argparse.ArgumentParser(description="Internal pgAdmin 4 Schema Diff worker.")
    parser.add_argument("--response", required=True, help="Path to write JSON output.")
    args = parser.parse_args(argv)

    response_path = Path(args.response)
    try:
        payload = json.loads(sys.stdin.read())
        items = run_worker_request(payload)
        response_data: dict[str, Any] = {"ok": True, "items": items}
        exit_code = 0
    except Exception as exc:
        response_data = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        exit_code = 1

    response_path.write_text(
        json.dumps(response_data, ensure_ascii=False),
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
