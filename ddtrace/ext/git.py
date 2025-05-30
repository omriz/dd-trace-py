"""
tags for common git attributes
"""

import contextlib
import logging
import os
import random
import re
from shutil import which
import subprocess
from tempfile import TemporaryDirectory
from typing import Dict  # noqa:F401
from typing import Generator  # noqa:F401
from typing import List  # noqa:F401
from typing import MutableMapping  # noqa:F401
from typing import NamedTuple  # noqa:F401
from typing import Optional  # noqa:F401
from typing import Tuple  # noqa:F401
from typing import Union  # noqa:F401

from ddtrace.internal import compat
from ddtrace.internal.logger import get_logger
from ddtrace.internal.utils.cache import cached
from ddtrace.internal.utils.time import StopWatch


GitNotFoundError = FileNotFoundError

# Git Branch
BRANCH = "git.branch"

# Git Commit SHA
COMMIT_SHA = "git.commit.sha"

# Git Repository URL
REPOSITORY_URL = "git.repository_url"

# Git Tag
TAG = "git.tag"

# Git Commit Author Name
COMMIT_AUTHOR_NAME = "git.commit.author.name"

# Git Commit Author Email
COMMIT_AUTHOR_EMAIL = "git.commit.author.email"

# Git Commit Author Date (UTC)
COMMIT_AUTHOR_DATE = "git.commit.author.date"

# Git Commit Committer Name
COMMIT_COMMITTER_NAME = "git.commit.committer.name"

# Git Commit Committer Email
COMMIT_COMMITTER_EMAIL = "git.commit.committer.email"

# Git Commit Committer Date (UTC)
COMMIT_COMMITTER_DATE = "git.commit.committer.date"

# Git Commit Message
COMMIT_MESSAGE = "git.commit.message"

# Python main package
MAIN_PACKAGE = "python_main_package"

_RE_REFS = re.compile(r"^refs/(heads/)?")
_RE_ORIGIN = re.compile(r"^origin/")
_RE_TAGS = re.compile(r"^tags/")

log = get_logger(__name__)

_GitSubprocessDetails = NamedTuple(
    "_GitSubprocessDetails", [("stdout", str), ("stderr", str), ("duration", float), ("returncode", int)]
)


def normalize_ref(name):
    # type: (Optional[str]) -> Optional[str]
    return _RE_TAGS.sub("", _RE_ORIGIN.sub("", _RE_REFS.sub("", name))) if name is not None else None


def is_ref_a_tag(ref):
    # type: (Optional[str]) -> bool
    return "tags/" in ref if ref else False


@cached()
def _get_executable_path(executable_name: str) -> Optional[str]:
    """Return the path to an executable.

    NOTE: cached() requires an argument which is why executable_name is passed in, even though it's really only ever
    used to find the git executable at this point.
    """
    return which(executable_name, mode=os.X_OK)


def _git_subprocess_cmd_with_details(*cmd, cwd=None, std_in=None):
    # type: (str, Optional[str], Optional[bytes]) -> _GitSubprocessDetails
    """Helper for invoking the git CLI binary

    Returns a tuple containing:
        - a str representation of stdout
        - a str representation of stderr
        - the time it took to execute the command, in milliseconds
        - the exit code
    """
    git_cmd = _get_executable_path("git")
    if git_cmd is None:
        raise FileNotFoundError("Git executable not found")
    git_cmd = [git_cmd]
    git_cmd.extend(cmd)

    log.debug("Executing git command: %s", git_cmd)

    with StopWatch() as stopwatch:
        process = subprocess.Popen(
            git_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, cwd=cwd
        )
        stdout, stderr = process.communicate(input=std_in)

    return _GitSubprocessDetails(
        compat.ensure_text(stdout).strip(),
        compat.ensure_text(stderr).strip(),
        stopwatch.elapsed() * 1000,  # StopWatch measures elapsed time in seconds
        process.returncode,
    )


