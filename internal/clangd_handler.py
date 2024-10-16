"""pyserver spesific handler"""

import logging
import threading

from functools import wraps
from pathlib import Path
from typing import Optional, Dict, List, Any

import sublime

from . import lsp_client
from .constant import (
    COMMAND_PREFIX,
    LOGGING_CHANNEL,
)
from .handler import (
    BaseHandler,
    DiagnosticPanel,
    COMPLETION_KIND_MAP,
    input_text,
    open_location,
)
from .sublime_settings import Settings
from .workspace import (
    Workspace,
    BufferedDocument,
    UnbufferedDocument,
    TextChange,
    get_workspace_path,
    path_to_uri,
    uri_to_path,
)

PathStr = str
PathEncodedStr = str
"""Path encoded '<file_name>:<row>:<column>'"""
LOGGER = logging.getLogger(LOGGING_CHANNEL)


class Session:
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


class ClangdHandler(BaseHandler):
    """"""

    session = Session()

    def __init__(self, transport: lsp_client.Transport):
        super().__init__(transport)
        self.diagnostic_manager = DiagnosticManager()

        self.handler_map.update(
            {
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
        )

    def is_ready(self) -> bool:
        return self.client.is_server_running() and self.session.is_begin()

    def terminate(self):
        """exit session"""
        self.client.terminate_server()
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

    def handle_initialize(self, params: dict):
        if err := params.get("error"):
            print(err["message"])
            return

        self.client.send_notification("initialized", {})
        self._initializing = False

        self.diagnostic_manager.reset()
        self.session.begin()

    def handle_window_logmessage(self, params: dict):
        print(params["message"])

    def handle_window_showmessage(self, params: dict):
        sublime.status_message(params["message"])

    @session.wait_begin
    def textdocument_didopen(self, view: sublime.View, *, reload: bool = False):
        # check if view not closed
        if not (view and view.is_valid()):
            return

        file_name = view.file_name()

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

    @session.must_begin
    def textdocument_didsave(self, view: sublime.View):
        if document := self.workspace.get_document(view):
            self.client.send_notification(
                "textDocument/didSave",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

        else:
            # untitled document not yet loaded to server
            self.textdocument_didopen(view)

    @session.must_begin
    def textdocument_didclose(self, view: sublime.View):
        file_name = view.file_name()
        if document := self.workspace.get_document(view):
            self.workspace.remove_document(view)

            # if document still opened in other View
            if self.workspace.get_documents(file_name):
                return

            self.diagnostic_manager.remove(file_name)
            DiagnosticReporter(
                self.diagnostic_manager.get_all(), self.diagnostic_panel
            ).show_report()

            self.client.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

    def _text_change_to_rpc(self, text_change: TextChange) -> dict:
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

    @session.must_begin
    def textdocument_didchange(self, view: sublime.View, changes: List[TextChange]):
        # Document can be related to multiple View but has same file_name.
        # Use get_document_by_name() because may be document already open
        # in other view and the argument view not assigned.
        file_name = view.file_name()
        if document := self.workspace.get_document_by_name(file_name):
            self.client.send_notification(
                "textDocument/didChange",
                {
                    "contentChanges": [self._text_change_to_rpc(c) for c in changes],
                    "textDocument": {
                        "uri": path_to_uri(document.file_name),
                        "version": document.version,
                    },
                },
            )

    @session.must_begin
    def textdocument_hover(self, view, row, col):
        method = "textDocument/hover"
        # In multi row/column layout, new popup will created in current View,
        # but active popup doesn't discarded.
        if other := self.action_target_map.get(method):
            other.view.hide_popup()

        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    def handle_textdocument_hover(self, params: dict):
        method = "textDocument/hover"
        if err := params.get("error"):
            print(err["message"])

        elif result := params.get("result"):
            message = result["contents"]["value"]
            start = result["range"]["start"]
            row, col = start["line"], start["character"]
            self.action_target_map[method].show_popup(message, row, col)

    @session.must_begin
    def textdocument_completion(self, view, row, col):
        method = "textDocument/completion"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    @staticmethod
    def _build_completion(completion_item: dict) -> sublime.CompletionItem:

        text = completion_item["filterText"]
        try:
            insert_text = completion_item["textEdit"]["newText"]
        except KeyError:
            insert_text = text

        # clangd defined 'label' starts with '<space>' or '•'
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

    def handle_textdocument_completion(self, params: dict):
        method = "textDocument/completion"
        if err := params.get("error"):
            print(err["message"])

        elif result := params.get("result"):
            items = [self._build_completion(item) for item in result["items"]]
            self.action_target_map[method].show_completion(items)

    @staticmethod
    def _get_diagnostic_region(view: sublime.View, diagnostic: dict) -> sublime.Region:

        start = diagnostic["range"]["start"]
        end = diagnostic["range"]["end"]

        start_point = view.text_point(start["line"], start["character"])
        end_point = view.text_point(end["line"], end["character"])
        return sublime.Region(start_point, end_point)

    def handle_textdocument_publishdiagnostics(self, params: dict):
        file_name = uri_to_path(params["uri"])
        diagnostics = params["diagnostics"]

        self.diagnostic_manager.add(file_name, diagnostics)
        DiagnosticReporter(
            self.diagnostic_manager.get_all(), self.diagnostic_panel
        ).show_report()

        for document in self.workspace.get_documents(file_name):
            regions = [
                self._get_diagnostic_region(document.view, diagnostic)
                for diagnostic in diagnostics
            ]
            document.highlight_text(regions)

    @staticmethod
    def _get_text_change(change: dict) -> TextChange:
        start = change["range"]["start"]
        end = change["range"]["end"]
        text = change["newText"]
        # "rangeLength" not implemented in clangd
        length = 0

        return TextChange(
            (start["line"], start["character"]),
            (end["line"], end["character"]),
            text,
            length,
        )

    @session.must_begin
    def textdocument_formatting(self, view):
        method = "textDocument/formatting"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "options": {"insertSpaces": True, "tabSize": 2},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    def handle_textdocument_formatting(self, params: dict):
        method = "textDocument/formatting"
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            changes = [self._get_text_change(c) for c in result]
            self.action_target_map[method].apply_text_changes(changes)

    def handle_workspace_applyedit(self, params: dict) -> dict:
        try:
            WorkspaceEdit(self.workspace).apply(params["edit"])

        except Exception as err:
            LOGGER.error(err, exc_info=True)
            return {"applied": False}
        else:
            return {"applied": True}

    def handle_workspace_executecommand(self, params: dict) -> dict:
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            LOGGER.info(result)

        return None

    @session.must_begin
    def textdocument_declaration(self, view, row, col):
        method = "textDocument/declaration"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    def handle_textdocument_declaration(self, params: dict):
        method = "textDocument/declaration"
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            view = self.action_target_map[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @session.must_begin
    def textdocument_definition(self, view, row, col):
        method = "textDocument/definition"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    @staticmethod
    def _build_location(location: dict) -> PathEncodedStr:
        file_name = uri_to_path(location["uri"])
        row = location["range"]["start"]["line"]
        col = location["range"]["start"]["character"]
        return f"{file_name}:{row+1}:{col+1}"

    def handle_textdocument_definition(self, params: dict):
        method = "textDocument/definition"
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            view = self.action_target_map[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @session.must_begin
    def textdocument_preparerename(self, view, row, col):
        method = "textDocument/prepareRename"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    @session.must_begin
    def textdocument_rename(self, view, row, col, new_name):
        method = "textDocument/rename"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "newName": new_name,
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

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

    def handle_textdocument_preparerename(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._handle_preparerename(result)

    def handle_textdocument_rename(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            WorkspaceEdit(self.workspace).apply(result)

    @session.must_begin
    def textdocument_code_action(self, view, start, end):
        method = "textDocument/codeAction"
        if document := self.workspace.get_document(view):
            self.client.send_request(
                method,
                {
                    "context": {
                        "diagnostics": self.diagnostic_manager.get(document.file_name),
                        "triggerKind": 2,
                    },
                    "range": {
                        "end": {"character": end[1], "line": end[0]},
                        "start": {"character": start[1], "line": start[0]},
                    },
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )
            self.action_target_map[method] = document

    def handle_textdocument_code_action(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._show_code_action(result)

    def _show_code_action(self, actions: List[dict]):
        def on_select(index):
            if index < 0:
                return

            if actions[index].get("command"):
                self.client.send_request("workspace/executeCommand", actions[index])
                return

            edit = actions[index]["edit"]
            WorkspaceEdit(self.workspace).apply(edit)

        def get_title(action: dict) -> str:
            title = action["title"]
            if kind := action.get("kind"):
                return f"({kind}){title}"
            return title

        items = [get_title(a) for a in actions]
        sublime.active_window().show_quick_panel(items, on_select=on_select)


class DiagnosticManager:
    def __init__(self) -> None:
        self.diagnostics: Dict[PathStr, dict] = {}
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self.diagnostics.clear()

    def get_all(self) -> Dict[PathStr, dict]:
        with self._lock:
            return self.diagnostics

    def get(self, file_name: PathStr) -> Optional[dict]:
        with self._lock:
            try:
                return self.diagnostics[file_name]
            except KeyError:
                return None

    def add(self, file_name: PathStr, diagnostics: dict):
        with self._lock:
            self.diagnostics[file_name] = diagnostics

    def remove(self, file_name: PathStr):
        with self._lock:
            try:
                del self.diagnostics[file_name]
            except KeyError:
                pass


class DiagnosticReporter:
    def __init__(
        self, diagnostic_map: Dict[PathStr, dict], diagnostic_panel: DiagnosticPanel
    ):
        self.diagnostic_map = diagnostic_map
        self.diagnostic_panel = diagnostic_panel

    def show_report(self):
        """"""
        report_text = self._build_report(self.diagnostic_map)

        self.diagnostic_panel.set_content(report_text)
        self.diagnostic_panel.show()

    def _build_report(self, diagnostics_map: Dict[PathStr, Any]) -> str:
        reports = []

        # build report for each file
        for file_name, diagnostics in diagnostics_map.items():
            lines = [self.build_line(file_name, d) for d in diagnostics]
            reports.extend(lines)

        return "\n".join(reports)

    @staticmethod
    def build_line(file_name: PathStr, diagnostic: dict) -> str:
        short_name = Path(file_name).name
        row = diagnostic["range"]["start"]["line"]
        col = diagnostic["range"]["start"]["character"]
        message = diagnostic["message"]
        source = diagnostic.get("source", "")

        # natural line index start with 1
        row += 1

        return f"{short_name}:{row}:{col}: {message} ({source})"


class WorkspaceEdit:

    def __init__(self, workspace_: Workspace):
        self.workspace = workspace_

    def apply(self, edit_changes: dict) -> None:
        """"""
        # Clangd implementation is a little different from standard
        for file_uri, changes in edit_changes["changes"].items():
            self._apply_textedit_changes(uri_to_path(file_uri), changes)

    def _apply_textedit_changes(self, file_name: PathStr, edits: dict):
        changes = [self._get_text_change(c) for c in edits]

        document = self.workspace.get_document_by_name(
            file_name, UnbufferedDocument(file_name)
        )
        document.apply_text_changes(changes)
        document.save()

    @staticmethod
    def _get_text_change(change: dict) -> TextChange:
        start = change["range"]["start"]
        end = change["range"]["end"]
        text = change["newText"]
        # "rangeLength" not implemented in clangd
        length = 0

        return TextChange(
            (start["line"], start["character"]),
            (end["line"], end["character"]),
            text,
            length,
        )


def get_handler() -> BaseHandler:
    """"""

    command = ["clangd", "--log=error", "--offset-encoding=utf-8"]
    transport = lsp_client.StandardIO(command, None)
    return ClangdHandler(transport)


def get_envs_settings() -> Optional[dict]:
    """get environments defined in '*.sublime-settings'"""

    with Settings() as settings:
        if envs := settings.get("envs"):
            return envs

        sublime.active_window().run_command("pythontools_set_environment")
        return None
