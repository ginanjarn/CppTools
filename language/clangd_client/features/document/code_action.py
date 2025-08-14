from collections import namedtuple
from functools import wraps
from typing import List

from ....plugin_core.session import Session
from ....plugin_core.features.document.code_action import DocumentCodeActionMixins
from ....plugin_core.features.workspace.edit import WorkspaceEdit


LineCharacter = namedtuple("LineCharacter", ["line", "character"])


def must_initialized(func):
    """exec if initialized"""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.session.is_initialized():
            return None
        return func(self, *args, **kwargs)

    return wrapper


class ClangdDocumentCodeActionMixins(DocumentCodeActionMixins):

    def show_action_panels(self, session: Session, code_actions: List[dict]):
        super().show_action_panels(
            session, [{**c, **{"kind": ""}} for c in code_actions]
        )

    def _handle_selected_action(self, session: Session, action: dict) -> None:
        if edit := action.get("edit"):
            WorkspaceEdit(session).apply_changes(edit)
        if _ := action.get("command"):
            self.workspace_executecommand(action)
