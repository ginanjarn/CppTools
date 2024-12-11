"""clangd implementation"""

import logging
import threading

from collections import namedtuple
from dataclasses import dataclass
from functools import wraps
from html import escape as escape_html
from pathlib import Path
from typing import Optional, Dict, List, Callable

import sublime

from .constant import (
    PACKAGE_NAME,
    COMMAND_PREFIX,
    LOGGING_CHANNEL,
)
from .session import (
    Session,
    DiagnosticPanel,
    COMPLETION_KIND_MAP,
    input_text,
    open_location,
)
from .document import (
    BufferedDocument,
    UnbufferedDocument,
    TextChange,
    path_to_uri,
    uri_to_path,
)
from .lsp_client import Transport, StandardIO, MethodName, Response
from .sublime_settings import Settings
from .workspace import (
    Workspace,
    get_workspace_path,
)

PathStr = str
PathEncodedStr = str
"""Path encoded '<file_name>:<row>:<column>'"""
LineCharacter = namedtuple("LineCharacter", ["line", "character"])
LOGGER = logging.getLogger(LOGGING_CHANNEL)


class InitializeManager:
    def __init__(self):
        self.event = threading.Event()

    def is_begin(self):
        return self.event.is_set()

    def begin(self):
        """begin session"""
        self.event.set()

    def done(self):
        """done session"""
        self.event.clear()

    def must_begin(self, func):
        """return 'None' if not begin"""

        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self.event.is_set():
                return None

            return func(*args, **kwargs)

        return wrapper

    def wait_begin(self, func):
        """return function after session is begin"""

        @wraps(func)
        def wrapper(*args, **kwargs):
            self.event.wait()
            return func(*args, **kwargs)

        return wrapper


