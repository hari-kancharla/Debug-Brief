"""Conservative destructive-command detection for redo safeguards."""

from __future__ import annotations

import contextlib
import os
import shlex
import sys
from typing import Any, List, Optional, Tuple

from .filters import _strip_shell_comment
from .markdown import code_span, normalize_free_text

_REMOVE_COMMANDS = {"rm", "rmdir", "shred", "unlink"}
_SHELL_COMMANDS = {"sh", "bash", "zsh", "dash", "ksh"}
_WRAPPERS = {
    "command",
    "builtin",
    "sudo",
    "doas",
    "nohup",
    "time",
    "exec",
    "nice",
    "ionice",
    "stdbuf",
    "setsid",
}
_ENV_VALUE_OPTIONS = {
    "-u",
    "--unset",
    "-C",
    "--chdir",
    "-P",
    "--default-signal",
}
_ENV_FLAG_OPTIONS = {"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"}
_ENV_AMBIGUOUS = "__debugbrief_ambiguous_env__"
_WRAPPER_VALUE_OPTIONS = {
    "-u",
    "--user",
    "-g",
    "--group",
    "-a",
    "--argv0",
}
_SUDO_VALUE_OPTIONS = {
    "-u",
    "--user",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-C",
    "--close-from",
    "-T",
    "--command-timeout",
    "-R",
    "--chroot",
    "-D",
    "--chdir",
}
_NICE_VALUE_OPTIONS = {"-n", "--adjustment"}
_IONICE_VALUE_OPTIONS = {"-c", "--class", "-n", "--classname", "-t", "--ignore"}
_TIME_VALUE_OPTIONS = {"-f", "--format", "-o", "--output"}
_TIMEOUT_VALUE_OPTIONS = {
    "-k",
    "--kill-after",
    "-s",
    "--signal",
}
_PUBLISH_COMMANDS = {
    "deploy",
    "publish",
    "release",
    "upload",
    "serverless",
    "vercel",
    "netlify",
    "heroku",
    "flyctl",
    "doctl",
}
_BUILD_COMMANDS = {"make", "just", "task", "mise", "tox", "npm", "pnpm", "yarn", "bun"}
_PACKAGE_COMMANDS = {"cargo", "poetry", "twine", "gem", "pip", "go", "nuget", "dotnet"}
_GIT_GLOBAL_VALUE_OPTIONS = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--super-prefix",
}
_GIT_CLEAN_VALUE_OPTIONS = {"-e", "--exclude"}
_DOCKER_GLOBAL_VALUE_OPTIONS = {"-H", "--host", "--context", "--config", "--log-level"}
_DOCKER_COMPOSE_VALUE_OPTIONS = {
    "--profile",
    "--project-name",
    "--env-file",
    "--project-directory",
    "-p",
    "-f",
    "--file",
}
_DOCKER_EXEC_VALUE_OPTIONS = {
    "-u",
    "--user",
    "-w",
    "--workdir",
    "-e",
    "--env",
}
_DOCKER_RUN_VALUE_OPTIONS = {
    "--name",
    "--network",
    "--net",
    "--volume",
    "-v",
    "--env",
    "-e",
    "--env-file",
    "--entrypoint",
    "--user",
    "-u",
    "--workdir",
    "-w",
    "--publish",
    "-p",
    "--mount",
    "--label",
    "-l",
    "--platform",
    "--pull",
    "--runtime",
    "--memory",
    "-m",
    "--cpus",
    "--add-host",
    "--dns",
    "--device",
    "--gpus",
    "--ipc",
    "--pid",
    "--uts",
    "--cgroupns",
    "--security-opt",
    "--cap-add",
    "--cap-drop",
    "--log-driver",
}
_TERRAFORM_VALUE_OPTIONS = {"-chdir"}
_XARGS_VALUE_OPTIONS = {
    "-a",
    "-d",
    "-E",
    "-I",
    "-L",
    "-n",
    "-P",
    "-s",
}
_KUBECTL_GLOBAL_VALUE_OPTIONS = {
    "-n",
    "--namespace",
    "--context",
    "--cluster",
    "--user",
    "--kubeconfig",
    "--as",
    "--as-group",
    "--token",
}
_KUBECTL_EXEC_VALUE_OPTIONS = {
    "-n",
    "--namespace",
    "--context",
    "--cluster",
    "--user",
    "--kubeconfig",
    "--as",
    "-e",
    "--env",
    "--container",
    "-c",
}
_HELM_GLOBAL_VALUE_OPTIONS = {
    "--kubeconfig",
    "--kube-context",
    "--registry-config",
    "--repository-config",
    "--repository-cache",
    "--namespace",
    "-n",
    "--output",
    "-o",
    "--post-renderer",
    "--burst-limit",
}
_SHELL_RESERVED = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "while",
    "until",
    "do",
    "done",
    "case",
    "esac",
    "select",
    "function",
    "coproc",
}


