"""Workspace module"""

import logging
import queue
import threading
import time
from collections import namedtuple
from dataclasses import dataclass, asdict
from functools import wraps, lru_cache
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import urlparse, unquote_plus
from urllib.request import url2pathname

import sublime

from .constant import (
    LOGGING_CHANNEL,
    PACKAGE_NAME,
    LANGUAGE_ID,
    VIEW_SELECTOR,
    COMMAND_PREFIX,
)

PathStr = str
DocumentURI = str
RowColIndex = namedtuple("RowColIndex", ["row", "column"])
LOGGER = logging.getLogger(LOGGING_CHANNEL)


@lru_cache(128)
def path_to_uri(path: PathStr) -> DocumentURI:
    """convert path to uri"""
    return Path(path).as_uri()


@lru_cache(128)
def uri_to_path(uri: DocumentURI) -> PathStr:
    """convert uri to path"""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("url scheme must be 'file'")

    return url2pathname(unquote_plus(parsed.path))


@dataclass
class TextChange:
    """TextChange

    Properties:
        start: RowColIndex
            start of change index
        end: RowColIndex
            end of change index
        text: str
            new text
        length: int
            length of changed text
    """

    start: RowColIndex
    end: RowColIndex
    text: str
    length: int

    def __post_init__(self):
        # possibly if user pass 'start' and 'end' as tuple
        self.start = RowColIndex(*self.start)
        self.end = RowColIndex(*self.end)


class TextHighlighter:
    REGIONS_KEY = f"{PACKAGE_NAME}_REGIONS"

    def __init__(self, view: sublime.View):
        self.view = view

    def apply(self, regions: List[sublime.Region]):
        self.view.add_regions(
            key=self.REGIONS_KEY,
            regions=regions,
            scope="Comment",
            icon="dot",
            flags=sublime.DRAW_NO_FILL
            | sublime.DRAW_NO_OUTLINE
            | sublime.DRAW_SQUIGGLY_UNDERLINE,
        )

    def clear(self):
        self.view.erase_regions(TextHighlighter.REGIONS_KEY)

    @staticmethod
    def clear_all():
        """clear all text hightlight"""
        for window in sublime.windows():
            for view in window.views(include_transient=True):
                view.erase_regions(TextHighlighter.REGIONS_KEY)


class UnbufferedDocument:
    def __init__(self, file_name: PathStr):
        self.file_name = file_name
        self.text = Path(file_name).read_text()

    def apply_text_changes(self, changes: List[TextChange]):
        self.text = self._update_text(self.text, changes)

    @staticmethod
    def get_offset(lines: List[str], row: int, column: int) -> int:
        line_offset = sum([len(l) for l in lines[:row]])
        return line_offset + column

    @staticmethod
    def _update_text(source: str, changes: List[TextChange]) -> str:
        temp = source

        for change in changes:
            try:
                start = change.start
                end = change.end
                new_text = change.text

                start_line, start_character = start[0], start[1]
                end_line, end_character = end[0], end[1]

            except KeyError as err:
                raise Exception(f"invalid params {err}") from err

            lines = source.splitlines(keepends=True)
            start_offset = UnbufferedDocument.get_offset(
                lines, start_line, start_character
            )
            end_offset = UnbufferedDocument.get_offset(lines, end_line, end_character)
            temp = f"{temp[:start_offset]}{new_text}{temp[end_offset:]}"

        return temp

    def save(self):
        Path(self.file_name).write_text(self.text)


