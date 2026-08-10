import unittest

from hologram.model import (
    BodyEventKind,
    CallKind,
    FileIR,
    ReferenceKind,
    SourceSpan,
)


def _position(span: SourceSpan, *, end: bool) -> tuple[int, int]:
    return (
        (span.end_line, span.end_column)
        if end
        else (span.start_line, span.start_column)
    )


def _inside(inner: SourceSpan, outer: SourceSpan) -> bool:
    return (
        inner.file == outer.file
        and _position(outer, end=False) <= _position(inner, end=False)
        and _position(inner, end=True) <= _position(outer, end=True)
    )


def assert_body_fact_events(test: unittest.TestCase, file_ir: FileIR) -> None:
    symbol_ids = [symbol.id for symbol in file_ir.symbols]
    test.assertEqual(
        len(symbol_ids),
        len(set(symbol_ids)),
        "duplicate Symbol.id values would be lost while indexing parser output",
    )
    body_owners = [body.owner for body in file_ir.bodies]
    test.assertEqual(
        len(body_owners),
        len(set(body_owners)),
        "duplicate BodyIR.owner values would be lost while indexing parser output",
    )
    bodies = {body.owner: body for body in file_ir.bodies}
    events = {
        owner: {(event.kind, event.span) for event in body.events}
        for owner, body in bodies.items()
    }
    for call in file_ir.calls:
        body = bodies.get(call.caller)
        if body is None or not _inside(call.span, body.span):
            continue
        kind = (
            BodyEventKind.CONSTRUCT
            if call.kind is CallKind.CONSTRUCT
            else BodyEventKind.CALL
        )
        test.assertIn((kind, call.span), events[call.caller])
    for reference in file_ir.references:
        body = bodies.get(reference.owner)
        if body is None or not _inside(reference.span, body.span):
            continue
        kind = (
            BodyEventKind.TYPE
            if reference.kind is ReferenceKind.TYPE
            else BodyEventKind.NAME
        )
        test.assertIn((kind, reference.span), events[reference.owner])
