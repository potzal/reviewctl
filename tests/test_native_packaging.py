from __future__ import annotations

import io
import os
import runpy
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

NATIVE = Path(__file__).parents[1] / "packaging" / "native"
pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "body, expected", [("return 7", 7), ("return None", 0), ("raise SystemExit(3)", 3)]
)
def test_native_entrypoint_preserves_console_script_exit_status(tmp_path, body, expected):
    package = tmp_path / "reviewctl"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(f"def main():\n    {body}\n")
    result = subprocess.run(
        [sys.executable, str(NATIVE / "entrypoint.py")],
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr


def native_archive(tmp_path, *, packaging, dependency=False, data=False):
    path = tmp_path / "source.tar"
    files = {
        "pyproject.toml": (
            '[project]\nname="reviewctl"\nversion="0.3.3"\nrequires-python=">=3.14"\n'
            + ('dependencies=["example==1"]\n' if dependency else "dependencies=[]\n")
        ).encode(),
        "src/reviewctl/__init__.py": b"",
    }
    if packaging:
        files.update(
            {f"packaging/native/{p.name}": p.read_bytes() for p in NATIVE.iterdir() if p.is_file()}
        )
    if packaging == "dirty":
        files["packaging/native/entrypoint.py"] += b"\n# changed packaging input\n"
    if data:
        files["src/reviewctl/contract.json"] = b"{}"
    with tarfile.open(
        path, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "a" * 40}
    ) as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


@pytest.mark.parametrize(
    "packaging, dependency, data, external, expected",
    [
        (False, False, False, False, "packaging inputs do not match"),
        ("dirty", False, False, False, "packaging inputs do not match"),
        (True, True, False, False, "runtime dependencies"),
        (True, False, True, False, "package data"),
        (False, True, False, True, "runtime dependencies"),
    ],
)
def test_build_fails_before_installing_unqualified_inputs(
    tmp_path, monkeypatch, capsys, packaging, dependency, data, external, expected
):
    archive = native_archive(tmp_path, packaging=packaging, dependency=dependency, data=data)
    output = tmp_path / "build"
    arguments = [
        str(NATIVE / "build-native.py"),
        "--archive",
        str(archive),
        "--revision",
        "a" * 40,
        "--output",
        str(output),
    ]
    if external:
        arguments.append("--allow-external-packaging")
    main = runpy.run_path(str(NATIVE / "build-native.py"))["main"]

    def no_build(*_args, **_kwargs):
        pytest.fail("unqualified archive reached venv creation or dependency installation")

    monkeypatch.setitem(main.__globals__, "run", no_build)
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert expected in capsys.readouterr().err
    assert not (output / ".venv").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux strace contract")
def test_required_linux_trace_fails_before_running_binary_when_strace_is_missing(tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(NATIVE / "smoke-native.py"),
            "--binary",
            str(tmp_path / "unused"),
            "--output",
            str(tmp_path / "smoke"),
            "--expected-version",
            "0.3.3",
            "--require-trace",
        ],
        env={**os.environ, "PATH": str(empty)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "strace is required" in result.stderr
    assert not (tmp_path / "smoke").exists()


@pytest.mark.parametrize(
    "extra, remove_python",
    [
        ('openat(AT_FDCWD, "/usr/lib/libpython3.14.so.1.0", O_RDONLY) = 4\n', False),
        ('openat(AT_FDCWD, "/usr/lib/python3.14/os.py", O_RDONLY) = 4\n', False),
        ("", True),
    ],
)
def test_linux_trace_rejects_external_or_missing_python(extra, remove_python):
    validate = runpy.run_path(str(NATIVE / "smoke-native.py"))["validate_linux_trace"]
    embedded = 'openat(AT_FDCWD, "/tmp/_MEIfixture/libpython3.14.so.1.0", O_RDONLY) = 3\n'
    base_library = 'openat(AT_FDCWD, "/tmp/_MEIfixture/base_library.zip", O_RDONLY) = 3\n'
    valid = embedded + base_library
    validate(valid, Path("/binary"))
    with pytest.raises(AssertionError):
        validate((base_library if remove_python else valid) + extra, Path("/binary"))
