"""Reading a Parquet file's fields from its footer alone, checked against files pyarrow writes."""
import io

import pytest

from odcl import parquet

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")


def written(table) -> bytes:
    out = io.BytesIO()
    pq.write_table(table, out)
    return out.getvalue()


def test_fields_and_types_come_from_the_footer():
    table = pa.table({
        "employee_id": pa.array(["E-1"]), "hours_at_cutoff": pa.array([40.5]), "shifts": pa.array([3], pa.int64()),
        "pay_period_start": pa.array([pa.scalar(19600, pa.date32()).as_py()]), "is_train": pa.array([True]),
        "loaded_at": pa.array([1], pa.timestamp("us", tz="UTC")), "amount": pa.array([1], pa.decimal128(9, 2)),
        "shift": pa.array([{"site": "A", "minutes": 30}]), "tags": pa.array([["x"]]),
    })
    data = written(table)
    assert parquet.fields(data[-parquet.footer_size(data[-8:]):]) == [
        ("employee_id", "STRING"), ("hours_at_cutoff", "DOUBLE"), ("shifts", "INT64"), ("pay_period_start", "DATE"), ("is_train", "BOOLEAN"),
        ("loaded_at", "TIMESTAMP"), ("amount", "DECIMAL"), ("shift.site", "STRING"), ("shift.minutes", "INT64"), ("tags", "LIST")]


def test_names_are_lower_cased_as_the_catalog_keys_fields():
    data = written(pa.table({"orgId": pa.array([1])}))
    assert parquet.fields(data[-parquet.footer_size(data[-8:]):]) == [("orgid", "INT64")]


def test_what_is_not_parquet_is_refused():
    with pytest.raises(ValueError):
        parquet.footer_size(b"not parquet at all")
    data = written(pa.table({"a": [1]}))
    with pytest.raises(ValueError):
        parquet.fields(data[-20:])  # a tail too short to hold the footer
