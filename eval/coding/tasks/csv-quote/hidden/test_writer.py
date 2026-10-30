from csvlite.writer import write_row


def test_plain_fields_are_unchanged():
    assert write_row(["a", 1]) == "a,1"


def test_comma_quote_and_newline_are_quoted():
    assert write_row(["a,b", "c"]) == '"a,b",c'
    assert write_row(['say "hi"']) == '"say ""hi"""'
    assert write_row(["x\ny"]) == '"x\ny"'
