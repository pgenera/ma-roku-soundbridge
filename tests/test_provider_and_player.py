"""Unit tests for SoundBridgeProvider dual discovery and SoundBridgePlayer protocol behavior."""

from __future__ import annotations

import asyncio
import ipaddress
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import (
    ContentType,
    IdentifierType,
    MediaType,
    PlaybackState,
)
from music_assistant_models.player import PlayerMedia
from zeroconf import ServiceStateChange

from music_assistant import constants as ma_constants
from music_assistant.constants import CONF_ENABLE_ICY_METADATA
from roku_soundbridge.player import SoundBridgePlayer
from roku_soundbridge.provider import (
    CONF_MANUAL_IPS,
    SoundBridgeProvider,
    extract_mdns_friendly_name,
    make_protocol_id,
    parse_host_entry,
)


def _make_mock_provider(
    manual_hosts: list[str] | None = None,
) -> tuple[SoundBridgeProvider, MagicMock]:
    """Construct a SoundBridgeProvider and its underlying MagicMock mass controller."""
    provider = SoundBridgeProvider.__new__(SoundBridgeProvider)
    values = {CONF_MANUAL_IPS: SimpleNamespace(value=list(manual_hosts or []))}
    config = MagicMock()
    config.instance_id = "roku_soundbridge_1"
    config.values = values
    config.get_value.side_effect = lambda key, default=None: (
        values[key].value if key in values else default
    )
    provider.config = config
    provider.logger = MagicMock()
    manifest = MagicMock()
    manifest.domain = "roku_soundbridge"
    provider.manifest = manifest

    registered: dict[str, SoundBridgePlayer] = {}

    async def _register(protocol: SoundBridgePlayer) -> None:
        registered[protocol.protocol_id] = protocol

    async def _remove_protocol(protocol_id: str) -> None:
        registered.pop(protocol_id, None)

    mass = MagicMock()
    mass.config.get_base_protocol_config.side_effect = lambda pid, _prov_id: SimpleNamespace(
        protocol_id=pid,
        enabled=True,
        default_name=None,
        setup_data={},
        values={},
    )
    stored_protocol_values: dict[tuple[str, str], Any] = {}
    mass.config.get_protocol_value.side_effect = lambda pid, key, default=None: (
        stored_protocol_values.get((pid, key), default)
    )

    def _set_protocol_value(pid: str, key: str, val: Any) -> None:
        stored_protocol_values[(pid, key)] = val

    mass.config.set_protocol_value.side_effect = _set_protocol_value
    mass.config.get_protocol_record.side_effect = lambda pid: (
        {"protocol_id": pid} if pid in registered else None
    )
    mass.players.wants_protocol.return_value = True
    mass.players.register = AsyncMock(side_effect=_register)
    mass.players.remove_protocol = AsyncMock(side_effect=_remove_protocol)
    mass.players.get_protocol.side_effect = registered.get
    mass.players.trigger_player_update = MagicMock()

    provider.mass = mass
    return provider, mass


def _make_zeroconf_info(ip: str, port: int = 5555) -> MagicMock:
    """Create a mock AsyncServiceInfo returning the given IPv4 address and port."""
    info = MagicMock()
    info.port = port
    addr = ipaddress.IPv4Address(ip)
    info.ip_addresses_by_version.side_effect = lambda version: (
        [addr] if "V4" in str(version) else []
    )
    return info


def test_host_entry_and_protocol_id_helpers() -> None:
    """Verify host[:port] parsing, backward-compatible protocol IDs, and mDNS name parsing."""
    assert parse_host_entry("  ") is None
    assert parse_host_entry("roku.fivesevenfive.org") == ("roku.fivesevenfive.org", 5555)
    assert parse_host_entry("192.168.1.50:5556") == ("192.168.1.50", 5556)

    assert make_protocol_id("roku.fivesevenfive.org", 5555) == "soundbridge_roku_fivesevenfive_org"
    assert make_protocol_id("192.168.1.50", 5556) == "soundbridge_192_168_1_50_5556"

    assert (
        extract_mdns_friendly_name("Kitchen SoundBridge._roku-rcp._tcp.local.", "192.168.1.50")
        == "Kitchen SoundBridge"
    )
    assert (
        extract_mdns_friendly_name("SoundBridge._roku-rcp._tcp.local.", "192.168.1.50")
        == "SoundBridge (192.168.1.50)"
    )


