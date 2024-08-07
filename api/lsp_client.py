"""client server api"""

import json
import logging
import os
import re
import threading
import subprocess
import shlex
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from typing import Optional, Union

from . import errors

URI = str
_PathLikeStr = str

LOGGER = logging.getLogger(__name__)
# LOGGER.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(levelname)s %(filename)s:%(lineno)d  %(message)s")
sh = logging.StreamHandler()
sh.setFormatter(fmt)
LOGGER.addHandler(sh)


def path_to_uri(path: _PathLikeStr) -> URI:
    """convert path to uri"""
    return Path(path).as_uri()


def uri_to_path(uri: URI) -> _PathLikeStr:
    """convert uri to path"""
    return url2pathname(unquote(urlparse(uri).path))


class BaseHandler:
    """Base handler"""

    @staticmethod
    def flatten_method(method: str) -> str:
        return f"handle_{method}".replace("/", "_").replace(".", "_").lower()

    def handle(self, method: str, params: dict):
        try:
            func = getattr(self, self.flatten_method(method))
        except AttributeError as err:
            raise errors.MethodNotFound(f"method not found {method!r}") from err

        else:
            return func(params)


class RPCMessage(dict):
    """rpc message"""

    @classmethod
    def request(cls, id, method, params):
        return cls({"id": id, "method": method, "params": params})

    @classmethod
    def notification(cls, method, params):
        return cls({"method": method, "params": params})

    @classmethod
    def response(cls, id, result, error):
        if error:
            return cls({"id": id, "error": error})
        return cls(
            {
                "id": id,
                "result": result,
            }
        )

    def dumps(self, *, as_bytes: bool = False):
        """dump rpc message to json text"""

        self["jsonrpc"] = "2.0"
        dumped = json.dumps(self)
        if as_bytes:
            return dumped.encode()
        return dumped

    @classmethod
    def load(cls, data: Union[str, bytes]):
        """load rpc message from json text"""

        loaded = json.loads(data)
        if loaded.get("jsonrpc") != "2.0":
            raise ValueError("Not a JSON-RPC 2.0")
        return cls(loaded)

    @staticmethod
    def exception_to_message(exception: Exception) -> dict:
        return {"message": str(exception), "code": 1}


if os.name == "nt":
    # if on Windows, hide process window
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.SW_HIDE | subprocess.STARTF_USESHOWWINDOW
else:
    STARTUPINFO = None


class ServerNotRunning(Exception):
    """server not running"""


class HeaderError(ValueError):
    """header error"""


def wrap_rpc(content: bytes) -> bytes:
    """wrap content as rpc body"""
    header = b"Content-Length: %d\r\n" % len(content)
    return b"%s\r\n%s" % (header, content)


@lru_cache(maxsize=512)
def get_content_length(header: bytes) -> int:
    for line in header.splitlines():
        if match := re.match(rb"Content-Length: (\d+)", line):
            return int(match.group(1))

    raise HeaderError("unable get 'Content-Length'")


class Transport(ABC):
    """transport abstraction"""

    @abstractmethod
    def is_running(self) -> bool:
        """check server is running"""

    @abstractmethod
    def run(self) -> None:
        """run server"""

    @abstractmethod
    def terminate(self) -> None:
        """terminate server"""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """write data to server"""

    @abstractmethod
    def read(self) -> bytes:
        """read data from server"""


@dataclass
class PopenOptions:
    env: dict = None
    cwd: str = None


class StandardIO(Transport):
    """StandardIO Transport implementation"""

    def __init__(self, command: list):
        self.command = command

        self._process: subprocess.Popen = None
        self._run_event = threading.Event()

        # make execution next to '(self._run_event).wait()' blocked
        self._run_event.clear()

    def is_running(self):
        return bool(self._process) and (self._process.poll() is None)

    def run(self, options: PopenOptions = None):
        options = options or PopenOptions()
        print("execute '%s'" % shlex.join(self.command))

        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=options.env,
            cwd=options.cwd,
            shell=True,
            bufsize=0,
            startupinfo=STARTUPINFO,
        )

        # ready to call 'Popen()' object
        self._run_event.set()

        thread = threading.Thread(target=self.listen_stderr)
        thread.start()

    @property
    def stdin(self):
        if self._process:
            return self._process.stdin
        return BytesIO()

    @property
    def stdout(self):
        if self.is_running():
            return self._process.stdout
        return BytesIO()

    @property
    def stderr(self):
        if self.is_running():
            return self._process.stderr
        return BytesIO()

    def listen_stderr(self):
        self._run_event.wait()

        prefix = f"[{self.command[0]}]"
        while bline := self.stderr.readline():
            print(prefix, bline.strip().decode())

        # else:
        return

    def terminate(self):
        """terminate process"""

        # reset state
        self._run_event.clear()

        if self._process:
            self._process.kill()
            # wait until terminated
            self._process.wait()
            # set to None to release 'Popen()' object from memory
            self._process = None

    def write(self, data: bytes):
        self._run_event.wait()

        prepared_data = wrap_rpc(data)
        self.stdin.write(prepared_data)
        self.stdin.flush()

    def read(self):
        self._run_event.wait()

        # get header
        temp_header = BytesIO()
        n_header = 0
        while line := self.stdout.readline():
            # header and content separated by newline with \r\n
            if line == b"\r\n":
                break

            n = temp_header.write(line)
            n_header += n

        # no header received
        if not n_header:
            raise EOFError("stdout closed")

        try:
            content_length = get_content_length(temp_header.getvalue())

        except HeaderError as err:
            LOGGER.exception("header: %s", temp_header.getvalue())
            raise err

        temp_content = BytesIO()
        n_content = 0
        # Read until defined content_length received.
        while n_content < content_length:
            unread_length = content_length - n_content
            if chunk := self.stdout.read(unread_length):
                n = temp_content.write(chunk)
                n_content += n
            else:
                raise EOFError("stdout closed")

        content = temp_content.getvalue()
        return content


