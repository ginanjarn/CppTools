from ....plugin_core.message import Response
from ....plugin_core.session import Session
from ....plugin_core.features.document.hover import DocumentHoverMixins


class ClangdDocumentHoverMixins(DocumentHoverMixins):

    hover_location = (0, 0)

    def textdocument_hover(self, view, row, col):
        self.trigger_location = (row, col)
        super().textdocument_hover(view, row, col)

    def handle_textdocument_hover(self, session: Session, response: Response):
        try:
            super().handle_textdocument_hover(session, response)
        except KeyError as err:
            # Sometime clangd return hover result without 'range'
            if err.args[0] != "range":
                raise err
            # show popup in trigger location
            message = response.result["contents"]["value"]
            row, col = self.trigger_location
            self.hover_target.show_popup(message, row, col)
