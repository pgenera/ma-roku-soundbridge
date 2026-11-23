"""Integration tests of the Roku SoundBridge provider loaded in a real Music Assistant server."""

from __future__ import annotations

import asyncio

from music_assistant_models.enums import ProviderTier

from music_assistant.mass import MusicAssistant
from music_assistant.models.player_provider import PlayerProvider
from roku_soundbridge.provider import CONF_MANUAL_IPS, make_protocol_id
from tests import DOMAIN
from tests.test_rcp_client import FakeRcpServer


async def test_provider_loads_as_optional_community_provider(mass: MusicAssistant) -> None:
    """The server loads Roku SoundBridge as an optional (non-builtin) community provider."""
    manifest = mass.get_provider_manifest(DOMAIN)
    assert manifest.tier == ProviderTier.COMMUNITY
    assert manifest.builtin is False
    assert manifest.multi_instance is False
    assert manifest.mdns_discovery == ["_roku-rcp._tcp.local."]
    assert mass.get_provider(DOMAIN) is None


async def test_provider_setup_and_live_rcp_player_registration(mass: MusicAssistant) -> None:
    """Enabling the provider connects to a SoundBridge over RCP and registers its player."""
    rcp_server = FakeRcpServer()
    await rcp_server.start()
    try:
        step = await mass.config.setup_provider(DOMAIN)
        assert step.result is not None
        instance_id = step.result["instance_id"]
        provider = mass.get_provider(instance_id)
        assert isinstance(provider, PlayerProvider)

        host_entry = f"127.0.0.1:{rcp_server.port}"
        protocol_id = make_protocol_id("127.0.0.1", rcp_server.port)
        await mass.config.save_provider_config(
            DOMAIN,
            {CONF_MANUAL_IPS: [host_entry]},
            instance_id=instance_id,
        )

        protocol = None
        for _ in range(40):
            protocol = mass.players.get_protocol(protocol_id)
            if (
                protocol is not None
                and protocol.available
                and protocol.player is not None
                and protocol.player.volume_level == 35
            ):
                break
            await asyncio.sleep(0.1)

        assert protocol is not None
        assert protocol.available is True
        assert protocol.player is not None
        assert protocol.player.volume_level == 35
    finally:
        await rcp_server.stop()
