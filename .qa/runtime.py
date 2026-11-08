# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Bounded QA destinations and receipts for real HA-MCP release workflows."""

from __future__ import annotations

import argparse
import email
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import tomllib

SOURCE = "julienld/ha-mcp-release-qa-20261107"
MIRROR = "julienld/ha-mcp-mirror-qa-20261107"
INDEX = "https://julienld.github.io/ha-mcp-mirror-qa-20261107"
SITE = "https://julienld.github.io/ha-mcp-release-qa-20261107"
TRAILERS = "\n\nCodex-Session: 01a1ab9a-90da-7452-9bec-86e40464cba2\nT3-Thread: a79d9703-47c6-4084-a1d7-62b70915a8f2"


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        encoding="utf-8",
        check=check,
    )


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")


def context() -> None:
    if (
        os.environ.get("GITHUB_REPOSITORY") not in {SOURCE, MIRROR}
        or os.environ.get("GITHUB_ACTIONS") != "true"
    ):
        raise ValueError(
            "Only the dedicated QA repositories may execute these destinations"
        )


def api(repo: str, path: str, *, fields: dict | None = None) -> Any:
    if repo not in {SOURCE, MIRROR}:
        raise ValueError("GitHub endpoint outside QA repositories")
    args = ["gh", "api", f"repos/{repo}/{path}"]
    if fields is not None:
        if repo != os.environ["GITHUB_REPOSITORY"]:
            raise ValueError("Cross-repository API writes are prohibited")
        args += ["--input", "-"]
    result = run(args, input_text=json.dumps(fields) if fields is not None else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?", value)
    if not match:
        raise ValueError("Unsupported QA package version")
    major, minor, patch, dev = match.groups()
    return int(major), int(minor), int(patch), int(dev is None), int(dev or 0)


def package_metadata(dist: Path) -> tuple[str, str, dict]:
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata = email.message_from_bytes(
            archive.read(
                next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            )
        )
    name = re.sub(r"[-_.]+", "-", str(metadata["Name"])).lower()
    version = str(metadata["Version"])
    if name not in {"ha-mcp", "ha-mcp-dev"}:
        raise ValueError("Only QA copies of the two HA-MCP distributions are allowed")
    version_key(version)
    files = []
    for path in sorted(dist.iterdir()):
        if not path.is_file() or not (
            path.name.endswith(".whl") or path.name.endswith(".tar.gz")
        ):
            continue
        files.append(
            {
                "filename": path.name,
                "url": INDEX + "/packages/" + path.name,
                "packagetype": "bdist_wheel" if path.suffix == ".whl" else "sdist",
                "requires_python": str(metadata["Requires-Python"]),
                "size": path.stat().st_size,
                "digests": {"sha256": digest(path)},
                "yanked": False,
            }
        )
    return (
        name,
        version,
        {
            "info": {
                "name": name,
                "version": version,
                "requires_python": str(metadata["Requires-Python"]),
            },
            "urls": files,
        },
    )


def publish_index(dist: Path) -> None:
    context()
    if os.environ["GITHUB_REPOSITORY"] != SOURCE:
        raise ValueError("Only the QA source publishes the package index")
    name, version, metadata = package_metadata(dist.resolve())
    with tempfile.TemporaryDirectory(
        prefix="qa-index-", dir=os.environ["RUNNER_TEMP"]
    ) as temporary:
        root = Path(temporary)
        key, hosts, checkout = root / "key", root / "known_hosts", root / "index"
        key.write_text(os.environ["QA_DEPLOY_KEY"].strip() + "\n", encoding="utf-8")
        key.chmod(0o600)
        hosts.write_text(run(["ssh-keyscan", "github.com"]).stdout, encoding="utf-8")
        environment = {
            **os.environ,
            "GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={hosts}",
        }
        remote = f"git@github.com:{MIRROR}.git"
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "qa-python-index",
                remote,
                str(checkout),
            ],
            env=environment,
        )
        for option, value in (
            ("user.name", "github-actions[bot]"),
            ("user.email", "41898282+github-actions[bot]@users.noreply.github.com"),
        ):
            run(["git", "config", option, value], cwd=checkout)
        for attempt in range(5):
            if attempt:
                run(
                    ["git", "fetch", "origin", "qa-python-index"],
                    cwd=checkout,
                    env=environment,
                )
                run(["git", "reset", "--hard", "FETCH_HEAD"], cwd=checkout)
            (checkout / "packages").mkdir(exist_ok=True)
            for file in metadata["urls"]:
                target = checkout / "packages" / file["filename"]
                if target.exists() and digest(target) != file["digests"]["sha256"]:
                    raise ValueError("Refusing to replace a published QA distribution")
                shutil.copyfile(dist / file["filename"], target)
            write(checkout / "pypi" / name / version / "json", metadata)
            versions = sorted(
                (p.name for p in (checkout / "pypi" / name).iterdir() if p.is_dir()),
                key=version_key,
            )
            latest = json.loads(
                (checkout / "pypi" / name / versions[-1] / "json").read_text()
            )
            latest["releases"] = {
                v: json.loads((checkout / "pypi" / name / v / "json").read_text())[
                    "urls"
                ]
                for v in versions
            }
            write(checkout / "pypi" / name / "json", latest)
            links = []
            for value in latest["releases"].values():
                for file in value:
                    links.append(
                        f'<a href="{html.escape(file["url"])}#sha256={file["digests"]["sha256"]}">{html.escape(file["filename"])}</a>'
                    )
            page = checkout / "simple" / name / "index.html"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(
                "<!doctype html><html><body>\n"
                + "<br>\n".join(links)
                + "\n</body></html>\n",
                encoding="utf-8",
            )
            (checkout / ".nojekyll").touch()
            run(["git", "add", "."], cwd=checkout)
            if (
                run(
                    ["git", "diff", "--cached", "--quiet"], cwd=checkout, check=False
                ).returncode
                == 0
            ):
                break
            run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"build(qa): publish {name} {version}" + TRAILERS,
                ],
                cwd=checkout,
            )
            if (
                run(
                    ["git", "push", "origin", "HEAD:qa-python-index"],
                    cwd=checkout,
                    env=environment,
                    check=False,
                ).returncode
                == 0
            ):
                break
        else:
            raise RuntimeError("QA package-index push never succeeded")
    write(
        Path("qa-package-publication.json"),
        {"name": name, "version": version, "index": INDEX, "files": metadata["urls"]},
    )


