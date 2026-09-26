"""Verify source and both installed distributions; never publish anything."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run(
    args: list[str], cwd: Path, *, cli: Path | None = None, example: Path | None = None
) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYMCDX_TEST_COMMAND", None)
    env.pop("PYMCDX_TEST_EXAMPLE", None)
    if cli is not None:
        env["PYMCDX_TEST_COMMAND"] = str(cli)
    if example is not None:
        env["PYMCDX_TEST_EXAMPLE"] = str(example)
    print("+", *args, flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)  # noqa: S603


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    run(["uv", "sync", "--locked"], root)
    for args in (
        ["ruff", "format", "--check", "."],
        ["ruff", "check", "."],
        ["pyright"],
        ["pytest", "-q"],
    ):
        run(["uv", "run", "--no-sync", *args], root)
    with tempfile.TemporaryDirectory(prefix="worksheet-release-") as temporary:
        scratch = Path(temporary)
        artifacts = scratch / "artifacts"
        run(["uv", "build", "--out-dir", str(artifacts)], root)
        wheel = list(artifacts.glob("*.whl"))
        sdist = list(artifacts.glob("*.tar.gz"))
        if len(wheel) != 1 or len(sdist) != 1:
            raise RuntimeError("expected exactly one wheel and one sdist")
        extracted = scratch / "sdist-source"
        with tarfile.open(sdist[0]) as archive:
            archive.extractall(extracted, filter="data")
        roots = list(extracted.iterdir())
        if len(roots) != 1 or not (roots[0] / "examples/basic.yaml").is_file():
            raise RuntimeError("sdist must include its synthetic examples/basic.yaml")
        sdist_example = roots[0] / "examples/basic.yaml"
        for index, artifact in enumerate([*wheel, *sdist]):
            environment = scratch / f"installed-{index}"
            # Pin the verifying interpreter: a bare `uv venv` picks the host default,
            # which may be older than requires-python and then fails to install.
            run(["uv", "venv", "--python", sys.executable, str(environment)], scratch)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            run(["uv", "pip", "install", "--python", str(python), str(artifact), "pytest"], scratch)
            run(
                [
                    str(python),
                    "-c",
                    "import pathlib,pkgutil,importlib,pymcdx; "
                    f"assert pathlib.Path(pymcdx.__file__).is_relative_to({str(environment)!r}); "
                    "[importlib.import_module(m.name) for m in "
                    "pkgutil.walk_packages(pymcdx.__path__, pymcdx.__name__+'.')]",
                ],
                scratch,
            )
            cli = python.with_name("pymcdx.exe" if os.name == "nt" else "pymcdx")
            run(
                [str(python), "-m", "pytest", str(root / "tests"), "-q"],
                scratch,
                cli=cli,
                example=sdist_example if artifact == sdist[0] else None,
            )
    print("Source, wheel and sdist checks passed. No publication performed.")


if __name__ == "__main__":
    main()
