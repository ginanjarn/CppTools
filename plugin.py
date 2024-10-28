"""C/C++ tools for Sublime Text"""

import logging
from typing import List, Optional

import sublime
import sublime_plugin
from sublime import HoverZone

from .internal.constant import LOGGING_CHANNEL
from .internal.plugin_implementation import (
    OpenEventListener,
    SaveEventListener,
    CloseEventListener,
    HoverEventListener,
    CompletionEventListener,
    TextChangeListener,
    ApplyTextChangesCommand,
    DocumentSignatureHelpCommand,
    DocumentFormattingCommand,
    GotoDeclarationCommand,
    GotoDefinitionCommand,
    PrepareRenameCommand,
    RenameCommand,
    CodeActionCommand,
)
from .internal.handler import BaseHandler
from .internal.clangd_handler import get_handler
from .internal.sublime_settings import Settings
from .internal.document import is_valid_document


LOGGER = logging.getLogger(LOGGING_CHANNEL)
HANDLER: BaseHandler = get_handler()


def setup_logger(level: int):
    """"""
    LOGGER.setLevel(level)
    fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)


def get_logging_settings():
    """get logging level defined in '*.sublime-settings'"""
    level_map = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "verbose": logging.DEBUG,
    }
    with Settings() as settings:
        settings_level = settings.get("logging")
        return level_map.get(settings_level, logging.ERROR)


def plugin_loaded():
    """plugin entry point"""
    setup_logger(get_logging_settings())


def plugin_unloaded():
    """executed before plugin unloaded"""
    if HANDLER:
        HANDLER.terminate()


class CppToolsOpenEventListener(sublime_plugin.EventListener, OpenEventListener):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_activated_async(self, view: sublime.View):
        self._on_activated_async(view)

    def on_load(self, view: sublime.View):
        self._on_load(view)

    def on_reload(self, view: sublime.View):
        self._on_reload(view)

    def on_revert(self, view: sublime.View):
        self._on_revert(view)


class CppToolsSaveEventListener(sublime_plugin.EventListener, SaveEventListener):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_post_save_async(self, view: sublime.View):
        self._on_post_save_async(view)


class CppToolsCloseEventListener(sublime_plugin.EventListener, CloseEventListener):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_close(self, view: sublime.View):
        self._on_close(view)


class CppToolsTextChangeListener(sublime_plugin.TextChangeListener, TextChangeListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_text_changed(self, changes: List[sublime.TextChange]):
        self._on_text_changed(changes)


class CppToolsCompletionEventListener(
    sublime_plugin.EventListener, CompletionEventListener
):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_query_completions(
        self, view: sublime.View, prefix: str, locations: List[int]
    ) -> sublime.CompletionList:
        return self._on_query_completions(view, prefix, locations)


class CppToolsHoverEventListener(sublime_plugin.EventListener, HoverEventListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def on_hover(self, view: sublime.View, point: int, hover_zone: HoverZone):
        self._on_hover(view, point, hover_zone)


class CppToolsDocumentSignatureHelpCommand(
    sublime_plugin.TextCommand, DocumentSignatureHelpCommand
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, point: int):
        self._run(edit, point)

    def is_visible(self):
        return is_valid_document(self.view)


class CppToolsDocumentFormattingCommand(
    sublime_plugin.TextCommand, DocumentFormattingCommand
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit):
        self._run(edit)

    def is_visible(self):
        return is_valid_document(self.view)


class CppToolsGotoDeclarationCommand(
    sublime_plugin.TextCommand, GotoDeclarationCommand
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        self._run(edit, event)

    def is_visible(self):
        return is_valid_document(self.view)

    def want_event(self):
        return True


class CppToolsGotoDefinitionCommand(sublime_plugin.TextCommand, GotoDefinitionCommand):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        self._run(edit, event)

    def is_visible(self):
        return is_valid_document(self.view)

    def want_event(self):
        return True


class CppToolsPrepareRenameCommand(sublime_plugin.TextCommand, PrepareRenameCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        self._run(edit, event)

    def is_visible(self):
        return is_valid_document(self.view)

    def want_event(self):
        return True


class CppToolsRenameCommand(sublime_plugin.TextCommand, RenameCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, row: int, column: int, new_name: str):
        self._run(edit, row, column, new_name)

    def is_visible(self):
        return is_valid_document(self.view)


class CppToolsCodeActionCommand(sublime_plugin.TextCommand, CodeActionCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        self._run(edit, event)

    def is_visible(self):
        return is_valid_document(self.view)

    def want_event(self):
        return True


class CppToolsApplyTextChangesCommand(
    sublime_plugin.TextCommand, ApplyTextChangesCommand
):
    """changes item must serialized from 'TextChange'"""

    def run(self, edit: sublime.Edit, changes: List[dict]):
        self._run(edit, changes)


class CppToolsTerminateCommand(sublime_plugin.WindowCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = HANDLER

    def run(self):
        if self.handler:
            self.handler.terminate()

    def is_visible(self):
        return self.handler and self.handler.is_ready()
