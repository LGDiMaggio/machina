"""Install the built wheel and sdist into fresh venvs and smoke them.

    python -m build
    python scripts/audit/package_smoke.py --dist dist --out audit-results/result.json

Every venv lives in a temporary directory outside the checkout and every
command runs from there without PYTHONPATH, so nothing imports machina from
src/. For the wheel and for the sdist: install it, run ``machina describe
--json`` and import the public API. Then, for each extra the wheel's metadata
declares, install ``wheel[extra]`` into its own fresh venv and import every
top-level module the extra's requirements provide. Each venv is deleted after
its checks, because the RAG extras pull in several GB.
"""

from __future__ import annotations

import argparse
import contextlib
import email
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from record import finding, write_result

if TYPE_CHECKING:
    from collections.abc import Iterator

PUBLIC_API = """
import importlib.metadata, machina
missing = [name for name in machina.__all__ if not hasattr(machina, name)]
assert not missing, f"machina.__all__ names that do not resolve: {missing}"
assert "site-packages" in machina.__file__, f"imported from {machina.__file__}"
assert machina.__version__ == importlib.metadata.version("machina-ai"), machina.__version__
print(f"{len(machina.__all__)} public names, version {machina.__version__}")
"""

# Runs inside the extra's venv; argv[1] is the extra. Nested machina-ai[...]
# requirements (iot, calendar, docs-rag-pro, all) are followed recursively.
EXTRA_IMPORTS = r"""
import importlib, importlib.metadata as md, re, sys
import machina

def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()

def requirements(extra, seen):
    for requirement in md.requires("machina-ai") or []:
        spec, _, marker = requirement.partition(";")
        if not re.search(r"extra\s*==\s*['\"]%s['\"]" % re.escape(extra), marker):
            continue
        name, extras = re.match(r"\s*([\w.-]+)\s*(?:\[([^\]]*)\])?", spec).groups()
        if canon(name) != "machina-ai":
            yield canon(name)
            continue
        for nested in filter(None, (e.strip() for e in (extras or "").split(","))):
            if nested not in seen:
                seen.add(nested)
                yield from requirements(nested, seen)

wanted = set(requirements(sys.argv[1], {sys.argv[1]}))
skip = {"tests", "test", "docs", "examples", "scripts", "benchmarks"}
modules = {}
for module, owners in md.packages_distributions().items():
    for owner in owners:
        if canon(owner) in wanted and module.isidentifier() and module[0] != "_":
            if module not in skip:
                modules.setdefault(canon(owner), set()).add(module)
errors = []
for dist in sorted(wanted):
    try:
        md.distribution(dist)
    except md.PackageNotFoundError:
        errors.append(f"{dist}: declared but not installed")
for dist, names in sorted(modules.items()):
    for name in sorted(names):
        try:
            importlib.import_module(name)
        except Exception as exc:
            errors.append(f"{name} (from {dist}): {type(exc).__name__}: {exc}")
print(f"{sum(map(len, modules.values()))} modules from {len(wanted)} requirements")
sys.exit("\n".join(errors) or None)
"""


class Smoke:
    """Runs smoke commands from ``workdir`` and collects their failures."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.executed = 0
        self.findings: list[dict[str, str]] = []
        self.env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    def run(self, check: str, *command: str | Path) -> str | None:
        """Run one check; return its stdout, or None after recording a failure."""
        self.executed += 1
        try:
            proc = subprocess.run(
                [str(part) for part in command],
                cwd=self.workdir,
                env=self.env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
            )
        except subprocess.TimeoutExpired as exc:
            self.findings.append(finding("smoke-failed", check, f"timed out after {exc.timeout}s"))
            return None
        if proc.returncode != 0:
            self.findings.append(finding("smoke-failed", check, proc.stdout + proc.stderr))
            print(f"FAIL {check}")
            return None
        last = proc.stdout.strip().rpartition("\n")[2]
        shown = len(last) < 80 and any(char.isalpha() for char in last)
        print(f"ok   {check}" + (f" ({last})" if shown else ""))
        return proc.stdout

    @contextlib.contextmanager
    def venv(self, name: str) -> Iterator[Path | None]:
        """Yield the python of a fresh venv (None if it failed), then delete it."""
        path = self.workdir / name
        bindir = path / ("Scripts" if os.name == "nt" else "bin")
        created = self.run(f"{name}: create venv", sys.executable, "-m", "venv", path)
        try:
            yield None if created is None else bindir / "python"
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def describe(self, label: str, python: Path) -> None:
        """``machina describe --json`` must print the capability spine."""
        check = f"{label}: machina describe --json"
        stdout = self.run(check, python.parent / "machina", "describe", "--json")
        if stdout is None:
            return
        try:
            spine = json.loads(stdout)
        except ValueError:
            spine = None
        if not (isinstance(spine, dict) and spine.get("connectors")):
            self.findings.append(finding("smoke-failed", check, f"no spine: {stdout[:500]}"))


def declared_extras(wheel: Path) -> list[str]:
    """The ``Provides-Extra`` names in the wheel's METADATA."""
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(name))
    return metadata.get_all("Provides-Extra") or []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    # Absolute paths: every command runs from the temporary directory.
    dist = args.dist.resolve()
    wheels, sdists = sorted(dist.glob("*.whl")), sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        message = f"want one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
        missing = [finding("no-distributions", message)]
        return write_result(args.out, "package-smoke", "package-smoke", "error", {}, missing)

    workdir = Path(tempfile.mkdtemp(prefix="machina-smoke-", dir=os.environ.get("RUNNER_TEMP")))
    smoke = Smoke(workdir)
    for label, artifact in (("wheel", wheels[0]), ("sdist", sdists[0])):
        with smoke.venv(label) as python:
            if python and smoke.run(f"{label}: install", python, "-m", "pip", "install", artifact):
                smoke.describe(label, python)
                smoke.run(f"{label}: public API", python, "-c", PUBLIC_API)
    extras = declared_extras(wheels[0])
    for extra in extras:
        with smoke.venv(f"extra-{extra}") as python:
            target = f"{wheels[0]}[{extra}]"
            if python and smoke.run(f"[{extra}]: install", python, "-m", "pip", "install", target):
                smoke.run(f"[{extra}]: imports", python, "-c", EXTRA_IMPORTS, extra)
    shutil.rmtree(workdir, ignore_errors=True)

    facts = {"executed": smoke.executed, "failed": len(smoke.findings), "extras": len(extras)}
    status = "fail" if smoke.findings else "pass"
    return write_result(args.out, "package-smoke", "package-smoke", status, facts, smoke.findings)


if __name__ == "__main__":
    sys.exit(main())
