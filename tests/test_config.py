"""Unit tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pgadmin_schemadiff.config import (
    Config,
    DatabaseConfig,
    IgnoreOptions,
    OutputFilterOptions,
    load_config,
)
from pgadmin_schemadiff.exceptions import ConfigError


def _write_toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_full_and_defaults(tmp_path: Path) -> None:
    cfg_path = _write_toml(
        tmp_path,
        """
[pgadmin]
install_path = "C:/pgAdmin4"
timeout_seconds = 120

[ignore]
owner = true
whitespace = true
tablespace = true
grants = true

[filter]
source_only = false
target_only = true
different = true
same = true

[databases.old]
host = "localhost"
port = 5432
database = "old_db"
user = "postgres"
password = "pw1"
search_path = '"$user", app_old, public'
sslmode = "disable"

[databases.new]
host = "localhost"
user = "postgres"
database = "new_db"
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.pgadmin_path == Path("C:/pgAdmin4")
    assert cfg.timeout_seconds == 120
    assert cfg.ignore == IgnoreOptions(
        owner=True, whitespace=True, tablespace=True, grants=True
    )
    assert cfg.output_filter == OutputFilterOptions(
        source_only=False, target_only=True, different=True, same=True
    )
    assert cfg.output_filter.selected_statuses() == {
        "Target Only",
        "Different",
        "Identical",
    }
    assert cfg.old_db.search_path_schemas() == ["$user", "app_old", "public"]
    assert cfg.old_db.primary_schema == "app_old"
    assert cfg.old_db.sslmode == "disable"
    assert cfg.new_db.port == 5432
    assert cfg.new_db.password == ""
    assert cfg.new_db.primary_schema == "public"


def test_load_config_aliases_and_password_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLD_DB_PW", "secret_old")
    monkeypatch.setenv("NEW_DB_PW", "secret_new")
    cfg_path = _write_toml(
        tmp_path,
        """
pgadmin_path = "/opt/pgadmin4"

[ignore]
whitespaces = true

[output_filter]
identical = true
source_only = false

[databases.old]
host = "db.example.com"
dbname = "main_db"
username = "alice"
password_env = "OLD_DB_PW"
search_path = "s1"

[databases.new]
host = "db.example.com"
dbname = "main_db"
username = "alice"
password_env = "NEW_DB_PW"
search_path = "s2"
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.pgadmin_path == Path("/opt/pgadmin4")
    assert cfg.ignore.whitespace is True
    assert cfg.output_filter.same is True
    assert cfg.output_filter.source_only is False
    assert cfg.old_db.database == "main_db"
    assert cfg.old_db.user == "alice"
    assert cfg.old_db.password == "secret_old"
    assert cfg.new_db.password == "secret_new"


def test_load_config_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="Unable to read"):
        load_config(tmp_path / "missing.toml")

    bad_toml = _write_toml(tmp_path, "pgadmin = [not valid")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(bad_toml)

    unknown_top = _write_toml(tmp_path, "extra = 1")
    with pytest.raises(ConfigError, match="Unknown top-level"):
        load_config(unknown_top)

    no_pgadmin = _write_toml(
        tmp_path,
        """
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="Missing pgAdmin 4 installation path"):
        load_config(no_pgadmin)

    conflicting_pgadmin = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[pgadmin]
install_path = "/b"
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="Conflicting values"):
        load_config(conflicting_pgadmin)

    bad_timeout = _write_toml(
        tmp_path,
        """
[pgadmin]
path = "/a"
timeout_seconds = true
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="must be an integer"):
        load_config(bad_timeout)

    non_pos_timeout = _write_toml(
        tmp_path,
        """
[pgadmin]
path = "/a"
timeout_seconds = 0
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="must be positive"):
        load_config(non_pos_timeout)

    both_filters = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[filter]
same = true
[output_filter]
same = false
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="either '\\[filter\\]' or '\\[output_filter\\]'"):
        load_config(both_filters)

    conflict_same = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[filter]
same = true
identical = false
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="Conflicting values for 'same' and 'identical'"):
        load_config(conflict_same)

    bad_bool = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[ignore]
