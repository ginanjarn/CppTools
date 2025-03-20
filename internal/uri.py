"""URI converter"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, unquote_plus
from urllib.request import url2pathname

URI = str
"""Uniform Resource Identifier.

See 'RFC 3986' specification.
"""

DocumentURI = URI
"""URI with scheme='file"""


@lru_cache(128)
def path_to_uri(path: Path) -> DocumentURI:
    """convert path to uri"""
    return Path(path).as_uri()


@lru_cache(128)
def uri_to_path(uri: DocumentURI) -> str:
    """convert uri to path"""
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("url scheme must be 'file'")

    return url2pathname(unquote_plus(parsed.path))
