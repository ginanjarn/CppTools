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
from typing import Optional, Union, List, Dict, Set

from . import errors
from .constant import LOGGING_CHANNEL

LOGGER = logging.getLogger(LOGGING_CHANNEL)


class MethodName(str):
    """Method name"""


class RPCMessage(dict):
    """rpc message"""

    @classmethod
    def request(cls, id: int, method: MethodName, params: dict):
        return cls({"id": id, "method": method, "params": params})

    @classmethod
    def notification(cls, method: MethodName, params: dict):
        return cls({"method": method, "params": params})

    @classmethod
    def response(
        cls, id: int, result: Optional[dict] = None, error: Optional[dict] = None
    ):
        if error:
            return cls({"id": id, "error": error})
        return cls({"id": id, "result": result})

    def dumps(self, *, as_bytes: bool = False) -> Union[str, bytes]:
        """dump rpc message to json text"""

        self["jsonrpc"] = "2.0"
        dumped = json.dumps(self)
        if as_bytes:
            return dumped.encode()
        return dumped

    @classmethod
    def load(cls, data: Union[str, bytes]) -> "RPCMessage":
        """load rpc message from json text"""

        loaded = json.loads(data)
        if loaded.get("jsonrpc") != "2.0":
            raise ValueError("JSON-RPC v2.0 is required")
        return cls(loaded)


class Handler(ABC):
    """Base handler"""

    @abstractmethod
    def handle(self, method: MethodName, params: dict) -> Optional[RPCMessage]:
        """handle message"""


class ServerNotRunning(Exception):
    """server not running"""


class HeaderError(ValueError):
    """header error"""


def wrap_rpc(content: bytes) -> bytes:
    """wrap content as rpc body"""
    header = b"Content-Length: %d\r\n" % len(content)
    separator = b"\r\n"
    return b"%s%s%s" % (header, separator, content)


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


if os.name == "nt":
    STARTUPINFO = subprocess.STARTUPINFO()
    # Hide created process window
    STARTUPINFO.dwFlags |= subprocess.SW_HIDE | subprocess.STARTF_USESHOWWINDOW
else:
    STARTUPINFO = None


class StandardIO(Transport):
    """StandardIO Transport implementation"""

    def __init__(self, command: List[str], cwd: Optional[Path] = None):
        self.command = command
        self.cwd = cwd

        self._process: subprocess.Popen = None
        self._run_proces_event = threading.Event()

    def is_running(self):
        try:
            return self._process.poll() is None
        except AttributeError:
            return False

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
        self._run_proces_event.set()

        thread = threading.Thread(target=self._listen_stderr_task)
        thread.start()

    @property
    def stdin(self):
        try:
            return self._process.stdin
        except AttributeError:
            return BytesIO()

    @property
    def stdout(self):
        try:
            return self._process.stdout
        except AttributeError:
            return BytesIO()

    @property
    def stderr(self):
        try:
            return self._process.stderr
        except AttributeError:
            return BytesIO()

    def _listen_stderr_task(self):
        self._run_proces_event.wait()

        prefix = f"[{self.command[0]}]"
        while bline := self.stderr.readline():
            print(prefix, bline.rstrip().decode())

        # else:
        return

    def terminate(self):
        """terminate process"""

        # reset state
        self._run_proces_event.clear()

        if self._process:
            self._process.kill()
            # wait until terminated
            self._process.wait()
            # set to None to release 'Popen()' object from memory
            self._process = None

    def write(self, data: bytes):
        self._run_proces_event.wait()

        prepared_data = wrap_rpc(data)
        self.stdin.write(prepared_data)
        self.stdin.flush()

    def read(self):
        self._run_proces_event.wait()

        # get header
        header_buffer = BytesIO()
        header_separator = b"\r\n"
        while line := self.stdout.readline():
            # header and content separated by newline with \r\n
            if line == header_separator:
                break
            header_buffer.write(line)

        header = header_buffer.getvalue()

        # no header received
        if not header:
            raise EOFError("stdout closed")

        try:
            defined_length = get_content_length(header)
        except HeaderError as err:
            LOGGER.exception("header: %s", header_buffer.getvalue())
            raise err

        content_buffer = BytesIO()
        received_length = 0
        # Read until defined content_length received.
        while (missing := defined_length - received_length) and missing > 0:
            if chunk := self.stdout.read(missing):
                received_length += content_buffer.write(chunk)
            else:
                raise EOFError("stdout closed")

        return content_buffer.getvalue()


