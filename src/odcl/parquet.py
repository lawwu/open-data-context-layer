"""A Parquet file's fields, read from its footer alone.

A Parquet file ends with its metadata, the metadata's length (4 bytes, little-endian) and `PAR1`. The metadata is a
Thrift compact-protocol `FileMetaData` whose second field is the schema: a flattened tree of `SchemaElement`s. Only
that list is decoded, so reading a file's fields takes a range read of its last bytes and no Parquet library.
See https://github.com/apache/parquet-format.
"""
from __future__ import annotations

MAGIC = b"PAR1"
PHYSICAL = {0: "BOOLEAN", 1: "INT32", 2: "INT64", 3: "INT96", 4: "FLOAT", 5: "DOUBLE", 6: "BYTE_ARRAY", 7: "FIXED_LEN_BYTE_ARRAY"}
LOGICAL = {1: "STRING", 2: "MAP", 3: "LIST", 4: "ENUM", 5: "DECIMAL", 6: "DATE", 7: "TIME", 8: "TIMESTAMP", 11: "NULL", 12: "JSON",
           13: "BSON", 14: "UUID", 15: "FLOAT16", 16: "VARIANT", 17: "GEOMETRY", 18: "GEOGRAPHY"}  # 10, INTEGER, says less than the physical type
CONVERTED = {0: "STRING", 1: "MAP", 3: "LIST", 4: "ENUM", 5: "DECIMAL", 6: "DATE", 7: "TIME", 8: "TIME", 9: "TIMESTAMP", 10: "TIMESTAMP",
             19: "JSON", 20: "BSON"}


def footer_size(last8: bytes) -> int:
    """How many bytes from the end of the file hold its metadata, given the file's last 8 bytes."""
    if len(last8) < 8 or last8[-4:] != MAGIC:
        raise ValueError("not a Parquet file: it does not end with PAR1")
    return int.from_bytes(last8[-8:-4], "little") + 8


def fields(tail: bytes) -> list[tuple[str, str]]:
    """(field path, type) for every field, in schema order, from the file's last `footer_size` (or more) bytes.
    Nested struct fields are dotted paths; a list or map is one field. Names are lower-cased."""
    return metadata(tail)[0]


def metadata(tail: bytes) -> tuple[list[tuple[str, str]], int | None]:
    """The fields, as `fields` gives them, and the number of rows the footer reports."""
    size = footer_size(tail[-8:])
    if len(tail) < size:
        raise ValueError(f"the footer needs the last {size} bytes of the file, not {len(tail)}")
    elements, rows = _file_metadata(_Reader(tail[-size:-8]))
    if not elements:
        raise ValueError("the footer holds no schema")
    out: list[tuple[str, str]] = []
    _walk(elements, 1, elements[0].get("num_children", 0), "", out)
    return out, rows


def _kind(e: dict) -> str | None:
    return LOGICAL.get(e.get("logical")) or CONVERTED.get(e.get("converted"))


def _walk(elements: list[dict], i: int, count: int, prefix: str, out: list | None) -> int:
    for _ in range(count):
        if i >= len(elements):
            raise ValueError("the schema is cut short")
        e, i = elements[i], i + 1
        name, children = prefix + e.get("name", "").lower(), e.get("num_children", 0)
        if children:
            if _kind(e) in ("LIST", "MAP") or out is None:
                if out is not None:
                    out.append((name, _kind(e)))
                i = _walk(elements, i, children, "", None)
            else:
                i = _walk(elements, i, children, name + ".", out)
        elif out is not None:
            out.append((name, _kind(e) or PHYSICAL.get(e.get("type"), "UNKNOWN")))
    return i


def _file_metadata(r: "_Reader") -> tuple[list[dict], int | None]:
    """FileMetaData's schema (field 2) and num_rows (field 3); the fields after them are not read."""
    elements, rows = [], None
    for field, kind in r.struct():
        if field == 2 and kind == 9:
            size, _ = r.list_header()
            elements = [_element(r) for _ in range(size)]
        elif field == 3 and kind == 6:
            rows = r.zigzag()
            return elements, rows
        else:
            r.skip(kind)
    return elements, rows


def _element(r: "_Reader") -> dict:
    e: dict = {}
    for field, kind in r.struct():
        if field in (1, 3, 5, 6) and kind == 5:
            e[{1: "type", 3: "repetition", 5: "num_children", 6: "converted"}[field]] = r.zigzag()
        elif field == 4 and kind == 8:
            e["name"] = r.binary().decode("utf-8", "replace")
        elif field == 10 and kind == 12:  # LogicalType is a union: the field present names it
            for logical, inner in r.struct():
                e["logical"] = logical
                r.skip(inner)
        else:
            r.skip(kind)
    return e


class _Reader:
    """Just enough of the Thrift compact protocol to read and skip values."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise ValueError("the footer is cut short")
        self.pos += 1
        return self.data[self.pos - 1]

    def varint(self) -> int:
        shift = value = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def binary(self) -> bytes:
        n = self.varint()
        if self.pos + n > len(self.data):
            raise ValueError("the footer is cut short")
        self.pos += n
        return self.data[self.pos - n:self.pos]

    def list_header(self) -> tuple[int, int]:
        b = self.byte()
        size = b >> 4
        return (self.varint() if size == 15 else size), b & 0x0F

    def struct(self):
        """(field id, type) for each field of a struct; the caller reads or skips each value before the next."""
        last = 0
        while True:
            b = self.byte()
            if b == 0:
                return
            delta = b >> 4
            last = last + delta if delta else self.zigzag()
            yield last, b & 0x0F

    def skip(self, kind: int) -> None:
        if kind in (1, 2):  # a boolean field carries its value in its type
            return
        if kind == 3:
            self.byte()
        elif kind in (4, 5, 6):
            self.varint()
        elif kind == 7:
            self.pos += 8
        elif kind == 8:
            self.binary()
        elif kind in (9, 10):
            size, element = self.list_header()
            for _ in range(size):
                self.byte() if element in (1, 2) else self.skip(element)
        elif kind == 11:
            size = self.varint()
            if size:
                kinds = self.byte()
                for _ in range(size):
                    for k in (kinds >> 4, kinds & 0x0F):
                        self.byte() if k in (1, 2) else self.skip(k)
        elif kind == 12:
            for _, inner in self.struct():
                self.skip(inner)
        else:
            raise ValueError(f"the footer holds a Thrift type {kind} this reader does not know")
