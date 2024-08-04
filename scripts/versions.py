import argparse
import glob
import json
import os
import regex
import requests

from functools import cmp_to_key
from hashlib import sha256
from subprocess import Popen
from time import sleep


TOKEN: str | None = None


class Change:
    def __init__(
        self,
        package: str,
        old_version: str,
        new_version: str,
        recipe_file: str | None,
    ):
        self.package = package
        self.old_version = old_version
        self.new_version = new_version
        self.recipe_file = recipe_file


def set_token(token: str | None) -> None:
    global TOKEN
    TOKEN = token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Force check")
    parser.add_argument("--token", default=None, help="Github access token")
    parser.add_argument("--yes", action="store_true", help="Accept all changes")
    parser.add_argument(
        "--package", default=None, help="Process only specified package"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Delay in seconds between package checks",
    )
    parser.add_argument("--commit", action="store_true", help="Create commit")
    parser.add_argument("--path", default=".", help="Recipe path")
    args = parser.parse_args()

    if args.token is not None:
        set_token(args.token)
    else:
        set_token(os.getenv("TOKEN"))

    changes: list[Change] = []
    for recipe_file in glob.glob(f"{args.path}/*.json"):
        if args.delay:
            sleep(args.delay)
        change = update_package(recipe_file, args.force, args.yes, args.package)
        if change:
            changes.append(change)
    if changes and args.commit:
        create_commit(changes)


def package_name_from_recipe_file(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[-1]


def update_package(
    recipe_file: str,
    force: bool,
    auto_accept: bool,
    package_filter: str | None,
) -> Change | None:
    # NOTE: currently we check all packages against github
    try:
        info = read_recipe_file(recipe_file)
        meta = info.get("meta", {})
        package = meta.get("package")
        if not package:
            package = package_name_from_recipe_file(recipe_file)

        if package_filter and package != package_filter:
            return None

        print(f"* checking {package}")

        # check url
        url = info.get("url", "")
        if "://github.com" not in url:
            print("  skipping non github project")
            return

        version = info["version"]

        # branch names
        if version in ["main", "master"]:
            print("  skipping branch")
            return None

        # commits
        # ... abit useless when processing recipes,
        #     since the cmake code can't handle the leading '#' character
        if version.startswith("#"):
            print("  skipping commit")
            return None

        tags = api_get_list(f"https://api.github.com/repos/{package}/tags")
        tags = filter_tags(tags, meta.get("tag_filter", ""))
        tags = sorted(tags, key=cmp_to_key(compare_tag_versions))
        tag = tags[0]

        tag_version = trim_version_string(tag["name"])

        if force:
            needs_update = True
        else:
            needs_update = (
                compare_versions(parse_version(version), parse_version(tag_version)) < 0
            )
            if needs_update and not auto_accept:
                needs_update = ask_user(
                    f"  update {package} from {version} to {tag_version}?"
                )

        if not needs_update:
            print("  no updates")
            return None

        commit_sha = tag["commit"]["sha"]
        commit = api_get(
            f"https://api.github.com/repos/{package}/git/commits/{commit_sha}"
        ).json()

        info["version"] = tag_version
        meta["date"] = commit["author"]["date"]
        # info["sha256"] = get_file_hash(tag["zipball_url"])
        file_url = f"https://github.com/{package}/archive/refs/tags/{tag['name']}.zip"
        info["url_hash"] = f"SHA256={get_file_hash(file_url)}"

        info["meta"] = meta
        write_recipe_file(recipe_file, info)

        print(f"  {version } -> {tag_version}")

        return Change(package, version, tag_version, recipe_file)

    except Exception as error:
        print(error)
        return None


def read_recipe_file(name: str) -> dict:
    with open(name, "r") as stream:
        return json.load(stream)


def write_recipe_file(name: str, data: dict) -> None:
    with open(name, "w") as stream:
        stream.write(json.dumps(data, indent=2, sort_keys=False))


def compare_tag_versions(a: dict, b: dict) -> int:
    tag_a = parse_version(a["name"])
    tag_b = parse_version(b["name"])
    return compare_versions(tag_b, tag_a)


def compare_versions(a, b) -> int:
    len_a = len(a)
    len_b = len(b)
    length = len_a if len_a < len_b else len_b
    for idx in range(0, len(a)):
        if a[idx] == b[idx]:
            continue
        return a[idx] - b[idx]
    return 0


def parse_version(s: str):
    def to_int(parts, idx) -> int:
        try:
            return int(parts[idx])
        except Exception:
            return 0

    delimiter = "."
    extra_delimiters = ["_", "-", "+"]
    s = trim_version_string(s)
    if len(s) > 0:
        for ed in extra_delimiters:
            s = s.replace(ed, delimiter)
    parts = s.split(delimiter)
    major = to_int(parts, 0)
    minor = to_int(parts, 1)
    patch = to_int(parts, 2)
    tweak = to_int(parts, 3)
    crazy = to_int(parts, 4)
    return (major, minor, patch, tweak, crazy)


def trim_version_string(s: str) -> str:
    if not s:
        return s
    idx = 0
    for idx in range(0, len(s)):
        if s[idx].isnumeric():
            break
    return s[idx:].strip()


def get_file_hash(url: str) -> str:
    data = api_get(url).content
    return sha256(data).hexdigest()


def api_get(url: str):
    num_loops = 10
    wait_seconds = 60
    for _ in range(0, num_loops):
        headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else None
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 403:
            # API rate limit?
            print("403 - waiting 60 seconds ...")
            sleep(wait_seconds)
        else:
            response.raise_for_status()
            return response
    raise Exception("no response")


def api_get_list(url: str) -> list:
    response = api_get(url)
    data = list(response.json())

    try:
        next_link = response.links["next"]["url"]
    except KeyError:
        next_link = None

    if next_link:
        sub_data = api_get_list(next_link)
        data.extend(sub_data)

    return data


def filter_tags(tags: list, tag_filter: str) -> list:
    if not tag_filter:
        return tags
    return [t for t in tags if regex.match(tag_filter, t.get("name", ""))]


def ask_user(question: str) -> bool:
    while True:
        inp = input(f"{question} [Y/n] ").lower()
        if inp == "":
            return True
        if inp == "y":
            return True
        if inp == "n":
            return False


def create_commit(changes: list[Change]) -> bool:
    commands = []
    for change in changes:
        commands.append(["git", "add", change.recipe_file])
    if not commands:
        return True

    message = "maint: update versions\n"
    for change in changes:
        message += f"\n  * {change.package} {change.new_version}"
    commands.append(["git", "commit", "-m", message])

    for command in commands:
        proc = Popen(command)
        if proc.wait() != 0:
            return False

    return True


if __name__ == "__main__":
    main()