class ClangdSession(Session):
    """"""

    initialize_manager = InitializeManager()

    def __init__(self, transport: Transport):
        super().__init__(transport)
        self.diagnostic_manager = DiagnosticManager(
            DiagnosticReportSettings(show_panel=False)
        )
        self._set_default_handler()

        # workspace status
        self._initializing = False
        self.hover_location = (0, 0)

        # document target
        self.action_target_map: Dict[MethodName, BufferedDocument] = {}
        self.workspace = Workspace()

    def _set_default_handler(self):
        handlers = {
            "initialize": self.handle_initialize,
            # window
            "window/logMessage": self.handle_window_logmessage,
            "window/showMessage": self.handle_window_showmessage,
            # workspace
            "workspace/applyEdit": self.handle_workspace_applyedit,
            "workspace/executeCommand": self.handle_workspace_executecommand,
            # textDocument
            "textDocument/hover": self.handle_textdocument_hover,
            "textDocument/completion": self.handle_textdocument_completion,
            "textDocument/publishDiagnostics": self.handle_textdocument_publishdiagnostics,
            "textDocument/formatting": self.handle_textdocument_formatting,
            "textDocument/declaration": self.handle_textdocument_declaration,
            "textDocument/definition": self.handle_textdocument_definition,
            "textDocument/prepareRename": self.handle_textdocument_preparerename,
            "textDocument/rename": self.handle_textdocument_rename,
            "textDocument/codeAction": self.handle_textdocument_code_action,
        }
        self.handler_map.update(handlers)

    def _reset_state(self) -> None:
        self._initializing = False
        self.workspace = Workspace()

        self.action_target_map.clear()
        self.initialize_manager.done()

    def _is_ready(self) -> bool:
        return self.client.is_server_running() and self.initialize_manager.is_begin()

    def _terminate(self):
        """exit session"""
        self.client.terminate_server()
        self.diagnostic_manager.reset()
        self._reset_state()

    def initialize(self, view: sublime.View):
        # cancel if initializing
        if self._initializing:
            return

        # check if view not closed
        if view is None:
            return

        workspace_path = get_workspace_path(view)
        if not workspace_path:
            return

        self._initializing = True
        self.client.send_request(
            "initialize",
            {
                "rootPath": workspace_path,
                "rootUri": path_to_uri(workspace_path),
                "capabilities": {
                    "textDocument": {
                        "hover": {
                            "contentFormat": ["markdown", "plaintext"],
                        },
                        "completion": {
                            "completionItem": {
                                "snippetSupport": True,
                            },
                            "insertTextMode": 2,
                        },
                    }
                },
            },
        )

    def handle_initialize(self, params: Response):
        if err := params.error:
            print(err["message"])
            return

        self.client.send_notification("initialized", {})
        self._initializing = False

        self.diagnostic_manager.reset()
        self.initialize_manager.begin()

    def handle_window_logmessage(self, params: dict):
        print(params["message"])

    def handle_window_showmessage(self, params: dict):
        sublime.status_message(params["message"])

    @initialize_manager.wait_begin
    def textdocument_didopen(self, view: sublime.View, *, reload: bool = False):
        # check if view not closed
        if not (view and view.is_valid()):
            return

        file_name = view.file_name()
        self.diagnostic_manager.set_active_view(view)

        if opened_document := self.workspace.get_document(view):
            if opened_document.file_name == file_name and (not reload):
                return

            # In SublimeText, rename file only retarget to new path
            # but the 'View' is not closed.
            # Close older document then reopen with new name.
            self.textdocument_didclose(view)

        document = BufferedDocument(view)
        self.workspace.add_document(document)

        # Document maybe opened in multiple 'View', send notification
        # only on first opening document.
        if len(self.workspace.get_documents(file_name)) == 1:
            self.client.send_notification(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "languageId": document.language_id,
                        "text": document.text,
                        "uri": path_to_uri(document.file_name),
                        "version": document.version,
                    }
                },
            )

    @initialize_manager.must_begin
    def textdocument_didsave(self, view: sublime.View):
        if document := self.workspace.get_document(view):
            self.client.send_notification(
                "textDocument/didSave",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

        else:
            # untitled document not yet loaded to server
            self.textdocument_didopen(view)

    @initialize_manager.must_begin
    def textdocument_didclose(self, view: sublime.View):
        file_name = view.file_name()
        self.diagnostic_manager.remove(view)
        if document := self.workspace.get_document(view):
            self.workspace.remove_document(view)

            # if document still opened in other View
            if self.workspace.get_documents(file_name):
                return

            self.client.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

    @initialize_manager.must_begin
    def textdocument_didchange(self, view: sublime.View, changes: List[TextChange]):
        # Document can be related to multiple View but has same file_name.
        # Use get_document_by_name() because may be document already open
        # in other view and the argument view not assigned.
        file_name = view.file_name()
        if document := self.workspace.get_document_by_name(file_name):
            self.client.send_notification(
                "textDocument/didChange",
                {
                    "contentChanges": [textchange_to_rpc(c) for c in changes],
                    "textDocument": {
                        "uri": path_to_uri(document.file_name),
                        "version": document.version,
                    },
                },
            )

    def _get_diagnostic_message(self, view: sublime.View, row: int, col: int):
        point = view.text_point(row, col)

        def contain_point(item: DiagnosticItem):
            return item.region.contains(point)

        diagnostics = self.diagnostic_manager.get_active_view_diagnostics(contain_point)
        if not diagnostics:
            return ""

        title = "### Diagnostics:\n"
        diagnostic_message = "\n".join(
            [f"- {escape_html(d.message)}" for d in diagnostics]
        )
        command_url = sublime.command_url(
            f"{COMMAND_PREFIX}_code_action", {"event": {"text_point": point}}
        )
        footer = f'***\n<a href="{command_url}">Code Action</a>'
        return f"{title}\n{diagnostic_message}\n{footer}"

    @initialize_manager.must_begin
    def textdocument_hover(self, view, row, col):
        method = "textDocument/hover"
        # In multi row/column layout, new popup will created in current View,
        # but active popup doesn't discarded.
        if other := self.action_target_map.get(method):
            other.view.hide_popup()

        if document := self.workspace.get_document(view):
            if message := self._get_diagnostic_message(view, row, col):
                document.show_popup(message, row, col)
                return

            self.action_target_map[method] = document
            self.hover_location = (row, col)
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_hover(self, params: Response):
        method = "textDocument/hover"
        if err := params.error:
            print(err["message"])

        elif result := params.result:
            message = result["contents"]["value"]
            try:
                start = result["range"]["start"]
                row, col = start["line"], start["character"]
            except KeyError:
                row, col = self.hover_location

            self.action_target_map[method].show_popup(message, row, col)

    @initialize_manager.must_begin
    def textdocument_completion(self, view, row, col):
        method = "textDocument/completion"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @staticmethod
    def _build_completion(completion_item: dict) -> sublime.CompletionItem:

        text = completion_item["filterText"]
        try:
            insert_text = completion_item["textEdit"]["newText"]
        except KeyError:
            insert_text = text

        # clangd defined 'label' starts with '<space>' or '�'
        signature = completion_item["label"][1:]

        # sublime text has complete the header bracket '<> or ""'
        # remove it from clangd result
        if completion_item["kind"] in (17, 19):
            closing_include = '">'
            text = text.rstrip(closing_include)
            insert_text = insert_text.rstrip(closing_include)
            signature = signature.rstrip(closing_include)

        kind = COMPLETION_KIND_MAP[completion_item["kind"]]

        return sublime.CompletionItem.snippet_completion(
            trigger=text,
            snippet=insert_text,
            annotation=signature,
            kind=kind,
        )

    def handle_textdocument_completion(self, params: Response):
        method = "textDocument/completion"
        if err := params.error:
            print(err["message"])

        elif result := params.result:
            items = [self._build_completion(item) for item in result["items"]]
            self.action_target_map[method].show_completion(items)

    def handle_textdocument_publishdiagnostics(self, params: dict):
        file_name = uri_to_path(params["uri"])
        diagnostics = params["diagnostics"]

        for document in self.workspace.get_documents(file_name):
            self.diagnostic_manager.set(document.view, diagnostics)

    @initialize_manager.must_begin
    def textdocument_formatting(self, view):
        method = "textDocument/formatting"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "options": {"insertSpaces": True, "tabSize": 2},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_formatting(self, params: Response):
        method = "textDocument/formatting"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            changes = [rpc_to_textchange(c) for c in result]
            self.action_target_map[method].apply_changes(changes)

    def handle_workspace_applyedit(self, params: dict) -> dict:
        try:
            WorkspaceEdit(self.workspace).apply_changes(params["edit"])

        except Exception as err:
            LOGGER.error(err, exc_info=True)
            return {"applied": False}
        else:
            return {"applied": True}

    def handle_workspace_executecommand(self, params: Response) -> dict:
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            LOGGER.info(result)

        return None

    @initialize_manager.must_begin
    def textdocument_declaration(self, view, row, col):
        method = "textDocument/declaration"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_declaration(self, params: Response):
        method = "textDocument/declaration"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            view = self.action_target_map[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @initialize_manager.must_begin
    def textdocument_definition(self, view, row, col):
        method = "textDocument/definition"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @staticmethod
    def _build_location(location: dict) -> PathEncodedStr:
        file_name = uri_to_path(location["uri"])
        row = location["range"]["start"]["line"]
        col = location["range"]["start"]["character"]
        return f"{file_name}:{row+1}:{col+1}"

    def handle_textdocument_definition(self, params: Response):
        method = "textDocument/definition"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            view = self.action_target_map[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @initialize_manager.must_begin
    def textdocument_preparerename(self, view, row, col):
        method = "textDocument/prepareRename"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @initialize_manager.must_begin
    def textdocument_rename(self, view, row, col, new_name):
        method = "textDocument/rename"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "newName": new_name,
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def _handle_preparerename(self, location: dict):
        method = "textDocument/prepareRename"
        view = self.action_target_map[method].view

        start = location["start"]
        start_point = view.text_point(start["line"], start["character"])
        end = location["end"]
        end_point = view.text_point(end["line"], end["character"])

        region = sublime.Region(start_point, end_point)
        old_name = view.substr(region)
        row, col = view.rowcol(start_point)

        def request_rename(new_name):
            if new_name and old_name != new_name:
                view.run_command(
                    f"{COMMAND_PREFIX}_rename",
                    {"row": row, "column": col, "new_name": new_name},
                )

        input_text("rename", old_name, request_rename)

    def handle_textdocument_preparerename(self, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            self._handle_preparerename(result)

    def handle_textdocument_rename(self, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            WorkspaceEdit(self.workspace).apply_changes(result)

    @initialize_manager.must_begin
    def textdocument_code_action(self, view, start, end):
        method = "textDocument/codeAction"
        if document := self.workspace.get_document(view):
            self.action_target_map[method] = document
            self.client.send_request(
                method,
                {
                    "context": {
                        "diagnostics": self.diagnostic_manager.get(view),
                        "triggerKind": 2,
                    },
                    "range": {
                        "end": {"character": end[1], "line": end[0]},
                        "start": {"character": start[1], "line": start[0]},
                    },
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_code_action(self, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            self._show_code_action(result)

    def _show_code_action(self, actions: List[dict]):
        def on_select(index):
            if index < 0:
                return

            if actions[index].get("command"):
                self.client.send_request("workspace/executeCommand", actions[index])
                return

            edit = actions[index]["edit"]
            WorkspaceEdit(self.workspace).apply_changes(edit)

        def get_title(action: dict) -> str:
            title = action["title"]
            if kind := action.get("kind"):
                return f"({kind}){title}"
            return title

        items = [get_title(a) for a in actions]
        sublime.active_window().show_quick_panel(items, on_select=on_select)


def textchange_to_rpc(text_change: TextChange) -> dict:
    """"""
    start = text_change.start
    end = text_change.end
    return {
        "range": {
            "end": {"character": end.column, "line": end.row},
            "start": {"character": start.column, "line": start.row},
        },
        "rangeLength": text_change.length,
        "text": text_change.text,
    }


def rpc_to_textchange(change: dict) -> TextChange:
    """"""
    return TextChange(
        LineCharacter(**change["range"]["start"]),
        LineCharacter(**change["range"]["end"]),
        change["newText"],
        change.get("rangeLength", -1),
    )


class DiagnosticItem:
    __slots__ = ["severity", "region", "message"]

    def __init__(self, severity: int, region: sublime.Region, message: str) -> None:
        self.severity = severity
        self.region = region
        self.message = message

    def __repr__(self) -> str:
        text = "DiagnosticItem(severity=%s, region=%s, message='%s')"
        return text % (self.severity, self.region, self.message)


@dataclass
class DiagnosticReportSettings:
    highlight_text: bool = True
    show_status: bool = True
    show_panel: bool = False


class DiagnosticManager:
    def __init__(self, settings: DiagnosticReportSettings = None) -> None:
        self.diagnostics: Dict[sublime.View, List[dict]] = {}

        self.settings = settings or DiagnosticReportSettings()
        self.panel = DiagnosticPanel()

        self._change_lock = threading.Lock()
        self._active_view: sublime.View = None
        self._active_view_diagnostics: List[DiagnosticItem] = []

    def reset(self):
        # erase regions
        for view in self.diagnostics.keys():
            view.erase_regions(self.REGIONS_KEY)

        self._active_view = None
        self._active_view_diagnostics = []
        self.panel.destroy()
        self.diagnostics = {}

    def get(self, view: sublime.View) -> List[dict]:
        with self._change_lock:
            return self.diagnostics.get(view, [])

    def set(self, view: sublime.View, diagostics: List[dict]):
        with self._change_lock:
            self.diagnostics.update({view: diagostics})
            self._on_diagnostic_changed(view)

    def remove(self, view: sublime.View):
        with self._change_lock:
            try:
                del self.diagnostics[view]
            except KeyError:
                pass
            self._on_diagnostic_changed(view)

    def set_active_view(self, view: sublime.View):
        if view == self._active_view:
            return

        self._active_view = view
        self._on_diagnostic_changed(view)

    def get_active_view_diagnostics(
        self, filter_func: Callable[[DiagnosticItem], bool] = None
    ) -> List[DiagnosticItem]:
        if not filter_func:
            return self._active_view_diagnostics
        return [d for d in self._active_view_diagnostics if filter_func(d)]

    def _on_diagnostic_changed(self, view: sublime.View):
        diagnostics = [
            self._to_diagnostic_item(view, diagnostic)
            for diagnostic in self.diagnostics.get(view, [])
        ]

        if self.settings.highlight_text:
            self._highlight_regions(view, diagnostics)
        if self.settings.show_status:
            self._show_status(view, diagnostics)

        if view != self._active_view:
            return

        self._active_view_diagnostics = diagnostics
        if self.settings.show_panel:
            self._show_panel(view, diagnostics)

    def _to_diagnostic_item(
        self, view: sublime.View, diagnostic: dict, /
    ) -> DiagnosticItem:

        start = LineCharacter(**diagnostic["range"]["start"])
        end = LineCharacter(**diagnostic["range"]["end"])
        region = sublime.Region(view.text_point(*start), view.text_point(*end))
        message = diagnostic["message"]
        if source := diagnostic.get("source"):
            message = f"{message} ({source})"

        return DiagnosticItem(diagnostic["severity"], region, message)

    REGIONS_KEY = f"{PACKAGE_NAME}_DIAGNOSTIC_REGIONS"

    def _highlight_regions(self, view: sublime.View, diagnostics: List[DiagnosticItem]):
        regions = [item.region for item in diagnostics]
        view.add_regions(
            key=self.REGIONS_KEY,
            regions=regions,
            scope="invalid",
            icon="dot",
            flags=sublime.DRAW_NO_FILL
            | sublime.DRAW_NO_OUTLINE
            | sublime.DRAW_SQUIGGLY_UNDERLINE,
        )

    STATUS_KEY = f"{PACKAGE_NAME}_DIAGNOSTIC_STATUS"

    def _show_status(self, view: sublime.View, diagnostics: List[DiagnosticItem]):
        value = "ERROR %s, WARNING %s"
        err_count = len([item for item in diagnostics if item.severity == 1])
        warn_count = len(diagnostics) - err_count
        view.set_status(self.STATUS_KEY, value % (err_count, warn_count))

    def _show_panel(self, view: sublime.View, diagnostics: List[DiagnosticItem]):
        def build_line(view: sublime.View, item: DiagnosticItem):
            short_name = Path(view.file_name()).name
            row, col = view.rowcol(item.region.begin())
            return f"{short_name}:{row+1}:{col} {item.message}"

        content = "\n".join([build_line(view, item) for item in diagnostics])
        self.panel.set_content(content)
        self.panel.show()


class WorkspaceEdit:

    def __init__(self, workspace_: Workspace):
        self.workspace = workspace_

    def apply_changes(self, edit_changes: dict) -> None:
        """"""
        # Clangd implementation is a little different from standard
        for file_uri, changes in edit_changes["changes"].items():
            self._apply_textedit_changes(uri_to_path(file_uri), changes)

    def _apply_textedit_changes(self, file_name: PathStr, edits: dict):
        changes = [rpc_to_textchange(c) for c in edits]

        document = self.workspace.get_document_by_name(
            file_name, UnbufferedDocument(file_name)
        )
        document.apply_changes(changes)
        document.save()


def get_session() -> Session:
    """"""

    command = ["clangd", "--log=error", "--offset-encoding=utf-8"]
    transport = StandardIO(command, None)
    return ClangdSession(transport)


def get_envs_settings() -> Optional[dict]:
    """get environments defined in '*.sublime-settings'"""

    with Settings() as settings:
        if envs := settings.get("envs"):
            return envs

        return None
