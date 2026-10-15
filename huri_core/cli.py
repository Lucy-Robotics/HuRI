"""The `huri` command: runs the commands installed packages provide.

huri-core registers no command itself. A package adds one in its
pyproject.toml:

    [project.entry-points."huri.commands"]
    hub = "huri_hub.cli:main"

and `huri hub search gesture` then calls `huri_hub.cli.main(["search",
"gesture"])`. The target takes the remaining arguments as a list and returns
the exit code (None counts as 0). The first line of its docstring is what
`huri --help` shows next to its name.
"""

import inspect
import sys
from importlib.metadata import EntryPoint
from typing import Any, Callable, Dict, List, Optional

from huri_core.core.plugins import PluginError, _dist, _entry_points

COMMANDS_GROUP = "huri.commands"

# The only hint core gives about a command; it knows nothing else of them.
WHERE = (
    f"Commands come from installed packages (entry point group "
    f"{COMMANDS_GROUP!r}): the huri-hub package of huri-modules, for instance, "
    "provides `huri hub`."
)

Command = Callable[[List[str]], Any]


class CommandError(RuntimeError):
    """A command is unknown or cannot be loaded."""


def installed_commands() -> Dict[str, EntryPoint]:
    """Installed commands by name. The same name registered with two targets
    is a PluginError, as for modules and events."""

    return dict(sorted(_entry_points(COMMANDS_GROUP).items()))


def load_command(ep: EntryPoint) -> Command:
    dist = _dist(ep)
    try:
        fn: Command = ep.load()
    except Exception as e:
        raise CommandError(
            f"the command {ep.name!r} (from {dist}) cannot be imported: "
            f"{type(e).__name__}: {e}. Reinstall or uninstall {dist}."
        ) from e
    if not callable(fn):
        raise CommandError(
            f"the command {ep.name!r} (from {dist}) points to {ep.value}, "
            "which is not callable"
        )
    return fn


def summary(ep: EntryPoint) -> str:
    """First line of the command's docstring, or else of its module's."""

    try:
        fn = load_command(ep)
    except CommandError:
        return f"(cannot be imported, from {_dist(ep)})"
    doc = inspect.getdoc(fn) or inspect.getdoc(inspect.getmodule(fn)) or ""
    return doc.strip().split("\n", 1)[0]


def usage(commands: Dict[str, EntryPoint]) -> str:
    lines = ["usage: huri <command> [args...]", ""]
    if commands:
        width = max(map(len, commands))
        lines.append("installed commands:")
        for name, ep in commands.items():
            lines.append(f"  {name:<{width}}  {summary(ep)}".rstrip())
    else:
        lines.append("no command is installed.")
    return "\n".join([*lines, "", WHERE])


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        commands = installed_commands()
    except PluginError as e:
        print(f"huri: {e}", file=sys.stderr)
        return 1

    if not args or args[0] in ("-h", "--help"):
        # Asked for: on stdout, success. Missing command: on stderr, usage error.
        print(usage(commands), file=sys.stdout if args else sys.stderr)
        return 0 if args else 2

    name, rest = args[0], args[1:]
    if name not in commands:
        installed = ", ".join(commands) or "none"
        print(
            f"huri: unknown command {name!r} (installed: {installed}). {WHERE}",
            file=sys.stderr,
        )
        return 2
    try:
        command = load_command(commands[name])
    except CommandError as e:
        print(f"huri: {e}", file=sys.stderr)
        return 1
    code = command(rest)
    return 0 if code is None else int(code)


if __name__ == "__main__":
    sys.exit(main())
