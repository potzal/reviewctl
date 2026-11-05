"""Build natively from a committed git archive into a NEW private output directory."""

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path


def run(*command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source_input = parser.add_mutually_exclusive_group(required=True)
    source_input.add_argument("--repo", type=Path)
    source_input.add_argument("--archive", type=Path, help="git archive with commit pax header")
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--allow-external-packaging",
        action="store_true",
        help="allow packaging inputs outside this revision for exploratory proofs only",
    )
    args = parser.parse_args()
    if sys.version_info < (3, 14):  # noqa: UP036 - may be invoked outside the package venv
        parser.error("build requires Python 3.14 or later")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    integration = Path(__file__).resolve().parent
    if args.repo:
        commit = run(
            "git",
            "-C",
            str(args.repo),
            "rev-parse",
            "--verify",
            f"{args.revision}^{{commit}}",
            capture_output=True,
            text=True,
        ).stdout.strip()
        archive = run("git", "-C", str(args.repo), "archive", commit, capture_output=True).stdout
    else:
        commit = args.revision
        archive = args.archive.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        if stream.pax_headers.get("comment") != commit:
            parser.error("git archive commit header does not match --revision")
    (output / "source.tar").write_bytes(archive)
    source = output / "source"
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter="data")
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    packaging_files = (
        "build-native.py",
        "build-requirements.txt",
        "entrypoint.py",
        "smoke-native.py",
    )
    packaging_matches = all(
        (source / "packaging/native" / name).is_file()
        and (source / "packaging/native" / name).read_bytes() == (integration / name).read_bytes()
        for name in packaging_files
    )
    if not packaging_matches and not args.allow_external_packaging:
        parser.error("packaging inputs do not match the archived revision; commit them first")
    if project.get("dependencies"):
        parser.error("runtime dependencies require a pinned native dependency and bundling policy")
    if any(
        path.is_file() and path.suffix != ".py" for path in (source / "src/reviewctl").rglob("*")
    ):
        parser.error("package data require explicit bundling and representative smoke checks")
    venv_dir = output / ".venv"
    run(str(Path(sys.executable).resolve()), "-m", "venv", str(venv_dir))
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = integration / "build-requirements.txt"
    run(
        str(python),
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "-r",
        str(requirements),
    )
    frozen = run(str(python), "-m", "pip", "freeze", "--all", capture_output=True, text=True).stdout
    (output / "build-freeze.txt").write_text(frozen)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source / "src")
    environment.pop("PYTHONHOME", None)
    environment["PYTHONHASHSEED"] = "0"
    environment["PYINSTALLER_CONFIG_DIR"] = str(output / "pyinstaller-cache")
    run(
        str(python),
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--noupx",
        "--name",
        "reviewctl",
        "--paths",
        str(source / "src"),
        "--collect-submodules",
        "reviewctl",
        "--distpath",
        str(output / "dist"),
        "--workpath",
        str(output / "build"),
        "--specpath",
        str(output),
        str(integration / "entrypoint.py"),
        env=environment,
        cwd=output,
    )
    binary = output / "dist" / ("reviewctl.exe" if os.name == "nt" else "reviewctl")
    shutil.copy2(source / "LICENSE", binary.parent / "LICENSE")
    metadata = {
        "sourceCommit": commit,
        "packagingMatchesSource": packaging_matches,
        "projectVersion": project["version"],
        "requiresPython": project["requires-python"],
        "sourceArchiveSha256": sha256(output / "source.tar"),
        "binarySha256": sha256(binary),
        "binaryBytes": binary.stat().st_size,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "libc": platform.libc_ver(),
        "buildDependencies": frozen.splitlines(),
        "buildScriptSha256": sha256(Path(__file__)),
        "entrypointSha256": sha256(integration / "entrypoint.py"),
        "requirementsSha256": sha256(requirements),
        "smokeScriptSha256": sha256(integration / "smoke-native.py"),
        "notice": "No publisher signature. Hashes do not establish identity or provenance.",
    }
    (output / "build-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    shutil.copy2(Path(__file__), output / "build-native-executed.py")
    shutil.copy2(requirements, output / "build-requirements-executed.txt")
    shutil.copy2(integration / "entrypoint.py", output / "entrypoint-executed.py")
    shutil.copy2(integration / "smoke-native.py", output / "smoke-native-executed.py")
    (binary.parent / "SHA256SUMS").write_text(f"{sha256(binary)}  {binary.name}\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
