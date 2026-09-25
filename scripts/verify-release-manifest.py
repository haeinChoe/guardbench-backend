#!/usr/bin/env python3
import argparse
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE_DIR = ROOT / "releases"
FILE_RE = re.compile(r"^guardbench-(v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))\.yaml$")
SEMVER_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

EXPECTED_REPOS = {
    "backend": "haeinChoe/guardbench-backend",
    "frontend": "haeinChoe/guardbench-frontend",
    "infrastructure": "haeinChoe/guardbench-iac",
}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def version_tuple(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        fail(f"invalid semantic version: {version}")
    return tuple(int(part) for part in version[1:].split("."))


def parse_manifest(path: pathlib.Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    result = {"components": {}}
    current_component = None

    for raw in lines:
        line = raw.rstrip()
        if re.fullmatch(r"product:\s*.+", line):
            result["product"] = line.split(":", 1)[1].strip()
        elif re.fullmatch(r"release:\s*.+", line):
            result["release"] = line.split(":", 1)[1].strip()
        elif re.fullmatch(r"stage:\s*.+", line):
            result["stage"] = line.split(":", 1)[1].strip()
        elif re.fullmatch(r"  (backend|frontend|infrastructure):", line):
            current_component = line.strip()[:-1]
            result["components"][current_component] = {}
        elif current_component and re.fullmatch(r"    repository:\s*.+", line):
            result["components"][current_component]["repository"] = line.split(":", 1)[1].strip()
        elif current_component and re.fullmatch(r"    tag:\s*.+", line):
            result["components"][current_component]["tag"] = line.split(":", 1)[1].strip()

    if result.get("product") != "GuardBench":
        fail(f"{path}: product must be GuardBench")
    if result.get("stage") != "MVP":
        fail(f"{path}: stage must be MVP")
    if not SEMVER_RE.fullmatch(result.get("release", "")):
        fail(f"{path}: release must match vMAJOR.MINOR.PATCH")

    if set(result["components"]) != set(EXPECTED_REPOS):
        fail(f"{path}: components must be exactly {sorted(EXPECTED_REPOS)}")

    for name, expected_repo in EXPECTED_REPOS.items():
        component = result["components"][name]
        if component.get("repository") != expected_repo:
            fail(f"{path}: {name}.repository must be {expected_repo}")
        if not SEMVER_RE.fullmatch(component.get("tag", "")):
            fail(f"{path}: {name}.tag must match vMAJOR.MINOR.PATCH")

    return result


def changed_release_files(base: str, head: str) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        ["git", "diff", "--name-status", base, head, "--", "releases/"],
        cwd=ROOT,
        text=True,
    )
    changes = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        path = parts[-1]
        changes.append((status, path))
    return changes


def verify_remote_tag(repository: str, tag: str) -> None:
    url = f"https://github.com/{repository}.git"
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--tags", url, f"refs/tags/{tag}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        fail(f"component tag does not exist: {repository}@{tag}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()

    changes = changed_release_files(args.base, args.head)

    for status, path in changes:
        if status != "A":
            fail(f"existing release manifests are immutable: {status} {path}")

    added = [path for status, path in changes if status == "A"]

    # Workflow/script-only PRs are allowed. When a manifest is added, keep the release atomic.
    if not added:
        print("No release manifest added; policy implementation change only.")
        return

    if len(added) != 1:
        fail("exactly one release manifest may be added per PR")

    path = pathlib.Path(added[0])
    match = FILE_RE.fullmatch(path.name)
    if not match:
        fail(f"manifest filename must be guardbench-vMAJOR.MINOR.PATCH.yaml: {path}")

    manifest = parse_manifest(ROOT / path)
    filename_version = match.group(1)

    if manifest["release"] != filename_version:
        fail(
            f"manifest release {manifest['release']} does not match filename version {filename_version}"
        )

    previous_versions = []
    for existing in RELEASE_DIR.glob("guardbench-v*.yaml"):
        if existing.name == path.name:
            continue
        existing_match = FILE_RE.fullmatch(existing.name)
        if existing_match:
            previous_versions.append(existing_match.group(1))

    if previous_versions:
        latest = max(previous_versions, key=version_tuple)
        if version_tuple(filename_version) <= version_tuple(latest):
            fail(f"new product version {filename_version} must be greater than existing {latest}")

    for component in manifest["components"].values():
        verify_remote_tag(component["repository"], component["tag"])

    print(f"Release manifest policy verified: {path}")


if __name__ == "__main__":
    main()
