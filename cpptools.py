"""C++ tools for Sublime Text"""

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass
from functools import wraps
from io import StringIO
from pathlib import Path
from typing import List, Dict, Optional, Any


import sublime
import sublime_plugin
from sublime import HoverZone

from . import api

LOGGER = logging.getLogger(__name__)
# LOGGER.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")
sh = logging.StreamHandler()
sh.setFormatter(fmt)
LOGGER.addHandler(sh)

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


DOCUMENT_CHANGE_EVENT = threading.Event()


class CpptoolsApplyTextChangesCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, changes: List[dict]):
        text_changes = [self.to_text_change(c) for c in changes]
        current_sel = list(self.view.sel())
        try:
            self.apply(edit, text_changes)
            self.relocate_selection(current_sel, text_changes)
        finally:
            self.view.show(self.view.sel(), show_surrounds=False)
            DOCUMENT_CHANGE_EVENT.set()

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


class UnbufferedDocument:
    def __init__(self, file_name: str):
        self._path = Path(file_name)
        self.text = self._path.read_text()

    def apply_text_changes(self, changes: List[dict]):
        try:
            self._apply_text_changes(changes)
        finally:
            DOCUMENT_CHANGE_EVENT.set()

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
    def __init__(self, view: sublime.View):
        self.view = view

        self.file_name = self.view.file_name()
        self._cached_completion = None

        self._add_view_settings()

    def _add_view_settings(self):
        self.view.settings().set("show_definitions", False)
        self.view.settings().set("auto_complete_use_index", False)

    @property
    def version(self) -> int:
        return self.view.change_count()

    @property
    def text(self):
        if self.view.is_loading():
            # read from file
            return Path(self.file_name).read_text()

        return self.view.substr(sublime.Region(0, self.view.size()))

    def document_uri(self) -> api.URI:
        return api.path_to_uri(self.file_name)

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
        self.view.run_command("markdown_popup", {"text": text, "point": point})

    def show_completion(self, items: List[dict]):
        def convert_kind(kind_num: int):
            return COMPLETION_KIND_MAP[kind_num]

        def build_completion(completion: dict):
            trigger = completion["filterText"]
            text = completion["insertText"]
            annotation = completion["label"]
            kind = convert_kind(completion["kind"])

            # sublime text has complete the header bracket '<> or ""'
            # remove it from clangd result
            if completion["kind"] in (17, 19):
                trigger = trigger.rstrip('>"')
                text = text.rstrip('>"')
                annotation = annotation.rstrip('>"')

            return sublime.CompletionItem.snippet_completion(
                trigger=trigger, snippet=text, annotation=annotation, kind=kind
            )

        self._cached_completion = [build_completion(c) for c in items]
        self._trigger_completion()

    @property
    def cached_completion(self):
        temp = self._cached_completion
        self._cached_completion = None
        return temp

    def completion_ready(self) -> bool:
        return self._cached_completion is not None

    def _trigger_completion(self):
        LOGGER.debug("trigger completion")
        self.view.run_command(
            "auto_complete",
            {
                "disable_auto_insert": True,
                "next_completion_if_showing": True,
                "auto_complete_commit_on_tab": True,
            },
        )

    def hide_completion(self):
        self.view.run_command("hide_auto_complete")

    def apply_text_changes(self, changes: List[dict]):
        self.view.run_command("cpptools_apply_text_changes", {"changes": changes})

    def highlight_text(self, diagnostics: List[dict]):
        def get_region(diagnostic):
            start = diagnostic["range"]["start"]
            end = diagnostic["range"]["end"]

            start_point = self.view.text_point(start["line"], start["character"])
            end_point = self.view.text_point(end["line"], end["character"])
            return sublime.Region(start_point, end_point)

        regions = [get_region(d) for d in diagnostics]
        key = "cpptools_diagnostic"

        self.view.add_regions(
            key=key,
            regions=regions,
            scope="Comment",
            icon="dot",
            flags=sublime.DRAW_NO_FILL
            | sublime.DRAW_NO_OUTLINE
            | sublime.DRAW_SQUIGGLY_UNDERLINE,
        )


