"""client server api"""

import json
import logging
import os
import re
import threading
import subprocess
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Optional, Union, List, Dict, Set

from . import errors
from .constant import LOGGING_CHANNEL

LOGGER = logging.getLogger(LOGGING_CHANNEL)


class MethodName(str):
    """Method name"""


@dataclass
class Message:
    """JSON-RPC Message interface"""


@dataclass
class Notification(Message):
    method: MethodName
    params: Union[dict, list]


@dataclass
class Request(Message):
    id: int
    method: MethodName
    params: Union[dict, list]


@dataclass
class Response(Message):
    id: int
    result: Optional[Union[dict, list]] = None
    error: Optional[dict] = None


def loads(json_str: Union[str, bytes]) -> Message:
    """loads json-rpc message"""

    dct = json.loads(json_str)
    try:
        if (jsonrpc_version := dct.pop("jsonrpc")) and jsonrpc_version != "2.0":
            raise ValueError("invalid jsonrpc version")
    except KeyError as err:
        raise ValueError("JSON-RPC 2.0 is required") from err

    if dct.get("method"):
        id = dct.get("id")
        if id is not None:
            return Request(**dct)
        return Notification(**dct)
    return Response(**dct)


def dumps(message: Message, as_bytes: bool = False) -> Union[str, bytes]:
    """dumps json-rpc message"""

    dct = asdict(message)
    dct["jsonrpc"] = "2.0"

    if isinstance(message, Response):
        if message.error is None:
            del dct["error"]
        else:
            del dct["result"]

    json_str = json.dumps(dct)
    if as_bytes:
        return json_str.encode()
    return json_str


class Handler(ABC):
    """Base handler"""

    @abstractmethod
    def handle(
        self, method: MethodName, params: Union[Message, dict]
    ) -> Optional[Response]:
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
    """Transport abstraction"""

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write data to server"""

    @abstractmethod
    def read(self) -> bytes:
        """Read data from server"""


if os.name == "nt":
    STARTUPINFO = subprocess.STARTUPINFO()
    # Hide created process window
    STARTUPINFO.dwFlags |= subprocess.SW_HIDE | subprocess.STARTF_USESHOWWINDOW
else:
    STARTUPINFO = None


class ServerProcess:
    """Server Process"""

    def __init__(self, command: List[str], cwd: Optional[Path] = None):
        if not isinstance(command, list):
            raise ValueError("command value must list of str")

        self.command = command
        self.cwd = cwd

        self.process: subprocess.Popen = None
        self._run_event = threading.Event()

        # Prevent run process until termination done
        self._terminate_event = threading.Event()
        self._terminate_event.set()

    def is_running(self) -> bool:
        """If process is running"""
        try:
            return self.process.poll() is None
        except AttributeError:
            return False

    def wait_process_running(self) -> None:
        """Wait process running"""
        self._run_event.wait()

    def run(self, env: Optional[dict] = None) -> None:
        """Run process"""

        # Wait if in termination process
        self._terminate_event.wait()

        print("execute '%s'" % shlex.join(self.command))

        self.process = subprocess.Popen(
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

        # Ready to call 'Popen()' object
        self._run_event.set()

        thread = threading.Thread(target=self._listen_stderr_task)
        thread.start()

    @property
    def stdin(self):
        return self.process.stdin

    @property
    def stdout(self):
        return self.process.stdout

    @property
    def stderr(self):
        return self.process.stderr

    def _listen_stderr_task(self):
        prefix = f"[{self.command[0]}]"
        while bline := self.stderr.readline():
            print(prefix, bline.rstrip().decode())

        # Stderr return empty character, process is terminated
        self._terminate_event.set()

    def terminate(self) -> None:
        """Terminate process"""

        self._terminate_event.clear()
        self._run_event.clear()

        if not self.process:
            return

        self.process.kill()
        return_code = self.process.wait()
        print("process terminated with exit code", return_code)
        # Set to None to release 'Popen()' object from memory
        self.process = None


class StandardIO(Transport):
    """StandardIO Transport implementation"""

    def __init__(self, server: ServerProcess) -> None:
        self.server = server

    def write(self, data: bytes):
        self.server.wait_process_running()

        prepared_data = wrap_rpc(data)
        self.server.stdin.write(prepared_data)
        self.server.stdin.flush()

    def read(self):
        self.server.wait_process_running()

        # get header
        header_buffer = BytesIO()
        header_separator = b"\r\n"
        while line := self.server.stdout.readline():
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
            if chunk := self.server.stdout.read(missing):
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

    def cancel_all(self):
        """cancel all request"""

        with self._lock:
            self.methods_map.clear()
            self.canceled_requests.clear()


class Client:
    def __init__(self, server: ServerProcess, transport: Transport, handler: Handler):
        self.server = server
        self.transport = transport
        self.handler = handler
        self._request_manager = RequestManager()

    def _reset_managers(self) -> None:
        self._request_manager = RequestManager()

    def send_message(self, message: Message) -> None:
        content = dumps(message, as_bytes=True)
        self.transport.write(content)

    def _listen_task(self) -> None:
        def listen_message() -> Message:
            content = self.transport.read()
            try:
                message = loads(content)
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
                self.server.terminate()
                break

            try:
                self.handle_message(message)
            except Exception:
                LOGGER.exception("error handle message: %s", message, exc_info=True)

    def listen(self) -> None:
        self._reset_managers()

        thread = threading.Thread(target=self._listen_task, daemon=True)
        thread.start()

    def handle_message(self, message: Message) -> None:
        handler_map = {
            Notification: self._handle_notification,
            Request: self._handle_request,
            Response: self._handle_response,
        }
        return handler_map[type(message)](message)

    def _handle_request(self, message: Request) -> None:
        result = None
        error = None
        try:
            result = self.handler.handle(message.method, message.params)
        except Exception as err:
            LOGGER.exception(err, exc_info=True)
            error = errors.transform_error(err)

        self.send_response(message.id, result, error)

    def _handle_notification(self, message: Notification) -> None:
        try:
            self.handler.handle(message.method, message.params)
        except Exception as err:
            LOGGER.exception(err, exc_info=True)

    def _handle_response(self, message: Response) -> None:
        try:
            method = self._request_manager.pop(message.id)
        except (Canceled, KeyError):
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
        self.send_message(Request(req_id, method, params))

    def send_notification(self, method: MethodName, params: dict) -> None:
        if method in {
            "textDocument/didOpen",
            "textDocument/didChange",
        }:
            # cancel all current request
            self._request_manager.cancel_all()
        self.send_message(Notification(method, params))

    def send_response(
        self, id: int, result: Optional[dict] = None, error: Optional[dict] = None
    ) -> None:
        self.send_message(Response(id, result, error))