def receipt() -> None:
    context()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    parent = event.get("workflow_run", {})
    needs = json.loads(os.environ.get("QA_NEEDS", "{}"))
    outputs = {
        job: {
            "result": data["result"],
            "outputs": {
                k: v
                for k, v in data.get("outputs", {}).items()
                if k.startswith("qa_")
                or k
                in {
                    "version",
                    "release_tag",
                    "dev_version",
                    "released",
                    "should_publish",
                }
            },
        }
        for job, data in needs.items()
    }
    write(
        Path("qa-workflow-receipt.json"),
        {
            "repository": os.environ["GITHUB_REPOSITORY"],
            "workflow": os.environ["GITHUB_WORKFLOW"],
            "run_id": os.environ["GITHUB_RUN_ID"],
            "event": os.environ["GITHUB_EVENT_NAME"],
            "head_sha": os.environ["GITHUB_SHA"],
            "parent": {
                k: parent.get(k)
                for k in (
                    "id",
                    "name",
                    "event",
                    "head_branch",
                    "head_sha",
                    "conclusion",
                )
            },
            "stage": os.environ.get("QA_STAGE"),
            "needs": outputs,
        },
    )

    failed = {
        job for job, data in needs.items() if data["result"] in {"failure", "cancelled"}
    }
    controlled = (
        os.environ.get("QA_FAIL_DOCKER") == "true"
        and failed == {"publish_docker"}
        and needs.get("finalize_github_release", {}).get("result") == "success"
    )
    if failed and not controlled and os.environ.get("QA_REPORT_FAILURES") == "true":
        sha, number = os.environ["QA_OBSERVER_SHA"], os.environ["QA_OBSERVER_PR"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or not number.isdigit():
            raise ValueError("Unexpected observer identity")
        stage = os.environ["QA_STAGE"]
        url = f"https://github.com/{SOURCE}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        api(
            SOURCE,
            f"statuses/{sha}",
            fields={
                "state": "failure",
                "context": f"Rehearsal / {stage}",
                "description": "Real workflow job failed",
                "target_url": url,
            },
        )
        api(
            SOURCE,
            f"issues/{number}/comments",
            fields={
                "body": f"[Agent] Workflow failure during {stage}: {', '.join(sorted(failed))}. [Run and receipt]({url})."
            },
        )


