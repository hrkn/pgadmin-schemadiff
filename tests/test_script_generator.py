"""Unit tests for diff item modeling, dependency ordering, filtering, and script generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from pgadmin_schemadiff.config import OutputFilterOptions
from pgadmin_schemadiff.exceptions import PgAdminRuntimeError
from pgadmin_schemadiff.script_generator import (
    SCRIPT_HEADER,
    ApplyResult,
    DiffItem,
    DiffResult,
    DiffStatus,
    compute_depend_levels,
    filter_items,
    generate_diff_script,
)


def test_diff_item_from_dict_and_to_dict() -> None:
    item = DiffItem.from_pgadmin_dict(
        {
            "id": 1,
            "type": "table",
            "label": "Tables",
            "title": "users",
            "oid": 100,
            "status": "Source Only",
            "source_ddl": "CREATE TABLE users();",
            "target_ddl": "",
            "diff_ddl": "CREATE TABLE users();",
            "dependencies": [{"oid": 50}],
            "source_schema_name": '"s1"',
        }
    )
    assert item.status == DiffStatus.SOURCE_ONLY
    serialized = item.to_dict()
    assert serialized["node_type"] == "table"
    assert serialized["status"] == "Source Only"

    with pytest.raises(PgAdminRuntimeError, match="Unexpected Schema Diff status"):
        DiffItem.from_pgadmin_dict({"id": 2, "status": "Unknown"})


def test_compute_depend_levels_chain_diamond_cycle_and_external() -> None:
    # A (oid 10) <- B (oid 20, depends on 10) <- C (oid 30, depends on 20 and 10)
    # D (oid 40) <-> E (oid 50) circular dependency
    # F (oid 60) depends on external oid 999
    a = DiffItem(id=1, node_type="table", label="Tables", title="a", status=DiffStatus.SOURCE_ONLY, oid=10)
    b = DiffItem(
        id=2,
        node_type="view",
        label="Views",
        title="b",
        status=DiffStatus.SOURCE_ONLY,
        oid=20,
        dependencies=[{"oid": 10}],
    )
    c = DiffItem(
        id=3,
        node_type="view",
        label="Views",
        title="c",
        status=DiffStatus.SOURCE_ONLY,
        oid=30,
        dependencies=[{"oid": 20}, {"oid": 10}],
    )
    d = DiffItem(
        id=4,
        node_type="function",
        label="Functions",
        title="d",
        status=DiffStatus.SOURCE_ONLY,
        oid=40,
        dependencies=[{"oid": 50}],
    )
    e = DiffItem(
        id=5,
        node_type="function",
        label="Functions",
        title="e",
        status=DiffStatus.SOURCE_ONLY,
        oid=50,
        dependencies=[{"oid": 40}],
    )
    f = DiffItem(
        id=6,
        node_type="view",
        label="Views",
        title="f",
        status=DiffStatus.SOURCE_ONLY,
        oid=60,
        dependencies=[{"oid": 999}],
    )

    compute_depend_levels([a, b, c, d, e, f])
    assert a.depend_level == 3
    assert b.depend_level == 2
    assert c.depend_level == 1
    assert d.depend_level >= 1 and e.depend_level >= 1
    assert f.depend_level == 1


def test_filter_and_generate_diff_script() -> None:
    tbl = DiffItem(
        id=1,
        node_type="table",
        label="Tables",
        title="parent",
        status=DiffStatus.SOURCE_ONLY,
        oid=10,
        diff_ddl="CREATE TABLE parent (id int);",
        source_schema_name='"app"',
    )
    vw = DiffItem(
        id=2,
        node_type="view",
        label="Views",
        title="child_v",
        status=DiffStatus.SOURCE_ONLY,
        oid=20,
        diff_ddl="CREATE VIEW child_v AS SELECT * FROM parent;",
        dependencies=[{"oid": 10}],
    )
    alt = DiffItem(
        id=3,
        node_type="table",
        label="Tables",
        title="modified",
        status=DiffStatus.DIFFERENT,
        oid=30,
        diff_ddl="ALTER TABLE modified ADD COLUMN x int;",
    )
    drp = DiffItem(
        id=4,
        node_type="table",
        label="Tables",
        title="legacy",
        status=DiffStatus.TARGET_ONLY,
        oid=40,
        diff_ddl="DROP TABLE legacy;",
    )
    same = DiffItem(
        id=5,
        node_type="table",
        label="Tables",
        title="unchanged",
        status=DiffStatus.IDENTICAL,
        oid=50,
    )

    all_items = [vw, tbl, alt, drp, same]
    flt = OutputFilterOptions(source_only=True, target_only=False, different=True, same=True)
    filtered = filter_items(all_items, flt)
    assert [i.title for i in filtered] == ["child_v", "parent", "modified", "unchanged"]

    script = generate_diff_script(all_items, flt)
    assert script.startswith(SCRIPT_HEADER + "BEGIN;\n")
    assert script.endswith("END;\n")
    assert 'CREATE SCHEMA IF NOT EXISTS "app";' in script
    assert "CREATE TABLE parent (id int);" in script
    assert "CREATE VIEW child_v" in script
    assert "ALTER TABLE modified ADD COLUMN x int;" in script
    assert "DROP TABLE legacy;" not in script
    # parent (level 2) must appear before child_v (level 1)
    assert script.index("CREATE TABLE parent") < script.index("CREATE VIEW child_v")

    empty_script = generate_diff_script([same], OutputFilterOptions(same=True))
    assert empty_script == f"{SCRIPT_HEADER}BEGIN;\n\nEND;\n"

    result = DiffResult(script=script, items=filtered, all_items=all_items)
    result_dict = result.to_dict()
    assert "apply" not in result_dict
    assert "full_dump_path" not in result_dict
    summary = result_dict["summary"]
    assert summary == {
        "total": 5,
        "filtered": 4,
        "source_only": 2,
        "target_only": 1,
        "different": 1,
        "identical": 1,
    }

    applied_ok = ApplyResult(succeeded=True)
    applied_err = ApplyResult(succeeded=False, error="syntax error")
    assert applied_ok.to_dict() == {"succeeded": True, "error": None}
    assert applied_err.to_dict() == {"succeeded": False, "error": "syntax error"}

    extended = DiffResult(
        script=script,
        items=filtered,
        all_items=all_items,
        apply_result=applied_err,
        full_dump_path=Path("out/full_dump.sql"),
    ).to_dict()
    assert extended["apply"] == {"succeeded": False, "error": "syntax error"}
    assert extended["full_dump_path"] == str(Path("out/full_dump.sql"))