def _basename(token: str) -> str:
    return os.path.basename(normalize_free_text(token).rstrip("/\\"))


def _tokens(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except (TypeError, ValueError):
        return normalize_free_text(command).split()


def _is_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    name = token.split("=", 1)[0]
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(
        char.isalnum() or char == "_" for char in name
    )


def _skip_option(tokens: List[str], index: int, value_options: set) -> Optional[int]:
    token = tokens[index]
    if token in value_options:
        return min(index + 2, len(tokens))
    if token.startswith("-") and not token.startswith("--") and any(
        option.startswith("-")
        and not option.startswith("--")
        and len(option) == 2
        and token.startswith(option)
        and len(token) > 2
        for option in value_options
    ):
        return index + 1
    if token.startswith("--") and "=" in token:
        return index + 1
    return None


def _pop_options(tokens: List[str], value_options: set) -> None:
    while tokens and tokens[0].startswith("-"):
        option = tokens.pop(0)
        if option in value_options and tokens:
            tokens.pop(0)


def _unwrap_env(tokens: List[str]) -> List[str]:
    result = list(tokens)
    result.pop(0)
    while result and result[0].startswith("-"):
        token = result[0]
        if token == "--":
            result.pop(0)
            break
        if token in {"-S", "--split-string"} or token.startswith("--split-string="):
            result.pop(0)
            if token in {"-S", "--split-string"}:
                if not result:
                    return [_ENV_AMBIGUOUS]
                payload = result.pop(0)
            else:
                payload = token.split("=", 1)[1]
            try:
                result[0:0] = shlex.split(payload)
            except ValueError:
                return [_ENV_AMBIGUOUS]
            continue
        if token.startswith("-S") and len(token) > 2:
            result.pop(0)
            try:
                result[0:0] = shlex.split(token[2:])
            except ValueError:
                return [_ENV_AMBIGUOUS]
            continue
        if token in _ENV_FLAG_OPTIONS:
            result.pop(0)
            continue
        next_index = _skip_option(result, 0, _ENV_VALUE_OPTIONS)
        if next_index is None or next_index == 0:
            return [_ENV_AMBIGUOUS]
        del result[:next_index]
    return result


def _unwrap(tokens: List[str]) -> List[str]:
    result = list(tokens)
    for _ in range(16):
        while result and _is_assignment(result[0]):
            result.pop(0)
        if not result:
            return []
        name = _basename(result[0])
        if name == "command" and len(result) > 1 and result[1] in {"-v", "-V"}:
            return []
        if name == "env":
            result = _unwrap_env(result)
            if result == [_ENV_AMBIGUOUS]:
                return result
            continue
        if name == "busybox":
            result.pop(0)
            _pop_options(result, set())
            continue
        if name in {"sudo", "doas"}:
            result.pop(0)
            _pop_options(result, _SUDO_VALUE_OPTIONS)
            continue
        if name == "nice":
            result.pop(0)
            _pop_options(result, _NICE_VALUE_OPTIONS)
            continue
        if name == "ionice":
            result.pop(0)
            _pop_options(result, _IONICE_VALUE_OPTIONS)
            continue
        if name == "time":
            result.pop(0)
            _pop_options(result, _TIME_VALUE_OPTIONS)
            continue
        if name == "timeout":
            result.pop(0)
            _pop_options(result, _TIMEOUT_VALUE_OPTIONS)
            if result and not result[0].startswith("-"):
                result.pop(0)
            if result and result[0] == "--":
                result.pop(0)
            continue
        if name in _WRAPPERS:
            result.pop(0)
            _pop_options(result, _WRAPPER_VALUE_OPTIONS)
            continue
        break
    return result


def _subcommand(
    tokens: List[str], value_options: set
) -> Tuple[Optional[str], Optional[List[str]]]:
    if not tokens:
        return "", []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if _is_help_flag(token):
            return "", []
        if token.startswith("-"):
            next_index = _skip_option(tokens, index, value_options)
            if next_index is None:
                return None, None
            index = next_index
            continue
        return token.lower(), tokens[index + 1 :]
    return "", tokens[index:]


def _first_command(tokens: List[str], value_options: set) -> Optional[List[str]]:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if _is_help_flag(token):
            return []
        if token.startswith("-"):
            next_index = _skip_option(tokens, index, value_options)
            if next_index is None:
                return None
            index = next_index
            continue
        break
    return tokens[index:]


def _has_unbalanced_quotes(command: str) -> bool:
    command = _strip_shell_comment(command)
    single = double = escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if character == "\\" and not single:
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
    return single or double or escaped


def _has_shell_syntax(command: str) -> bool:
    command = _strip_shell_comment(command)
    single = double = escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            index += 1
            continue
        if character == "'" and not double:
            single = not single
            index += 1
            continue
        if character == '"' and not single:
            double = not double
            index += 1
            continue
        if single:
            index += 1
            continue
        if double:
            if character == "`":
                return True
            if character == "$" and index + 1 < len(command):
                if command[index + 1] in "{(":
                    return True
                if command[index + 1].isalnum() or command[index + 1] == "_":
                    return True
            index += 1
            continue
        if character in ";&|<>()\n`":
            return True
        if character == "$" and not double:
            if index + 1 >= len(command) or command[index + 1] in "{(":
                return True
            if command[index + 1].isalnum() or command[index + 1] == "_":
                return True
        index += 1
    return False


def _has_substitution(command: str) -> bool:
    command = _strip_shell_comment(command)
    single = double = escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if character == "\\" and not single:
            escaped = True
            index += 1
            continue
        if character == "'" and not double:
            single = not single
            index += 1
            continue
        if character == '"' and not single:
            double = not double
            index += 1
            continue
        if single:
            index += 1
            continue
        if character == "`":
            return True
        if (
            character == "$"
            and index + 1 < len(command)
            and command[index + 1] in "{("
        ):
            return True
        if character in "<>" and index + 1 < len(command) and command[index + 1] == "(":
            return True
        index += 1
    return False


def _split_shell_segments(command: str) -> List[str]:
    command = _strip_shell_comment(command)
    segments: List[str] = []
    current: List[str] = []
    single = double = escaped = False
    for character in command:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and not single:
            current.append(character)
            escaped = True
            continue
        if character == "'" and not double:
            single = not single
            current.append(character)
        elif character == '"' and not single:
            double = not double
            current.append(character)
        elif character in ";&|\n" and not single and not double:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
        else:
            current.append(character)
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _is_help_flag(token: str) -> bool:
    return token in {"--help", "-h", "-?", "/?", "--usage", "--version"}


def _is_dry_run_flag(token: str) -> bool:
    lower = token.lower()
    if lower == "--dry-run":
        return True
    if not lower.startswith("--dry-run="):
        return False
    return lower.split("=", 1)[1] not in {"false", "0", "no", "none"}


def _has_preview_flag(
    tokens: List[str], value_options: set, stop_at_separator: bool = False
) -> bool:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if stop_at_separator and token == "--":
            return False
        if (token == "-n" and "-n" not in value_options) or _is_dry_run_flag(token):
            return True
        if token.startswith("-"):
            next_index = _skip_option(tokens, index, value_options)
            if next_index is None:
                return False
            index = next_index
            continue
        index += 1
    return False


def _has_positional_operand(tokens: List[str]) -> bool:
    after_separator = False
    for token in tokens:
        if token == "--":
            after_separator = True
        elif after_separator or not token.startswith("-"):
            return True
    return False


def _has_destructive_option(tokens: List[str]) -> bool:
    return any(
        token in {"--force", "--recursive"}
        or token.startswith(("--force=", "--recursive="))
        or (token.startswith("-") and not token.startswith("--") and "f" in token[1:])
        or (token.startswith("-") and not token.startswith("--") and "r" in token[1:])
        for token in tokens
    )


def _is_preview_command(tokens: List[str]) -> bool:
    if not tokens:
        return False
    name = _basename(tokens[0])
    lowered = [token.lower() for token in tokens]
    if (
        name in _SHELL_COMMANDS or name in {"eval", "source", "."}
    ) and any(_is_help_flag(token) for token in tokens[1:]):
        return not any(
            token == "-c" or (token.startswith("-") and "c" in token[1:])
            for token in tokens[1:])
    if name in _REMOVE_COMMANDS and any(_is_help_flag(token) for token in tokens[1:]):
        return not _has_positional_operand(tokens[1:]) and not _has_destructive_option(
            tokens[1:]
        )
    if any(_is_help_flag(token) for token in tokens[1:]):
        if name == "git":
            subcommand, _sub_args = _subcommand(tokens, _GIT_GLOBAL_VALUE_OPTIONS)
            if subcommand is None:
                return False
            return subcommand not in {
                "reset",
                "clean",
                "push",
                "restore",
                "checkout",
                "switch",
                "branch",
                "update-ref",
                "worktree",
                "rm",
            }
        return name not in {"docker", "docker-compose", "kubectl", "terraform", "tofu", "helm"}
    if name == "git":
        subcommand, sub_args = _subcommand(tokens, _GIT_GLOBAL_VALUE_OPTIONS)
        if subcommand is None or sub_args is None:
            return False
        if subcommand in {"status", "diff", "log", "show", "help", "version"}:
            return True
        if subcommand == "clean" and _has_preview_flag(
            sub_args, _GIT_CLEAN_VALUE_OPTIONS
        ):
            return True
    if name in {"terraform", "tofu"}:
        subcommand = lowered[1] if len(lowered) > 1 else ""
        if subcommand in {"plan", "validate", "version", "fmt", "show"}:
            return True
    if name == "kubectl" and _has_preview_flag(
        tokens[1:], _KUBECTL_GLOBAL_VALUE_OPTIONS | _KUBECTL_EXEC_VALUE_OPTIONS, True
    ):
        return True
    if name == "docker":
        subcommand = lowered[1] if len(lowered) > 1 else ""
        if subcommand in {"ps", "images", "version", "info", "inspect", "logs"}:
            return True
        if subcommand == "compose":
            compose_sub = lowered[2] if len(lowered) > 2 else ""
            if compose_sub in {"config", "logs", "ps", "top", "port", "version"}:
                return True
    if name == "helm":
        subcommand = lowered[1] if len(lowered) > 1 else ""
        if subcommand in {
            "diff",
            "get",
            "history",
            "list",
            "show",
            "status",
            "template",
            "version",
        }:
            return True
    return False


def _git_destructive(args: List[str]) -> bool:
    if not args:
        return False
    subcommand = args[0].lower()
    options = args[1:]
    if subcommand == "reset":
        return any(
            option == "--hard" or option.startswith("--hard=") for option in options
        )
    if subcommand in {"clean", "push", "restore", "checkout", "rm"}:
        return True
    if subcommand == "switch":
        return any(
            option in {"--discard-changes", "-C", "--force-create", "--force", "-f"}
            or option.startswith(("-C", "-f", "--discard-changes=", "--force="))
            for option in options
        )
    if subcommand == "branch":
        return any(
            option in ("-d", "-D", "--delete", "--force-delete")
            or option.startswith("-D")
            for option in options
        )
    if subcommand == "update-ref":
        return any(option in ("-d", "--delete") for option in options)
    if subcommand == "worktree":
        return len(args) > 1 and args[1].lower() in {"remove", "delete"}
    return False


def _docker_entrypoint_destructive(args: List[str], depth: int) -> bool:
    for index, token in enumerate(args):
        if token == "--entrypoint" and index + 1 < len(args):
            candidate = [args[index + 1], *args[index + 2 :]]
            if _is_destructive_tokens(candidate, depth + 1, True):
                return True
        if token.startswith("--entrypoint="):
            candidate = [token.split("=", 1)[1], *args[index + 1 :]]
            if _is_destructive_tokens(candidate, depth + 1, True):
                return True
    return False


def _docker_destructive(args: List[str], depth: int = 0) -> bool:
    if not args:
        return False
    if _docker_entrypoint_destructive(args, depth):
        return True
    subcommand = args[0].lower()
    if subcommand in {
        "rm",
        "rmi",
        "kill",
        "stop",
        "prune",
        "down",
        "restart",
        "push",
    }:
        return True
    if subcommand == "compose":
        compose_args = _first_command(args[1:], _DOCKER_COMPOSE_VALUE_OPTIONS)
        if compose_args is None:
            return True
        if not compose_args:
            return False
        compose_sub = compose_args[0].lower()
        if compose_sub in {"down", "rm", "kill", "stop", "restart", "push"}:
            return True
        if compose_sub in {"run", "exec"}:
            command_args = _first_command(
                compose_args[1:], _DOCKER_RUN_VALUE_OPTIONS
            )
            if command_args is None:
                return True
            nested_command = (
                _first_command(command_args[1:], _DOCKER_EXEC_VALUE_OPTIONS)
                if len(command_args) > 1
                else []
            )
            if nested_command is None:
                return True
            return bool(
                nested_command
                and _is_destructive_tokens(nested_command, depth + 1, True)
            )
        return False
    if subcommand in {
        "container",
        "image",
        "volume",
        "network",
        "system",
        "builder",
        "buildx",
        "context",
    }:
        nested = args[1:]
        if not nested:
            return False
        nested_sub = nested[0].lower()
        if nested_sub in {
            "rm",
            "rmi",
            "prune",
            "kill",
            "stop",
            "down",
            "restart",
            "remove",
            "push",
        }:
            return True
        if nested_sub in {"run", "exec"}:
            command_args = _first_command(nested[1:], _DOCKER_RUN_VALUE_OPTIONS)
            if command_args is None:
                return True
            nested_command = (
                _first_command(command_args[1:], _DOCKER_EXEC_VALUE_OPTIONS)
                if len(command_args) > 1
                else []
            )
            if nested_command is None:
                return True
            return bool(
                nested_command
                and _is_destructive_tokens(nested_command, depth + 1, True)
            )
        return False
    if subcommand in {"run", "create", "exec"}:
        command_args = _first_command(args[1:], _DOCKER_RUN_VALUE_OPTIONS)
        if command_args is None:
            return True
        nested_command = (
            _first_command(command_args[1:], _DOCKER_EXEC_VALUE_OPTIONS)
            if len(command_args) > 1
            else []
        )
        if nested_command is None:
            return True
        return bool(
            nested_command
            and _is_destructive_tokens(nested_command, depth + 1, True)
        )

    return False


def _kubectl_destructive(args: List[str], depth: int = 0) -> bool:
    if not args:
        return False
    verb = args[0].lower()
    if verb == "exec":
        remainder = args[1:]
        if "--" in remainder:
            command = remainder[remainder.index("--") + 1 :]
        else:
            positional = _first_command(remainder, _KUBECTL_EXEC_VALUE_OPTIONS)
            if positional is None:
                return True
            command = positional[1:] if positional else []
        return bool(command and _is_destructive_tokens(command, depth + 1, True))
    if verb == "run":
        remainder = args[1:]
        if "--" in remainder:
            command = remainder[remainder.index("--") + 1 :]
            return bool(command and _is_destructive_tokens(command, depth + 1, True))
        return True
    return verb in {
        "apply",
        "delete",
        "drain",
        "cordon",
        "uncordon",
        "taint",
        "patch",
        "replace",
        "edit",
        "scale",
        "rollout",
        "set",
        "expose",
        "cp",
    }


def _terraform_destructive(args: List[str]) -> bool:
    if not args:
        return False
    subcommand = args[0].lower()
    if subcommand in {"apply", "destroy", "import", "taint", "untaint", "force-unlock"}:
        return True
    if subcommand == "state":
        return len(args) > 1 and args[1].lower() in {"rm", "mv", "replace"}
    if subcommand == "workspace":
        return len(args) > 1 and args[1].lower() in {"new", "delete"}
    return False


def _shell_command_index(tokens: List[str]) -> Optional[int]:
    for index in range(1, len(tokens)):
        token = tokens[index]
        if token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if token.startswith("-") and not token.startswith("--") and "c" in token[1:]:
            return index + 1 if index + 1 < len(tokens) else None
    return None


def _is_interpreter(name: str) -> bool:
    interpreters = {"python", "node", "ruby", "perl", "php"}
    if name in interpreters:
        return True
    return any(
        name.startswith(interpreter)
        and name[len(interpreter) :].replace(".", "").isdigit()
        and bool(name[len(interpreter) :])
        for interpreter in interpreters
    )


def _has_inline_code(tokens: List[str]) -> bool:
    return any(
        token in {"-c", "-e", "-r", "-E", "--eval"}
        or token.startswith("--eval=")
        or (token.startswith("-") and not token.startswith("--") and "c" in token[1:])
        for token in tokens[1:]
    )


def _shell_requires_confirmation(tokens: List[str], depth: int) -> bool:
    if _shell_command_index(tokens) is not None:
        return True
    return not any(
        _is_help_flag(token) or token == "--version" for token in tokens[1:]
    )


def _is_publish_style(name: str, args: List[str]) -> bool:
    if name in _PUBLISH_COMMANDS:
        return True
    if name in _BUILD_COMMANDS and any(
        argument.lower() in {"publish", "deploy", "release", "upload"}
        for argument in args
    ):
        return True
    if name in _PACKAGE_COMMANDS and any(
        argument.lower() in {"publish", "deploy", "release", "upload", "push"}
        for argument in args
    ):
        return True
    if name == "git":
        return bool(args) and args[0].lower() in {"push", "publish", "deploy", "release"}
    if name == "gh":
        if not args:
            return False
        group = args[0].lower()
        action = args[1].lower() if len(args) > 1 else ""
        return (group, action) in {
            ("pr", "create"),
            ("pr", "comment"),
            ("release", "create"),
            ("release", "upload"),
            ("workflow", "run"),
        }
    if name in {"aws", "gcloud", "az"}:
        return any(argument.lower() in {"deploy", "publish", "release"} for argument in args)
    return name in {"rsync", "scp"} and any(
        argument in {"--delete", "--remove-source-files"} for argument in args
    )


def _is_destructive_tokens(
    tokens: List[str], depth: int = 0, shell_context: bool = False
) -> bool:
    if depth > 6:
        return True
    if not tokens:
        return False
    effective = _unwrap(tokens)
    if not effective:
        return False
    name = _basename(effective[0])
    if name == _ENV_AMBIGUOUS:
        return True
    args = effective[1:]
    if name in _SHELL_COMMANDS:
        if _is_preview_command(effective):
            return False
        return _shell_requires_confirmation(effective, depth)
    if name in {"eval", "source", "."}:
        return True
    if _is_interpreter(name) and _has_inline_code(effective):
        return True
    if name in {"make", "just", "task"} and any(
        argument.lower() in {"clean", "distclean", "realclean"} for argument in args
    ):
        return True
    if name == "cargo" and args and args[0].lower() == "clean":
        return True
    if name in {"aws", "gcloud", "az"}:
        lowered_args = [argument.lower() for argument in args]
        if name == "aws" and len(lowered_args) >= 2 and lowered_args[:2] == ["s3", "rm"]:
            return True
        if name == "aws" and len(lowered_args) >= 2 and lowered_args[:2] == ["s3", "sync"]:
            return "--delete" in lowered_args
        if (
            name == "gcloud"
            and len(lowered_args) >= 2
            and lowered_args[0] == "storage"
            and lowered_args[1] in {"rm", "delete"}
        ):
            return True
        if name == "az" and "delete" in lowered_args:
            return True
    if _is_preview_command(effective):
        return False
    if name in _REMOVE_COMMANDS:
        return True
    if shell_context and (
        any(token in _SHELL_RESERVED for token in effective)
        or effective[0] == "!"
    ):
        return True
    if name == "git":
        subcommand, sub_args = _subcommand(effective, _GIT_GLOBAL_VALUE_OPTIONS)
        if subcommand is None or sub_args is None:
            return True
        return _git_destructive([subcommand, *sub_args])
    if name == "docker-compose":
        compose_args = _first_command(effective[1:], _DOCKER_COMPOSE_VALUE_OPTIONS)
        if compose_args is None:
            return True
        return _docker_destructive(["compose", *compose_args], depth)
    if name == "docker":
        subcommand, sub_args = _subcommand(effective, _DOCKER_GLOBAL_VALUE_OPTIONS)
        if subcommand is None or sub_args is None:
            return True
        return _docker_destructive([subcommand, *sub_args], depth)
    if name == "kubectl":
        subcommand, sub_args = _subcommand(effective, _KUBECTL_GLOBAL_VALUE_OPTIONS)
        if subcommand is None or sub_args is None:
            return True
        return _kubectl_destructive([subcommand, *sub_args], depth)
    if name in {"terraform", "tofu"}:
        subcommand, sub_args = _subcommand(effective, _TERRAFORM_VALUE_OPTIONS)
        if subcommand is None or sub_args is None:
            return True
        return _terraform_destructive([subcommand, *sub_args])
    if name == "helm":
        helm_args = _first_command(args, _HELM_GLOBAL_VALUE_OPTIONS)
        if helm_args is None:
            return True
        return bool(helm_args) and helm_args[0].lower() in {
            "delete",
            "install",
            "rollback",
            "uninstall",
            "upgrade",
        }
    if name == "xargs":
        command = _first_command(args, _XARGS_VALUE_OPTIONS)
        if command is None:
            return True
        return bool(command and _is_destructive_tokens(command, depth + 1, True))
    if name == "find":
        if "-delete" in args:
            return True
        for index, argument in enumerate(args):
            if argument in {"-exec", "-execdir"} and index + 1 < len(args):
                return _is_destructive_tokens(args[index + 1 :], depth + 1, True)
        return False
    return _is_publish_style(name, args)


def is_destructive_command(command: str, used_shell: Optional[bool] = None) -> bool:
    """Return whether redo needs confirmation for a stored command."""
    if not isinstance(command, str) or not command.strip():
        return False
    shell_context = used_shell is not False
    if _has_unbalanced_quotes(command):
        return True
    if _has_substitution(command):
        return True
    if _has_shell_syntax(command):
        return True
    segments = _split_shell_segments(command)
    if len(segments) > 1:
        return True
    for segment in segments:
        if _is_destructive_tokens(
            _tokens(segment), shell_context=shell_context
        ):
            return True
    return False


def _prompt(message: str) -> None:
    print(message, file=sys.stderr, end="", flush=True)


def confirm_redo(
    command: str,
    assume_yes: bool = False,
    stdin: Optional[Any] = None,
    used_shell: Optional[bool] = None,
) -> bool:
    """Confirm a destructive redo, declining safely when no answer is available."""
    if assume_yes or not is_destructive_command(command, used_shell=used_shell):
        return True
    source = sys.stdin if stdin is None else stdin
    try:
        closed = source is None or getattr(source, "closed", False)
    except (AttributeError, KeyboardInterrupt, OSError, ValueError):
        closed = True
    if closed:
        with contextlib.suppress(KeyboardInterrupt):
            _prompt(
                "Refusing to redo a destructive or ambiguous command without "
                "confirmation. Use --yes.\n"
            )
        return False
    try:
        _prompt(f"Redo destructive or ambiguous command {_display(command)}? [y/N] ")
        answer = source.readline() if stdin is not None else input()
    except (AttributeError, EOFError, KeyboardInterrupt, OSError, ValueError):
        answer = ""
    if not isinstance(answer, str) or answer.strip().lower() not in {"y", "yes"}:
        with contextlib.suppress(KeyboardInterrupt):
            _prompt("Redo aborted; the command was not run.\n")
        return False
    return True


def _display(command: str) -> str:
    return code_span(" ".join(normalize_free_text(command).split()))
