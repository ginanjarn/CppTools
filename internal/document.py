"""Document handler module"""

import logging
import time
from collections import namedtuple
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple
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
Span = Tuple[int, int]
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


def is_valid_document(view: sublime.View) -> bool:
    """check if view is valid document"""

    if not view.file_name():
        return False
    return view.match_selector(0, VIEW_SELECTOR)


@dataclass
class TextChange:
    start: RowColIndex
    end: RowColIndex
    text: str
    length: int = 0

    def __post_init__(self):
        # possibly if user pass 'start' and 'end' as tuple
        self.start = RowColIndex(*self.start)
        self.end = RowColIndex(*self.end)


class _UnbufferedTextChange:
    __slots__ = ["span", "old_text", "new_text"]

    def __init__(self, span: Span, old_text: str, new_text: str) -> None:
        self.span = span
        self.old_text = old_text
        self.new_text = new_text

    def offset_move(self) -> int:
        return len(self.new_text) - len(self.old_text)

    def get_moved_span(self, move: int) -> Span:
        return (self.span[0] + move, self.span[1] + move)


class UnbufferedDocument:
    def __init__(self, file_name: PathStr):
        self.file_name = file_name
        self.text = Path(file_name).read_text()
        self.is_saved = True

        self._cached_lines = []

    def lines(self) -> List[str]:
        if not all([self.is_saved, self._cached_lines]):
            self._cached_lines = self.text.splitlines(keepends=True)
        return self._cached_lines

    def apply_text_changes(self, changes: List[TextChange]):
        self.is_saved = False
        self.text = self._update_text(self.text, changes)

    def _update_text(self, source: str, changes: List[TextChange]) -> str:
        text_changes = [self.to_text_change(c) for c in changes]

        temp = source
        move = 0
        for change in text_changes:
            start_offset, end_offset = change.get_moved_span(move)
            temp = f"{temp[:start_offset]}{change.new_text}{temp[end_offset:]}"
            move += change.offset_move()

        return temp

    def calculate_offset(self, row: int, column: int) -> int:
        line_offset = sum([len(l) for l in self.lines()[:row]])
        return line_offset + column

    def to_text_change(self, change: TextChange) -> _UnbufferedTextChange:
        start = self.calculate_offset(*change.start)
        end = self.calculate_offset(*change.end)

        old_text = self.text[start:end]
        new_text = change.text

        return _UnbufferedTextChange((start, end), old_text, new_text)

    def save(self):
        Path(self.file_name).write_text(self.text)
        self.is_saved = True


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
        self._cached_completion = None

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
        self._cached_completion = items
        self._trigger_completion()

    def pop_completion(self) -> List[sublime.CompletionItem]:
        temp = self._cached_completion
        self._cached_completion = None
        return temp

    def is_completion_available(self) -> bool:
        return self._cached_completion is not None

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


class TextHighlighter:
    REGIONS_KEY = f"{PACKAGE_NAME}_REGIONS"

    def __init__(self, view: sublime.View):
        self.view = view

    def apply(self, regions: List[sublime.Region]):
        self.view.add_regions(
            key=self.REGIONS_KEY,
            regions=regions,
            scope="invalid",
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
