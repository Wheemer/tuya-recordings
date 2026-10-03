"""One device on the account."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TuyaDevice:
    """
    A device as the account's device list describes it.

    ``local_key`` is the property the streaming path needs and the RTC config
    does not carry: it keys the signaling payloads and the channel-0
    credential.
    """

    device_id: str
    name: str
    category: str
    local_key: str = field(repr=False)
    product_id: str | None = None
    online: bool = True