async def test_multiple_soundbridges_via_mdns_and_manual_with_dedup() -> None:
    """Multiple SoundBridges register via mDNS and manual config without duplicating shared IPs."""
    provider, _mass = _make_mock_provider(manual_hosts=["192.168.1.50", "10.20.0.15:5556"])
    await provider.handle_async_init()
    await provider.loaded_in_mass()
    await provider.discover_players()

    assert set(provider._players) == {
        "soundbridge_192_168_1_50",
        "soundbridge_10_20_0_15_5556",
    }

    # mDNS announces 192.168.1.50 (already manually configured) AND a third unit 192.168.1.51
    await provider.on_mdns_service_state_change(
        "Living Room._roku-rcp._tcp.local.",
        ServiceStateChange.Added,
        _make_zeroconf_info("192.168.1.50", 5555),
    )
    await provider.on_mdns_service_state_change(
        "Office._roku-rcp._tcp.local.",
        ServiceStateChange.Added,
        _make_zeroconf_info("192.168.1.51", 5555),
    )

    assert set(provider._players) == {
        "soundbridge_192_168_1_50",
        "soundbridge_10_20_0_15_5556",
        "soundbridge_192_168_1_51",
    }
    assert provider._players["soundbridge_192_168_1_51"].name == "Office"


async def test_removing_player_prunes_manual_host_entry() -> None:
    """Removing a manually configured SoundBridge prunes only that host from provider config."""
    provider, mass = _make_mock_provider(manual_hosts=["192.168.1.50", "192.168.1.52:5555"])
    await provider.handle_async_init()
    await provider.loaded_in_mass()
    await provider.discover_players()

    player = provider._players["soundbridge_192_168_1_50"]
    await player.on_remove()

    mass.config.set_raw_provider_config_value.assert_called_once_with(
        "roku_soundbridge_1",
        CONF_MANUAL_IPS,
        ["192.168.1.52:5555"],
        encrypted=False,
        immediate=False,
    )


# servers up to 2.11 have an output codec setting, newer ones a playback quality setting
_CONF_OUTPUT_CODEC: str | None = getattr(ma_constants, "CONF_OUTPUT_CODEC", None)
_PLAYBACK_QUALITY: Any = getattr(ma_constants, "PlaybackQuality", None)


def _assert_mp3_default(player: SoundBridgePlayer, by_key: dict[str, Any]) -> None:
    """Assert the player streams MP3 by default under the server's codec setting."""
    if _CONF_OUTPUT_CODEC is not None:
        assert by_key[_CONF_OUTPUT_CODEC].default_value == "mp3"
    if _PLAYBACK_QUALITY is not None:
        assert player.playback_quality_codecs == {
            _PLAYBACK_QUALITY.LOSSLESS_UNCOMPRESSED: ContentType.WAV,
            _PLAYBACK_QUALITY.COMPRESSED: ContentType.MP3,
        }
        assert player.default_playback_quality == _PLAYBACK_QUALITY.COMPRESSED


