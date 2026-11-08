"""Use the workflow-produced ZIP and published QA wheel in the existing HA test."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from testcontainers.core.container import DockerContainer

INDEX = "https://julienld.github.io/ha-mcp-mirror-qa-20261107"
_original_run = subprocess.run
_original_start = DockerContainer.start
_replacements: list[dict] = []
_baselines: list[dict] = []
_seeds: dict[Any, Any] = {}
_report: dict = {}


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def pytest_sessionstart(session: pytest.Session) -> None:
    global _report
    subject = Path(os.environ["QA_HA_SUBJECT"]).resolve()
    archive = Path(os.environ["QA_HA_ARCHIVE"]).resolve()
    _report = json.loads(Path(os.environ["QA_HA_INPUT"]).read_text())
    actual = _original_run(
        ["git", "rev-parse", "HEAD"],
        cwd=subject,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual != _report["source_commit"] or sha(archive) != _report["digest"]:
        raise pytest.UsageError(
            "The actual release checkout/archive differs from its workflow receipt"
        )
    if not _report["wheel_url"].startswith(INDEX + "/packages/"):
        raise pytest.UsageError("The selected wheel left the QA registry")

    def start(container: DockerContainer, *args: Any, **kwargs: Any) -> Any:
        container.with_env("PIP_INDEX_URL", INDEX + "/simple/")
        container.with_env("PIP_EXTRA_INDEX_URL", "https://pypi.org/simple")
        container.with_env("UV_INDEX_URL", INDEX + "/simple/")
        container.with_env("UV_EXTRA_INDEX_URL", "https://pypi.org/simple")
        container.with_env("UV_INDEX_STRATEGY", "unsafe-best-match")
        return _original_start(container, *args, **kwargs)

    def run_with_release(args: Any, *positional: Any, **kwargs: Any) -> Any:
        result = _original_run(args, *positional, **kwargs)
        if not isinstance(args, (list, tuple)) or len(args) < 4:
            return result
        if (
            Path(str(args[1])).resolve() != subject / "scripts/build_component_zip.py"
            or "--output" not in args
        ):
            return result
        if (
            "--component-dir" in args
            and Path(str(args[args.index("--component-dir") + 1])).resolve()
            != subject / "custom_components/ha_mcp_tools"
        ):
            return result
        output = Path(str(args[args.index("--output") + 1])).resolve()
        if not output.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise pytest.UsageError(
                "The fixture ZIP output is outside its temporary directory"
            )
        with zipfile.ZipFile(output) as built, zipfile.ZipFile(archive) as produced:
            if set(built.namelist()) != set(produced.namelist()) or any(
                built.read(n) != produced.read(n) for n in built.namelist()
            ):
                raise pytest.UsageError(
                    "The mirror ZIP differs from the release component"
                )
        shutil.copyfile(archive, output)
        _replacements.append(
            {
                "context": os.environ.get("PYTEST_CURRENT_TEST", ""),
                "digest": sha(output),
            }
        )
        return result

    DockerContainer.start = start
    subprocess.run = run_with_release


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    expected = (
        Path(os.environ["QA_HA_SUBJECT"]).resolve()
        / "tests/src/e2e/workflows/embedded/test_embedded_update_path.py"
    )
    for item in items:
        module = item.module
        if Path(module.__file__).resolve() != expected or module in _seeds:
            continue
        original = module._seed_config
        _seeds[module] = original

        def seed(
            config_path: Path,
            component_src: Path | None,
            component_zip: Path | None = None,
            *,
            _original: Any = original,
        ) -> None:
            _original(config_path, component_src, component_zip)
            storage = config_path / ".storage/core.config_entries"
            data = json.loads(storage.read_text())
            entry = next(
                e
                for e in data["data"]["entries"]
                if e["entry_id"] == "e2e_update_path_server_entry"
            )
            entry["options"]["pip_spec"] = "ha-mcp @ " + _report["wheel_url"]
            storage.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        module._seed_config = seed


def pytest_runtest_call(item: pytest.Item) -> None:
    fixture = item.funcargs.get("update_path_ha")
    if fixture:
        assert fixture["phase1_version"] == _report["version"], (
            "HA did not load the package published by the real QA workflow"
        )
        _baselines.append(
            {
                "test": item.nodeid,
                "version": fixture["phase1_version"],
                "wheel_url": _report["wheel_url"],
            }
        )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    subprocess.run = _original_run
    DockerContainer.start = _original_start
    for module, original in _seeds.items():
        module._seed_config = original
    value = {
        "source_tag": _report.get("source_tag"),
        "zip_digest": _report.get("digest"),
        "wheel_digest": _report.get("wheel_digest"),
        "replacements": _replacements,
        "baselines": _baselines,
    }
    Path(os.environ["QA_HA_RECEIPT"]).write_text(
        json.dumps(value, indent=2) + "\n", encoding="utf-8"
    )
    if not _baselines or not any(
        "component@working-tree" in r["context"] for r in _replacements
    ):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