class Canceled(Exception):
    """Request Canceled"""


class RequestManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.request_map = {}
        self.canceled_request = set()
        self.request_count = 0

    def add_request(self, method: str):
        with self.lock:
            self.request_count += 1
            self.request_map[self.request_count] = method

    def get_request(self, request_id: int) -> str:
        """
        Return:
            method: str
        Raises:
            KeyError if request_id not found
            Canceled if request canceled
        """

        with self.lock:
            if request_id in self.canceled_request:
                # Clean request map
                self.canceled_request.remove(request_id)
                del self.request_map[request_id]

                raise Canceled(request_id)

            return self.request_map.pop(request_id)

    def get_request_id(self, method: str) -> Optional[int]:
        """return None if not found"""
        with self.lock:
            for req_id, meth in self.request_map.items():
                if meth == method:
                    return req_id

            return None

    def cancel_requests(self, *request_id: int):
        with self.lock:
            self.canceled_request.update(request_id)


class Client:
    def __init__(self, transport: Transport, handler: BaseHandler):
        self._transport = weakref.ref(transport, lambda x: self._reset_state())
        self._handler = weakref.ref(handler, lambda x: self._reset_state())

        self.request_manager = RequestManager()

    @property
    def transport(self):
        return self._transport()

    @property
    def handler(self):
        return self._handler()

    def _reset_state(self):
        self.request_manager = RequestManager()

    def send_message(self, message: RPCMessage):
        content = message.dumps(as_bytes=True)
        self.transport.write(content)

    def _listen(self):
        def listen_func():
            if not self.transport:
                return

            content = self.transport.read()

            try:
                message = RPCMessage.load(content)
            except json.JSONDecodeError as err:
                LOGGER.exception("content: %s", content)
                raise err

            try:
                self.handle_message(message)
            except Exception as err:
                LOGGER.exception("message: %s", message)
                raise err

        while True:
            try:
                listen_func()
            except EOFError:
                break

            except Exception as err:
                LOGGER.exception(err)
                self.terminate_server()
                break

    def listen(self):
        thread = threading.Thread(target=self._listen, daemon=True)
        thread.start()

    def server_running(self):
        return bool(self.transport) and self.transport.is_running()

    def run_server(self, options: PopenOptions = None):
        self.transport.run(options)

    def terminate_server(self):
        if self.transport:
            self.transport.terminate()

        self._reset_state()

    def handle_message(self, message: RPCMessage):
        id = message.get("id")

        # handle server command
        method = message.get("method")
        if method:
            if id is None:
                self.handle_notification(message)
            else:
                self.handle_request(message)

        # handle server response
        elif id is not None:
            self.handle_response(message)

        else:
            LOGGER.error("invalid message: %s", message)

    def handle_request(self, message: RPCMessage):
        result = None
        error = None
        try:
            result = self.handler.handle(message["method"], message["params"])
        except Exception as err:
            LOGGER.exception(err, exc_info=True)
            error = RPCMessage.exception_to_message(err)

        self.send_response(message["id"], result, error)

    def handle_notification(self, message: RPCMessage):
        try:
            self.handler.handle(message["method"], message["params"])
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def handle_response(self, message: RPCMessage):
        try:
            method = self.request_manager.get_request(message["id"])
        except (Canceled, KeyError):
            # handle exception here
            return

        try:
            self.handler.handle(method, message)
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def send_request(self, method: str, params: dict):
        prev_request = self.request_manager.get_request_id(method)
        if prev_request is not None:
            # cancel previous request
            self.request_manager.cancel_requests(prev_request)
            self.send_notification("$/cancelRequest", {"id": prev_request})

        self.request_manager.add_request(method)
        req_id = self.request_manager.request_count
        self.send_message(RPCMessage.request(req_id, method, params))

    def send_notification(self, method: str, params: dict):
        self.send_message(RPCMessage.notification(method, params))

    def send_response(
        self, id: int, result: Optional[dict] = None, error: Optional[dict] = None
    ):
        self.send_message(RPCMessage.response(id, result, error))
