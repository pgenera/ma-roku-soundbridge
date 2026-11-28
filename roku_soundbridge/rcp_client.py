"""Roku SoundBridge RCP protocol client."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

RCP_PORT = 5555
_INTERNET_RADIO_SERVER = "internet radio"
_UNCOMPRESSED_FORMATS: frozenset[str] = frozenset({"WAV", "AIFF", "LPCM"})

_ERROR_VALUES = frozenset(
    {
        "genericerror",
        "error",
        "invalidcommand",
        "errordisconnected",
        "parametererror",
        "unknowncommand",
    }
)
_SONG_INFO_TERMINAL = _ERROR_VALUES | {"ok"}
_TX_TERMINAL = _ERROR_VALUES | {"transactioncomplete", "connectionfailedalreadyconnected"}


def _sanitize_value(value: str) -> str:
    """Strip CR/LF characters that would break RCP line framing."""
    return value.replace("\r", " ").replace("\n", " ").strip()


class RcpClient:
    """Async client for the Roku SoundBridge RCP protocol (TCP port 5555)."""

    def __init__(
        self,
        host: str,
        port: int,
        on_state_change: Callable[[], None],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the RCP client.

        :param host: IP address or hostname of the SoundBridge.
        :param port: TCP port for the RCP control connection (default 5555).
        :param on_state_change: Callback invoked whenever parsed device state changes.
        :param logger: Optional logger (e.g. provider child logger).
        """
        self.host = host
        self.port = port
        self._on_state_change = on_state_change
        self._logger = logger or _LOGGER

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected: bool = False
        self._closing: bool = False
        self._connect_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._pending_responses: dict[str, deque[asyncio.Future[Any]]] = {}

        self._read_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Device state
        self.power_state: str = "unknown"
        self.transport_state: str = "stop"
        self.active_server_name: str = ""
        self.volume: int = 0
        self.muted: bool = False
        self.title: str = ""
        self.artist: str = ""
        self.album: str = ""
        self.genre: str = ""
        self.url: str = ""
        self.duration: int = 0
        self.position: int = 0
        self.position_updated_at: float = 0.0
        self.mac_address: str = ""
        self.version: str = ""

        self._list_future: asyncio.Future[list[str]] | None = None
        self._list_command_key: str | None = None
        self._current_list: list[str] = []

    @property
    def is_connected(self) -> bool:
        """Return True if connected to the SoundBridge."""
        return self._connected

    async def connect(self) -> bool:
        """Connect to the SoundBridge and start background read/poll tasks."""
        async with self._connect_lock:
            if self._connected:
                return True
            try:
                self._logger.debug("Connecting to %s:%d", self.host, self.port)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=5.0
                )
                self._reader = reader
                self._writer = writer
                self._connected = True
                self._closing = False

                line = b""
                for _ in range(3):
                    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                    if line.strip():
                        break
                if not line.startswith(b"roku: ready"):
                    self._logger.debug(
                        "Invalid RCP banner from %s:%d: %r", self.host, self.port, line
                    )
                    self._handle_disconnect()
                    return False

                if not self._read_task or self._read_task.done():
                    self._read_task = asyncio.create_task(self._read_loop())
                if not self._poll_task or self._poll_task.done():
                    self._poll_task = asyncio.create_task(self._poll_loop())
            except (TimeoutError, OSError) as err:
                self._logger.debug("Failed to connect to %s:%d: %s", self.host, self.port, err)
                self._handle_disconnect()
                return False
            else:
                return True

    async def disconnect(self) -> None:
        """Disconnect from the SoundBridge and cancel background tasks."""
        self._closing = True
        self._connected = False
        self._clear_pending_responses()

        for task in (
            self._read_task,
            self._poll_task,
            self._reconnect_task,
            *self._background_tasks,
        ):
            if task and not task.done():
                task.cancel()

        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None

    async def play(self) -> None:
        """Resume playback."""
        if await self._send_command("Play") is not None:
            self.transport_state = "play"

    async def pause(self) -> None:
        """Pause playback."""
        if await self._send_command("Pause") is not None:
            self.transport_state = "pause"

    async def stop(self) -> None:
        """Stop playback and clear active track metadata."""
        if await self._send_command("Stop") is not None:
            self.transport_state = "stop"
        self._clear_song_state()

    async def play_url(
        self,
        url: str,
        title: str = "",
        artist: str = "",
        album: str = "",
        fmt: str = "WAV",
        length_ms: int | None = None,
        *,
        remote_stream: bool | None = None,
    ) -> None:
        """
        Push a stream URL to the device and start playing.

        Uses the documented working-song flow rather than the undocumented
        PlayStation command. Setting ``format`` explicitly avoids the
        SoundBridge's content-type probing, which is unreliable for
        chunked-encoded WAV with no Content-Length. ``remoteStream=1`` marks
        compressed streams as endless ICY radio broadcasts, whereas uncompressed
        formats (``WAV``, ``AIFF``, ``LPCM``) omit ``remoteStream=1`` by default
        because SoundBridge firmware rejects WAV with ``remoteStream 1``.

        All commands are pipelined into a single TCP send so the device can
        process the whole sequence without per-command round-trips.

        :param url: Stream URL to play.
        :param title: Initial title (or source/album line in ICY mode).
        :param artist: Artist name to display (when not relying on in-stream ICY).
        :param album: Album name to display (when not relying on in-stream ICY).
        :param fmt: Audio format token (e.g. ``MP3``, ``WAV``, ``AAC``, ``AIFF``).
        :param length_ms: Track length in milliseconds, if known.
        :param remote_stream: Optional override for sending ``SetWorkingSongInfo remoteStream 1``.
        """
        self._clear_song_state()
        use_remote_stream = (
            (fmt.upper() not in _UNCOMPRESSED_FORMATS) if remote_stream is None else remote_stream
        )
        commands = [
            "ClearWorkingSong",
            f"SetWorkingSongInfo url {_sanitize_value(url)}",
            f"SetWorkingSongInfo format {_sanitize_value(fmt)}",
        ]
        if use_remote_stream:
            commands.append("SetWorkingSongInfo remoteStream 1")
        if sanitized_title := _sanitize_value(title):
            commands.append(f"SetWorkingSongInfo title {sanitized_title}")
        if sanitized_artist := _sanitize_value(artist):
            commands.append(f"SetWorkingSongInfo artist {sanitized_artist}")
        if sanitized_album := _sanitize_value(album):
            commands.append(f"SetWorkingSongInfo album {sanitized_album}")
        if length_ms is not None and length_ms > 0:
            commands.append(f"SetWorkingSongInfo trackLength {length_ms}")
        commands.append("QueueAndPlayOne working")
        if await self._send_pipeline(commands):
            self.transport_state = "play"

    async def set_volume(self, volume: int) -> None:
        """
        Set volume level (0-100).

        :param volume: Target volume percentage from 0 to 100.
        """
        if await self._send_command(f"SetVolume {volume}") is not None:
            self.volume = volume

    async def set_mute(self, *, muted: bool) -> None:
        """
        Mute or unmute audio output.

        :param muted: True to mute, False to unmute.
        """
        if await self._send_command(f"Mute {'on' if muted else 'off'}") is not None:
            self.muted = muted

    async def turn_on(self) -> None:
        """Wake the device from standby without auto-resuming presets."""
        if await self._send_command("SetPowerState on no") is not None:
            self.power_state = "on"

    async def wait_for_power_on(self, timeout: float = 5.0) -> bool:
        """
        Poll until the device confirms it is awake from standby.

        :param timeout: Maximum seconds to wait.
        :return: True if powered on within timeout.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            result = await self._send_command(
                "GetPowerState", wait_for_response=True, disconnect_on_error=False
            )
            if isinstance(result, str) and result.lower() == "on":
                return True
            await asyncio.sleep(0.1)
        return False

    async def turn_off(self) -> None:
        """Put the device into standby."""
        if await self._send_command("SetPowerState standby") is not None:
            self.power_state = "standby"
            self._clear_song_state()

    async def set_working_song_info(self, field: str, value: str) -> None:
        """
        Set a working song info field shown on the device VFD display.

        :param field: RCP working song field name (e.g. ``title``, ``artist``, ``trackLength``).
        :param value: Field value string.
        """
        sanitized = _sanitize_value(value)
        if sanitized:
            await self._send_command(f"SetWorkingSongInfo {field} {sanitized}")

    async def list_servers(self) -> list[str]:
        """Return the list of servers/sources reported by ``ListServers``."""
        return await self._send_list_command("ListServers")

    async def connect_internet_radio(self) -> bool:
        """
        Connect the SoundBridge to its ``Internet Radio`` source so ``QueueAndPlayOne`` works.

        Waking from standby leaves the SoundBridge in ``Disconnected`` state while
        still holding a stale session, so ``ServerDisconnect`` is always sent first.

        :return: True if connected to ``Internet Radio``.
        """
        await self._send_command(
            "ServerDisconnect", wait_for_response=True, disconnect_on_error=False
        )
        servers = await self.list_servers()
        target_idx = next(
            (i for i, name in enumerate(servers) if name.strip().lower() == _INTERNET_RADIO_SERVER),
            None,
        )
        if target_idx is None:
            self._logger.warning(
                "SoundBridge %s:%d did not list 'Internet Radio' in ListServers: %s",
                self.host,
                self.port,
                servers,
            )
            return False

        result = await self._send_command(
            f"ServerConnect {target_idx}", wait_for_response=True, disconnect_on_error=False
        )
        if isinstance(result, str) and result.lower() == "connectionfailedalreadyconnected":
            await self._send_command(
                "ServerDisconnect", wait_for_response=True, disconnect_on_error=False
            )
            result = await self._send_command(
                f"ServerConnect {target_idx}", wait_for_response=True, disconnect_on_error=False
            )

        ok = isinstance(result, str) and result.lower() == "transactioncomplete"
        if (
            not ok
            and isinstance(result, str)
            and result.lower() == "connectionfailedalreadyconnected"
        ):
            transport = await self._send_command(
                "GetTransportState", wait_for_response=True, disconnect_on_error=False
            )
            ok = isinstance(transport, str) and transport.strip().lower() != "disconnected"
        if ok:
            self.active_server_name = "Internet Radio"
            if self.transport_state == "disconnected":
                self.transport_state = "stop"
        return ok

    async def ensure_ready_for_playback(self) -> bool:
        """
        Ensure the SoundBridge is awake and connected to ``Internet Radio``.

        Queries live power and transport state so standby transitions since the
        last poll are still caught before ``QueueAndPlayOne``.

        :return: True if ready for ``play_url``.
        """
        state = await self._send_command(
            "GetPowerState", wait_for_response=True, disconnect_on_error=False
        )
        is_standby = isinstance(state, str) and state.lower() == "standby"
        tr = (
            None
            if is_standby
            else await self._send_command(
                "GetTransportState", wait_for_response=True, disconnect_on_error=False
            )
        )
        transport = tr.lower() if isinstance(tr, str) else self.transport_state
        if is_standby or transport == "standby":
            await self.turn_on()
            await self.wait_for_power_on()
            tr = await self._send_command(
                "GetTransportState", wait_for_response=True, disconnect_on_error=False
            )
            transport = tr.lower() if isinstance(tr, str) else self.transport_state

        if transport == "disconnected":
            return await self.connect_internet_radio()

        if self.active_server_name.lower() != _INTERNET_RADIO_SERVER:
            await self._send_command(
                "GetActiveServerInfo", wait_for_response=True, disconnect_on_error=False
            )
            if self.active_server_name.lower() != _INTERNET_RADIO_SERVER:
                return await self.connect_internet_radio()
        return True

    def _clear_song_state(self) -> None:
        """Reset cached song metadata and elapsed position."""
        self.title = ""
        self.artist = ""
        self.album = ""
        self.genre = ""
        self.url = ""
        self.duration = 0
        self.position = 0
        self.position_updated_at = time.time()

    def _spawn_background_task(self, coro: Any) -> None:
        """Create and track a short-lived background task."""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _handle_disconnect(self) -> None:
        """Handle an unexpected disconnection."""
        if self._closing:
            return
        was_connected = self._connected
        self._connected = False
        self._clear_pending_responses()
        self._spawn_background_task(self._cleanup_connection(was_connected=was_connected))
        self._ensure_reconnect()

    async def _cleanup_connection(self, *, was_connected: bool) -> None:
        """Clean up tasks and stream writer after disconnect."""
        current = asyncio.current_task()
        for task in (self._read_task, self._poll_task):
            if task and not task.done() and task is not current:
                task.cancel()
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        if was_connected:
            self._logger.info("Disconnected from %s:%d", self.host, self.port)
            self._on_state_change()

    def _ensure_reconnect(self) -> None:
        """Schedule reconnect loop if not already running."""
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Reconnect with exponential backoff."""
        delay = 5
        while not self._connected and not self._closing:
            await asyncio.sleep(delay)
            if await self.connect():
                self._logger.info("Reconnected to %s:%d", self.host, self.port)
                self._on_state_change()
                break
            delay = min(delay * 2, 60)

    def _clear_pending_responses(self) -> None:
        """Resolve all pending response futures with None."""
        for deq in self._pending_responses.values():
            while deq:
                future = deq.popleft()
                if not future.done():
                    future.set_result(None)
        self._pending_responses.clear()
        if self._list_future and not self._list_future.done():
            self._list_future.set_result([])
        self._list_future = None
        self._list_command_key = None

    def _discard_future(self, command_name: str, future: asyncio.Future[Any] | None) -> None:
        """Remove a timed-out or failed future from the pending queue."""
        if future is None:
            return
        deq = self._pending_responses.get(command_name)
        if deq:
            with contextlib.suppress(ValueError):
                deq.remove(future)

    async def _send_command(
        self,
        command: str,
        *,
        wait_for_response: bool = False,
        disconnect_on_error: bool = True,
    ) -> Any | None:
        """Send a single RCP command, optionally awaiting its response line."""
        if not self._connected and not self._closing:
            await self.connect()
        if not self._connected:
            return None

        async with self._lock:
            writer = self._writer
            if writer is None:
                return None
            command_name = command.split(None, 1)[0].lower()
            future: asyncio.Future[Any] | None = None
            if wait_for_response:
                future = asyncio.get_running_loop().create_future()
                self._pending_responses.setdefault(command_name, deque()).append(future)

            try:
                self._logger.debug("TX: %s", command)
                writer.write(f"{command}\r\n".encode())
                await writer.drain()
                return await asyncio.wait_for(future, timeout=5.0) if future else "SENT"
            except (TimeoutError, OSError, asyncio.CancelledError) as err:
                self._logger.debug("Command '%s' failed: %s", command, err)
                self._discard_future(command_name, future)
                if disconnect_on_error or isinstance(err, OSError):
                    self._handle_disconnect()
                if isinstance(err, asyncio.CancelledError) and self._closing:
                    raise
                return None

    async def _send_list_command(self, command: str) -> list[str]:
        """Send an RCP list command and collect entries until ``ListResultEnd``."""
        if not self._connected and not self._closing:
            await self.connect()
        if not self._connected:
            return []

        async with self._lock:
            writer = self._writer
            if writer is None:
                return []
            command_name = command.split(None, 1)[0].lower()
            future = asyncio.get_running_loop().create_future()
            self._current_list = []
            self._list_command_key = command_name
            self._list_future = future
            try:
                self._logger.debug("TX: %s", command)
                writer.write(f"{command}\r\n".encode())
                await writer.drain()
                return await asyncio.wait_for(future, timeout=5.0)
            except (TimeoutError, OSError, asyncio.CancelledError) as err:
                self._logger.debug("List command '%s' failed: %s", command, err)
                if self._list_future is future:
                    self._list_future = None
                    self._list_command_key = None
                if isinstance(err, OSError):
                    self._handle_disconnect()
                if isinstance(err, asyncio.CancelledError) and self._closing:
                    raise
                return []

    async def _send_pipeline(self, commands: list[str]) -> bool:
        """Send several RCP commands in a single TCP write."""
        if not self._connected and not self._closing:
            await self.connect()
        if not self._connected:
            return False
        payload = "".join(f"{cmd}\r\n" for cmd in commands).encode()
        async with self._lock:
            writer = self._writer
            if writer is None:
                return False
            try:
                for cmd in commands:
                    self._logger.debug("TX: %s", cmd)
                writer.write(payload)
                await writer.drain()
            except (TimeoutError, OSError, asyncio.CancelledError) as err:
                self._logger.debug("Pipeline send failed: %s", err)
                self._handle_disconnect()
                if isinstance(err, asyncio.CancelledError) and self._closing:
                    raise
                return False
            else:
                return True

    async def _read_loop(self) -> None:
        """Read lines from the RCP socket and dispatch to the state parser."""
        reader = self._reader
        if reader is None:
            return
        try:
            while self._connected:
                line_bytes = await reader.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode(errors="replace").strip()
                if line:
                    self._parse_line(line)
        except asyncio.CancelledError:
            pass
        except OSError as err:
            self._logger.debug("Read loop error: %s", err)
        finally:
            self._handle_disconnect()

    async def _poll_loop(self) -> None:
        """Periodically poll the SoundBridge for power, transport, volume, and song state."""
        try:
            consecutive_timeouts = 0
            while self._connected:
                result = await self._send_command(
                    "GetPowerState", wait_for_response=True, disconnect_on_error=False
                )
                if result is None:
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 3:
                        self._logger.warning(
                            "Poll timeout on %s:%d; disconnecting", self.host, self.port
                        )
                        self._handle_disconnect()
                        break
                else:
                    consecutive_timeouts = 0

                if self.power_state != "standby":
                    cmds = ["GetMACAddress", "GetTransportState", "GetVolume"]
                    if self.transport_state not in {"disconnected", "standby"}:
                        cmds.extend(["GetCurrentSongInfo", "GetElapsedTime", "GetTotalTime"])
                    for cmd in cmds:
                        await self._send_command(
                            cmd, wait_for_response=False, disconnect_on_error=False
                        )
                        await asyncio.sleep(0.05)

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    def _parse_line(self, line: str) -> None:
        """Parse a single line received from the SoundBridge."""
        self._logger.debug("RX: %s", line)

        if ":" not in line:
            if self._list_future and not self._list_future.done():
                self._current_list.append(line)
            return

        command_key, value = line.split(":", 1)
        command_key = command_key.strip().lower()
        value = value.strip()
        val_lower = value.lower()

        if self._list_future and not self._list_future.done():
            if val_lower.startswith("listresultsize") or command_key.endswith("listresultsize"):
                self._current_list = []
                return
            if val_lower == "listresultend" or command_key.endswith("listresultend"):
                self._list_future.set_result(self._current_list)
                self._list_future = None
                self._list_command_key = None
                return
            if self._list_command_key == command_key:
                if val_lower in _ERROR_VALUES:
                    self._list_future.set_result([])
                    self._list_future = None
                    self._list_command_key = None
                    return
                self._current_list.append(value)
                return

        self._resolve_future(command_key, value)
        if self._update_state(command_key, value):
            self._on_state_change()

    def _resolve_future(self, command_key: str, value: str) -> None:
        """Resolve a pending command future if one is waiting."""
        deq = self._pending_responses.get(command_key)
        if not deq:
            return
        future = deq[0]
        val_lower = value.lower()
        should_resolve = True
        if command_key in {"getcurrentsonginfo", "getactiveserverinfo"}:
            should_resolve = val_lower in _SONG_INFO_TERMINAL
        elif command_key in {"serverconnect", "serverdisconnect"}:
            should_resolve = val_lower in _TX_TERMINAL
        if should_resolve:
            deq.popleft()
            if not future.done():
                future.set_result(value)

    def _set_attr(self, attr: str, value: Any) -> bool:
        """Assign ``value`` to ``attr`` and return True if it changed."""
        changed: bool = getattr(self, attr) != value
        setattr(self, attr, value)
        return changed

    def _update_time_attr(self, command_key: str, value: str) -> bool:
        """Update ``position`` or ``duration`` when ``value`` parses as a valid timestamp."""
        if (parsed := self._parse_time(value)) is None:
            return False
        attr = "position" if command_key == "getelapsedtime" else "duration"
        changed = self._set_attr(attr, parsed)
        if command_key == "getelapsedtime":
            self.position_updated_at = time.time()
        return changed

    def _update_state(self, command_key: str, value: str) -> bool:
        """Update internal device attributes and return True if state changed."""
        val_lower = value.lower()
        changed = False
        if command_key in {"getpowerstate", "gettransportstate"}:
            if val_lower not in _ERROR_VALUES:
                attr = "power_state" if command_key == "getpowerstate" else "transport_state"
                if command_key == "gettransportstate" and val_lower == "disconnected":
                    self.active_server_name = ""
                changed = self._set_attr(attr, val_lower)
        elif command_key == "getvolume":
            with contextlib.suppress(ValueError):
                changed = self._set_attr("volume", int(value))
        elif command_key in {"getelapsedtime", "gettotaltime"}:
            changed = self._update_time_attr(command_key, value)
        elif command_key in {"getmacaddress", "getversion"}:
            if val_lower not in _ERROR_VALUES:
                attr = "mac_address" if command_key == "getmacaddress" else "version"
                changed = self._set_attr(attr, value)
        elif command_key == "getactiveserverinfo":
            changed = self._parse_active_server_info(value)
        elif command_key == "getcurrentsonginfo":
            changed = self._parse_song_info(value)
        return changed

    @staticmethod
    def _parse_time(time_str: str) -> int | None:
        """Parse H:MM:SS or MM:SS into seconds, returning None for non-time errors."""
        if ":" not in time_str:
            return None
        parts = time_str.split(":")
        try:
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
        except ValueError:
            return None
        return None

    def _parse_active_server_info(self, value: str) -> bool:
        """Parse ``GetActiveServerInfo`` lines for server name or disconnected state."""
        val_lower = value.lower()
        if val_lower in _ERROR_VALUES:
            return self._set_attr("active_server_name", "")
        if ":" not in value:
            return False
        info_key, info_val = value.split(":", 1)
        if info_key.strip().lower() == "name":
            return self._set_attr("active_server_name", info_val.strip())
        return False

    def _parse_song_info(self, value: str) -> bool:
        """Parse a GetCurrentSongInfo key:value line."""
        val_lower = value.lower()
        if val_lower == "ok":
            return True
        if val_lower in _ERROR_VALUES or ":" not in value:
            return False
        info_key, info_val = value.split(":", 1)
        attr = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "genre": "genre",
            "resource[0] url": "url",
            "playlisturl": "url",
        }.get(info_key.strip().lower())
        if attr is None:
            return False
        return self._set_attr(attr, info_val.strip())
