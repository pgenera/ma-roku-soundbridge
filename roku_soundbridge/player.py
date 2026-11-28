"""Roku SoundBridge PlayerProtocol implementation for Music Assistant."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import (
    ContentType,
    IdentifierType,
    MediaType,
    PlaybackState,
    PlayerFeature,
    PlayerType,
)
from music_assistant_models.player import DeviceInfo, PlayerMedia

from music_assistant import constants as ma_constants
from music_assistant.constants import CONF_ENABLE_ICY_METADATA, CONF_ENTRY_ENABLE_ICY_METADATA
from music_assistant.models.player_protocol import PlayerProtocol

from .rcp_client import RcpClient

if TYPE_CHECKING:
    from .provider import SoundBridgeProvider

# The SoundBridge does not support FLAC and rejects WAV via ICY/Shoutcast,
# so new SoundBridge protocols default to MP3 with basic ICY metadata:
#  - Reliable startup (avoids probe-and-reconnect issues seen with WAV)
#  - In-stream metadata blocks parsed natively by the SoundBridge
#  - ~400ms faster click-to-audio than WAV
# Users can still switch to WAV in the player settings UI if desired.
_CONF_ENTRY_ICY_METADATA_BASIC = ConfigEntry.from_dict(
    {**CONF_ENTRY_ENABLE_ICY_METADATA.to_dict(), "default_value": "basic"}
)

# Servers up to 2.11 pick the stream codec from an output codec setting; newer servers replaced
# it with a playback quality setting that the protocol maps to codecs. Both are looked up at
# runtime so one release loads on either.
_CONF_OUTPUT_CODEC: str | None = getattr(ma_constants, "CONF_OUTPUT_CODEC", None)
_CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3: ConfigEntry | None = getattr(
    ma_constants, "CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3", None
)
_PlaybackQuality: Any = getattr(ma_constants, "PlaybackQuality", None)

_TRANSPORT_TO_PLAYBACK = {
    "play": PlaybackState.PLAYING,
    "pause": PlaybackState.PAUSED,
    "stop": PlaybackState.IDLE,
    "buffering": PlaybackState.PLAYING,
    "connecting": PlaybackState.PLAYING,
    "disconnected": PlaybackState.IDLE,
}

# Seconds to hold the PLAYING state after issuing a play command, suppressing
# transient "stop" or "connecting" reports during initial buffering.
_PLAY_GRACE_SECONDS = 15


class SoundBridgePlayer(PlayerProtocol):
    """A Roku SoundBridge player controlled via the RCP protocol."""

    _attr_type = PlayerType.PLAYER

    def __init__(
        self,
        provider: SoundBridgeProvider,
        protocol_id: str,
        host: str,
        port: int,
        *,
        name: str,
        resolved_ip: str | None = None,
    ) -> None:
        """
        Initialize the SoundBridge protocol player and RCP client.

        :param provider: Owning SoundBridgeProvider instance.
        :param protocol_id: Unique protocol identifier.
        :param host: Configured or mDNS-discovered host/IP.
        :param port: TCP port for RCP (default 5555).
        :param name: Initial display name for the player.
        :param resolved_ip: Resolved IPv4 address when ``host`` is a hostname.
        """
        super().__init__(provider, protocol_id)
        self.host = host
        self.port = port
        self.resolved_ip = resolved_ip
        self._client = RcpClient(
            host,
            port,
            self._on_client_state_change,
            logger=provider.logger.getChild("rcp"),
        )
        self._last_play_url_time: float = 0.0
        self._active_media: PlayerMedia | None = None

        self._attr_name = name
        self._attr_available = False
        self._attr_supported_features = {
            PlayerFeature.PLAY_MEDIA,
            PlayerFeature.POWER,
            PlayerFeature.VOLUME_SET,
            PlayerFeature.VOLUME_MUTE,
            PlayerFeature.PAUSE,
        }
        self._attr_device_info = DeviceInfo(
            model="SoundBridge",
            manufacturer="Roku",
        )
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, resolved_ip or host)

    @property
    def needs_poll(self) -> bool:
        """Return True because polling synchronizes state and track metadata."""
        return True

    @property
    def poll_interval(self) -> int:
        """Return polling interval in seconds (faster while playing)."""
        return 10 if self._attr_playback_state == PlaybackState.PLAYING else 30

    @property
    def playback_quality_codecs(self) -> dict[Any, ContentType]:
        """Return the codec for each playback quality level the SoundBridge can play."""
        if _PlaybackQuality is None:
            return {}
        return {
            _PlaybackQuality.LOSSLESS_UNCOMPRESSED: ContentType.WAV,
            _PlaybackQuality.COMPRESSED: ContentType.MP3,
        }

    @property
    def default_playback_quality(self) -> Any:
        """Return the playback quality level to use when the user picked none."""
        return None if _PlaybackQuality is None else _PlaybackQuality.COMPRESSED

    async def get_config_entries(self) -> list[ConfigEntry]:
        """Return player-level config entries overriding codec and ICY defaults."""
        if _CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3 is None:
            return [_CONF_ENTRY_ICY_METADATA_BASIC]
        return [_CONF_ENTRY_OUTPUT_CODEC_DEFAULT_MP3, _CONF_ENTRY_ICY_METADATA_BASIC]

    async def on_config_updated(self) -> None:
        """
        Persist MP3/ICY protocol defaults and connect to the SoundBridge.

        ``get_config_entries`` sets the UI defaults to ``mp3`` / ``basic``, while
        the streams controller reads ``CONF_ENABLE_ICY_METADATA`` via
        ``get_protocol_value``, which only returns explicitly stored values.
        Writing defaults on first attach ensures ICY headers are active immediately
        without overwriting any explicit user choice. Servers with a playback quality
        setting take MP3 from ``default_playback_quality`` instead of a stored codec.
        """
        defaults = [(CONF_ENABLE_ICY_METADATA, "basic")]
        if _CONF_OUTPUT_CODEC is not None:
            defaults.insert(0, (_CONF_OUTPUT_CODEC, "mp3"))
        for key, default in defaults:
            if self.mass.config.get_protocol_value(self.protocol_id, key) is None:
                self.mass.config.set_protocol_value(self.protocol_id, key, default)

        await self._client.connect()
        self._sync_client_state()
        self.update_state()

    async def on_detach(self) -> None:
        """Close the RCP connection on detach."""
        await super().on_detach()
        self._attr_available = False
        self._active_media = None
        await self._client.disconnect()

    async def on_remove(self) -> None:
        """Remove manually configured host entry when the player is deleted."""
        cast("SoundBridgeProvider", self.provider).forget_host(self.host, self.port)

    async def poll(self) -> None:
        """Refresh player state from the RCP client."""
        self._sync_client_state()
        self.update_state()

    async def power(self, powered: bool) -> None:
        """
        Wake the SoundBridge from standby or put it into standby.

        :param powered: True to wake from standby, False for standby.
        """
        if powered:
            await self._client.turn_on()
            await self._client.wait_for_power_on()
        else:
            await self._client.turn_off()
        self._sync_client_state()
        self.update_state()

    async def volume_set(self, volume_level: int) -> None:
        """
        Set output volume level (0-100).

        :param volume_level: Volume percentage from 0 to 100.
        """
        await self._client.set_volume(volume_level)
        self._attr_volume_level = volume_level
        self.update_state()

    async def volume_mute(self, muted: bool) -> None:
        """
        Mute or unmute audio output.

        :param muted: True to mute, False to unmute.
        """
        await self._client.set_mute(muted=muted)
        self._attr_volume_muted = muted
        self.update_state()

    async def play(self) -> None:
        """Resume playback."""
        await self._client.play()
        self._attr_playback_state = PlaybackState.PLAYING
        self.update_state()

    async def pause(self) -> None:
        """Pause playback."""
        await self._client.pause()
        self._attr_playback_state = PlaybackState.PAUSED
        self.update_state()

    async def stop(self) -> None:
        """Stop playback."""
        self._active_media = None
        await self._client.stop()
        self._attr_playback_state = PlaybackState.IDLE
        self._attr_current_media = None
        self.update_state()

    async def play_media(self, media: PlayerMedia) -> None:
        """
        Resolve the Music Assistant stream URL and start playback over RCP.

        :param media: The PlayerMedia item/stream to play.
        """
        await self._client.ensure_ready_for_playback()

        url = await self.mass.streams.resolve_stream_url(self.protocol_id, media)
        ext = url.split("?", 1)[0].rsplit(".", 1)[-1].lower()
        fmt = {"wav": "WAV", "mp3": "MP3", "aac": "AAC", "aif": "AIFF", "aiff": "AIFF"}.get(
            ext, "WAV"
        )

        queue_title, queue_artist, queue_album, queue_duration, is_single_item = (
            self._current_queue_track_metadata()
        )
        icy_pref = self.mass.config.get_protocol_value(
            self.protocol_id, CONF_ENABLE_ICY_METADATA, "basic"
        )
        use_icy = fmt == "MP3" and icy_pref != "disabled"

        # When ICY StreamTitle arrives, SoundBridge firmware shifts the initial
        # working `title` into `artist` (VFD line 2) and places ICY StreamTitle on
        # `title` (VFD line 1). Seeding initial `title` with the shared album (for
        # single tracks or full-album queues) or "Music Assistant" (for mixed-album
        # flows) gives a clean 2-line VFD readout without stale album metadata.
        if use_icy:
            uniform_album = self._uniform_queue_album(self._get_active_queue(), queue_album)
            title = uniform_album or "Music Assistant"
            artist = ""
            album = ""
        else:
            title = queue_title
            artist = queue_artist
            album = queue_album if is_single_item else ""

        length_ms = int(queue_duration * 1000) if queue_duration else None

        self._active_media = media
        now = time.time()
        self._last_play_url_time = now
        self._attr_elapsed_time = 0.0
        self._attr_elapsed_time_last_updated = now
        await self._client.play_url(
            url,
            title=title,
            artist=artist,
            album=album,
            fmt=fmt,
            length_ms=length_ms,
        )
        self._attr_playback_state = PlaybackState.PLAYING
        self._attr_current_media = media
        self.update_state()

    async def update_endpoint(
        self,
        host: str,
        port: int,
        resolved_ip: str | None = None,
    ) -> None:
        """
        Update connection endpoint if mDNS or manual discovery learned a new address.

        :param host: Updated host or IP address.
        :param port: Updated RCP TCP port.
        :param resolved_ip: Resolved IPv4 address if known.
        """
        if resolved_ip:
            self.resolved_ip = resolved_ip
            self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, resolved_ip)
        if (host, port) == (self.host, self.port):
            return
        self.host = host
        self.port = port
        self._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, resolved_ip or host)
        if self.player is not None:
            await self._client.disconnect()
            self._client = RcpClient(
                host,
                port,
                self._on_client_state_change,
                logger=self.logger.getChild("rcp"),
            )
            await self._client.connect()
            self._sync_client_state()
            self.update_state()
        else:
            self._client.host = host
            self._client.port = port

    def _on_client_state_change(self) -> None:
        """Synchronize and publish state when the RCP client receives updates."""
        self._sync_client_state()
        self.update_state()

    def _sync_client_state(self) -> None:
        """Copy RCP client state into PlayerProtocol attributes."""
        client = self._client
        self._attr_available = client.is_connected

        if client.power_state == "standby":
            self._attr_powered = False
            self._attr_playback_state = PlaybackState.IDLE
        else:
            self._attr_powered = True
            transport = client.transport_state
            if transport in {"stop", "connecting"} and (
                time.time() - self._last_play_url_time < _PLAY_GRACE_SECONDS
            ):
                transport = "buffering"
            self._attr_playback_state = _TRANSPORT_TO_PLAYBACK.get(transport, PlaybackState.IDLE)

        self._attr_volume_level = client.volume
        self._attr_volume_muted = client.muted

        if client.mac_address:
            normalized_mac = client.mac_address.upper().replace("-", ":")
            self._attr_device_info.add_identifier(IdentifierType.MAC_ADDRESS, normalized_mac)

        if self._attr_playback_state == PlaybackState.PLAYING:
            self._attr_elapsed_time = float(client.position)
            self._attr_elapsed_time_last_updated = client.position_updated_at or time.time()
            queue_title, queue_artist, queue_album, queue_duration, _ = (
                self._current_queue_track_metadata()
            )
            active = self._active_media
            self._attr_current_media = PlayerMedia(
                uri=(active.uri if active and active.uri else client.url or ""),
                media_type=active.media_type if active else MediaType.UNKNOWN,
                title=queue_title or client.title or None,
                artist=queue_artist or client.artist or None,
                album=queue_album or client.album or None,
                duration=queue_duration or client.duration or None,
                source_id=active.source_id if active else None,
                queue_item_id=active.queue_item_id if active else None,
                queue_session_id=active.queue_session_id if active else None,
            )
        else:
            if self._attr_playback_state == PlaybackState.IDLE:
                self._active_media = None
            self._attr_elapsed_time = None
            self._attr_elapsed_time_last_updated = None
            self._attr_current_media = None

    def _get_active_queue(self) -> Any | None:
        """Return the active PlayerQueue for this player, if any."""
        return (
            self.mass.players.get_active_queue(self.player)
            if self.player is not None
            else self.mass.player_queues.get_active_queue(self.protocol_id)
        )

    def _uniform_queue_album(self, queue: Any | None, fallback_album: str) -> str:
        """
        Return the shared album name when all remaining queue items match it.

        :param queue: Active PlayerQueue instance or mock.
        :param fallback_album: Current track's resolved album name.
        :return: Shared album name for single tracks or full-album queues, else ``""``.
        """
        if not queue or not fallback_album:
            return ""
        if queue.items <= 1:
            return fallback_album
        items_fn = getattr(self.mass.player_queues, "items", None)
        offset = getattr(queue, "current_index", None) or 0
        remaining = (
            items_fn(queue.queue_id, limit=max(queue.items - offset, 1), offset=offset)
            if callable(items_fn)
            else None
        )
        if not isinstance(remaining, list) or not remaining:
            return ""
        for idx, qi in enumerate(remaining):
            qi_media = getattr(qi, "media_item", None)
            qi_album = getattr(getattr(qi_media, "album", None), "name", None) or (
                fallback_album if idx == 0 else ""
            )
            if qi_album != fallback_album:
                return ""
        return fallback_album

    def _current_queue_track_metadata(self) -> tuple[str, str, str, int | None, bool]:
        """Return (title, artist, album, duration_seconds, is_single_item) for the active queue."""
        queue = self._get_active_queue()
        if not queue or not queue.current_item:
            return "", "", "", None, False
        item = queue.current_item
        media_item = item.media_item
        streamdetails = getattr(item, "streamdetails", None)
        stream_meta = getattr(streamdetails, "stream_metadata", None) if streamdetails else None

        sm_title = getattr(stream_meta, "title", None) if stream_meta else None
        sm_artist = getattr(stream_meta, "artist", None) if stream_meta else None
        sm_album = getattr(stream_meta, "album", None) if stream_meta else None

        title = sm_title or (media_item.name if media_item else None) or item.name or ""
        artists = getattr(media_item, "artists", None) if media_item else None
        artist = sm_artist or (" / ".join(a.name for a in artists) if artists else "")
        album_obj = getattr(media_item, "album", None) if media_item else None
        album = sm_album or (getattr(album_obj, "name", None) or "")

        # Only expose duration when there is a single track in the queue: in flow mode
        # trackLength is locked at QueueAndPlayOne time for the continuous stream.
        is_single_item = queue.items == 1
        duration = item.duration if (item.duration and is_single_item) else None
        return title, artist, album, duration, is_single_item
