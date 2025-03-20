"""Workspace module"""

from pathlib import Path
from typing import List

import sublime

from .document import TextChange

PathStr = str


def get_workspace_path(view: sublime.View, return_parent: bool = True) -> str:
    """Get workspace path for view.

    Params:
        view: View
            target
        return_parent: bool
            if True, return parent folder if view not opened in 'Window folders'

    Returns:
        workspace path or empty string
    """
    file_name = view.file_name()
    if not file_name:
        return ""

    if folders := [
        folder for folder in view.window().folders() if file_name.startswith(folder)
    ]:
        # File is opened in multiple folder
        return max(folders)

    if not return_parent:
        return ""

    return str(Path(file_name).parent)


class FileUpdater:
    def __init__(self, file_name: PathStr) -> None:
        self.path = Path(file_name)

    def apply(self, changes: List[TextChange]) -> None:
        old_text = self.path.read_text()
        new_text = self._update_text(old_text, changes)
        self.path.write_text(new_text)

    def _get_offset(self, lines: List[str], row: int, column: int) -> int:
        line_offset = sum([len(l) for l in lines[:row]])
        return line_offset + column

    def _update_text(self, source: str, changes: List[TextChange]) -> str:
        temp = source

        for change in changes:
            lines = temp.splitlines(keepends=True)
            start_offset = self._get_offset(lines, *change.start)
            end_offset = self._get_offset(lines, *change.end)

            temp = f"{temp[:start_offset]}{change.text}{temp[end_offset:]}"

        return temp


def update_document(file_name: PathStr, changes: List[TextChange]):
    """update document"""
    updater = FileUpdater(file_name)
    updater.apply(changes)


def create_document(file_name: PathStr, text: str = ""):
    """create document"""
    path = Path(file_name)
    path.touch()
    path.write_text(text)


def rename_document(old_name: PathStr, new_name: PathStr):
    """rename document"""
    path = Path(old_name)
    path.rename(new_name)

    # Sublime Text didn't update the view target if renamed
    for window in sublime.windows():
        for view in [v for v in window.views() if v.file_name() == old_name]:
            view.retarget(new_name)


def delete_document(file_name: PathStr):
    """delete document"""
    path = Path(file_name)
    path.unlink()

    # Sublime Text didn't close deleted file
    for window in sublime.windows():
        for view in [v for v in window.views() if v.file_name() == file_name]:
            view.close()