class DiagnosticPanel:
    OUTPUT_PANEL_NAME = "cpptools_panel"

    def __init__(self):
        self.panel: sublime.View = None

    def _create_panel(self):
        self.panel = sublime.active_window().create_output_panel(self.OUTPUT_PANEL_NAME)
        settings = {"gutter": False, "word_wrap": False}
        self.panel.settings().update(settings)
        self.panel.set_read_only(False)

    def set_content(self, diagnostics_map: Dict[str, List[dict]]):
        """set content with document mapped diagnostics"""

        message_buffer = StringIO()

        def build_message(file_name: str, diagnostics: Dict[str, List[dict]]):
            for diagnostic in diagnostics:
                short_name = Path(file_name).name
                row = diagnostic["range"]["start"]["line"]
                col = diagnostic["range"]["start"]["character"]
                message = diagnostic["message"]
                source = diagnostic.get("source", "")

                # natural line index start with 1
                row += 1

                message_buffer.write(
                    f"{short_name}:{row}:{col}: {message} ({source})\n"
                )

        try:
            for file_name, diagnostics in diagnostics_map.items():
                build_message(file_name, diagnostics)

        except RuntimeError:
            # maybe dictionary changed during iteration
            return

        if not (self.panel and self.panel.is_valid()):
            self._create_panel()

        # recreate panel if assigned window has closed
        if not self.panel.is_valid():
            self.window = sublime.active_window()
            self._create_panel()

        # clear content
        self.panel.run_command("select_all")
        self.panel.run_command("left_delete")

        self.panel.run_command(
            "append",
            {"characters": message_buffer.getvalue()},
        )

    def show(self) -> None:
        """show output panel"""
        sublime.active_window().run_command(
            "show_panel", {"panel": f"output.{self.OUTPUT_PANEL_NAME}"}
        )

    def destroy(self):
        """destroy output panel"""
        sublime.active_window().destroy_output_panel(self.OUTPUT_PANEL_NAME)


