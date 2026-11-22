from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
import os
import shutil
import tempfile


def extraction_path(directory, name):
    member = PureWindowsPath(name)
    if (member.anchor or not member.parts or ".." in member.parts
            or any(":" in part or part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                   for part in member.parts)):
        raise ValueError(f"Invalid archive member path: {name!r}")
    root = Path(directory).resolve()
    target = root.joinpath(*member.parts).resolve()
    if not target.is_relative_to(root) or target == root:
        raise ValueError(f"Archive member is outside the extraction directory: {name!r}")
    return target


@contextmanager
def staged_output(destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    temp = Path(name)
    try:
        yield temp
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)


def backup_if_needed(source, destination, enabled):
    backup = source.with_name(source.name + ".bak")
    if enabled and source.resolve() == destination.resolve() and not backup.exists():
        with staged_output(backup) as temp:
            shutil.copyfile(source, temp)
