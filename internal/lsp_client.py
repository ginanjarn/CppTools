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
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Union, List

from . import errors
from .constant import LOGGING_CHANNEL

LOGGER = logging.getLogger(LOGGING_CHANNEL)


class Handler(ABC):
    """Base handler"""

    @abstractmethod
    def handle(self, method: str, params: dict) -> Optional[dict]:
        """handle message"""


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
    def run(self, env: Optional[dict] = None) -> None:
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


class StandardIO(Transport):
    """StandardIO Transport implementation"""

    def __init__(self, command: List[str], cwd: Optional[Path] = None):
        self.command = command
        self.cwd = cwd

        self._process: subprocess.Popen = None
        self._run_event = threading.Event()

        # make execution next to '(self._run_event).wait()' blocked
        self._run_event.clear()

    def is_running(self):
        return bool(self._process) and (self._process.poll() is None)

    def run(self, env: Optional[dict] = None):
        print("execute '%s'" % shlex.join(self.command))

        self._process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or None,
            cwd=self.cwd or None,
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
            print(prefix, bline.rstrip().decode())

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
    """RequestManager manage method mapped to request_id."""

    def __init__(self):
        self.methods_map = {}
        self.canceled_requests = set()
        self.request_count = 0

        self._lock = threading.Lock()

    def add_method(self, method: str) -> int:
        """add request method to request_map

        Return:
            request_count: int
        """
        with self._lock:
            self.request_count += 1
            self.methods_map[self.request_count] = method

            return self.request_count

    def get_method(self, request_id: int) -> str:
        """get method paired with request_id

        Return:
            method: str
        Raises:
            KeyError if request_id not found
            Canceled if request canceled
        """

        with self._lock:
            if request_id in self.canceled_requests:
                self.canceled_requests.remove(request_id)
                raise Canceled(request_id)

            # pop() is simpler than get() and del
            return self.methods_map.pop(request_id)

    def get_request_id(self, method: str) -> Optional[int]:
        """get request_id paired with method

        Return:
            request_id: Optional[int]
        """

        with self._lock:
            for req_id, meth in self.methods_map.items():
                if meth == method:
                    return req_id

            return None

    def mark_canceled(self, request_id: int):
        """mark request as canceled"""

        with self._lock:
            try:
                del self.methods_map[request_id]
            except KeyError:
                pass
            else:
                # mark canceled if 'request_id' in 'request_map'
                self.canceled_requests.add(request_id)


class Client:
    def __init__(self, transport: Transport, handler: Handler):
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
        def listen_message() -> RPCMessage:
            if not self.transport:
                raise EOFError("transport is closed")

            content = self.transport.read()
            try:
                message = RPCMessage.load(content)
            except json.JSONDecodeError as err:
                LOGGER.exception("content: '%s'", content)
                raise err

            return message

        while True:
            try:
                message = listen_message()

            except EOFError:
                # if stdout closed
                break

            except Exception as err:
                LOGGER.exception(err, exc_info=True)
                self.terminate_server()
                break

            try:
                self.handle_message(message)
            except Exception:
                LOGGER.exception("error handle message: %s", message, exc_info=True)

    def listen(self):
        thread = threading.Thread(target=self._listen, daemon=True)
        thread.start()

    def is_server_running(self):
        return bool(self.transport) and self.transport.is_running()

    def run_server(self, env: Optional[dict] = None):
        self.transport.run(env)

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
            error = errors.transform_error(err)

        self.send_response(message["id"], result, error)

    def handle_notification(self, message: RPCMessage):
        try:
            self.handler.handle(message["method"], message["params"])
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def handle_response(self, message: RPCMessage):
        try:
            method = self.request_manager.get_method(message["id"])
        except Canceled:
            # ignore canceled response
            return

        try:
            self.handler.handle(method, message)
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def send_request(self, method: str, params: dict):
        # cancel previous request with same method
        if prev_request := self.request_manager.get_request_id(method):
            self.request_manager.mark_canceled(prev_request)
            self.send_notification("$/cancelRequest", {"id": prev_request})

        req_id = self.request_manager.add_method(method)
        self.send_message(RPCMessage.request(req_id, method, params))

    def send_notification(self, method: str, params: dict):
        self.send_message(RPCMessage.notification(method, params))

    def send_response(
        self, id: int, result: Optional[dict] = None, error: Optional[dict] = None
    ):
        self.send_message(RPCMessage.response(id, result, error))
