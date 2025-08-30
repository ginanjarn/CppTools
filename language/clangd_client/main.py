"""plugin entry point"""

import logging
import time
from collections import defaultdict

import sublime
import sublime_plugin

from ..constant import LOGGING_CHANNEL, COMMAND_PREFIX
from ..plugin_core.document import is_valid_document
from ..plugin_core.sublime_settings import Settings
from ..plugin_core.features.document_helper import _ApplyTextChangesCommand
from .client import get_client, get_envs_settings

from ..plugin_core.features.server_manager import (
    _StartServerCommand,
    _TerminateServerCommand,
)
from ..plugin_core.features.initializer import _InitializeCommand
from ..plugin_core.features.document.synchronizer import (
    DocumentSynchronizeEventListener,
    DocumentSynchronizeTextChangeListener,
)
from ..plugin_core.features.document.completion import CompletionEventListener
from ..plugin_core.features.document.signature_help import (
    _DocumentSignatureHelpCommand,
    DocumentSignatureHelpEventListener,
)
from ..plugin_core.features.document.hover import HoverEventListener
from ..plugin_core.features.document.formatting import _DocumentFormattingCommand
from ..plugin_core.features.document.definition import _GotoDefinitionCommand
from ..plugin_core.features.document.rename import (
    _PrepareRenameCommand,
    _RenameCommand,
)
from ..plugin_core.features.document.code_action import _CodeActionCommand

LOGGER = logging.getLogger(LOGGING_CHANNEL)


class InitializerEventListener(sublime_plugin.EventListener):
    client = None

    def on_activated_async(self, view: sublime.View):
        if not is_valid_document(view):
            return
        if not self.client:
            return
        if self.client.is_ready():
            return

        if not self.client.server.is_running():
            view.run_command(
                f"{COMMAND_PREFIX}_start_server",
                {"envs": get_envs_settings()},
            )

        # initialize
        for _ in range(25):
            if self.client.server.is_running():
                self.client.initialize(view)
                break
            # pause next iteration
            time.sleep(0.5)  # seconds
        else:
            # server not running
            return

        # open active document
        for _ in range(25):
            if self.client.is_ready():
                self.client.textdocument_didopen(view)
                break
            # pause next iteration
            time.sleep(0.5)  # seconds


class CppToolsInitializeCommand(_InitializeCommand):
    """CppToolsInitializeCommand"""


class CppToolsDocumentSignatureHelpCommand(_DocumentSignatureHelpCommand):
    """CppToolsDocumentSignatureHelpCommand"""


class CppToolsDocumentFormattingCommand(_DocumentFormattingCommand):
    """CppToolsDocumentFormattingCommand"""


class CppToolsGotoDefinitionCommand(_GotoDefinitionCommand):
    """CppToolsGotoDefinitionCommand"""


class CppToolsPrepareRenameCommand(_PrepareRenameCommand):
    """CppToolsPrepareRenameCommand"""


class CppToolsRenameCommand(_RenameCommand):
    """CppToolsRenameCommand"""


class CppToolsCodeActionCommand(_CodeActionCommand):
    """CppToolsCodeActionCommand"""


class CppToolsApplyTextChangesCommand(_ApplyTextChangesCommand):
    """CppToolsApplyTextChangesCommand"""


class CppToolsStartServerCommand(_StartServerCommand):
    """CppToolsStartServerCommand"""


class CppToolsTerminateServerCommand(_TerminateServerCommand):
    """CppToolsTerminateServerCommand"""


def setup_logger():
    """"""
    level = _get_logging_settings()
    LOGGER.setLevel(level)
    fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)


def _get_logging_settings():
    """get logging level defined in '*.sublime-settings'"""
    name_to_loglevel_map = defaultdict(
        lambda: logging.NOTSET,
        {
            "error": logging.ERROR,
            "warning": logging.WARNING,
            "info": logging.INFO,
            "debug": logging.DEBUG,
        },
    )
    with Settings() as settings:
        named_level = settings.get("logging_level", "")
    return name_to_loglevel_map[str(named_level).lower()]


# CLIENT SINGLETON
CLIENT = None


def setup_client():
    """"""
    global CLIENT
    CLIENT = get_client()

    for command_or_event in {
        InitializerEventListener,
        # ------------------------------
        _InitializeCommand,
        _StartServerCommand,
        _TerminateServerCommand,
        DocumentSynchronizeEventListener,
        DocumentSynchronizeTextChangeListener,
        CompletionEventListener,
        _DocumentSignatureHelpCommand,
        DocumentSignatureHelpEventListener,
        HoverEventListener,
        _DocumentFormattingCommand,
        _GotoDefinitionCommand,
        _PrepareRenameCommand,
        _RenameCommand,
        _CodeActionCommand,
    }:
        command_or_event.client = CLIENT


def plugin_loaded():
    """plugin entry point"""
    setup_logger()
    setup_client()


def plugin_unloaded():
    """executed before plugin unloaded"""
    if CLIENT:
        CLIENT.terminate()
