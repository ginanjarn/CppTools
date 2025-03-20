"""clangd spesific handler"""

import logging
import threading

from collections import namedtuple, defaultdict
from functools import wraps
from html import escape as escape_html
from typing import Optional, Dict, List, Callable, Any, Union

import sublime

from .constant import (
    COMMAND_PREFIX,
    LOGGING_CHANNEL,
)
from .document import (
    Document,
    TextChange,
)
from .diagnostics import DiagnosticItem
from .errors import MethodNotFound
from .uri import (
    path_to_uri,
    uri_to_path,
)
from .lsp_client import (
    Client,
    ServerProcess,
    Transport,
    StandardIO,
    MethodName,
    Response,
)
from .panels import (
    input_text,
    PathEncodedStr,
    open_location,
)
from .session import Session, InitializeStatus
from .sublime_settings import Settings
from .workspace import (
    get_workspace_path,
    create_document,
    update_document,
    rename_document,
    delete_document,
)

LOGGER = logging.getLogger(LOGGING_CHANNEL)
LineCharacter = namedtuple("LineCharacter", ["line", "character"])
"""Line Character namedtuple"""

HandleParams = Union[Response, dict]
HandlerFunction = Callable[[Session, HandleParams], Any]


COMPLETION_KIND_MAP = defaultdict(
    lambda _: sublime.KIND_AMBIGUOUS,
    {
        1: (sublime.KindId.COLOR_ORANGISH, "t", ""),  # text
        2: (sublime.KindId.FUNCTION, "", ""),  # method
        3: (sublime.KindId.FUNCTION, "", ""),  # function
        4: (sublime.KindId.FUNCTION, "c", ""),  # constructor
        5: (sublime.KindId.VARIABLE, "", ""),  # field
        6: (sublime.KindId.VARIABLE, "", ""),  # variable
        7: (sublime.KindId.TYPE, "", ""),  # class
        8: (sublime.KindId.TYPE, "", ""),  # interface
        9: (sublime.KindId.NAMESPACE, "", ""),  # module
        10: (sublime.KindId.VARIABLE, "", ""),  # property
        11: (sublime.KindId.TYPE, "", ""),  # unit
        12: (sublime.KindId.COLOR_ORANGISH, "v", ""),  # value
        13: (sublime.KindId.TYPE, "", ""),  # enum
        14: (sublime.KindId.KEYWORD, "", ""),  # keyword
        15: (sublime.KindId.SNIPPET, "s", ""),  # snippet
        16: (sublime.KindId.VARIABLE, "v", ""),  # color
        17: (sublime.KindId.VARIABLE, "p", ""),  # file
        18: (sublime.KindId.VARIABLE, "p", ""),  # reference
        19: (sublime.KindId.VARIABLE, "p", ""),  # folder
        20: (sublime.KindId.VARIABLE, "v", ""),  # enum member
        21: (sublime.KindId.VARIABLE, "c", ""),  # constant
        22: (sublime.KindId.TYPE, "", ""),  # struct
        23: (sublime.KindId.TYPE, "e", ""),  # event
        24: (sublime.KindId.KEYWORD, "", ""),  # operator
        25: (sublime.KindId.TYPE, "", ""),  # type parameter
    },
)


