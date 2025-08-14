from typing import Iterator
from ....plugin_core.features.workspace.edit import WorkspaceApplyEditMixins


class ClangdWorkspaceApplyEditMixins(WorkspaceApplyEditMixins):

    def _get_changes(self, edit: dict) -> Iterator[dict]:
        # Clangd implementation is a little different from standard
        for file_uri, changes in edit["changes"].items():
            yield {"textDocument": {"uri": file_uri}, "edits": changes}
