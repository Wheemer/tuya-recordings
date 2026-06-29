from __future__ import annotations

import queue
from datetime import date

from custom_components.tuya_recordings.lib.ipc import TuyaIpcRecordingBackend


class FakeLogger:
    def info(self, *args):
        pass


class FakeSession:
    dev_id = "camera"
    session_id = "session"

    def __init__(self, payload):
        self.inbound = queue.Queue()
        self.inbound.put(payload)
        self.published = []

    def publish(self, protocol, msg_type, msg, tid=""):
        self.published.append((protocol, msg_type, msg, tid))


def test_recording_query_rescode_minus_23_is_empty_day():
    backend = TuyaIpcRecordingBackend(logger=FakeLogger(), query_timeout=1, playback_timeout=1, playback_max_timeout=1)
    session = FakeSession(
        {
            "protocol": 312,
            "data": {
                "header": {
                    "sessionid": "session",
                    "type": "recordQueryByDay",
                },
                "msg": {
                    "resCode": -23,
                },
            },
        }
    )

    clips = backend._query_recordings_for_day(session, date(2026, 6, 28))

    assert clips == []
