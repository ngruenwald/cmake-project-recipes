import argparse
import glob
import json
import os
from functools import cmp_to_key
from hashlib import sha256
from subprocess import Popen
from time import sleep

import regex
import requests

VERBOSE: bool = False
TOKEN: str | None = None
SESSION: requests.Session | None = None


class Change:
    def __init__(
        self,
        package: str,
        old_version: str,
        new_version: str,
        recipe_file: str | None,
        info: dict,
        file_url: str,
        auto_accept: bool
    ):
        self.package = package
        self.old_version = old_version
        self.new_version = new_version
        self.recipe_file = recipe_file
        self.info = info
        self.file_url = file_url
        self.auto_accept = auto_accept


def set_token(token: str | None) -> None:
    global TOKEN
    TOKEN = token


def get_session() -> requests.Session:
    global SESSION
    if not SESSION:
        SESSION = requests.Session()
        if TOKEN:
            SESSION.headers = {"Authorization": f"Bearer {TOKEN}"}
    return SESSION


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
    parser.add_argument("--doc", default="", help="Create package documentation")
    args = parser.parse_args()

    if args.token is not None:
        set_token(args.token)
    else:
        set_token(os.getenv("TOKEN"))

    recipe_files: list[str] = glob.glob(f"{args.path}/*.json")
    idxw = len(str(len(recipe_files)))
    detected_changes: list[Change] = []
    for idx, recipe_file in enumerate(recipe_files):
        if args.delay:
            sleep(args.delay)
        if not VERBOSE:
            print(f"\r{idx + 1:>{idxw}}/{len(recipe_files):>{idxw}}  ", end="")
        detected_change = check_for_updates(recipe_file, args.package, args.force)
        if detected_change:
            detected_changes.append(detected_change)
    print("\n")
    applied_changes: list[Change] = []
    for detected_change in detected_changes:
        auto_accept = True if args.yes else detected_change.auto_accept
        applied_change = apply_update(detected_change, auto_accept)
        if applied_change:
            applied_changes.append(applied_change)
    if args.doc:
        create_documentation(args.doc, recipe_files)
    if applied_changes and args.commit:
        create_commit(applied_changes, args.doc)


