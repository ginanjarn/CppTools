"""session object"""

import logging
import threading
from enum import Enum
from typing import Optional, Dict, List, Any

import sublime

from .constant import LOGGING_CHANNEL
from .document import Document
from .diagnostics import DiagnosticManager, ReportSettings


MethodName = str
PathStr = str
LOGGER = logging.getLogger(LOGGING_CHANNEL)


class InitializeStatus(Enum):
    NotInitialized = 0
    Initializing = 1
    Initialized = 2


class Session:
    """Session"""

    def __init__(self) -> None:

        # Map document by view is easier to track if view is valid.
        # If we map by file name, one document my related to multiple 'View'
        # and some times the 'View' is invalid.
        self.working_documents: Dict[sublime.View, Document] = {}
        self._lock = threading.Lock()

        # Target document where result applied, e.g: completion result.
        self.action_target: Dict[MethodName, Document] = {}

        # Diagnostic manager
        self.diagnostic_manager = DiagnosticManager(ReportSettings(show_panel=False))

        # Initialize status
        self.inittialize_status: InitializeStatus = InitializeStatus.NotInitialized

    def reset(self):
        """"""
        with self._lock:
            self.working_documents.clear()
            self.action_target.clear()
            self.diagnostic_manager.reset()
            self.inittialize_status = InitializeStatus.NotInitialized

    def get_document(
        self, view: sublime.View, /, default: Any = None
    ) -> Optional[Document]:
        with self._lock:
            return self.working_documents.get(view, default)

    def add_document(self, document: Document) -> None:
        with self._lock:
            self.working_documents[document.view] = document

    def remove_document(self, view: sublime.View) -> None:
        with self._lock:
            try:
                del self.working_documents[view]
            except KeyError as err:
                LOGGER.debug("document not found %s", err)
                pass

    def get_document_by_name(
        self, file_name: PathStr, /, default: Any = None
    ) -> Optional[Document]:
        """get document by name"""

        with self._lock:
            for view, document in self.working_documents.items():
                if view.file_name() == file_name:
                    return document
            return default

    def get_documents(
        self, file_name: Optional[PathStr] = None
    ) -> List[Document]:
        """get documents.
        If file_name assigned, return documents with file_name filtered.
        """
        with self._lock:
            if not file_name:
                return [doc for _, doc in self.working_documents.items()]
            return [
                doc
                for _, doc in self.working_documents.items()
                if doc.file_name == file_name
            ]