owner = "yes"
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="must be a boolean"):
        load_config(bad_bool)

    unknown_key = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[ignore]
bogus = true
[databases.old]
host = "h1"
user = "u"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="Unknown configuration key"):
        load_config(unknown_key)

    monkeypatch.delenv("MISSING_PW_VAR", raising=False)
    missing_env = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[databases.old]
host = "h1"
user = "u"
password_env = "MISSING_PW_VAR"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="is not set"):
        load_config(missing_env)

    both_pw = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[databases.old]
host = "h1"
user = "u"
password = "a"
password_env = "B"
[databases.new]
host = "h2"
user = "u"
""",
    )
    with pytest.raises(ConfigError, match="either 'password' or 'password_env'"):
        load_config(both_pw)


def test_load_config_database_section_errors(tmp_path: Path) -> None:
    missing_dbs = _write_toml(tmp_path, 'pgadmin_path = "/a"\n')
    with pytest.raises(ConfigError, match="\\[databases\\]"):
        load_config(missing_dbs)

    missing_old = _write_toml(
        tmp_path,
        'pgadmin_path = "/a"\n[databases.new]\nhost="h"\nuser="u"\n',
    )
    with pytest.raises(ConfigError, match="databases.old"):
        load_config(missing_old)

    missing_new = _write_toml(
        tmp_path,
        'pgadmin_path = "/a"\n[databases.old]\nhost="h"\nuser="u"\n',
    )
    with pytest.raises(ConfigError, match="databases.new"):
        load_config(missing_new)

    missing_host = _write_toml(
        tmp_path,
        'pgadmin_path = "/a"\n[databases.old]\nuser="u"\n[databases.new]\nhost="h2"\nuser="u"\n',
    )
    with pytest.raises(ConfigError, match="Missing required key 'host'"):
        load_config(missing_host)

    missing_user = _write_toml(
        tmp_path,
        'pgadmin_path = "/a"\n[databases.old]\nhost="h1"\n[databases.new]\nhost="h2"\nuser="u"\n',
    )
    with pytest.raises(ConfigError, match="Missing required key 'user'"):
        load_config(missing_user)

    same_target = _write_toml(
        tmp_path,
        """
pgadmin_path = "/a"
[databases.old]
host = "LocalHost"
port = 5432
database = "db"
user = "u"
search_path = '"$user", public'
[databases.new]
host = "localhost"
port = 5432
database = "db"
user = "u"
search_path = "public"
""",
    )
    with pytest.raises(ConfigError, match="Old and New database targets must differ"):
        load_config(same_target)


def test_dataclass_validation() -> None:
    with pytest.raises(ConfigError, match="host"):
        DatabaseConfig(host="", user="u")
    with pytest.raises(ConfigError, match="port"):
        DatabaseConfig(host="h", port=True, user="u")  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="port"):
        DatabaseConfig(host="h", port=70000, user="u")
    with pytest.raises(ConfigError, match="database"):
        DatabaseConfig(host="h", database=" ", user="u")
    with pytest.raises(ConfigError, match="user"):
        DatabaseConfig(host="h", user="")
    with pytest.raises(ConfigError, match="password"):
        DatabaseConfig(host="h", user="u", password=123)  # type: ignore[arg-type]
    with pytest.raises(ConfigError, match="sslmode"):
        DatabaseConfig(host="h", user="u", sslmode="")
    with pytest.raises(ConfigError, match="search_path"):
        DatabaseConfig(host="h", user="u", search_path=" , ")
    with pytest.raises(ConfigError, match="concrete schema"):
        DatabaseConfig(host="h", user="u", search_path="$user")

    old = DatabaseConfig(host="h1", user="u")
    new = DatabaseConfig(host="h2", user="u")
    with pytest.raises(ConfigError, match="timeout_seconds"):
        Config(pgadmin_path=Path("/a"), old_db=old, new_db=new, timeout_seconds=-5)
    with pytest.raises(ConfigError, match="pgadmin_path"):
        Config(pgadmin_path="   ", old_db=old, new_db=new)  # type: ignore[arg-type]
