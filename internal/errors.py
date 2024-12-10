"""rpc errors"""

from typing import Union, Dict, Any, Optional


class ContentIncomplete(ValueError):
    """expected size less than defined"""


class RPCException(Exception):
    """base rpc exception"""

    code = 0
    message = ""


class ParseError(RPCException):
    """message not comply to jsonrpc 2.0 specification"""

    code = -32700
    message = "parse error"


class InvalidRequest(RPCException):
    """invalid request"""

    code = -32600
    message = "invalid request"


class MethodNotFound(RPCException):
    """method not found"""

    code = -32601
    message = "method not found"


class InvalidParams(RPCException):
    """invalid params"""

    code = -32602
    message = "invalid params"


class InternalError(RPCException):
    """internal error"""

    code = -32603
    message = "internal error"


class ServerNotInitialized(RPCException):
    """workspace not initialize"""

    code = -32002
    message = "server not initialized"


class InvalidResource(InternalError):
    """invalid resource"""

    message = "invalid resource"


class RequestCancelled(RPCException):
    """request canceled"""

    message = "request canceled"
    code = -32800


class ContentModified(RPCException):
    """content modified"""

    message = "content modified"
    code = -32801


class FeatureDisabled(InternalError):
    """feature disabled"""

    message = "feature disabled"


def transform_error(
    error: Union[RPCException, Exception, None]
) -> Optional[Dict[str, Any]]:
    """transform exception to rpc error"""

    if not error:
        return None

    code, message = None, None
    try:
        code = error.code
        message = str(error) or error.message

    except AttributeError:
        # 'err.code' and 'err.message' may be not defined
        code = InternalError.code
        message = repr(error)

    return {"code": code, "message": message}
