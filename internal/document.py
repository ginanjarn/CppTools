"""Document handler module"""

import logging
import time
from collections import namedtuple
from dataclasses import dataclass, asdict
from typing import List

import sublime

from .constant import (
    LOGGING_CHANNEL,
    LANGUAGE_ID,
    VIEW_SELECTOR,
    COMMAND_PREFIX,
)

RowColIndex = namedtuple("RowColIndex", ["row", "column"])
LOGGER = logging.getLogger(LOGGING_CHANNEL)


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


class Document:
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

    def apply_changes(self, text_changes: List[TextChange]):
        self.view.run_command(
            f"{COMMAND_PREFIX}_apply_text_changes",
            {
                "changes": [asdict(c) for c in text_changes],
            },
        )