def _git_subprocess_cmd(cmd, cwd=None, std_in=None):
    # type: (Union[str, list[str]], Optional[str], Optional[bytes]) -> str
    """Helper for invoking the git CLI binary."""
    if isinstance(cmd, str):
        cmd = cmd.split(" ")

    stdout, stderr, _, returncode = _git_subprocess_cmd_with_details(*cmd, cwd=cwd, std_in=None)

    if returncode == 0:
        return stdout
    raise ValueError(stderr)


def _set_safe_directory():
    try:
        _git_subprocess_cmd("config --global --add safe.directory *")
    except GitNotFoundError:
        log.error("Git executable not found, cannot extract git metadata.")
    except ValueError:
        log.error("Error setting safe directory")


def _extract_clone_defaultremotename_with_details(cwd):
    # type: (Optional[str]) -> _GitSubprocessDetails
    return _git_subprocess_cmd_with_details(
        "config", "--default", "origin", "--get", "clone.defaultRemoteName", cwd=cwd
    )


def _extract_upstream_sha(cwd=None):
    # type: (Optional[str]) -> str
    output = _git_subprocess_cmd("rev-parse @{upstream}", cwd=cwd)
    return output


def _is_shallow_repository_with_details(cwd=None):
    # type: (Optional[str]) -> Tuple[bool, float, int]
    stdout, _, duration, returncode = _git_subprocess_cmd_with_details("rev-parse", "--is-shallow-repository", cwd=cwd)
    is_shallow = stdout.strip() == "true"
    return (is_shallow, duration, returncode)


def _get_device_for_path(path):
    # type: (str) -> int
    return os.stat(path).st_dev


def _unshallow_repository_with_details(cwd=None, repo=None, refspec=None):
    # type (Optional[str], Optional[str], Optional[str]) -> _GitSubprocessDetails
    cmd = [
        "fetch",
        '--shallow-since="1 month ago"',
        "--update-shallow",
        "--filter=blob:none",
        "--recurse-submodules=no",
    ]
    if repo is not None:
        cmd.append(repo)
    if refspec is not None:
        cmd.append(refspec)

    return _git_subprocess_cmd_with_details(*cmd, cwd=cwd)


def _unshallow_repository(cwd=None, repo=None, refspec=None):
    # type (Optional[str], Optional[str], Optional[str]) -> None
    _unshallow_repository_with_details(cwd, repo, refspec)


def extract_user_info(cwd=None):
    # type: (Optional[str]) -> Dict[str, Tuple[str, str, str]]
    """Extract commit author info from the git repository in the current directory or one specified by ``cwd``."""
    # Note: `git show -s --format... --date...` is supported since git 2.1.4 onwards
    stdout = _git_subprocess_cmd(
        "show -s --format=%an|||%ae|||%ad|||%cn|||%ce|||%cd --date=format:%Y-%m-%dT%H:%M:%S%z", cwd=cwd
    )
    author_name, author_email, author_date, committer_name, committer_email, committer_date = stdout.split("|||")
    return {
        "author": (author_name, author_email, author_date),
        "committer": (committer_name, committer_email, committer_date),
    }


def extract_git_version(cwd=None):
    output = _git_subprocess_cmd("--version")
    try:
        version_info = tuple([int(part) for part in output.split()[2].split(".")])
    except ValueError:
        log.error("Git version not found, it is not following the desired version format: %s", output)
        return 0, 0, 0
    return version_info


def _extract_remote_url_with_details(cwd=None):
    # type: (Optional[str]) -> _GitSubprocessDetails
    return _git_subprocess_cmd_with_details("config", "--get", "remote.origin.url", cwd=cwd)


def extract_remote_url(cwd=None):
    remote_url, error, _, returncode = _extract_remote_url_with_details(cwd=cwd)
    if returncode == 0:
        return remote_url
    raise ValueError(error)


def _extract_latest_commits_with_details(cwd=None):
    # type: (Optional[str]) -> _GitSubprocessDetails
    return _git_subprocess_cmd_with_details("log", "--format=%H", "-n", "1000", '--since="1 month ago"', cwd=cwd)


def extract_latest_commits(cwd=None):
    # type: (Optional[str]) -> List[str]
    latest_commits, error, _, returncode = _extract_latest_commits_with_details(cwd=cwd)
    if returncode == 0:
        return latest_commits.split("\n") if latest_commits else []
    raise ValueError(error)


