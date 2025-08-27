"""client"""

import logging
import shlex

from typing import Optional

from ..constant import LOGGING_CHANNEL
from ..plugin_core.transport import StandardIO
from ..plugin_core.client import BaseClient, ServerArguments
from ..plugin_core.sublime_settings import Settings

LOGGER = logging.getLogger(LOGGING_CHANNEL)


from ..plugin_core.features.initializer import InitializerMixins
from ..plugin_core.features.document.synchronizer import DocumentSynchronizerMixins

from ..plugin_core.features.document.definition import DocumentDefinitionMixins
from ..plugin_core.features.document.diagnostics import DocumentDiagnosticsMixins
from ..plugin_core.features.document.formatting import DocumentFormattingMixins
from ..plugin_core.features.document.signature_help import DocumentSignatureHelpMixins

from ..plugin_core.features.workspace.command import WorkspaceExecuteCommandMixins

from ..plugin_core.features.window.message import WindowMessageMixins

from .features.document.completion import ClangdDocumentCompletionMixins
from .features.document.hover import ClangdDocumentHoverMixins
from .features.document.rename import ClangdDocumentRenameMixins
from .features.document.code_action import ClangdDocumentCodeActionMixins
from .features.workspace.edit import ClangdWorkspaceApplyEditMixins


class ClangdClient(
    BaseClient,
    InitializerMixins,
    DocumentSynchronizerMixins,
    DocumentDefinitionMixins,
    DocumentDiagnosticsMixins,
    DocumentFormattingMixins,
    ClangdDocumentHoverMixins,
    DocumentSignatureHelpMixins,
    WorkspaceExecuteCommandMixins,
    WindowMessageMixins,
    ClangdDocumentCompletionMixins,
    ClangdDocumentRenameMixins,
    ClangdDocumentCodeActionMixins,
    ClangdWorkspaceApplyEditMixins,
):
    """Clangd Client"""


def get_client() -> ClangdClient:
    """"""
    log = "error" if LOGGER.level in {logging.NOTSET, logging.ERROR} else "verbose"
    command = shlex.split(f"clangd --log={log} --offset-encoding=utf-8")
    return ClangdClient(ServerArguments(command, None), StandardIO)


def get_envs_settings() -> Optional[dict]:
    """get environments defined in '*.sublime-settings'"""

    with Settings() as settings:
        if envs := settings.get("envs"):
            return envs
        return None
