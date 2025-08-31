from copy import deepcopy
from typing import List

from ....plugin_core.session import Session
from ....plugin_core.features.document.code_action import DocumentCodeActionMixins


class ClangdDocumentCodeActionMixins(DocumentCodeActionMixins):

    def show_action_panels(self, session: Session, code_actions: List[dict]):
        super().show_action_panels(
            session, [self.adapt_field(action) for action in code_actions]
        )

    @staticmethod
    def adapt_field(code_action: dict) -> dict:
        # clangd don't define code action kind
        code_action["kind"] = "refactor"
        if command := code_action.get("command"):
            if command == "clangd.applyFix":
                code_action["kind"] = "quickfix"
            # clangd define this action as command
            inner = deepcopy(code_action)
            code_action["command"] = inner
        return code_action