class Session:
    def __init__(self):
        self._begin = False
        self._begin_event = threading.Event()

    def is_ready(self):
        return self._begin

    def begin(self):
        self._begin = True
        self._begin_event.set()

    def done(self):
        self._begin = False
        self._begin_event.clear()

    def must_begin(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not self._begin:
                return None

            return func(*args, **kwargs)

        return wrapper

    def wait_begin(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            self._begin_event.wait()
            return func(*args, **kwargs)

        return wrapper


@dataclass
class Workspace:
    documents: Dict[sublime.View, BufferedDocument] = None
    diagnostics: Dict[str, dict] = None

    def __post_init__(self):
        self.documents = {}
        self.diagnostics = {}

    def get_document_by_name(
        self, file_name: str, /, default: Any = None
    ) -> Optional[BufferedDocument]:
        """get document by name"""

        for view, document in self.documents.items():
            if view.file_name() == file_name:
                return document
        return default

    def get_documents(self, file_name: Optional[str] = None) -> List[BufferedDocument]:
        """get documents.
        If file_name assigned, return documents with file_name filtered.
        """

        if not file_name:
            return [doc for _, doc in self.documents.items()]
        return [doc for _, doc in self.documents.items() if doc.file_name == file_name]


@dataclass
class ActionTarget:
    hover: BufferedDocument = None
    completion: BufferedDocument = None
    formatting: BufferedDocument = None
    definition: BufferedDocument = None
    rename: BufferedDocument = None


class ClangdHandler(api.BaseHandler):
    """"""

    session = Session()

    def __init__(self):
        # client initializer
        server_command = ["clangd"]
        # logging verbosity
        verbosity = "verbose" if LOGGER.level == logging.DEBUG else "error"
        server_command.append(f"--log={verbosity}")

        transport = api.StandardIO(server_command)
        self.client = api.Client(transport, self)

        # workspace status
        self._initializing = False
        self.workspace = Workspace()

        self.diagnostics_panel = DiagnosticPanel()

        # commands document target
        self.action_target = ActionTarget()

    def _reset_state(self):
        self.workspace = Workspace()
        self._initializing = False

        # commands document target
        self.action_target = ActionTarget()
        self.session.done()

    def ready(self) -> bool:
        return self.client.server_running() and self.session.is_ready()

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

        self._initializing = True
        self.client.send_request(
            "initialize",
            {
                "rootPath": workspace_path,
                "rootUri": api.path_to_uri(workspace_path),
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
        if not view.is_valid():
            return

        if (view in self.workspace.documents) and (not reload):
            return

        file_name = view.file_name()
        other = self.workspace.get_documents(file_name)

        document = BufferedDocument(view)
        self.workspace.documents[view] = document

        # if document has opened in other View
        if other:
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
        if document := self.workspace.documents.get(view):
            self.client.send_notification(
                "textDocument/didSave",
                {"textDocument": {"uri": document.document_uri()}},
            )

        else:
            # untitled document not yet loaded to server
            self.textdocument_didopen(view)

    def textdocument_didclose(self, view: sublime.View):
        file_name = view.file_name()
        if document := self.workspace.documents.get(view):
            try:
                del self.workspace.documents[view]
                del self.workspace.diagnostics[file_name]
            except KeyError:
                pass

            self.diagnostics_panel.set_content(self.workspace.diagnostics)
            self.diagnostics_panel.show()

            # if document still opened in other View
            if self.workspace.get_documents(file_name):
                return

            self.client.send_notification(
                "textDocument/didClose",
                {"textDocument": {"uri": document.document_uri()}},
            )

    @session.must_begin
    def textdocument_didchange(self, view: sublime.View, changes: List[dict]):
        # document can be related to multiple View but has same file_name
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
        if document := self.workspace.documents.get(view):
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
        if document := self.workspace.documents.get(view):
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

    def handle_textdocument_publishdiagnostics(self, params: dict):
        file_name = api.uri_to_path(params["uri"])
        diagnostics = params["diagnostics"]

        self.workspace.diagnostics[file_name] = diagnostics

        self.diagnostics_panel.set_content(self.workspace.diagnostics)
        self.diagnostics_panel.show()

        for document in self.workspace.get_documents(file_name):
            document.highlight_text(diagnostics)

    @session.must_begin
    def textdocument_formatting(self, view):
        if document := self.workspace.documents.get(view):
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
        if document := self.workspace.documents.get(view):
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
            DOCUMENT_CHANGE_EVENT.clear()
            file_name = api.uri_to_path(file_uri)
            document = self.workspace.get_document_by_name(
                file_name, UnbufferedDocument(file_name)
            )
            document.apply_text_changes(changes)
            # wait until changes applied
            DOCUMENT_CHANGE_EVENT.wait()
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
        if document := self.workspace.documents.get(view):
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
        if document := self.workspace.documents.get(view):
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

        def build_location(location: dict):
            file_name = api.uri_to_path(location["uri"])
            row = location["range"]["start"]["line"]
            col = location["range"]["start"]["character"]
            return f"{file_name}:{row+1}:{col+1}"

        locations = [build_location(l) for l in locations]

        def open_location(index):
            if index < 0:
                self.active_window().focus_view(current_view)
                current_view.sel().clear()
                current_view.sel().add_all(current_sel)
                current_view.show(visible_region, show_surrounds=False)

            else:
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
        if document := self.workspace.documents.get(view):
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
    if view.match_selector(point, "source.c++"):
        return True
    if view.match_selector(point, "source.c"):
        return True
    return False


def get_workspace_path(view: sublime.View) -> str:
    window = view.window()
    file_name = view.file_name()

    if folders := [
        folder for folder in window.folders() if file_name.startswith(folder)
    ]:
        return max(folders)
    return str(Path(file_name).parent)


class EventListener(sublime_plugin.EventListener):
    def on_hover(self, view: sublime.View, point: int, hover_zone: HoverZone):
        # check point in valid source
        if not (valid_context(view, point) and hover_zone == sublime.HOVER_TEXT):
            return

        row, col = view.rowcol(point)

        threading.Thread(target=self._on_hover, args=(view, row, col)).start()

    def _on_hover(self, view, row, col):
        # check if server available
        try:
            if HANDLER.ready():
                # on multi column layout, sometime we hover on other document which may
                # not loaded yet
                HANDLER.textdocument_didopen(view)
                # request on hover
                HANDLER.textdocument_hover(view, row, col)
            else:
                # initialize server
                HANDLER.run_server()

                HANDLER.initialize(view)
                HANDLER.textdocument_didopen(view)
                HANDLER.textdocument_hover(view, row, col)

        except api.ServerNotRunning:
            pass

    prev_completion_loc = 0

    def on_query_completions(
        self, view: sublime.View, prefix: str, locations: List[int]
    ) -> sublime.CompletionList:
        if not HANDLER.ready():
            return None

        point = locations[0]

        # check point in valid source
        if not valid_context(view, point):
            return

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
            word = view.word(self.prev_completion_loc)
            word_str = view.substr(word).strip()

            # point unchanged
            if point == self.prev_completion_loc:
                show = is_show_after(word_str)

            # point changed but still in same word
            elif word_str.isidentifier() and point in word:
                show = True
            else:
                show = False

            if (cache := document.cached_completion) and show:
                LOGGER.debug("show auto_complete")
                return sublime.CompletionList(
                    cache, flags=sublime.INHIBIT_WORD_COMPLETIONS
                )

            LOGGER.debug("hide auto_complete")
            document.hide_completion()
            return

        self.prev_completion_loc = point
        row, col = view.rowcol(point)

        threading.Thread(
            target=self._on_query_completions, args=(view, row, col)
        ).start()

        view.run_command("hide_auto_complete")

    def _on_query_completions(self, view, row, col):
        if HANDLER.ready():
            HANDLER.textdocument_completion(view, row, col)

    def on_activated_async(self, view: sublime.View):
        # check point in valid source
        if not valid_context(view, 0):
            return

        if HANDLER.ready():
            HANDLER.textdocument_didopen(view)

        else:
            if LOGGER.level == logging.DEBUG:
                return

            try:
                # initialize server
                HANDLER.run_server()

                HANDLER.initialize(view)
                HANDLER.textdocument_didopen(view)

            except api.ServerNotRunning:
                pass

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