def get_rev_list_excluding_commits(commit_shas, cwd=None):
    return _get_rev_list_with_details(excluded_commit_shas=commit_shas, cwd=cwd)[0]


def _get_rev_list_with_details(excluded_commit_shas=None, included_commit_shas=None, cwd=None):
    # type: (Optional[list[str]], Optional[list[str]], Optional[str]) -> _GitSubprocessDetails
    command = ["rev-list", "--objects", "--filter=blob:none"]
    if extract_git_version(cwd=cwd) >= (2, 23, 0):
        command.append('--since="1 month ago"')
        command.append("--no-object-names")
    command.append("HEAD")
    if excluded_commit_shas:
        exclusions = ["^%s" % sha for sha in excluded_commit_shas]
        command.extend(exclusions)
    if included_commit_shas:
        inclusions = ["%s" % sha for sha in included_commit_shas]
        command.extend(inclusions)
    return _git_subprocess_cmd_with_details(*command, cwd=cwd)


def _get_rev_list(excluded_commit_shas=None, included_commit_shas=None, cwd=None):
    # type: (Optional[list[str]], Optional[list[str]], Optional[str]) -> str
    return _get_rev_list_with_details(
        excluded_commit_shas=excluded_commit_shas, included_commit_shas=included_commit_shas, cwd=cwd
    )[0]


def _extract_repository_url_with_details(cwd=None):
    # type: (Optional[str]) -> _GitSubprocessDetails
    """Extract the repository url from the git repository in the current directory or one specified by ``cwd``."""

    return _git_subprocess_cmd_with_details("ls-remote", "--get-url", cwd=cwd)


def extract_repository_url(cwd=None):
    # type: (Optional[str]) -> str
    """Extract the repository url from the git repository in the current directory or one specified by ``cwd``."""
    stdout, stderr, _, returncode = _extract_repository_url_with_details(cwd=cwd)
    if returncode == 0:
        return stdout
    raise ValueError(stderr)


def extract_commit_message(cwd=None):
    # type: (Optional[str]) -> str
    """Extract git commit message from the git repository in the current directory or one specified by ``cwd``."""
    # Note: `git show -s --format... --date...` is supported since git 2.1.4 onwards
    commit_message = _git_subprocess_cmd("show -s --format=%s", cwd=cwd)
    return commit_message


def extract_workspace_path(cwd=None):
    # type: (Optional[str]) -> str
    """Extract the root directory path from the git repository in the current directory or one specified by ``cwd``."""
    workspace_path = _git_subprocess_cmd("rev-parse --show-toplevel", cwd=cwd)
    return workspace_path


def extract_branch(cwd=None):
    # type: (Optional[str]) -> str
    """Extract git branch from the git repository in the current directory or one specified by ``cwd``."""
    branch = _git_subprocess_cmd("rev-parse --abbrev-ref HEAD", cwd=cwd)
    return branch


def extract_commit_sha(cwd=None):
    # type: (Optional[str]) -> str
    """Extract git commit SHA from the git repository in the current directory or one specified by ``cwd``."""
    commit_sha = _git_subprocess_cmd("rev-parse HEAD", cwd=cwd)
    return commit_sha


def extract_git_metadata(cwd=None):
    # type: (Optional[str]) -> Dict[str, Optional[str]]
    """Extract git commit metadata."""
    tags = {}  # type: Dict[str, Optional[str]]
    _set_safe_directory()
    try:
        tags[REPOSITORY_URL] = extract_repository_url(cwd=cwd)
        tags[COMMIT_MESSAGE] = extract_commit_message(cwd=cwd)
        users = extract_user_info(cwd=cwd)
        tags[COMMIT_AUTHOR_NAME] = users["author"][0]
        tags[COMMIT_AUTHOR_EMAIL] = users["author"][1]
        tags[COMMIT_AUTHOR_DATE] = users["author"][2]
        tags[COMMIT_COMMITTER_NAME] = users["committer"][0]
        tags[COMMIT_COMMITTER_EMAIL] = users["committer"][1]
        tags[COMMIT_COMMITTER_DATE] = users["committer"][2]
        tags[BRANCH] = extract_branch(cwd=cwd)
        tags[COMMIT_SHA] = extract_commit_sha(cwd=cwd)
    except GitNotFoundError:
        log.error("Git executable not found, cannot extract git metadata.")
    except ValueError as e:
        debug_mode = log.isEnabledFor(logging.DEBUG)
        stderr = str(e)
        log.error("Error extracting git metadata: %s", stderr, exc_info=debug_mode)

    return tags


