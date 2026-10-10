"""Tests for incident_suite/parsing/readers.py."""

from __future__ import annotations

import gzip
import io
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from incident_suite.parsing.readers import (
    TRUNCATION_MARK,
    ContainerFormat,
    ReadError,
    ReadLimits,
    StructuredRecord,
    TextRecord,
    decode_text,
    detect_container,
    flatten,
    read_bytes,
    to_text,
    truncate,
)

CSV = b"timestamp,level,service,message\n2026-10-09T10:00:00Z,ERROR,api,boom\n"


def _parquet(rows: list[dict[str, object]]) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "filename", "expected"),
    [
        (b"2026-10-09 ERROR boom\n", "a.log", ContainerFormat.TEXT),
        (b'{"msg": "a"}\n{"msg": "b"}\n', "a.jsonl", ContainerFormat.JSON_LINES),
        (b'[{"msg": "a"}]', "a.json", ContainerFormat.JSON_DOCUMENT),
        (b'{\n  "logs": [{"msg": "a"}]\n}', "a.json", ContainerFormat.JSON_DOCUMENT),
        (b'{"logs": [{"msg": "a"}, {"msg": "b"}]}', "a.json", ContainerFormat.JSON_DOCUMENT),
        (CSV, "a.csv", ContainerFormat.CSV),
        (b"time\tlvl\tmsg\n1\tINFO\tok\n", "a.tsv", ContainerFormat.TSV),
        (b"PAR1....", "a.parquet", ContainerFormat.PARQUET),
    ],
)
def test_detect_container(data: bytes, filename: str, expected: ContainerFormat) -> None:
    assert detect_container(data, filename) is expected


def test_content_wins_over_extension() -> None:
    assert detect_container(CSV, "renamed.log") is ContainerFormat.CSV
    assert detect_container(b'{"msg": "a"}\n', "data.csv") is ContainerFormat.JSON_LINES


def test_log_lines_with_commas_are_not_csv() -> None:
    data = b"2026-10-09 ERROR db, cache, queue failed\n2026-10-09 INFO all good, really\n"
    assert detect_container(data, "a.log") is ContainerFormat.TEXT


def test_text_keeps_line_numbers_and_skips_blanks() -> None:
    result = read_bytes(b"first\n\nthird\n", "a.log")
    assert result.container is ContainerFormat.TEXT
    assert result.records == [TextRecord(1, "first"), TextRecord(3, "third")]
    assert result.note == "Detected text"


def test_bad_bytes_become_replacement_characters() -> None:
    record = read_bytes(b"caf\xff error\n", "a.log").records[0]
    assert isinstance(record, TextRecord)
    assert "�" in record.text


def test_utf16_with_bom() -> None:
    assert decode_text("hello".encode("utf-16")) == "hello"


def test_long_lines_are_cut() -> None:
    limits = ReadLimits(max_line_chars=50)
    record = read_bytes(b"x" * 1000, "a.log", limits).records[0]
    assert isinstance(record, TextRecord)
    assert len(record.text) == 50
    assert record.text.endswith(TRUNCATION_MARK)


def test_truncate_leaves_short_text() -> None:
    assert truncate("short", 10) == "short"


def test_json_lines_with_a_non_json_line() -> None:
    result = read_bytes(b'{"msg": "a", "http": {"status": 502}}\nnot json\n', "a.jsonl")
    first, second = result.records
    assert isinstance(first, StructuredRecord)
    assert first.fields == {"msg": "a", "http.status": "502"}
    assert isinstance(second, TextRecord)
    assert result.warnings == ["1 lines were not JSON objects and were read as text."]


def test_json_document_items_that_are_not_objects() -> None:
    record = read_bytes(b'["plain message"]', "a.json").records[0]
    assert isinstance(record, StructuredRecord)
    assert record.fields == {"message": "plain message"}


def test_json_object_without_a_list_is_one_record() -> None:
    result = read_bytes(b'{\n"message": "only one"\n}', "a.json")
    assert len(result.records) == 1


def test_invalid_json_document() -> None:
    with pytest.raises(ReadError, match="Invalid JSON"):
        read_bytes(b'[{"msg": "a"},', "a.json")


def test_json_nesting_is_flattened_to_the_depth_limit() -> None:
    nested = {"a": {"b": {"c": {"d": 1}}}}
    assert flatten(nested, max_depth=2) == {"a.b": {"c": {"d": 1}}}
    assert to_text({"c": 1}) == '{"c":1}'