def package_name_from_recipe_file(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[-1]


def check_for_updates(
    recipe_file: str,
    package_filter: str | None,
    force: bool,
) -> Change | None:
    try:
        info = read_recipe_file(recipe_file)
        meta = info.get("meta", {})
        package = meta.get("package")
        if not package:
            package = package_name_from_recipe_file(recipe_file)

        if package_filter and package != package_filter:
            return None

        if VERBOSE:
            print(f"* checking {package}")

        try:
            # check url
            url = info.get("url", "")
            if "://github.com" not in url:
                if VERBOSE:
                    print_check_info(package, "skipping non github project", "!")
                return None

            version = info["version"]

            # branch names
            if version in ["main", "master"]:
                if VERBOSE:
                    print_check_info(package, "skipping branch", "!")
                return None

            # commits
            # ... a bit useless when processing recipes,
            #     since the cmake code can't handle the leading '#' character
            if version.startswith("#"):
                if VERBOSE:
                    print_check_info(package, "skipping commit", "!")
                return None

            tags = api_get_list(
                f"https://api.github.com/repos/{package}/tags", max_commits=700
            )
            tags = filter_tags(tags, meta.get("tag_filter", ""))
            tags = sorted(tags, key=cmp_to_key(compare_tag_versions))

            if len(tags) == 0:
                print_check_info(package, "no matching tags", "!")
                return None

            tag = tags[0]

            tag_version = trim_version_string(tag["name"])
            cmp_result = compare_versions(
                parse_version(version), parse_version(tag_version)
            )
            needs_update = True if force else (cmp_result < 0)

            if not needs_update:
                if VERBOSE:
                    print("  no updates")
                return None

            commit_sha = tag["commit"]["sha"]
            commit = api_get(
                f"https://api.github.com/repos/{package}/git/commits/{commit_sha}"
            ).json()

            info["version"] = tag_version
            meta["date"] = commit["author"]["date"]
            # info["sha256"] = get_file_hash(tag["zipball_url"])
            file_url = (
                f"https://github.com/{package}/archive/refs/tags/{tag['name']}.zip"
            )
            info["url_hash"] = ""

            info["meta"] = meta

            auto_accept_updates = info.get("auto_accept_updates", False)

            cc = "=" if cmp_result == 0 else ("+" if cmp_result < 0 else "-")
            print_check_info(package, f"{version} -> {tag_version}", cc)

            return Change(package, version, tag_version, recipe_file, info, file_url, auto_accept_updates)

        except Exception as error:
            print_check_info(package, str(error), "!")
            return None

    except Exception as error:
        print(error)
        return None


def print_check_info(package, message: str, cc: str = " ") -> None:
    if VERBOSE:
        print(f"  {message}")
    else:
        print(f"{cc} {package}: {message}")


def apply_update(
    change: Change,
    auto_accept: bool,
) -> Change | None:
    try:
        if not auto_accept:
            if not ask_user(
                f"  update {change.package} from {change.old_version} to {change.new_version}?"
            ):
                return None

        if not change.info.get("url_hash", ""):
            change.info["url_hash"] = f"SHA256={get_file_hash(change.file_url)}"

        if change.recipe_file:
            write_recipe_file(change.recipe_file, change.info)

        print(f"  {change.old_version} -> {change.new_version}")

        return change

    except Exception as error:
        print(error)


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
    for idx in range(0, length):
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
        response = get_session().get(url, timeout=30)
        if response.status_code == 403:
            # API rate limit?
            print("403 - waiting 60 seconds ...")
            sleep(wait_seconds)
        else:
            response.raise_for_status()
            return response
    raise Exception("no response")


def api_get_list(url: str, max_commits: int | None = None) -> list:
    response = api_get(url)
    data = list(response.json())

    try:
        next_link = response.links["next"]["url"]
    except KeyError:
        next_link = None

    if data and max_commits is not None:
        max_commits -= len(data)
        if max_commits <= 0:
            return data

    if next_link:
        sub_data = api_get_list(next_link, max_commits)
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


def create_commit(changes: list[Change], docufile: str | None) -> bool:
    commands = []
    for change in changes:
        commands.append(["git", "add", change.recipe_file])
    if not commands:
        return True

    if docufile:
        commands.append(["git", "add", docufile])

    message = "maint: update versions\n"
    for change in changes:
        message += f"\n  * {change.package} {change.new_version}"
    commands.append(["git", "commit", "-m", message])

    for command in commands:
        proc = Popen(command)
        if proc.wait() != 0:
            return False

    return True


def create_documentation(filename: str, recipe_files: list[str]) -> None:
    class Info:
        def __init__(self, name, package, description, version, timestamp, url):
            self.name = name
            self.package = package
            self.description = description
            self.version = version
            self.timestamp = timestamp
            self.url = url

    infos: list[Info] = []
    for recipe_file in recipe_files:
        with open(recipe_file, "r") as file:
            data = json.load(file)
        package: str = data.get("meta", {}).get("package", "")
        name: str = package.split("/")[-1]
        description: str = data.get("meta", {}).get("description", "")
        version: str = data.get("version", "")
        timestamp: str = data.get("meta", {}).get("date", "")
        url: str = data.get("meta", {}).get("source", "")
        if not name:
            continue
        if version.startswith("#"):
            version = version[0:9]
        infos.append(Info(name, package, description, version, timestamp, url))

    infos.sort(key=lambda item: item.name)

    with open(filename, "w") as file:
        file.write("# Packages\n")
        file.write("\n")
        file.write("| Name | Version | Description | Project | Updated |\n")
        file.write("|------|---------|-------------|---------|---------|\n")
        for info in infos:
            file.write(
                f"| {info.name} | {info.version} | {info.description} | [{info.package}]({info.url}) | {info.timestamp} |\n"
            )


if __name__ == "__main__":
    main()