async def _make_player_with_mock_client() -> tuple[SoundBridgePlayer, MagicMock, MagicMock]:
    """Build a SoundBridgePlayer with a mocked RcpClient and parent player."""
    provider, mass = _make_mock_provider()
    await provider.handle_async_init()

    player = SoundBridgePlayer(
        provider=provider,
        protocol_id="soundbridge_192_168_1_50",
        host="192.168.1.50",
        port=5555,
        name="Living Room",
    )
    mock_client = MagicMock()
    mock_client.is_connected = True
    mock_client.power_state = "on"
    mock_client.transport_state = "stop"
    mock_client.volume = 40
    mock_client.muted = False
    mock_client.mac_address = "00-0d-4b-aa-bb-cc"
    mock_client.position = 0
    mock_client.position_updated_at = 0.0
    mock_client.url = ""
    mock_client.title = ""
    mock_client.artist = ""
    mock_client.album = ""
    mock_client.duration = 0
    mock_client.connect = AsyncMock(return_value=True)
    mock_client.ensure_ready_for_playback = AsyncMock(return_value=True)
    mock_client.play_url = AsyncMock()
    mock_client.stop = AsyncMock()
    player._client = mock_client

    parent_player = MagicMock()
    parent_player.player_id = "ma_player_1"
    player._player = parent_player
    return player, mock_client, mass


async def test_player_defaults_mp3_icy_and_play_media_modes() -> None:
    """SoundBridgePlayer defaults to MP3+basic ICY, seeds line 2 cleanly, and supports WAV."""
    player, mock_client, mass = await _make_player_with_mock_client()

    entries = await player.get_config_entries()
    by_key = {entry.key: entry for entry in entries}
    assert by_key[CONF_ENABLE_ICY_METADATA].default_value == "basic"
    _assert_mp3_default(player, by_key)

    await player.on_config_updated()
    if _CONF_OUTPUT_CODEC is not None:
        assert (
            mass.config.get_protocol_value("soundbridge_192_168_1_50", _CONF_OUTPUT_CODEC) == "mp3"
        )
    assert (
        mass.config.get_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA)
        == "basic"
    )
    assert (IdentifierType.MAC_ADDRESS, "00:0D:4B:AA:BB:CC") in player.identifiers

    queue = SimpleNamespace(
        queue_id="ma_player_1",
        items=1,
        current_item=SimpleNamespace(
            name="Fallback Name",
            duration=210,
            media_item=SimpleNamespace(
                name="So What",
                artists=[SimpleNamespace(name="Miles Davis")],
                album=SimpleNamespace(name="Kind of Blue"),
            ),
        ),
    )
    mass.players.get_active_queue.return_value = queue

    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://192.168.1.10:8097/flow/session/item.mp3"
    )
    media = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id="ma_player_1",
        queue_item_id="qi_1",
    )
    await player.play_media(media)
    mock_client.ensure_ready_for_playback.assert_awaited_once()
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.mp3",
        title="Kind of Blue",
        artist="",
        album="",
        fmt="MP3",
        length_ms=210000,
    )
    assert player.playback_state == PlaybackState.PLAYING

    mock_client.transport_state = "play"
    mock_client.position = 12
    player._sync_client_state()
    assert player.current_media is not None
    assert player.current_media.uri == "library://track/1"
    assert player.current_media.source_id == "ma_player_1"
    assert player.current_media.queue_item_id == "qi_1"

    mock_client.play_url.reset_mock()
    track2 = SimpleNamespace(
        name="Freddie Freeloader",
        duration=589,
        media_item=SimpleNamespace(
            name="Freddie Freeloader",
            artists=[SimpleNamespace(name="Miles Davis")],
            album=SimpleNamespace(name="Kind of Blue"),
        ),
    )
    track3 = SimpleNamespace(
        name="Blue in Green",
        duration=337,
        media_item=SimpleNamespace(
            name="Blue in Green",
            artists=[SimpleNamespace(name="Miles Davis")],
            album=SimpleNamespace(name="Kind of Blue"),
        ),
    )
    queue.items = 3
    queue.current_index = 1
    queue.current_item = track2
    mass.player_queues.items.return_value = [track2, track3]
    await player.play_media(media)
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.mp3",
        title="Kind of Blue",
        artist="",
        album="",
        fmt="MP3",
        length_ms=None,
    )

    mock_client.play_url.reset_mock()
    other_album_track = SimpleNamespace(
        name="Pharaoh's Dance",
        duration=1200,
        media_item=SimpleNamespace(
            name="Pharaoh's Dance",
            artists=[SimpleNamespace(name="Miles Davis")],
            album=SimpleNamespace(name="Bitches Brew"),
        ),
    )
    queue.items = 2
    queue.current_index = 0
    mass.player_queues.items.return_value = [track2, other_album_track]
    await player.play_media(media)
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.mp3",
        title="Music Assistant",
        artist="",
        album="",
        fmt="MP3",
        length_ms=None,
    )

    mock_client.play_url.reset_mock()
    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://192.168.1.10:8097/flow/session/item.wav"
    )
    await player.play_media(media)
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.wav",
        title="Freddie Freeloader",
        artist="Miles Davis",
        album="",
        fmt="WAV",
        length_ms=None,
    )


