import sublime

from ....plugin_core.features.document.completion import (
    DocumentCompletionMixins,
    COMPLETION_KIND_MAP,
)


class ClangdDocumentCompletionMixins(DocumentCompletionMixins):

    @staticmethod
    def _build_completion(completion_item: dict) -> sublime.CompletionItem:
        # clangd defined 'label' starts with '<space>' or '�'
        label = completion_item["label"][1:]

        try:
            insert_text = completion_item["textEdit"]["newText"]
        except KeyError:
            insert_text = completion_item["insertText"]

        # sublime text has complete the header bracket '<> or ""'
        # remove it from clangd result
        if completion_item["kind"] in (17, 19):
            closing_include = '">'
            label = label.rstrip(closing_include)
            insert_text = insert_text.rstrip(closing_include)

        kind = COMPLETION_KIND_MAP[completion_item["kind"]]
        annotation = completion_item.get("detail", "")

        return sublime.CompletionItem.snippet_completion(
            trigger=label,
            snippet=insert_text,
            annotation=annotation,
            kind=kind,
        )