def wait(event: threading.Event):
    """decorator to wait function call execution event set"""

    def func_wrapper(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            event.wait()
            return func(*args, **kwargs)

        return wrapper

    return func_wrapper


def cancel_if_unset(event: threading.Event):
    """cancel function call if event unset"""

    def func_wrapper(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not event.is_set():
                return None
            return func(*args, **kwargs)

        return wrapper

    return func_wrapper


class ClangdClient(Client):
    """"""

    initialize_event = threading.Event()

    def __init__(self, server: ServerProcess, transport: Transport):
        super().__init__(server, transport, self)

        # server message handler
        self.handler_map: Dict[MethodName, HandlerFunction] = dict()
        self._start_server_lock = threading.Lock()

        self._set_default_handler()

        # session data
        self.session = Session()

    def handle(self, method: MethodName, params: HandleParams) -> Optional[Response]:
        """"""
        try:
            func = self.handler_map[method]
        except KeyError as err:
            raise MethodNotFound(err)

        return func(self.session, params)

    def register_handler(self, method: MethodName, function: HandlerFunction) -> None:
        """"""
        self.handler_map[method] = function

    def start_server(self, env: Optional[dict] = None) -> None:
        """"""
        # only one thread can run server
        if self._start_server_lock.locked():
            return

        with self._start_server_lock:
            if not self.server.is_running():
                sublime.status_message("running language server...")
                # sometimes the server stop working
                # we must reset the state before run server
                self.reset_session()

                self.server.run(env)
                self.listen()

    def reset_session(self) -> None:
        """reset session state"""
        self.session.reset()
        self.initialize_event.clear()

    def is_ready(self) -> bool:
        """check session is ready"""
        return self.server.is_running() and self.initialize_event.is_set()

    def terminate(self) -> None:
        """terminate session"""
        self.server.terminate()
        self.reset_session()

    def _set_default_handler(self):
        default_handlers = {
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
            "textDocument/signatureHelp": self.handle_textdocument_signaturehelp,
            "textDocument/publishDiagnostics": self.handle_textdocument_publishdiagnostics,
            "textDocument/formatting": self.handle_textdocument_formatting,
            "textDocument/declaration": self.handle_textdocument_declaration,
            "textDocument/definition": self.handle_textdocument_definition,
            "textDocument/prepareRename": self.handle_textdocument_preparerename,
            "textDocument/rename": self.handle_textdocument_rename,
            "textDocument/codeAction": self.handle_textdocument_code_action,
        }
        self.handler_map.update(default_handlers)

    def initialize(self, view: sublime.View):
        # cancel if initializing
        if self.session.inittialize_status == InitializeStatus.Initializing:
            return

        # check if view not closed
        if view is None:
            return

        workspace_path = get_workspace_path(view)
        if not workspace_path:
            return

        self.session.inittialize_status = InitializeStatus.Initializing
        self.send_request(
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

    def handle_initialize(self, session: Session, params: Response):
        if err := params.error:
            print(err["message"])
            return

        self.send_notification("initialized", {})
        self.session.inittialize_status = InitializeStatus.Initialized
        self.initialize_event.set()

    def handle_window_logmessage(self, session: Session, params: dict):
        print(params["message"])

    def handle_window_showmessage(self, session: Session, params: dict):
        sublime.status_message(params["message"])

    @wait(initialize_event)
    def textdocument_didopen(self, view: sublime.View, *, reload: bool = False):
        # check if view not closed
        if not (view and view.is_valid()):
            return

        file_name = view.file_name()
        self.session.diagnostic_manager.set_active_view(view)

        # In SublimeText, rename file only retarget to new path
        # but the 'View' did not closed.
        if older_document := self.session.get_document(view):
            rename = older_document.file_name != file_name
            if not (rename or reload):
                return

            # Close older document.
            self.textdocument_didclose(view)

        document = Document(view)

        # Same document maybe opened in multiple 'View', send notification
        # only on first opening document.
        if not self.session.get_documents(file_name):
            self.send_notification(
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

        # Add current document
        self.session.add_document(document)

    @cancel_if_unset(initialize_event)
    def textdocument_didsave(self, view: sublime.View):
        if document := self.session.get_document(view):
            self.send_notification(
                "textDocument/didSave",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

        else:
            # untitled document not yet loaded to server
            self.textdocument_didopen(view)

    @cancel_if_unset(initialize_event)
    def textdocument_didclose(self, view: sublime.View):
        file_name = view.file_name()
        self.session.diagnostic_manager.remove(view)

        if document := self.session.get_document(view):
            self.session.remove_document(view)

            # if document still opened in other View
            if self.session.get_documents(file_name):
                return

            self.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": path_to_uri(document.file_name)}},
            )

    @cancel_if_unset(initialize_event)
    def textdocument_didchange(self, view: sublime.View, changes: List[TextChange]):
        # Document can be related to multiple View but has same file_name.
        # Use get_document_by_name() because may be document already open
        # in other view and the argument view not assigned.
        file_name = view.file_name()
        if document := self.session.get_document_by_name(file_name):
            self.send_notification(
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

        items = self.session.diagnostic_manager.get_diagnostic_items(
            view, contain_point
        )
        if not items:
            return ""

        title = "### Diagnostics:\n"
        diagnostic_message = "\n".join([f"- {escape_html(d.message)}" for d in items])

        command_url = sublime.command_url(
            f"{COMMAND_PREFIX}_code_action", {"event": {"text_point": point}}
        )
        footer = f'***\n<a href="{command_url}">Code Action</a>'
        return f"{title}\n{diagnostic_message}\n{footer}"

    @cancel_if_unset(initialize_event)
    def textdocument_hover(self, view, row, col):
        method = "textDocument/hover"
        # In multi row/column layout, new popup will created in current View,
        # but active popup doesn't discarded.
        if other := self.session.action_target.get(method):
            other.view.hide_popup()

        if document := self.session.get_document(view):
            if message := self._get_diagnostic_message(view, row, col):
                document.show_popup(message, row, col)
                return

            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_hover(self, session: Session, params: Response):
        method = "textDocument/hover"
        if err := params.error:
            print(err["message"])

        elif result := params.result:
            message = result["contents"]["value"]
            row, col = LineCharacter(**result["range"]["start"])
            session.action_target[method].show_popup(message, row, col)

    @cancel_if_unset(initialize_event)
    def textdocument_completion(self, view, row, col):
        method = "textDocument/completion"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @staticmethod
    def _build_completion(completion_item: dict) -> sublime.CompletionItem:
        text = completion_item["insertText"]
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

    def handle_textdocument_completion(self, session: Session, params: Response):
        method = "textDocument/completion"
        if err := params.error:
            print(err["message"])

        elif result := params.result:
            items = [self._build_completion(item) for item in result["items"]]
            session.action_target[method].show_completion(items)

    @cancel_if_unset(initialize_event)
    def textdocument_signaturehelp(self, view, row, col): ...

    def handle_textdocument_signaturehelp(self, session: Session, params: Response): ...

    def handle_textdocument_publishdiagnostics(self, session: Session, params: dict):
        file_name = uri_to_path(params["uri"])
        diagnostics = params["diagnostics"]

        for document in session.get_documents(file_name):
            self.session.diagnostic_manager.set(document.view, diagnostics)

    @cancel_if_unset(initialize_event)
    def textdocument_formatting(self, view):
        method = "textDocument/formatting"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "options": {"insertSpaces": True, "tabSize": 2},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_formatting(self, session: Session, params: Response):
        method = "textDocument/formatting"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            changes = [rpc_to_textchange(c) for c in result]
            session.action_target[method].apply_changes(changes)

    def handle_workspace_applyedit(self, session: Session, params: dict) -> dict:
        try:
            WorkspaceEdit(session).apply_changes(params["edit"])

        except Exception as err:
            LOGGER.error(err, exc_info=True)
            return {"applied": False}
        else:
            return {"applied": True}

    def handle_workspace_executecommand(
        self, session: Session, params: Response
    ) -> dict:
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            LOGGER.info(result)

        return None

    @cancel_if_unset(initialize_event)
    def textdocument_declaration(self, view, row, col):
        method = "textDocument/declaration"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_declaration(self, session: Session, params: Response):
        method = "textDocument/declaration"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            view = session.action_target[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @cancel_if_unset(initialize_event)
    def textdocument_definition(self, view, row, col):
        method = "textDocument/definition"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @staticmethod
    def _build_location(location: dict) -> PathEncodedStr:
        file_name = uri_to_path(location["uri"])
        start_row, start_col = LineCharacter(**location["range"]["start"])
        return f"{file_name}:{start_row+1}:{start_col+1}"

    def handle_textdocument_definition(self, session: Session, params: Response):
        method = "textDocument/definition"
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            view = session.action_target[method].view
            locations = [self._build_location(l) for l in result]
            open_location(view, locations)

    @cancel_if_unset(initialize_event)
    def textdocument_preparerename(self, view, row, col):
        method = "textDocument/prepareRename"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    @cancel_if_unset(initialize_event)
    def textdocument_rename(self, view, row, col, new_name):
        method = "textDocument/rename"

        # Save all changes before perform rename
        for document in self.session.get_documents():
            document.save()

        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "newName": new_name,
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def _handle_preparerename(self, session: Session, location: dict):
        method = "textDocument/prepareRename"
        view = session.action_target[method].view

        start = LineCharacter(**location["range"]["start"])
        end = LineCharacter(**location["range"]["end"])
        start_point = view.text_point(*start)
        end_point = view.text_point(*end)

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

    def handle_textdocument_preparerename(self, session: Session, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            self._handle_preparerename(session, result)

    def handle_textdocument_rename(self, session: Session, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            WorkspaceEdit(session).apply_changes(result)

    @cancel_if_unset(initialize_event)
    def textdocument_code_action(self, view, start, end):
        method = "textDocument/codeAction"
        if document := self.session.get_document(view):
            self.session.action_target[method] = document
            self.send_request(
                method,
                {
                    "context": {
                        "diagnostics": self.session.diagnostic_manager.get(view),
                        "triggerKind": 2,
                    },
                    "range": {
                        "end": {"character": end[1], "line": end[0]},
                        "start": {"character": start[1], "line": start[0]},
                    },
                    "textDocument": {"uri": path_to_uri(document.file_name)},
                },
            )

    def handle_textdocument_code_action(self, session: Session, params: Response):
        if error := params.error:
            print(error["message"])
        elif result := params.result:
            self._show_code_action(result)

    def _show_code_action(self, actions: List[dict]):
        def on_select(index):
            if index < 0:
                return

            if actions[index].get("command"):
                self.send_request("workspace/executeCommand", actions[index])
                return

            edit = actions[index]["edit"]
            WorkspaceEdit(self.session).apply_changes(edit)

        def get_title(action: dict) -> str:
            title = action["title"]
            if kind := action.get("kind"):
                return f"({kind}){title}"
            return title

        items = [get_title(a) for a in actions]
        sublime.active_window().show_quick_panel(items, on_select=on_select)


class WorkspaceEdit:

    def __init__(self, session: Session):
        self.session = session

    def apply_changes(self, edit_changes: dict) -> None:
        """"""

        # Clangd implementation is a little different from standard
        for file_uri, changes in edit_changes["changes"].items():
            self._apply_textedit_changes(uri_to_path(file_uri), changes)

    def _apply_textedit_changes(self, file_name: str, changes: dict):
        changes = [rpc_to_textchange(c) for c in changes]

        if document := self.session.get_document_by_name(file_name):
            document.apply_changes(changes)
            document.save()

        else:
            update_document(file_name, changes)

    def _apply_resource_changes(self, changes: dict):
        func = {
            "create": self._create_document,
            "rename": self._rename_document,
            "delete": self._delete_document,
        }
        kind = changes["kind"]
        func[kind](changes)

    @staticmethod
    def _create_document(document_changes: dict):
        file_name = uri_to_path(document_changes["uri"])
        create_document(file_name)

    @staticmethod
    def _rename_document(document_changes: dict):
        old_name = uri_to_path(document_changes["oldUri"])
        new_name = uri_to_path(document_changes["newUri"])
        rename_document(old_name, new_name)

    @staticmethod
    def _delete_document(document_changes: dict):
        file_name = uri_to_path(document_changes["uri"])
        delete_document(file_name)


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
    )


def get_client() -> ClangdClient:
    """"""
    command = ["clangd", "--log=error", "--offset-encoding=utf-8"]
    server = ServerProcess(command)
    transport = StandardIO(server)
    return ClangdClient(server, transport)


def get_envs_settings() -> Optional[dict]:
    """get environments defined in '*.sublime-settings'"""

    with Settings() as settings:
        if envs := settings.get("envs"):
            return envs