def site_marker() -> None:
    context()
    project = tomllib.loads(Path("../pyproject.toml").read_text())["project"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    (Path("dist") / "CNAME").unlink(missing_ok=True)
    write(
        Path("dist/qa-release.json"),
        {
            "version": project["version"],
            "commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
            "event": os.environ["GITHUB_EVENT_NAME"],
            "run_id": os.environ["GITHUB_RUN_ID"],
            "parent_run_id": event.get("workflow_run", {}).get("id"),
        },
    )


def wait_release(repo: str, tag: str) -> dict:
    for _ in range(60):
        try:
            value = api(repo, "releases/tags/" + tag)
            if any(
                a["name"] == "ha_mcp_tools.zip" and a["state"] == "uploaded"
                for a in value["assets"]
            ):
                return value
        except subprocess.CalledProcessError:
            pass
        time.sleep(5)
    raise TimeoutError("The real mirror release did not become available")


def verify_chain(parent: Path) -> None:
    context()
    data = json.loads(parent.read_text())
    sync = data["needs"].get("sync", {})
    fields = sync.get("outputs", {})
    tag, pin = fields.get("qa_tag"), fields.get("qa_pin")
    if sync.get("result") != "success" or not tag or not pin:
        write(
            Path("qa-chain-result.json"),
            {
                "ignored": True,
                "reason": "No successful tagged synchronization",
                "parent": data,
            },
        )
        return
    channel = fields["qa_channel"]
    if data["parent"].get("conclusion") != "success":
        if channel != "stable" or os.environ.get("QA_FAIL_DOCKER") != "true":
            raise ValueError("The producing workflow failed unexpectedly")
        upstream = Path(os.environ["RUNNER_TEMP"]) / "qa-publisher-receipt"
        run(
            [
                "gh",
                "run",
                "download",
                str(data["parent"]["id"]),
                "--repo",
                SOURCE,
                "--name",
                "qa-workflow-receipt",
                "--dir",
                str(upstream),
            ]
        )
        producer = json.loads((upstream / "qa-workflow-receipt.json").read_text())
        failures = {j for j, d in producer["needs"].items() if d["result"] == "failure"}
        assert (
            failures == {"publish_docker"}
            and producer["needs"]["finalize_github_release"]["result"] == "success"
        )
    source_tag = "v" + pin
    source = wait_release(SOURCE, source_tag)
    mirror = wait_release(MIRROR, tag)
    source_asset = next(a for a in source["assets"] if a["name"] == "ha_mcp_tools.zip")
    mirror_asset = next(a for a in mirror["assets"] if a["name"] == "ha_mcp_tools.zip")
    assert source_asset["digest"] == mirror_asset["digest"]
    work = Path(os.environ["RUNNER_TEMP"]) / "qa-chain-download"
    work.mkdir(exist_ok=True)
    run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            MIRROR,
            "--pattern",
            "ha_mcp_tools.zip",
            "--dir",
            str(work),
        ]
    )
    archive = work / "ha_mcp_tools.zip"
    assert "sha256:" + digest(archive) == source_asset["digest"]
    source_commit = api(SOURCE, "git/ref/tags/" + source_tag)["object"]
    while source_commit["type"] == "tag":
        source_commit = api(SOURCE, "git/tags/" + source_commit["sha"])["object"]
    run(
        [
            "git",
            "fetch",
            "--no-tags",
            f"https://github.com/{SOURCE}.git",
            "refs/tags/" + source_tag,
        ]
    )
    checkout = work / "source"
    run(["git", "worktree", "add", "--detach", str(checkout), source_commit["sha"]])
    run(
        [
            "uv",
            "run",
            "-s",
            "scripts/build_component_zip.py",
            "--component-dir",
            str(checkout / "custom_components/ha_mcp_tools"),
            "--verify",
            str(archive),
        ]
    )
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read("manifest.json"))
    assert fields["qa_dist"] + "==" + pin in manifest["requirements"]
    site = None
    if channel == "stable":
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                    SITE + "/qa-release.json?run=" + os.environ["GITHUB_RUN_ID"],
                    timeout=15,
                ) as response:
                    site = json.load(response)
                if (
                    site.get("commit") == source_commit["sha"]
                    and site.get("version") == pin
                ):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(5)
        else:
            raise TimeoutError(
                "The real site deployment did not serve the selected tag"
            )
    result = {
        "passed": True,
        "stage": data["stage"],
        "channel": channel,
        "source_tag": source_tag,
        "source_commit": source_commit["sha"],
        "mirror_tag": tag,
        "digest": source_asset["digest"],
        "source_asset_id": source_asset["id"],
        "mirror_asset_id": mirror_asset["id"],
        "manifest": manifest,
        "site": site,
        "synchronization": data,
    }
    write(Path("qa-chain-result.json"), result)


def report() -> None:
    context()
    number = os.environ.get("QA_OBSERVER_PR", "")
    sha = os.environ.get("QA_OBSERVER_SHA", "")
    if not number.isdigit() or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Missing QA observer identity")
    path = Path("qa-chain-result.json")
    result = json.loads(path.read_text()) if path.exists() else {}
    if result.get("ignored"):
        return
    successful = (
        result.get("passed") and os.environ.get("QA_VERIFY_OUTCOME") == "success"
    )
    stage, channel = (
        result.get("stage", os.environ.get("QA_STAGE", "unknown")),
        result.get("channel", "chain"),
    )
    url = f"https://github.com/{SOURCE}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    api(
        SOURCE,
        f"statuses/{sha}",
        fields={
            "state": "success" if successful else "failure",
            "context": f"Rehearsal / {stage}",
            "description": "Real workflow-chain verification",
            "target_url": url,
        },
    )
    api(
        SOURCE,
        f"issues/{number}/comments",
        fields={
            "body": f"[Agent] Real workflow chain **{'passed' if successful else 'failed'}**: {stage}, {channel}. [Run and receipts]({url}). Source tag `{result.get('source_tag')}`, mirror `{result.get('mirror_tag')}`. Python publications use the QA index; production PyPI/MCP registration is excluded."
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("publish-index", "receipt", "site-marker", "verify-chain", "report"),
    )
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument(
        "--parent", type=Path, default=Path("parent/qa-workflow-receipt.json")
    )
    args = parser.parse_args()
    if args.command == "publish-index":
        publish_index(args.dist)
    elif args.command == "receipt":
        receipt()
    elif args.command == "site-marker":
        site_marker()
    elif args.command == "verify-chain":
        verify_chain(args.parent)
    else:
        report()


if __name__ == "__main__":
    main()
