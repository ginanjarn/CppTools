"""C++ tools for Sublime Text"""

import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import List, Dict, Optional, Any


import sublime
import sublime_plugin
from sublime import HoverZone

from .api import lsp_client


LOGGER = logging.getLogger(__name__)
# LOGGER.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")
sh = logging.StreamHandler()
sh.setFormatter(fmt)
LOGGER.addHandler(sh)

# support type
PathStr = str

PACKAGE_NAME = str(Path(__file__).parent)

# custom kind
KIND_PATH = (sublime.KIND_ID_VARIABLE, "p", "")
KIND_VALUE = (sublime.KIND_ID_VARIABLE, "u", "")
KIND_TEXT = (sublime.KIND_ID_VARIABLE, "t", "")
COMPLETION_KIND_MAP = defaultdict(
    lambda _: sublime.KIND_AMBIGUOUS,
    {
        1: KIND_TEXT,  # text
        2: sublime.KIND_FUNCTION,  # method
        3: sublime.KIND_FUNCTION,  # function
        4: sublime.KIND_FUNCTION,  # constructor
        5: sublime.KIND_VARIABLE,  # field
        6: sublime.KIND_VARIABLE,  # variable
        7: sublime.KIND_TYPE,  # class
        8: sublime.KIND_TYPE,  # interface
        9: sublime.KIND_NAMESPACE,  # module
        10: sublime.KIND_VARIABLE,  # property
        11: KIND_VALUE,  # unit
        12: KIND_VALUE,  # value
        13: sublime.KIND_NAMESPACE,  # enum
        14: sublime.KIND_KEYWORD,  # keyword
        15: sublime.KIND_SNIPPET,  # snippet
        16: KIND_VALUE,  # color
        17: KIND_PATH,  # file
        18: sublime.KIND_NAVIGATION,  # reference
        19: KIND_PATH,  # folder
        20: sublime.KIND_VARIABLE,  # enum member
        21: sublime.KIND_VARIABLE,  # constant
        22: sublime.KIND_TYPE,  # struct
        23: sublime.KIND_MARKUP,  # event
        24: sublime.KIND_MARKUP,  # operator
        25: sublime.KIND_TYPE,  # type parameter
    },
)


@dataclass
class TextChange:
    region: sublime.Region
    new_text: str
    cursor_move: int = 0

    def moved_region(self, move: int) -> sublime.Region:
        return sublime.Region(self.region.a + move, self.region.b + move)


MULTIDOCUMENT_CHANGE_LOCK = threading.Lock()


class CpptoolsApplyTextChangesCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, changes: List[dict]):
        text_changes = [self.to_text_change(c) for c in changes]
        current_sel = list(self.view.sel())

        with MULTIDOCUMENT_CHANGE_LOCK:
            self.apply(edit, text_changes)
            self.relocate_selection(current_sel, text_changes)
            self.view.show(self.view.sel(), show_surrounds=False)

    def to_text_change(self, change: dict) -> TextChange:
        start = change["range"]["start"]
        end = change["range"]["end"]

        start_point = self.view.text_point(start["line"], start["character"])
        end_point = self.view.text_point(end["line"], end["character"])

        region = sublime.Region(start_point, end_point)
        new_text = change["newText"]
        cursor_move = len(new_text) - region.size()

        return TextChange(region, new_text, cursor_move)

    def apply(self, edit: sublime.Edit, text_changes: List[TextChange]):
        cursor_move = 0
        for change in text_changes:
            replaced_region = change.moved_region(cursor_move)
            self.view.erase(edit, replaced_region)
            self.view.insert(edit, replaced_region.a, change.new_text)
            cursor_move += change.cursor_move

    def relocate_selection(
        self, selections: List[sublime.Region], changes: List[TextChange]
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


class TextHighlighter:
    REGIONS_KEY = f"{PACKAGE_NAME}_REGIONS"

    def __init__(self, view: sublime.View):
        self.view = view

    def get_region(self, diagnostic: dict):
        start = diagnostic["range"]["start"]
        end = diagnostic["range"]["end"]

        start_point = self.view.text_point(start["line"], start["character"])
        end_point = self.view.text_point(end["line"], end["character"])
        return sublime.Region(start_point, end_point)

    def apply(self, diagnostics: List[dict]):
        regions = [self.get_region(d) for d in diagnostics]

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
        self._path = Path(file_name)

    @property
    def text(self):
        return self._path.read_text()

    def apply_text_changes(self, changes: List[dict]):
        with MULTIDOCUMENT_CHANGE_LOCK:
            self._apply_text_changes(changes)

    def _apply_text_changes(self, changes: List[dict]):
        for change in changes:
            try:
                start = change["range"]["start"]
                end = change["range"]["end"]
                new_text = change["newText"]

                start_line, start_character = start["line"], start["character"]
                end_line, end_character = end["line"], end["character"]

            except KeyError as err:
                raise Exception(f"invalid params {err}") from err

            lines = self.text.split("\n")
            temp_lines = []

            # pre change line
            temp_lines.extend(lines[:start_line])
            # line changed
            prefix = lines[start_line][:start_character]
            suffix = lines[end_line][end_character:]
            line = f"{prefix}{new_text}{suffix}"
            temp_lines.append(line)
            # post change line
            temp_lines.extend(lines[end_line + 1 :])

            self.text = "\n".join(temp_lines)

    def save(self):
        self._path.write_text(self.text)


class BufferedDocument:
    VIEW_SETTINGS = {
        "show_definition": False,
        "auto_complete_use_index": False,
    }

    def __init__(self, view: sublime.View):
        self.view = view

        self.file_name = self.view.file_name()
        self._cached_completion = queue.Queue(maxsize=1)

        self.view.settings().update(self.VIEW_SETTINGS)

    @property
    def version(self) -> int:
        return self.view.change_count()

    @property
    def text(self):
        # wait until complete loaded
        while self.view.is_loading():
            time.sleep(0.5)

        return self.view.substr(sublime.Region(0, self.view.size()))

    def document_uri(self) -> lsp_client.URI:
        return lsp_client.path_to_uri(self.file_name)

    @property
    def language_id(self) -> str:
        if self.view.match_selector(0, "source.c++"):
            return "cpp"
        if self.view.match_selector(0, "source.c"):
            return "c"

    @property
    def window(self) -> sublime.Window:
        return self.view.window()

    def save(self):
        self.view.run_command("save")

    def show_popup(self, text: str, row: int, col: int):
        point = self.view.text_point(row, col)
        self.view.run_command(
            "marked_popup", {"location": point, "text": text, "markup": "markdown"}
        )

    def show_completion(self, items: List[dict]):
        def convert_kind(kind_num: int):
            return COMPLETION_KIND_MAP[kind_num]

        def build_completion(completion: dict):
            text = completion["filterText"]
            try:
                insert_text = completion["textEdit"]["newText"]
            except KeyError:
                insert_text = text

            # clangd defined 'label' starts with '<space>' or '•'
            signature = completion["label"][1:]
            kind = convert_kind(completion["kind"])

            # sublime text has complete the header bracket '<> or ""'
            # remove it from clangd result
            if completion["kind"] in (17, 19):
                closing_include = '">'
                text = text.rstrip(closing_include)
                insert_text = insert_text.rstrip(closing_include)
                signature = signature.rstrip(closing_include)

            return sublime.CompletionItem.snippet_completion(
                trigger=text,
                snippet=insert_text,
                annotation=signature,
                kind=kind,
            )

        temp = [build_completion(c) for c in items]
        try:
            self._cached_completion.put_nowait(temp)
        except queue.Full:
            # get current completion
            _ = self._cached_completion.get()
            self._cached_completion.put(temp)

        self._trigger_completion()

    def pop_completion(self) -> List[sublime.CompletionItem]:
        try:
            return self._cached_completion.get_nowait()
        except queue.Empty:
            return []

    def completion_ready(self) -> bool:
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

    def apply_text_changes(self, changes: List[dict]):
        self.view.run_command("cpptools_apply_text_changes", {"changes": changes})

    def highlight_text(self, diagnostics: List[dict]):
        highligter = TextHighlighter(self.view)
        highligter.clear()
        highligter.apply(diagnostics)


class DiagnosticPanel:
    OUTPUT_PANEL_NAME = f"{PACKAGE_NAME}_PANEL"
    SETTINGS = {"gutter": False, "word_wrap": False}

    def __init__(self):
        self.panel: sublime.View = None

    def _create_panel(self):
        self.panel = sublime.active_window().create_output_panel(self.OUTPUT_PANEL_NAME)
        self.panel.settings().update(self.SETTINGS)
        self.panel.set_read_only(False)

    def set_content(self, text: str):
        if not (self.panel and self.panel.is_valid()):
            self._create_panel()

        # clear content
        self.panel.run_command("select_all")
        self.panel.run_command("left_delete")

        self.panel.run_command("append", {"characters": text})

    def show(self) -> None:
        """show output panel"""
        sublime.active_window().run_command(
            "show_panel", {"panel": f"output.{self.OUTPUT_PANEL_NAME}"}
        )

    def destroy(self):
        """destroy output panel"""
        for window in sublime.windows():
            window.destroy_output_panel(self.OUTPUT_PANEL_NAME)


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


class Workspace:
    # Map document by view is easier to track if view is valid.
    # If we map by file name, one document my related to multiple 'View'
    # and some times the 'View' is invalid.
    def __init__(self):
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
        return self.diagnostics

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

        removed_file = set()
        document_files = {doc.file_name for _, doc in self.documents.items()}
        for file_name, _ in self.diagnostics.items():
            if not file_name in document_files:
                removed_file.add(file_name)

        for file_name in removed_file:
            del self.diagnostics[file_name]


@dataclass
class ActionTarget:
    hover: BufferedDocument = None
    completion: BufferedDocument = None
    formatting: BufferedDocument = None
    definition: BufferedDocument = None
    rename: BufferedDocument = None


class ClangdHandler(lsp_client.BaseHandler):
    """"""

    session = Session()

    def __init__(self):
        # client initializer
        server_command = ["clangd"]
        # logging verbosity
        verbosity = "verbose" if LOGGER.level == logging.DEBUG else "error"
        server_command.append(f"--log={verbosity}")

        self.transport = lsp_client.StandardIO(server_command)
        self.client = lsp_client.Client(self.transport, self)

        # workspace status
        self._initializing = False
        self.workspace = Workspace()

        self.diagnostics_panel = DiagnosticPanel()

        # commands document target
        self.action_target = ActionTarget()

    def _reset_state(self):
        self.workspace = Workspace()
        self._initializing = False
        self.diagnostics_panel.destroy()
        TextHighlighter.clear_all()

        # commands document target
        self.action_target = ActionTarget()
        self.session.done()

    def ready(self) -> bool:
        return self.client.server_running() and self.session.is_begin()

    run_server_lock = threading.Lock()

    def run_server(self):
        # only one thread can run server
        if self.run_server_lock.locked():
            return

        with self.run_server_lock:
            if not self.client.server_running():
                sublime.status_message("running clangd...")
                # sometimes the server stop working
                # we must reset the state before run server
                self._reset_state()

                self.client.run_server()
                self.client.listen()

    def terminate(self):
        """exit session"""
        self.client.terminate_server()
        self._reset_state()

    def active_window(self) -> sublime.Window:
        return sublime.active_window()

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
                "rootUri": lsp_client.path_to_uri(workspace_path),
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

        self.session.begin()

    @session.wait_begin
    def textdocument_didopen(self, view: sublime.View, *, reload: bool = False):
        # check if view not closed
        if not (view and view.is_valid()):
            return

        file_name = view.file_name()

        if opened_document := self.workspace.get_document(view):
            if opened_document.file_name == file_name and (not reload):
                return

            # In Sublime Text, rename file only retarget to new file
            # but the view is not closed.
            # Close older document then reopen with new name.
            self.textdocument_didclose(view)

        # document may open in other views
        other_documents = self.workspace.get_documents(file_name)

        document = BufferedDocument(view)
        self.workspace.add_document(document)

        # if document has opened in other View
        if other_documents:
            LOGGER.debug("%s has opened in %s", file_name, other_documents)
            return

        self.client.send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "languageId": document.language_id,
                    "text": document.text,
                    "uri": document.document_uri(),
                    "version": document.version,
                }
            },
        )

    def textdocument_didsave(self, view: sublime.View):
        if document := self.workspace.get_document(view):
            self.client.send_notification(
                "textDocument/didSave",
                {"textDocument": {"uri": document.document_uri()}},
            )

        else:
            # untitled document not yet loaded to server
            self.textdocument_didopen(view)

    def textdocument_didclose(self, view: sublime.View):
        file_name = view.file_name()
        if document := self.workspace.get_document(view):
            self.workspace.remove_document(view)

            # if document still opened in other View
            if self.workspace.get_documents(file_name):
                return

            self.workspace.remove_invalid_diagnostic()

            diagnostic_text = self._build_diagnostic_message(
                self.workspace.get_diagnostics()
            )
            self.diagnostics_panel.set_content(diagnostic_text)
            self.diagnostics_panel.show()

            self.client.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": document.document_uri()}},
            )

    @session.must_begin
    def textdocument_didchange(self, view: sublime.View, changes: List[dict]):
        # Document can be related to multiple View but has same file_name.
        # Use get_document_by_name() because may be document already open
        # in other view and the argument view not assigned.
        file_name = view.file_name()
        if document := self.workspace.get_document_by_name(file_name):
            self.client.send_notification(
                "textDocument/didChange",
                {
                    "contentChanges": changes,
                    "textDocument": {
                        "uri": document.document_uri(),
                        "version": document.version,
                    },
                },
            )

    @session.must_begin
    def textdocument_hover(self, view, row, col):
        # In multi row/column layout, new popup will created in current View,
        # but active popup doesn't discarded.
        if other := self.action_target.hover:
            other.view.hide_popup()

        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/hover",
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.hover = document

    def handle_textdocument_hover(self, params: dict):
        if err := params.get("error"):
            print(err["message"])

        elif result := params.get("result"):
            try:
                message = result["contents"]["value"]
                start = result["range"]["start"]
                row, col = start["line"], start["character"]
            except Exception:
                pass
            else:
                self.action_target.hover.show_popup(message, row, col)

    @session.must_begin
    def textdocument_completion(self, view, row, col):
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/completion",
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.completion = document

    def handle_textdocument_completion(self, params: dict):
        if err := params.get("error"):
            print(err["message"])

        elif result := params.get("result"):
            try:
                items = result["items"]
            except Exception:
                pass
            else:
                self.action_target.completion.show_completion(items)

    def _build_diagnostic_message(self, diagnostics_map: Dict[PathStr, Any]) -> str:
        messages = []

        def build_line(file_name, diagnostic):
            short_name = Path(file_name).name
            row = diagnostic["range"]["start"]["line"]
            col = diagnostic["range"]["start"]["character"]
            message = diagnostic["message"]
            source = diagnostic.get("source", "")

            # natural line index start with 1
            row += 1

            return f"{short_name}:{row}:{col}: {message} ({source})"

        for file_name, diagnostics in diagnostics_map.items():
            lines = [build_line(file_name, item) for item in diagnostics]
            messages.extend(lines)

        return "\n".join(messages)

    def handle_textdocument_publishdiagnostics(self, params: dict):
        file_name = lsp_client.uri_to_path(params["uri"])
        diagnostics = params["diagnostics"]

        diagnostic_text = ""
        self.workspace.set_diagnostic(file_name, diagnostics)

        # Ensure diagnostics unchanged while buid message and applying syntax highlight
        with self.workspace.diagnostic_lock:
            diagnostic_text = self._build_diagnostic_message(
                self.workspace.get_diagnostics()
            )

            for document in self.workspace.get_documents(file_name):
                document.highlight_text(diagnostics)

        self.diagnostics_panel.set_content(diagnostic_text)
        self.diagnostics_panel.show()

    @session.must_begin
    def textdocument_formatting(self, view):
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/formatting",
                {
                    "options": {"insertSpaces": True, "tabSize": 2},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.formatting = document

    def handle_textdocument_formatting(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self.action_target.formatting.apply_text_changes(result)

    @session.must_begin
    def textdocument_codeaction(self, view, start_row, start_col, end_row, end_col):
        file_name = view.file_name()
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/codeAction",
                {
                    "context": {
                        "diagnostics": self.workspace.diagnostics.get(file_name, []),
                        "triggerKind": 2,
                    },
                    "range": {
                        "end": {"character": end_col, "line": end_row},
                        "start": {"character": start_col, "line": start_row},
                    },
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.codeaction_target = document

    def handle_textdocument_codeaction(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._show_codeaction(result)

    def _show_codeaction(self, actions: List[dict]):
        def on_select(index):
            if index < 0:
                return

            action = actions[index]
            if edit := action.get("edit"):
                self._apply_edit(edit)
            elif action.get("command"):
                self.client.send_request("workspace/executeCommand", action)

        def get_title(action: dict) -> str:
            title = action["title"]
            if kind := action.get("kind"):
                return f"({kind}){title}"
            return title

        self.active_window().show_quick_panel(
            items=[get_title(i) for i in actions],
            on_select=on_select,
            flags=sublime.MONOSPACE_FONT,
            placeholder="Code actions...",
        )

    def _apply_edit(self, edit: dict):
        for file_uri, changes in edit["changes"].items():
            file_name = lsp_client.uri_to_path(file_uri)
            document = self.workspace.get_document_by_name(
                file_name, UnbufferedDocument(file_name)
            )
            document.apply_text_changes(changes)
            document.save()

    def handle_workspace_applyedit(self, params: dict) -> dict:
        try:
            self._apply_edit(params["edit"])
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
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/declaration",
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.definition = document

    @session.must_begin
    def textdocument_definition(self, view, row, col):
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/definition",
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.definition = document

    def _open_locations(self, locations: List[dict]):
        current_view = self.action_target.definition.view
        current_sel = tuple(current_view.sel())
        visible_region = current_view.visible_region()

        def restore_selection():
            self.active_window().focus_view(current_view)
            current_view.sel().clear()
            current_view.sel().add_all(current_sel)
            current_view.show(visible_region, show_surrounds=False)

        def build_location(location: dict):
            file_name = lsp_client.uri_to_path(location["uri"])
            row = location["range"]["start"]["line"]
            col = location["range"]["start"]["character"]
            return f"{file_name}:{row+1}:{col+1}"

        locations = [build_location(l) for l in locations]
        locations.sort()

        def open_location(index):
            if index < 0:
                restore_selection()
                return

            flags = sublime.ENCODED_POSITION
            self.active_window().open_file(locations[index], flags=flags)

        def preview_location(index):
            flags = sublime.ENCODED_POSITION | sublime.TRANSIENT
            self.active_window().open_file(locations[index], flags=flags)

        self.active_window().show_quick_panel(
            items=locations,
            on_select=open_location,
            flags=sublime.MONOSPACE_FONT,
            on_highlight=preview_location,
            placeholder="Open location...",
        )

    def handle_textdocument_declaration(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._open_locations(result)

    def handle_textdocument_definition(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._open_locations(result)

    @session.must_begin
    def textdocument_preparerename(self, view, row, col):
        if document := self.workspace.get_document(view):
            self.client.send_request(
                "textDocument/prepareRename",
                {
                    "position": {"character": col, "line": row},
                    "textDocument": {"uri": document.document_uri()},
                },
            )
            self.action_target.rename = document

    @session.must_begin
    def textdocument_rename(self, new_name, row, col):
        self.client.send_request(
            "textDocument/rename",
            {
                "newName": new_name,
                "position": {"character": col, "line": row},
                "textDocument": {"uri": self.action_target.rename.document_uri()},
            },
        )

    def _input_rename(self, symbol_location: dict):
        start = symbol_location["start"]
        start_point = self.action_target.rename.view.text_point(
            start["line"], start["character"]
        )
        end = symbol_location["end"]
        end_point = self.action_target.rename.view.text_point(
            end["line"], end["character"]
        )

        def request_rename(new_name):
            self.textdocument_rename(new_name, start["line"], start["character"])

        self.active_window().show_input_panel(
            caption="rename",
            initial_text=self.action_target.rename.view.substr(
                sublime.Region(start_point, end_point)
            ),
            on_done=request_rename,
            on_change=None,
            on_cancel=None,
        )

    def handle_textdocument_preparerename(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._input_rename(result)

    def handle_textdocument_rename(self, params: dict):
        if error := params.get("error"):
            print(error["message"])
        elif result := params.get("result"):
            self._apply_edit(result)


HANDLER: ClangdHandler = None


def plugin_loaded():
    global HANDLER
    HANDLER = ClangdHandler()


def plugin_unloaded():
    if HANDLER:
        HANDLER.terminate()


def valid_context(view: sublime.View, point: int):
    if not (view and view.is_valid()):
        return False
    return view.match_selector(point, "source.c,source.c++")


def get_workspace_path(view: sublime.View) -> str:
    window = view.window()
    file_name = view.file_name()
    if not file_name:
        return ""

    if folders := [
        folder for folder in window.folders() if file_name.startswith(folder)
    ]:
        return max(folders)
    return str(Path(file_name).parent)


class EventListener(sublime_plugin.EventListener):

    def __init__(self):
        self.prev_completion_point = 0

    def on_hover(self, view: sublime.View, point: int, hover_zone: HoverZone):
        # check point in valid source
        if not (valid_context(view, point) and hover_zone == sublime.HOVER_TEXT):
            return

        row, col = view.rowcol(point)
        if HANDLER.ready():
            HANDLER.textdocument_hover(view, row, col)

        else:
            threading.Thread(target=self._on_hover, args=(view, row, col)).start()

    def _on_hover(self, view, row, col):
        # initialize server if not ready
        if not HANDLER.ready():
            HANDLER.run_server()
            HANDLER.initialize(view)

        # on multi column layout, sometime we hover on other document which may
        # not loaded yet
        HANDLER.textdocument_didopen(view)
        # request on hover
        HANDLER.textdocument_hover(view, row, col)

    def on_query_completions(
        self, view: sublime.View, prefix: str, locations: List[int]
    ) -> sublime.CompletionList:
        if not HANDLER.ready():
            return None

        point = locations[0]

        # check point in valid source
        if not valid_context(view, point):
            return None

        def is_show_after(word_str: str) -> bool:
            if word_str.isspace() or word_str.isidentifier():
                return True

            # we must modify the prefix because argument defined prefix
            # only set if the prefix is identifier
            prefix_region = sublime.Region(point - 2, point)
            prefix = view.substr(prefix_region)

            # accessor
            if prefix.endswith("."):
                return True
            if prefix.endswith("->"):
                return True
            if prefix.endswith("::"):
                return True

            # prevent show completion after following puctuation
            if prefix.endswith(":"):
                return False
            if prefix.endswith(";"):
                return False

            # else
            return True

        if (
            document := HANDLER.action_target.completion
        ) and document.completion_ready():
            word = view.word(self.prev_completion_point)
            word_str = view.substr(word).strip()

            # point unchanged
            if point == self.prev_completion_point:
                show = is_show_after(word_str)

            # point changed but still in same word
            elif word_str.isidentifier() and point in word:
                show = True
            else:
                show = False

            if (cache := document.pop_completion()) and show:
                return sublime.CompletionList(
                    cache, flags=sublime.INHIBIT_WORD_COMPLETIONS
                )

            document.hide_completion()
            return None

        self.prev_completion_point = point
        row, col = view.rowcol(point)

        HANDLER.textdocument_completion(view, row, col)
        view.run_command("hide_auto_complete")

        return None

    def on_activated_async(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didopen(view)
            return

        if LOGGER.level == logging.DEBUG:
            return

        # initialize server
        HANDLER.run_server()
        HANDLER.initialize(view)
        HANDLER.textdocument_didopen(view)

    def on_post_save_async(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didsave(view)

    def on_close(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didclose(view)

    def on_load(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didopen(view, reload=True)

    def on_reload(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didopen(view, reload=True)

    def on_revert(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didopen(view, reload=True)


class TextChangeListener(sublime_plugin.TextChangeListener):
    def on_text_changed(self, changes: List[sublime.TextChange]):
        view = self.buffer.primary_view()

        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didchange(
                view, [self.change_as_rpc(c) for c in changes]
            )

    @staticmethod
    def change_as_rpc(change: sublime.TextChange) -> dict:
        start = change.a
        end = change.b
        return {
            "range": {
                "end": {"character": end.col, "line": end.row},
                "start": {"character": start.col, "line": start.row},
            },
            "rangeLength": change.len_utf8,
            "text": change.str,
        }


class CpptoolsDocumentFormattingCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit):
        if HANDLER.ready():
            HANDLER.textdocument_formatting(self.view)

    def is_visible(self):
        return valid_context(self.view, 0)


class CpptoolsCodeActionCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit):
        cursor = self.view.sel()[0]
        if HANDLER.ready():
            start_row, start_col = self.view.rowcol(cursor.a)
            end_row, end_col = self.view.rowcol(cursor.b)
            HANDLER.textdocument_codeaction(
                self.view, start_row, start_col, end_row, end_col
            )

    def is_visible(self):
        return valid_context(self.view, 0)


class CpptoolsGotoDefinitionCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if HANDLER.ready():
            start_row, start_col = self.view.rowcol(point)
            HANDLER.textdocument_definition(self.view, start_row, start_col)

    def is_visible(self):
        return valid_context(self.view, 0)

    def want_event(self):
        return True


class CpptoolsGotoDeclarationCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if HANDLER.ready():
            start_row, start_col = self.view.rowcol(point)
            HANDLER.textdocument_declaration(self.view, start_row, start_col)

    def is_visible(self):
        return valid_context(self.view, 0)

    def want_event(self):
        return True


class CpptoolsRenameCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, event: Optional[dict] = None):
        cursor = self.view.sel()[0]
        point = event["text_point"] if event else cursor.a
        if HANDLER.ready():
            # move cursor to point
            self.view.sel().clear()
            self.view.sel().add(point)

            start_row, start_col = self.view.rowcol(point)
            HANDLER.textdocument_preparerename(self.view, start_row, start_col)

    def is_visible(self):
        return valid_context(self.view, 0)

    def want_event(self):
        return True


class CpptoolsTerminateCommand(sublime_plugin.WindowCommand):
    def run(self):
        if HANDLER:
            HANDLER.terminate()

    def is_visible(self):
        return HANDLER and HANDLER.ready()