async def test_player_wav_auto_disables_and_restores_icy() -> None:
    """Playing WAV disables ICY automatically, switching back to MP3 restores it, and manual disable sticks."""
    player, mock_client, mass = await _make_player_with_mock_client()
    await player.on_config_updated()

    track2 = SimpleNamespace(
        name="Freddie Freeloader",
        duration=589,
        media_item=SimpleNamespace(
            name="Freddie Freeloader",
            artists=[SimpleNamespace(name="Miles Davis")],
            album=SimpleNamespace(name="Kind of Blue"),
        ),
    )
    track3 = SimpleNamespace(
        name="Blue in Green",
        duration=337,
        media_item=SimpleNamespace(
            name="Blue in Green",
            artists=[SimpleNamespace(name="Miles Davis")],
            album=SimpleNamespace(name="Kind of Blue"),
        ),
    )
    queue = SimpleNamespace(
        queue_id="ma_player_1",
        items=2,
        current_index=0,
        current_item=track2,
    )
    mass.players.get_active_queue.return_value = queue
    mass.player_queues.items.return_value = [track2, track3]

    media = PlayerMedia(
        uri="library://track/2",
        media_type=MediaType.TRACK,
        source_id="ma_player_1",
        queue_item_id="qi_2",
    )

    # Full-album WAV disables ICY automatically and passes the shared album name.
    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://192.168.1.10:8097/flow/session/item.wav"
    )
    await player.play_media(media)
    assert (
        mass.config.get_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA)
        == "disabled"
    )
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.wav",
        title="Freddie Freeloader",
        artist="Miles Davis",
        album="Kind of Blue",
        fmt="WAV",
        length_ms=None,
    )

    # Switching back to MP3 restores the previous ICY setting automatically.
    mock_client.play_url.reset_mock()
    mass.streams.resolve_stream_url = AsyncMock(
        return_value="http://192.168.1.10:8097/flow/session/item.mp3"
    )
    await player.play_media(media)
    assert (
        mass.config.get_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA)
        == "basic"
    )
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.mp3",
        title="Kind of Blue",
        artist="",
        album="",
        fmt="MP3",
        length_ms=None,
    )

    # Manual user ICY="disabled" on MP3 must not be overwritten.
    mock_client.play_url.reset_mock()
    mass.config.set_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA, "disabled")
    await player.play_media(media)
    assert (
        mass.config.get_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA)
        == "disabled"
    )
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/flow/session/item.mp3",
        title="Freddie Freeloader",
        artist="Miles Davis",
        album="Kind of Blue",
        fmt="MP3",
        length_ms=None,
    )


