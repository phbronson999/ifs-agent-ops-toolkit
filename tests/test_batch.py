"""$batch change sets: building the request and parsing IFS's nested multipart answer."""
import json

import pytest

from ifs_agent_ops.ifs.batch import BatchRequest, build_changeset, parse_batch_response

CRLF = "\r\n"


def test_build_changeset_wraps_writes_in_one_changeset_in_order():
    ctype, body = build_changeset([
        BatchRequest("POST", "PrePostSave", {"Init": True}),
        BatchRequest("PATCH", "/Orders(OrderNo='1')/Lines(OrderNo='1',LineNo=10)", {"Resource": "R1"},
                     {"If-Match": 'W/"abc"'}),
        BatchRequest("POST", "PrePostSave", {"Init": False}),
    ], batch_boundary="b1", changeset_boundary="c1")
    assert ctype == "multipart/mixed;boundary=b1"
    assert body.startswith("--b1\r\nContent-Type: multipart/mixed;boundary=c1\r\n")
    assert body.endswith("--c1--\r\n--b1--\r\n")
    assert [line for line in body.split(CRLF) if line.startswith("Content-ID")] == \
        ["Content-ID: 1", "Content-ID: 2", "Content-ID: 3"]
    lines = body.split(CRLF)
    assert "POST PrePostSave HTTP/1.1" in lines
    assert "PATCH Orders(OrderNo='1')/Lines(OrderNo='1',LineNo=10) HTTP/1.1" in lines   # leading slash dropped
    assert 'If-Match: W/"abc"' in lines
    assert '{"Init":true}' in lines and '{"Resource":"R1"}' in lines and '{"Init":false}' in lines
    assert body.index('{"Init":true}') < body.index('{"Resource":"R1"}') < body.index('{"Init":false}')


def test_build_changeset_refuses_reads_and_empty_sets():
    with pytest.raises(ValueError):
        build_changeset([BatchRequest("GET", "Orders")])
    with pytest.raises(ValueError):
        build_changeset([])


def _response(parts: list[tuple[int, str, dict, str]]) -> tuple[str, str]:
    lines = ["--batch_x", "Content-Type: multipart/mixed; boundary=changeset_y", ""]
    for n, (status, reason, headers, body) in enumerate(parts, 1):
        lines += ["--changeset_y", "Content-Type: application/http", "Content-Transfer-Encoding: binary",
                  f"Content-ID: {n}", "", f"HTTP/1.1 {status} {reason}", "OData-Version: 4.0"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        lines += ["", body]
    lines += ["--changeset_y--", "--batch_x--", ""]
    return CRLF.join(lines), "multipart/mixed;boundary=batch_x"


def test_parse_batch_response_returns_each_changeset_part():
    entity = {"OrderNo": "1", "LineNo": "10", "Resource": "R1"}
    text, ctype = _response([
        (204, "No Content", {"Content-Length": "0"}, ""),
        (200, "OK", {"ETag": 'W/"new"', "Content-Type": "application/json"}, json.dumps(entity)),
        (204, "No Content", {"Content-Length": "0"}, ""),
    ])
    parts = parse_batch_response(text, ctype)
    assert [(p.content_id, p.status, p.ok) for p in parts] == [("1", 204, True), ("2", 200, True), ("3", 204, True)]
    assert parts[0].data is None
    assert parts[1].data == entity
    assert parts[1].headers["etag"] == 'W/"new"'


def test_parse_batch_response_reports_a_failed_part():
    err = {"error": {"code": "DATABASE_ERROR", "message": "Database error occurred."}}
    text, ctype = _response([(500, "Internal Server Error", {"Content-Type": "application/json"}, json.dumps(err))])
    parts = parse_batch_response(text, ctype)
    assert len(parts) == 1 and parts[0].status == 500 and not parts[0].ok
    assert parts[0].data == err
