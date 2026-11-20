import argparse
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def version_parts(value):
    match = VERSION_PATTERN.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def project_version():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    npm_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads(
        (ROOT / "src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    with (ROOT / "src-tauri/Cargo.toml").open("rb") as file:
        crate = tomllib.load(file)["package"]
    with (ROOT / "src-tauri/Cargo.lock").open("rb") as file:
        cargo_lock = tomllib.load(file)
    locked_crate = next(
        (p for p in cargo_lock["package"] if p["name"] == crate["name"] and "source" not in p),
        {},
    )
    version = package["version"]
    if not isinstance(version, str) or version_parts(version) is None:
        raise SystemExit("package.json must have a stable major.minor.patch version.")
    versions = {
        "package-lock.json": npm_lock.get("version"),
        "package-lock.json (root package)": npm_lock["packages"][""].get("version"),
        "src-tauri/tauri.conf.json": tauri.get("version"),
        "src-tauri/Cargo.toml": crate.get("version"),
        "src-tauri/Cargo.lock": locked_crate.get("version"),
    }
    mismatches = [
        f"  {path}: {value!r}" for path, value in versions.items() if value != version
    ]
    if mismatches:
        raise SystemExit(
            f"Expected version {version} from package.json:\n" + "\n".join(mismatches)
        )
    return version


def gh(*args):
    return subprocess.run(
        ["gh", *args], cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def github_releases():
    repo = os.environ["GITHUB_REPOSITORY"]
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100")
    )
    return [release for page in pages for release in page]


def should_publish(version, releases):
    candidate = version_parts(version)
    for release in releases:
        if release["draft"]:
            continue
        tag = release["tag_name"]
        if tag == f"v{version}":
            return False
        published = version_parts(tag.removeprefix("v"))
        if not release["prerelease"] and published is not None and published >= candidate:
            return False
    return True


def is_main_push():
    return (
        os.environ["GITHUB_EVENT_NAME"] == "push"
        and os.environ["GITHUB_REF"] == "refs/heads/main"
    )


def plan(version):
    release_needed = is_main_push() and should_publish(version, github_releases())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"version={version}\npublish={str(release_needed).lower()}\n")
    print(f"Clickey {version}: {'release needed' if release_needed else 'CI only'}")


def verify_tag(tag, commit):
    repo = os.environ["GITHUB_REPOSITORY"]
    refs = json.loads(gh("api", f"repos/{repo}/git/matching-refs/tags/{tag}"))
    ref = next((ref for ref in refs if ref["ref"] == f"refs/tags/{tag}"), None)
    if ref is None:
        return
    target = ref["object"]
    while target["type"] == "tag":
        annotation = json.loads(gh("api", f"repos/{repo}/git/tags/{target['sha']}"))
        target = annotation["object"]
    if target["type"] != "commit" or target["sha"] != commit:
        raise SystemExit(f"{tag} points to {target['sha']}, expected commit {commit}.")


def verify_uploads(tag, installers):
    assets = json.loads(gh("release", "view", tag, "--json", "assets"))["assets"]
    uploaded = {asset["name"]: asset for asset in assets}
    if uploaded.keys() != {installer.name for installer in installers}:
        raise SystemExit(f"{tag} has unexpected release assets: {', '.join(uploaded)}")
    for installer in installers:
        with installer.open("rb") as file:
            digest = "sha256:" + hashlib.file_digest(file, "sha256").hexdigest()
        asset = uploaded[installer.name]
        if asset["state"] != "uploaded" or asset["digest"] != digest:
            raise SystemExit(f"Could not verify the uploaded {installer.name}.")


def publish(version):
    if not is_main_push():
        raise SystemExit("Publishing requires a push to main.")
    releases = github_releases()
    tag = f"v{version}"
    if not should_publish(version, releases):
        print(f"Skipping {tag}: this version or a newer stable version is published.")
        return

    installers = [
        ROOT / "release-assets" / f"Clickey_{version}_aarch64.dmg",
        ROOT / "release-assets" / f"Clickey_{version}_x64.msi",
    ]
    for installer in installers:
        if not installer.is_file() or installer.stat().st_size == 0:
            raise SystemExit(f"Missing installer: {installer.name}")

    commit = os.environ["GITHUB_SHA"]
    verify_tag(tag, commit)
    draft = next((r for r in releases if r["draft"] and r["tag_name"] == tag), None)
    if draft is None:
        gh(
            "release",
            "create",
            tag,
            "--draft",
            "--target",
            commit,
            "--title",
            f"Clickey {tag}",
            "--generate-notes",
        )
    elif (
        draft["target_commitish"] != commit
        or draft["author"]["login"] != "github-actions[bot]"
    ):
        raise SystemExit(f"{tag} already has a draft from another commit or author.")

    gh("release", "upload", tag, *(str(path) for path in installers), "--clobber")
    verify_uploads(tag, installers)
    print(gh("release", "edit", tag, "--draft=false", "--latest"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate and publish a Clickey release.")
    parser.add_argument("command", choices=("plan", "publish"))
    args = parser.parse_args()
    {"plan": plan, "publish": publish}[args.command](project_version())