async def test_player_wav_requires_no_flow_mode_and_enqueue_handoff() -> None:
    """Lossless WAV disables flow mode and hands off to enqueued next track cleanly on EOF."""
    player, mock_client, mass = await _make_player_with_mock_client()
    tasks: list[asyncio.Task[Any]] = []

    def _create_task(coro: Any) -> asyncio.Task[Any]:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        tasks.append(task)
        return task

    mass.create_task = _create_task

    # Default MP3 uses continuous flow mode; WAV switches to per-track mode.
    await player.on_config_updated()
    if _CONF_OUTPUT_CODEC is not None:
        assert player.requires_flow_mode is True
        mass.config.set_protocol_value("soundbridge_192_168_1_50", _CONF_OUTPUT_CODEC, "wav")
        assert player.requires_flow_mode is False
    elif _PLAYBACK_QUALITY is not None:
        quality_key = getattr(ma_constants, "CONF_PLAYBACK_QUALITY", "playback_quality")
        mass.config.set_protocol_value(
            "soundbridge_192_168_1_50",
            quality_key,
            _PLAYBACK_QUALITY.LOSSLESS_UNCOMPRESSED,
        )
        assert player.requires_flow_mode is False

    track1 = SimpleNamespace(
        queue_item_id="qi_1",
        name="Track One",
        duration=155,
        media_item=SimpleNamespace(
            name="007",
            artists=[SimpleNamespace(name="Sex Mob")],
            album=SimpleNamespace(name="Sex Mob Does Bond"),
        ),
    )
    track2 = SimpleNamespace(
        queue_item_id="qi_2",
        name="Track Two",
        duration=240,
        media_item=SimpleNamespace(
            name="01 - Sinfonia",
            artists=[SimpleNamespace(name="Gabriele Santini")],
            album=SimpleNamespace(name="Madama Butterfly"),
        ),
    )
    queue = SimpleNamespace(queue_id="ma_player_1", items=2, current_index=0, current_item=track1)
    mass.players.get_active_queue.return_value = queue
    mass.player_queues.items.return_value = [track1, track2]
    mass.player_queues.get_item.side_effect = lambda _qid, item_id: (
        track1 if item_id == "qi_1" else track2
    )

    media1 = PlayerMedia(
        uri="library://track/1",
        media_type=MediaType.TRACK,
        source_id="ma_player_1",
        queue_item_id="qi_1",
    )
    media2 = PlayerMedia(
        uri="library://track/2",
        media_type=MediaType.TRACK,
        source_id="ma_player_1",
        queue_item_id="qi_2",
    )

    mass.streams.resolve_stream_url = AsyncMock(
        side_effect=[
            "http://192.168.1.10:8097/single/session/qi_1/player.wav",
            "http://192.168.1.10:8097/single/session/qi_2/player.wav",
        ]
    )
    await player.play_media(media1)
    assert (
        mass.config.get_protocol_value("soundbridge_192_168_1_50", CONF_ENABLE_ICY_METADATA)
        == "basic"
    )
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/single/session/qi_1/player.wav",
        title="007",
        artist="Sex Mob",
        album="Sex Mob Does Bond",
        fmt="WAV",
        length_ms=155000,
    )

    await player.enqueue_next_media(media2)
    mock_client.play_url.reset_mock()

    # SoundBridge plays past 0s, then stops cleanly at EOF -> triggers Track 2 handoff.
    mock_client.position = 12
    mock_client.transport_state = "play"
    player._sync_client_state()
    mock_client.transport_state = "stop"
    player._sync_client_state()
    assert player.playback_state == PlaybackState.PLAYING
    assert len(tasks) == 1
    await tasks[0]
    mock_client.play_url.assert_awaited_once_with(
        "http://192.168.1.10:8097/single/session/qi_2/player.wav",
        title="01 - Sinfonia",
        artist="Gabriele Santini",
        album="Madama Butterfly",
        fmt="WAV",
        length_ms=240000,
    )

    # User stop clears enqueued media so stopping never auto-advances.
    await player.enqueue_next_media(media1)
    await player.stop()
    mock_client.play_url.reset_mock()
    player._sync_client_state()
    assert len(tasks) == 1
    mock_client.play_url.assert_not_awaited()