@pytest.mark.parametrize(
    ("value", "expected"), [(None, ""), (True, "true"), (3, "3"), ([1, 2], "[1,2]")]
)
def test_to_text(value: object, expected: str) -> None:
    assert to_text(value) == expected


def test_csv_header_and_rows() -> None:
    result = read_bytes(CSV, "a.csv")
    assert result.columns == ["timestamp", "level", "service", "message"]
    assert result.note == "Detected CSV, 4 columns"
    record = result.records[0]
    assert isinstance(record, StructuredRecord)
    assert record.line_no == 1
    assert record.fields["message"] == "boom"


def test_malformed_csv_rows_are_kept_and_reported() -> None:
    data = b"time,level,message\n1,INFO,ok\n2,ERROR,too,many,cells\n3,WARN\n"
    result = read_bytes(data, "a.csv")
    assert len(result.records) == 3
    extra = result.records[1]
    assert isinstance(extra, StructuredRecord)
    assert extra.fields["_extra"] == "many,cells"
    assert result.warnings == ["2 rows had a different number of columns than the header."]


def test_csv_by_extension_when_header_is_unusual() -> None:
    data = b"1st col,2nd col\nhello,world\n"
    result = read_bytes(data, "odd.csv")
    assert result.container is ContainerFormat.CSV


def test_too_many_columns() -> None:
    header = ",".join(f"c{i}" for i in range(5)).encode()
    with pytest.raises(ReadError, match="columns"):
        read_bytes(header + b"\n" + b"1,2,3,4,5\n", "a.csv", ReadLimits(max_columns=4))


def test_too_many_lines() -> None:
    with pytest.raises(ReadError, match="more than 3"):
        read_bytes(b"a\nb\nc\nd\n", "a.log", ReadLimits(max_records=3))


def test_parquet_rows() -> None:
    data = _parquet([{"level": "ERROR", "msg": "boom", "count": 3}, {"level": "INFO", "msg": "ok"}])
    result = read_bytes(data, "a.parquet")
    assert result.container is ContainerFormat.PARQUET
    assert result.columns == ["level", "msg", "count"]
    first, second = result.records
    assert isinstance(first, StructuredRecord)
    assert isinstance(second, StructuredRecord)
    assert first.fields == {"level": "ERROR", "msg": "boom", "count": "3"}
    assert second.line_no == 2
    assert second.fields["count"] == ""


def test_parquet_with_too_many_rows() -> None:
    data = _parquet([{"msg": str(i)} for i in range(10)])
    with pytest.raises(ReadError, match="more than 5"):
        read_bytes(data, "a.parquet", ReadLimits(max_records=5))


def test_damaged_parquet() -> None:
    with pytest.raises(ReadError, match="Parquet"):
        read_bytes(b"PAR1 this is not parquet", "a.parquet")


def test_gzip_is_read_as_the_inner_format() -> None:
    result = read_bytes(gzip.compress(CSV), "a.csv.gz")
    assert result.container is ContainerFormat.CSV
    assert result.compressed
    assert result.note == "Detected CSV (gzip), 4 columns"


def test_gzip_over_the_size_cap_is_refused() -> None:
    bomb = gzip.compress(b"A" * 2_000_000)
    with pytest.raises(ReadError, match="decompressed"):
        read_bytes(bomb, "bomb.log.gz", ReadLimits(max_decompressed_bytes=1_000_000))


def test_damaged_gzip() -> None:
    with pytest.raises(ReadError, match="damaged"):
        read_bytes(b"\x1f\x8b\x08\x00garbage", "a.log.gz")


def test_nested_gzip_is_refused() -> None:
    with pytest.raises(ReadError, match="inside gzip"):
        read_bytes(gzip.compress(gzip.compress(b"hello")), "a.log.gz")


def test_plain_file_over_the_size_cap() -> None:
    with pytest.raises(ReadError, match="too large"):
        read_bytes(b"x" * 101, "a.log", ReadLimits(max_decompressed_bytes=100))


@pytest.mark.parametrize("data", [b"", b"   \n\n"])
def test_empty_file(data: bytes) -> None:
    with pytest.raises(ReadError, match="empty"):
        read_bytes(data, "a.log")


def test_deeply_nested_json_does_not_crash() -> None:
    data = ("[" * 100_000 + "]" * 100_000).encode()
    with pytest.raises(ReadError, match="nested too deeply"):
        read_bytes(data, "deep.json")


def test_json_lines_record_with_too_many_fields() -> None:
    record = json.dumps({f"k{i}": i for i in range(10)}).encode()
    with pytest.raises(ReadError, match="fields"):
        read_bytes(record + b"\n", "a.jsonl", ReadLimits(max_columns=5))