class BufferedDocument:
    VIEW_SETTINGS = {
        "show_definitions": False,
        "auto_complete_use_index": False,
    }

    def __init__(self, view: sublime.View):
        self.view = view
        self.file_name = self.view.file_name()
        self.language_id = LANGUAGE_ID

        self.view.settings().update(self.VIEW_SETTINGS)
        self._cached_completion = queue.Queue(maxsize=1)

    @property
    def window(self) -> sublime.Window:
        return self.view.window()

    @property
    def version(self) -> int:
        return self.view.change_count()

    @property
    def text(self):
        # wait until complete loaded
        while self.view.is_loading():
            time.sleep(0.5)

        return self.view.substr(sublime.Region(0, self.view.size()))

    def save(self):
        self.view.run_command("save")

    def show_popup(self, text: str, row: int, col: int):
        point = self.view.text_point(row, col)
        self.view.run_command(
            "marked_popup", {"location": point, "text": text, "markup": "markdown"}
        )

    def show_completion(self, items: List[sublime.CompletionItem]):
        try:
            self._cached_completion.put_nowait(items)
        except queue.Full:
            # get current completion
            _ = self._cached_completion.get()
            self._cached_completion.put(items)

        self._trigger_completion()

    def pop_completion(self) -> List[sublime.CompletionItem]:
        try:
            return self._cached_completion.get_nowait()
        except queue.Empty:
            return []

    def is_completion_available(self) -> bool:
        return not self._cached_completion.empty()

    auto_complete_arguments = {
        "disable_auto_insert": True,
        "next_completion_if_showing": True,
        "auto_complete_commit_on_tab": True,
    }

    def _trigger_completion(self):
        self.view.run_command(
            "auto_complete",
            self.auto_complete_arguments,
        )

    def hide_completion(self):
        self.view.run_command("hide_auto_complete")

    def apply_text_changes(self, changes: List[TextChange]):
        self.view.run_command(
            f"{COMMAND_PREFIX}_apply_text_changes",
            {
                "changes": [asdict(c) for c in changes],
            },
        )

    def highlight_text(self, regions: List[sublime.Region]):
        highligter = TextHighlighter(self.view)
        highligter.clear()
        highligter.apply(regions)


class Workspace:
    def __init__(self):
        # Map document by view is easier to track if view is valid.
        # If we map by file name, one document my related to multiple 'View'
        # and some times the 'View' is invalid.
        self.documents: Dict[sublime.View, BufferedDocument] = {}
        self.diagnostics: Dict[str, dict] = {}

    document_lock = threading.Lock()
    diagnostic_lock = threading.RLock()

    def lock(locker: threading.Lock):
        def wrapper1(func):
            @wraps(func)
            def wrapper2(*args, **kwargs):
                with locker:
                    return func(*args, **kwargs)

            return wrapper2

        return wrapper1

    @lock(document_lock)
    def get_document(
        self, view: sublime.View, /, default: Any = None
    ) -> Optional[BufferedDocument]:
        return self.documents.get(view, default)

    @lock(document_lock)
    def add_document(self, document: BufferedDocument):
        self.documents[document.view] = document

    @lock(document_lock)
    def remove_document(self, view: sublime.View):
        try:
            del self.documents[view]
        except KeyError as err:
            LOGGER.debug("document not found %s", err)
            pass

    @lock(document_lock)
    def get_document_by_name(
        self, file_name: PathStr, /, default: Any = None
    ) -> Optional[BufferedDocument]:
        """get document by name"""

        for view, document in self.documents.items():
            if view.file_name() == file_name:
                return document
        return default

    @lock(document_lock)
    def get_documents(
        self, file_name: Optional[PathStr] = None
    ) -> List[BufferedDocument]:
        """get documents.
        If file_name assigned, return documents with file_name filtered.
        """
        if not file_name:
            return [doc for _, doc in self.documents.items()]
        return [doc for _, doc in self.documents.items() if doc.file_name == file_name]

    @lock(diagnostic_lock)
    def get_diagnostic(self, file_name: PathStr) -> Dict[str, Any]:
        return self.diagnostics.get(file_name)

    @lock(diagnostic_lock)
    def get_diagnostics(self) -> Dict[PathStr, Dict[str, Any]]:
        # return copy to prevent 'RuntimeError' during iteration
        return dict(self.diagnostics)

    @lock(diagnostic_lock)
    def set_diagnostic(self, file_name: PathStr, diagnostic: dict):
        self.diagnostics[file_name] = diagnostic

    @lock(diagnostic_lock)
    def remove_diagnostic(self, file_name: PathStr):
        try:
            del self.diagnostics[file_name]
        except KeyError as err:
            LOGGER.debug("diagnostic not found %s", err)
            pass

    @lock(document_lock)
    @lock(diagnostic_lock)
    def remove_invalid_diagnostic(self):
        """remove invalid diagnostic"""

        document_names = {doc.file_name for _, doc in self.documents.items()}
        diagnostic_keys = set(self.diagnostics.keys())

        invalid_keys = diagnostic_keys.difference(document_names)
        self.diagnostics = {
            file_name: diagnostic
            for file_name, diagnostic in self.diagnostics.items()
            if file_name not in invalid_keys
        }


def is_valid_document(view: sublime.View) -> bool:
    """check if view is valid document"""

    if not view.file_name():
        return False
    return view.match_selector(0, VIEW_SELECTOR)


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


def open_document(file_name: PathStr, preview: bool = False):
    """open document"""
    flags = sublime.ENCODED_POSITION
    if preview:
        flags |= sublime.TRANSIENT

    sublime.active_window().open_file(file_name, flags=flags)


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
