"""plugin implementation"""

import logging
import threading
from dataclasses import dataclass
from typing import List, Optional

import sublime
from sublime import HoverZone

from .handler import BaseHandler
from .constant import LOGGING_CHANNEL
from .workspace import TextChange, is_valid_document

LOGGER = logging.getLogger(LOGGING_CHANNEL)


def initialize_server(handler: BaseHandler, view: sublime.View):
    """initialize server"""
    handler.run_server()
    handler.initialize(view)


class OpenEventListener:

    def __init__(self, *args, **kwargs):
        self.handler: BaseHandler
        self.prev_completion_point = 0

    def _on_activated_async(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didopen(view)
            return

        if LOGGER.level == logging.DEBUG:
            return

        # initialize server
        initialize_server(self.handler, view)
        self.handler.textdocument_didopen(view)

    def _on_load(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didopen(view, reload=True)

    def _on_reload(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didopen(view, reload=True)

    def _on_revert(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didopen(view, reload=True)


class SaveEventListener:

    def __init__(self, *args, **kwargs):
        self.handler: BaseHandler
        self.prev_completion_point = 0

    def _on_post_save_async(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didsave(view)


class CloseEventListener:

    def __init__(self, *args, **kwargs):
        self.handler: BaseHandler
        self.prev_completion_point = 0

    def _on_close(self, view: sublime.View):
        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didclose(view)


class TextChangeListener:

    def __init__(self, *args, **kwargs):
        self.buffer: sublime.Buffer
        self.handler: BaseHandler

    def _on_text_changed(self, changes: List[sublime.TextChange]):
        view = self.buffer.primary_view()

        # check point in valid source
        if not is_valid_document(view):
            return

        if self.handler.is_ready():
            self.handler.textdocument_didchange(
                view, [self.to_text_change(c) for c in changes]
            )

    @staticmethod
    def to_text_change(change: sublime.TextChange) -> TextChange:
        """"""
        start = (change.a.row, change.a.col)
        end = (change.b.row, change.b.col)
        return TextChange(start, end, change.str, change.len_utf8)


class CompletionEventListener:

    def __init__(self, *args, **kwargs):
        self.handler: BaseHandler
        self.prev_completion_point = 0

    def _is_completion_valid(self, view: sublime.View, point: int) -> bool:
        """is completion valid at point"""

        # point unchanged
        if point == self.prev_completion_point:
            return True
        # point changed but still in same word
        word = view.word(self.prev_completion_point)
        if view.substr(word).isidentifier() and point in word:
            return True
        return False

    def _on_query_completions(
        self, view: sublime.View, prefix: str, locations: List[int]
    ) -> sublime.CompletionList:
        if not self.handler.is_ready():
            return None

        point = locations[0]

        # check point in valid source
        if not is_valid_document(view):
            return None

        if (
            document := self.handler.action_target_map.get("textDocument/completion")
        ) and document.is_completion_available():

            items = document.pop_completion()
            if items and self._is_completion_valid(view, point):
                return sublime.CompletionList(
                    items, flags=sublime.INHIBIT_WORD_COMPLETIONS
                )

            document.hide_completion()
            return None

        self.prev_completion_point = point

        row, col = view.rowcol(point)
        self.handler.textdocument_completion(view, row, col)
        view.run_command("hide_auto_complete")

        # Use timeout because of slowdown in completion request
        sublime.set_timeout_async(self.show_signature_help(view, point), 0.5)
        return None

    def show_signature_help(self, view: sublime.View, point: int):
        # not implemented
        ...


class HoverEventListener:

    def __init__(self, *args, **kwargs):
        self.handler: BaseHandler
        self.prev_completion_point = 0

    def _on_hover(self, view: sublime.View, point: int, hover_zone: HoverZone):
        # check point in valid source
        if not (is_valid_document(view) and hover_zone == sublime.HOVER_TEXT):
            return

        row, col = view.rowcol(point)
        threading.Thread(target=self._on_hover_task, args=(view, row, col)).start()

    def _on_hover_task(self, view: sublime.View, row: int, col: int):
        if not self.handler.is_ready():
            initialize_server(self.handler, view)

        self.handler.textdocument_didopen(view)
        self.handler.textdocument_hover(view, row, col)


class DocumentSignatureHelpCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, point: int):
        # not implemented
        pass


class DocumentFormattingCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit):
        if self.handler.is_ready():
            self.handler.textdocument_formatting(self.view)


class GotoDeclarationCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if self.handler.is_ready():
            start_row, start_col = self.view.rowcol(point)
            self.handler.textdocument_declaration(self.view, start_row, start_col)


class GotoDefinitionCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if self.handler.is_ready():
            start_row, start_col = self.view.rowcol(point)
            self.handler.textdocument_definition(self.view, start_row, start_col)


class PrepareRenameCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if self.handler.is_ready():
            # move cursor to point
            self.view.sel().clear()
            self.view.sel().add(point)

            start_row, start_col = self.view.rowcol(point)
            self.handler.textdocument_preparerename(self.view, start_row, start_col)


class RenameCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, row: int, column: int, new_name: str):
        if self.handler.is_ready():
            self.handler.textdocument_rename(self.view, row, column, new_name)


class CodeActionCommand:

    def __init__(self, *args, **kwargs):
        self.view: sublime.View
        self.handler: BaseHandler

    def _run(self, edit: sublime.Edit, event: Optional[dict] = None):
        if self.handler.is_ready():
            if event:
                point = event["text_point"]
                # move cursor to point
                self.view.sel().clear()
                self.view.sel().add(point)

            selection = self.view.sel()[0]
            start = self.view.rowcol(selection.begin())
            end = self.view.rowcol(selection.end())
            self.handler.textdocument_code_action(self.view, start, end)


@dataclass
class _BufferedTextChange:
    """"""

    region: sublime.Region
    new_text: str
    cursor_move: int = 0

    def moved_region(self, move: int) -> sublime.Region:
        return sublime.Region(self.region.a + move, self.region.b + move)


class ApplyTextChangesCommand:
    """changes item must serialized from 'TextChange'"""

    def __init__(self, *args, **kwargs):
        self.view: sublime.View

    def _run(self, edit: sublime.Edit, changes: List[dict]):
        text_changes = [self.to_text_change(self.view, c) for c in changes]
        active_selection = list(self.view.sel())

        self.apply(edit, text_changes)
        self.relocate_selection(active_selection, text_changes)

    @staticmethod
    def to_text_change(view: sublime.View, change: dict) -> _BufferedTextChange:
        change = TextChange(**change)
        start_point = view.text_point(*change.start)
        end_point = view.text_point(*change.end)

        region = sublime.Region(start_point, end_point)
        cursor_move = len(change.text) - region.size()

        return _BufferedTextChange(region, change.text, cursor_move)

    def apply(self, edit: sublime.Edit, text_changes: List[_BufferedTextChange]):
        cursor_move = 0
        for change in text_changes:
            replaced_region = change.moved_region(cursor_move)
            self.view.erase(edit, replaced_region)
            self.view.insert(edit, replaced_region.a, change.new_text)
            cursor_move += change.cursor_move

    def relocate_selection(
        self, selections: List[sublime.Region], changes: List[_BufferedTextChange]
    ):
        """relocate current selection following text changes"""
        moved_selections = []
        for selection in selections:
            temp_selection = selection
            for change in changes:
                if temp_selection.begin() > change.region.begin():
                    temp_selection.a += change.cursor_move
                    temp_selection.b += change.cursor_move

            moved_selections.append(temp_selection)

        # we must clear current selection
        self.view.sel().clear()
        self.view.sel().add_all(moved_selections)
