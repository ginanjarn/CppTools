"""panels"""

from dataclasses import asdict
from typing import Callable, List

import sublime

from .constant import PACKAGE_NAME, COMMAND_PREFIX
from .document import TextChange


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

        start = (0, 0)
        end = self.panel.rowcol(self.panel.size())

        change = TextChange(start, end, text, -1)
        self.panel.run_command(
            f"{COMMAND_PREFIX}_apply_text_changes",
            {"changes": [asdict(change)]},
        )

    def show(self) -> None:
        """show output panel"""
        sublime.active_window().run_command(
            "show_panel", {"panel": f"output.{self.OUTPUT_PANEL_NAME}"}
        )

    def destroy(self):
        """destroy output panel"""
        for window in sublime.windows():
            window.destroy_output_panel(self.OUTPUT_PANEL_NAME)


def input_text(
    title: str, default_text: str, on_done_callback: Callable[[str], None]
) -> None:
    """"""
    sublime.active_window().show_input_panel(
        caption=title,
        initial_text=default_text,
        on_done=on_done_callback,
        on_change=None,
        on_cancel=None,
    )


PathEncodedStr = str
"""Path encoded '<file_name>:<row>:<column>'"""


def set_selection(view: sublime.View, regions: List[sublime.Region]):
    """"""
    view.sel().clear()
    view.sel().add_all(regions)


def open_document(
    window: sublime.Window, file_name: PathEncodedStr, preview: bool = False
):
    """open document"""
    flags = sublime.ENCODED_POSITION
    if preview:
        flags |= sublime.TRANSIENT

    window.open_file(file_name, flags=flags)


def open_location(current_view: sublime.View, locations: List[PathEncodedStr]) -> None:
    """"""
    window = current_view.window()
    current_selections = list(current_view.sel())
    current_visible_region = current_view.visible_region()

    locations = sorted(locations)

    def open_location(index):
        if index >= 0:
            open_document(window, locations[index])
            return

        # else: revert to current state
        current_view.window().focus_view(current_view)
        set_selection(current_view, current_selections)
        current_view.show(current_visible_region, show_surrounds=False)

    def preview_location(index):
        open_document(window, locations[index], preview=True)

    window.show_quick_panel(
        items=locations,
        on_select=open_location,
        flags=sublime.MONOSPACE_FONT,
        on_highlight=preview_location,
        placeholder="Open location...",
    )
