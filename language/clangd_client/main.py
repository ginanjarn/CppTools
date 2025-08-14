"""plugin entry point"""

import logging
import time
from functools import wraps

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
CLIENT = get_client()

_InitializeCommand.client = CLIENT
_StartServerCommand.client = CLIENT
_TerminateServerCommand.client = CLIENT
DocumentSynchronizeEventListener.client = CLIENT
DocumentSynchronizeTextChangeListener.client = CLIENT
CompletionEventListener.client = CLIENT
_DocumentSignatureHelpCommand.client = CLIENT
DocumentSignatureHelpEventListener.client = CLIENT
HoverEventListener.client = CLIENT
_DocumentFormattingCommand.client = CLIENT
_GotoDefinitionCommand.client = CLIENT
_PrepareRenameCommand.client = CLIENT
_RenameCommand.client = CLIENT
_CodeActionCommand.client = CLIENT


def setup_logger(level: int):
    """"""
    LOGGER.setLevel(level)
    fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOGGER.addHandler(sh)


def get_logging_settings():
    """get logging level defined in '*.sublime-settings'"""
    level_map = {
        "error": logging.ERROR,
        "warning": logging.WARNING,
        "info": logging.INFO,
        "verbose": logging.DEBUG,
    }
    with Settings() as settings:
        settings_level = settings.get("logging")
        return level_map.get(settings_level, logging.ERROR)


def plugin_loaded():
    """plugin entry point"""
    setup_logger(get_logging_settings())


def plugin_unloaded():
    """executed before plugin unloaded"""
    if CLIENT:
        CLIENT.terminate()


def client_must_ready(func):
    """only call function if client is ready"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not CLIENT.is_ready():
            return None
        return func(*args, **kwargs)

    return wrapper


class InitializerEventListener(sublime_plugin.EventListener):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = CLIENT

    def on_activated_async(self, view: sublime.View):
        if not is_valid_document(view):
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
