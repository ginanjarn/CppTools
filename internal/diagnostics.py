"""diagnostics object"""

import threading
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Callable

import sublime

from .constant import PACKAGE_NAME
from .panels import DiagnosticPanel

PathStr = str
"""Path in string"""


@dataclass
class DiagnosticItem:
    severity: int
    region: sublime.Region
    message: str


LineCharacter = namedtuple("LineCharacter", ["line", "character"])


def rpc_to_diagnosticitem(view: sublime.View, diagnostic: dict, /) -> DiagnosticItem:
    """"""
    start = LineCharacter(**diagnostic["range"]["start"])
    end = LineCharacter(**diagnostic["range"]["end"])
    region = sublime.Region(view.text_point(*start), view.text_point(*end))
    message = diagnostic["message"]
    if source := diagnostic.get("source"):
        message = f"{message} ({source})"

    return DiagnosticItem(diagnostic["severity"], region, message)


@dataclass
class ReportSettings:
    highlight_text: bool = True
    show_status: bool = True
    show_panel: bool = False


DefaultSettings = ReportSettings()

REGIONS_KEY = f"{PACKAGE_NAME}_DIAGNOSTIC_REGIONS"
STATUS_KEY = f"{PACKAGE_NAME}_DIAGNOSTIC_STATUS"


class DiagnosticManager:
    """"""

    def __init__(self, settings: ReportSettings = DefaultSettings) -> None:
        self.diagnostics_map: Dict[PathStr, List[dict]] = {}
        self.diagnostics_items_map: Dict[PathStr, List[DiagnosticItem]] = {}

        self.settings = settings
        self.panel = DiagnosticPanel()

        self._change_lock = threading.Lock()
        self._active_view: sublime.View = None

    def reset(self):
        # clear all regions before diagnostics_map cleared
        self._clear_all_regions()
        self._clear_all_status()
        self._active_view = None
        self.panel.destroy()
        self.diagnostics_map.clear()
        self.diagnostics_items_map.clear()

    def get(self, view: sublime.View) -> List[dict]:
        with self._change_lock:
            return self.diagnostics_map.get(view.file_name(), [])

    def set(self, view: sublime.View, diagnostics: List[dict]):
        with self._change_lock:
            self.diagnostics_map[view.file_name()] = diagnostics
            # Save DiagnostictsItems separate to diagnostic data
            # to prevent rebuild later.
            self.diagnostics_items_map[view.file_name()] = [
                rpc_to_diagnosticitem(view, d) for d in diagnostics
            ]
            self._show_report(view)

    def remove(self, view: sublime.View):
        with self._change_lock:
            try:
                del self.diagnostics_map[view.file_name()]
                del self.diagnostics_items_map[view.file_name()]
            except KeyError:
                pass
            self._show_report(view)

    def set_active_view(self, view: sublime.View):
        if view == self._active_view:
            # Ignore if view not changed
            return

        self._active_view = view
        self._show_report(view)

    def get_diagnostic_items(
        self, view: sublime.View, filter_fn: Callable[[DiagnosticItem], bool] = None
    ) -> List[DiagnosticItem]:
        try:
            diagnostic_items = self.diagnostics_items_map[view.file_name()]
        except KeyError:
            return []

        if filter_fn:
            return [d for d in diagnostic_items if filter_fn(d)]
        return diagnostic_items

    def _show_report(self, view: sublime.View):
        try:
            diagnostic_items = self.diagnostics_items_map[view.file_name()]
        except KeyError:
            return []

        if self.settings.highlight_text:
            self._highlight_regions(view, diagnostic_items)
        if self.settings.show_status:
            self._show_status(view, diagnostic_items)

        # Cancel show panel if reported diagnostics not in active view
        if view != self._active_view:
            return

        if self.settings.show_panel:
            self._show_panel(self.panel, view, diagnostic_items)

    @staticmethod
    def _highlight_regions(view: sublime.View, diagnostic_items: List[DiagnosticItem]):
        regions = [item.region for item in diagnostic_items]
        view.add_regions(
            key=REGIONS_KEY,
            regions=regions,
            scope="invalid",
            icon="dot",
            flags=sublime.DRAW_NO_FILL
            | sublime.DRAW_NO_OUTLINE
            | sublime.DRAW_SQUIGGLY_UNDERLINE,
        )

    @staticmethod
    def _clear_all_regions():
        for window in sublime.windows():
            # erase regions
            for view in [v for v in window.views()]:
                view.erase_regions(REGIONS_KEY)

    @staticmethod
    def _clear_all_status():
        for window in sublime.windows():
            # erase regions
            for view in [v for v in window.views()]:
                view.erase_status(STATUS_KEY)

    @staticmethod
    def _show_status(view: sublime.View, diagnostic_items: List[DiagnosticItem]):
        err_count = len([item for item in diagnostic_items if item.severity == 1])
        warn_count = len(diagnostic_items) - err_count
        view.set_status(STATUS_KEY, f"ERROR {err_count}, WARNING {warn_count}")

    @staticmethod
    def _show_panel(
        self,
        panel: DiagnosticPanel,
        view: sublime.View,
        diagnostic_items: List[DiagnosticItem],
    ):
        def build_line(view: sublime.View, item: DiagnosticItem):
            short_name = Path(view.file_name()).name
            row, col = view.rowcol(item.region.begin())
            return f"{short_name}:{row+1}:{col} {item.message}"

        content = "\n".join([build_line(view, item) for item in diagnostic_items])
        panel.set_content(content)
        panel.show()
