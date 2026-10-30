from listcli.cli import main


def _file(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    return str(path)


def test_without_limit_all_lines_are_printed(tmp_path, capsys):
    assert main([_file(tmp_path)]) == 0
    assert capsys.readouterr().out == "a\nb\nc\n"


def test_limit_prints_only_the_first_lines(tmp_path, capsys):
    assert main([_file(tmp_path), "--limit", "2"]) == 0
    assert capsys.readouterr().out == "a\nb\n"
    assert main(["-n", "1", _file(tmp_path)]) == 0
    assert capsys.readouterr().out == "a\n"


def test_non_positive_limit_is_an_error(tmp_path, capsys):
    assert main([_file(tmp_path), "--limit", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--limit must be a positive integer" in captured.err
