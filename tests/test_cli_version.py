import pytest

from reviewctl import __version__
from reviewctl.cli import build_parser


@pytest.mark.contract
def test_version_exits_successfully_without_a_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(["--version"])

    assert result.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"reviewctl {__version__}\n"
    assert captured.err == ""
