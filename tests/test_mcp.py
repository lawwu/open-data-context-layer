import json
from pathlib import Path

import pytest

from fathom_data import Fathom
from fathom_data.dbt import dbt_entries
from fathom_data.mcp import Tools, build

pytest.importorskip("mcp")
import anyio
from mcp import Client

DBT = Path(__file__).parent / "fixtures" / "dbt_medallion" / "target"


def test_an_assistant_finds_data_and_records_use_over_mcp(tmp_path):
    async def session():
        with Fathom(tmp_path / "f.db", actor="assistant") as fathom:
            fathom.apply(dbt_entries(DBT))
            async with Client(build(Tools(fathom))) as reader:
                names = [t.name for t in (await reader.list_tools()).tools]
                assert names == ["find_data", "get_entry", "trace_lineage", "used_by", "project_uses"]  # no writes unless allowed
                found = json.loads((await reader.call_tool("find_data", {"query": "tenure"})).content[0].text)
                assert found["results"][0]["key"].endswith("tenure_months")
                refused = await reader.call_tool("record_use", {"project": "p", "keys": ["x"]})
                assert refused.is_error and "writes are off" in refused.content[0].text
            async with Client(build(Tools(fathom, allow_writes=True))) as writer:
                await writer.call_tool("register_project", {"name": "churn-model", "description": "Predict churn"})
                key = found["results"][0]["key"]
                used = await writer.call_tool("record_use", {"project": "churn-model", "keys": [key], "purpose": "a feature"})
                assert not used.is_error
                by = json.loads((await writer.call_tool("used_by", {"key": key})).content[0].text)
                assert [p["project"] for p in by["projects"]] == ["project:churn-model"]
                wrong = await writer.call_tool("used_by", {"key": "nothing.here", "extra": 1})
                assert wrong.is_error and "takes no argument extra" in wrong.content[0].text
    anyio.run(session)
