from typing import Iterator
from ....plugin_core.features.document.rename import DocumentRenameMixins


class ClangdDocumentRenameMixins(DocumentRenameMixins):

    def _get_changes(self, edit: dict) -> Iterator[dict]:
        # Clangd implementation is a little different from standard
        for file_uri, changes in edit["changes"].items():
            yield {"textDocument": {"uri": file_uri}, "edits": changes}
