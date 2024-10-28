"""Workspace module"""

import logging
import threading
from pathlib import Path
from typing import Dict, List, Any, Optional

import sublime

from .constant import LOGGING_CHANNEL
from .document import BufferedDocument

PathStr = str
LOGGER = logging.getLogger(LOGGING_CHANNEL)


class Workspace:
    def __init__(self):
        # Map document by view is easier to track if view is valid.
        # If we map by file name, one document my related to multiple 'View'
        # and some times the 'View' is invalid.
        self.documents: Dict[sublime.View, BufferedDocument] = {}
        self._lock = threading.Lock()

    def reset(self):
        """"""
        with self._lock:
            self.documents.clear()

    def get_document(
        self, view: sublime.View, /, default: Any = None
    ) -> Optional[BufferedDocument]:
        with self._lock:
            return self.documents.get(view, default)

    def add_document(self, document: BufferedDocument):
        with self._lock:
            self.documents[document.view] = document

    def remove_document(self, view: sublime.View):
        with self._lock:
            try:
                del self.documents[view]
            except KeyError as err:
                LOGGER.debug("document not found %s", err)
                pass

    def get_document_by_name(
        self, file_name: PathStr, /, default: Any = None
    ) -> Optional[BufferedDocument]:
        """get document by name"""

        with self._lock:
            for view, document in self.documents.items():
                if view.file_name() == file_name:
                    return document
            return default

    def get_documents(
        self, file_name: Optional[PathStr] = None
    ) -> List[BufferedDocument]:
        """get documents.
        If file_name assigned, return documents with file_name filtered.
        """
        with self._lock:
            if not file_name:
                return [doc for _, doc in self.documents.items()]
            return [
                doc for _, doc in self.documents.items() if doc.file_name == file_name
            ]


def get_workspace_path(view: sublime.View, return_parent: bool = True) -> str:
    """Get workspace path for view.

    Params:
        view: View
            target
        return_parent: bool
            if True, return parent folder if view not opened in 'Window folders'

    Returns:
        workspace path or empty string
    """
    file_name = view.file_name()
    if not file_name:
        return ""

    if folders := [
        folder for folder in view.window().folders() if file_name.startswith(folder)
    ]:
        # File is opened in multiple folder
        return max(folders)

    if not return_parent:
        return ""

    return str(Path(file_name).parent)


def open_document(file_name: PathStr, preview: bool = False):
    """open document"""
    flags = sublime.ENCODED_POSITION
    if preview:
        flags |= sublime.TRANSIENT

    sublime.active_window().open_file(file_name, flags=flags)


def create_document(file_name: PathStr, text: str = ""):
    """create document"""
    path = Path(file_name)
    path.touch()
    path.write_text(text)


def rename_document(old_name: PathStr, new_name: PathStr):
    """rename document"""
    path = Path(old_name)
    path.rename(new_name)

    # Sublime Text didn't update the view target if renamed
    for window in sublime.windows():
        for view in [v for v in window.views() if v.file_name() == old_name]:
            view.retarget(new_name)


def delete_document(file_name: PathStr):
    """delete document"""
    path = Path(file_name)
    path.unlink()

    # Sublime Text didn't close deleted file
    for window in sublime.windows():
        for view in [v for v in window.views() if v.file_name() == file_name]:
            view.close()
