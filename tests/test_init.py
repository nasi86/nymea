"""Test setup and unload lifecycle for the nymea integration."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
import pytest

from custom_components.nymea import async_setup_entry, async_unload_entry


async def test_setup_completes_and_starts_config_entry_listener(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test setup returns after registering the listener with the config entry."""
    with (
        patch(
            "custom_components.nymea.maveo_box.MaveoBox",
            return_value=mock_maveo_box,
        ),
        patch(
            "custom_components.nymea.MaveoStick.add",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.nymea.Thing.add",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
    ):
        assert await async_setup_entry(hass, mock_config_entry)

    mock_maveo_box.start_notification_listener.assert_called_once_with(
        mock_config_entry
    )
    mock_maveo_box.async_close.assert_not_awaited()


async def test_setup_error_closes_hub(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test a setup connection error closes all hub resources."""
    mock_maveo_box.init_connection = AsyncMock(side_effect=OSError("offline"))

    with patch(
        "custom_components.nymea.maveo_box.MaveoBox",
        return_value=mock_maveo_box,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, mock_config_entry)

    mock_maveo_box.async_close.assert_awaited_once()


async def test_successful_unload_closes_hub(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test unload closes resources after unloading platforms."""
    mock_config_entry.runtime_data = mock_maveo_box

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=True),
    ):
        assert await async_unload_entry(hass, mock_config_entry)

    mock_maveo_box.async_close.assert_awaited_once()


async def test_failed_unload_keeps_hub_open(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test a failed platform unload leaves the still-loaded hub usable."""
    mock_config_entry.runtime_data = mock_maveo_box

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await async_unload_entry(hass, mock_config_entry)

    mock_maveo_box.async_close.assert_not_awaited()


async def test_unload_exception_keeps_hub_open(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test an unload exception does not tear down the still-loaded hub."""
    mock_config_entry.runtime_data = mock_maveo_box

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(side_effect=RuntimeError("platform unload failed")),
        ),
        pytest.raises(RuntimeError, match="platform unload failed"),
    ):
        await async_unload_entry(hass, mock_config_entry)

    mock_maveo_box.async_close.assert_not_awaited()


async def test_cleanup_error_does_not_mask_setup_error(
    hass: HomeAssistant, mock_config_entry, mock_maveo_box
) -> None:
    """Test cleanup failures preserve the original setup exception."""
    mock_maveo_box.init_connection = AsyncMock(side_effect=OSError("offline"))
    mock_maveo_box.async_close = AsyncMock(side_effect=RuntimeError("close failed"))

    with patch(
        "custom_components.nymea.maveo_box.MaveoBox",
        return_value=mock_maveo_box,
    ):
        with pytest.raises(ConfigEntryNotReady) as error:
            await async_setup_entry(hass, mock_config_entry)

    assert isinstance(error.value.__cause__, OSError)
    assert str(error.value.__cause__) == "offline"