class Canceled(Exception):
    """Request Canceled"""


class RequestManager:
    """RequestManager manage method mapped to request_id."""

    def __init__(self):
        self.methods_map: Dict[int, MethodName] = {}
        self.canceled_requests: Set[int] = set()
        self.request_count = 0

        self._lock = threading.Lock()

    def add(self, method: MethodName) -> int:
        """add request method to request_map

        Return:
            request_count: int
        """
        with self._lock:
            self.request_count += 1
            self.methods_map[self.request_count] = method

            return self.request_count

    def pop(self, request_id: int) -> MethodName:
        """pop method paired with request_id

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

    def _get_previous_request(self, method: MethodName) -> Optional[int]:
        for req_id, meth in self.methods_map.items():
            if meth == method:
                return req_id

        return None

    def cancel(self, method: MethodName) -> Optional[int]:
        """cancel request

        Return:
            request_id: Optional[int]
        """

        with self._lock:
            request_id = self._get_previous_request(method)
            if request_id is None:
                return None

            del self.methods_map[request_id]
            self.canceled_requests.add(request_id)
            return request_id


class Client:
    def __init__(self, transport: Transport, handler: Handler):
        self._transport = weakref.ref(transport, lambda x: self._reset_state())
        self._handler = weakref.ref(handler, lambda x: self._reset_state())

        self._request_manager = RequestManager()

    @property
    def transport(self) -> Transport:
        return self._transport()

    @property
    def handler(self) -> Handler:
        return self._handler()

    def _reset_state(self) -> None:
        self._request_manager = RequestManager()

    def send_message(self, message: RPCMessage) -> None:
        content = message.dumps(as_bytes=True)
        self.transport.write(content)

    def _listen_task(self) -> None:
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

    def listen(self) -> None:
        thread = threading.Thread(target=self._listen_task, daemon=True)
        thread.start()

    def is_server_running(self) -> bool:
        try:
            return self.transport.is_running()
        except AttributeError:
            return False

    def run_server(self, env: Optional[dict] = None) -> None:
        self.transport.run(env)

    def terminate_server(self) -> None:
        try:
            self.transport.terminate()
        except AttributeError:
            pass

        self._reset_state()

    def handle_message(self, message: RPCMessage) -> None:
        id = message.get("id")

        # handle server command
        method = message.get("method")
        if method:
            if id is None:
                self._handle_notification(message)
            else:
                self._handle_request(message)

        # handle server response
        elif id is not None:
            self._handle_response(message)

        else:
            LOGGER.error("invalid message: %s", message)

    def _handle_request(self, message: RPCMessage) -> None:
        result = None
        error = None
        try:
            result = self.handler.handle(message["method"], message["params"])
        except Exception as err:
            LOGGER.exception(err, exc_info=True)
            error = errors.transform_error(err)

        self.send_response(message["id"], result, error)

    def _handle_notification(self, message: RPCMessage) -> None:
        try:
            self.handler.handle(message["method"], message["params"])
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def _handle_response(self, message: RPCMessage) -> None:
        try:
            method = self._request_manager.pop(message["id"])
        except Canceled:
            # ignore canceled response
            return

        try:
            self.handler.handle(method, message)
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def send_request(self, method: MethodName, params: dict) -> None:
        # cancel previous request with same method
        if prev_request := self._request_manager.cancel(method):
            self.send_notification("$/cancelRequest", {"id": prev_request})

        req_id = self._request_manager.add(method)
        self.send_message(RPCMessage.request(req_id, method, params))

    def send_notification(self, method: MethodName, params: dict) -> None:
        self.send_message(RPCMessage.notification(method, params))

    def send_response(
        self, id: int, result: Optional[dict] = None, error: Optional[dict] = None
    ) -> None:
        self.send_message(RPCMessage.response(id, result, error))