def extract_user_git_metadata(env=None):
    # type: (Optional[MutableMapping[str, str]]) -> Dict[str, Optional[str]]
    """Extract git commit metadata from user-provided env vars."""
    env = os.environ if env is None else env

    branch = normalize_ref(env.get("DD_GIT_BRANCH"))
    tag = normalize_ref(env.get("DD_GIT_TAG"))

    # if DD_GIT_BRANCH is a tag, we associate its value to TAG instead of BRANCH
    if is_ref_a_tag(env.get("DD_GIT_BRANCH")):
        tag = branch
        branch = None

    tags = {}
    tags[REPOSITORY_URL] = env.get("DD_GIT_REPOSITORY_URL")
    tags[COMMIT_SHA] = env.get("DD_GIT_COMMIT_SHA")
    tags[BRANCH] = branch
    tags[TAG] = tag
    tags[COMMIT_MESSAGE] = env.get("DD_GIT_COMMIT_MESSAGE")
    tags[COMMIT_AUTHOR_DATE] = env.get("DD_GIT_COMMIT_AUTHOR_DATE")
    tags[COMMIT_AUTHOR_EMAIL] = env.get("DD_GIT_COMMIT_AUTHOR_EMAIL")
    tags[COMMIT_AUTHOR_NAME] = env.get("DD_GIT_COMMIT_AUTHOR_NAME")
    tags[COMMIT_COMMITTER_DATE] = env.get("DD_GIT_COMMIT_COMMITTER_DATE")
    tags[COMMIT_COMMITTER_EMAIL] = env.get("DD_GIT_COMMIT_COMMITTER_EMAIL")
    tags[COMMIT_COMMITTER_NAME] = env.get("DD_GIT_COMMIT_COMMITTER_NAME")

    return tags


@contextlib.contextmanager
def _build_git_packfiles_with_details(revisions, cwd=None, use_tempdir=True):
    # type: (str, Optional[str], bool) -> Generator
    basename = str(random.randint(1, 1000000))

    # check that the tempdir and cwd are on the same filesystem, otherwise git pack-objects will fail
    cwd = cwd if cwd else os.getcwd()
    tempdir = TemporaryDirectory()
    if _get_device_for_path(cwd) == _get_device_for_path(tempdir.name):
        basepath = tempdir.name
    else:
        log.debug("tempdir %s and cwd %s are on different filesystems, using cwd", tempdir.name, cwd)
        basepath = cwd

    prefix = "{basepath}/{basename}".format(basepath=basepath, basename=basename)

    log.debug("Building packfiles in prefix path: %s", prefix)

    try:
        process_details = _git_subprocess_cmd_with_details(
            "pack-objects",
            "--compression=9",
            "--max-pack-size=3m",
            prefix,
            cwd=cwd,
            std_in=revisions.encode("utf-8"),
        )
        yield prefix, process_details
    finally:
        if isinstance(tempdir, TemporaryDirectory):
            log.debug("Cleaning up temporary directory: %s", basepath)
            tempdir.cleanup()


@contextlib.contextmanager
def build_git_packfiles(revisions, cwd=None):
    # type: (str, Optional[str]) -> Generator
    with _build_git_packfiles_with_details(revisions, cwd=cwd) as (prefix, process_details):
        if process_details.returncode == 0:
            yield prefix
            return
        log.debug(
            "Failed to pack objects, command return code: %s, error: %s",
            process_details.returncode,
            process_details.stderr,
        )
        raise ValueError(process_details.stderr)
