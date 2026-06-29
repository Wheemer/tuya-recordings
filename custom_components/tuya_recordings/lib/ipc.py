"""Tuya IPC recording query and media transfer backend."""

from __future__ import annotations

import json
import platform
import queue
import ssl
import subprocess
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt

from .pion import read_helper_stderr, read_helper_stdout, start_pion_helper
from .recordings import best_clip_match, normalize_clip
from .webrtc import (
    WebRTCProbeSummary,
    browser_relay_candidate_list,
    mqtt_message_body,
    mqtt_message_type,
    mqtt_session_id,
    normalize_outgoing_candidate,
    p2p_envelope,
    strip_sdp_candidates,
)

NO_MEDIA_AFTER_PLAYBACK_START_TIMEOUT = 12


class TuyaIpcPlaybackError(RuntimeError):
    def __init__(self, res_code: Any) -> None:
        self.res_code = res_code
        super().__init__(f"Tuya IPC playback failed: {res_code}")


class TuyaIpcRecordingBackend:
    def __init__(
        self,
        *,
        logger: Any,
        query_timeout: int,
        playback_timeout: int,
        playback_max_timeout: int,
    ) -> None:
        self.logger = logger
        self.query_timeout = query_timeout
        self.playback_timeout = playback_timeout
        self.playback_max_timeout = playback_max_timeout

    def recordings_for_day(
        self,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        day: date,
    ) -> list[dict[str, Any]]:
        with self._session(dev_id, config, mqtt_auth) as session:
            return self._query_recordings_for_day(session, day)

    def download_clip_h264(
        self,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        start: int,
        end: int,
        h264_path: Path,
        playback_timeout: int | None = None,
        verify_clip: bool = True,
    ) -> None:
        timeout = playback_timeout or min(max((end - start) + 15, self.playback_timeout), self.playback_max_timeout)
        with self._session(dev_id, config, mqtt_auth, h264_output=h264_path, helper_timeout=timeout) as session:
            if verify_clip:
                playback_start, playback_end = self._query_playback_clip_for_session(session, start, end)
                self._start_playback_and_wait_for_media(session, playback_start, playback_end, timeout)
                return

            try:
                self._start_playback_and_wait_for_media(session, start, end, timeout)
            except TuyaIpcPlaybackError as exc:
                if exc.res_code != -23:
                    raise
                self.logger.info(
                    "Tuya IPC playback rejected cached clip bounds for %s %s-%s; querying exact camera bounds",
                    dev_id,
                    start,
                    end,
                )
                playback_start, playback_end = self._query_playback_clip_for_session(session, start, end)
                self._start_playback_and_wait_for_media(session, playback_start, playback_end, timeout)

    def _session(
        self,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        *,
        h264_output: Path | None = None,
        helper_timeout: int | None = None,
    ) -> "_IpcSession":
        return _IpcSession(
            dev_id=dev_id,
            config=config,
            mqtt_auth=mqtt_auth,
            query_timeout=self.query_timeout,
            playback_timeout=self.playback_timeout,
            h264_output=h264_output,
            helper_timeout=helper_timeout,
            logger=self.logger,
        )

    def _query_playback_clip_for_session(self, session: "_IpcSession", start: int, end: int) -> tuple[int, int]:
        query_days = [datetime.fromtimestamp(start).date()]
        query_days.extend([query_days[0] - timedelta(days=1), query_days[0] + timedelta(days=1)])

        for day in query_days:
            clips = self._query_recordings_for_day(session, day)
            match = best_clip_match(clips, start, end)
            if match is None:
                continue
            matched_start = int(match.get("start") or start)
            matched_end = int(match.get("end") or end)
            self.logger.info(
                "Primed Tuya IPC playback session for %s %s-%s with queried clip %s-%s on %s",
                session.dev_id,
                start,
                end,
                matched_start,
                matched_end,
                day,
            )
            return matched_start, matched_end

        self.logger.warning(
            "Tuya IPC playback query did not return clip %s %s-%s; trying cached clip bounds",
            session.dev_id,
            start,
            end,
        )
        return start, end

    def _query_recordings_for_day(self, session: "_IpcSession", day: date) -> list[dict[str, Any]]:
        tid = str(uuid.uuid4())
        session.publish(
            312,
            "recordQueryByDay",
            {
                "mode": "webrtc",
                "pageId": -1,
                "channel": 0,
                "event": "all",
                "year": day.year,
                "month": day.month,
                "day": day.day,
            },
            tid=tid,
        )
        clips: list[dict[str, Any]] = []
        deadline = time.time() + self.query_timeout
        while time.time() < deadline:
            try:
                payload = session.inbound.get(timeout=2 if clips else 1)
            except queue.Empty:
                if clips:
                    return clips
                continue
            if payload.get("protocol") != 312:
                continue
            data = payload.get("data") or {}
            header = data.get("header") or {}
            if header.get("sessionid") != session.session_id or header.get("type") != "recordQueryByDay":
                continue
            if header.get("tid") not in ("", tid):
                continue
            msg = data.get("msg") or {}
            if msg.get("resCode") not in (None, 0):
                if msg.get("resCode") == -23:
                    self.logger.info(
                        "Tuya IPC recording query returned no playable page for %s on %s: %s",
                        session.dev_id,
                        day,
                        msg.get("resCode"),
                    )
                    return clips
                raise RuntimeError(f"Tuya IPC recording query failed: {msg.get('resCode')}")
            for item in msg.get("files") or []:
                if normalized_clip := normalize_clip(item):
                    clips.append(normalized_clip)
            if msg.get("pageId") == msg.get("totalPageNum"):
                break
        return clips

    def _start_playback_and_wait_for_media(
        self,
        session: "_IpcSession",
        start: int,
        end: int,
        playback_timeout: int | None = None,
    ) -> None:
        session.publish(
            312,
            "playbackStart",
            {
                "mode": "webrtc",
                "operation": "start",
                "st": start,
                "ed": end,
                "playTime": start,
                "webrtc_sessionid": session.session_id,
            },
            tid=str(uuid.uuid4()),
        )
        timeout = playback_timeout or min(max((end - start) + 15, self.playback_timeout), self.playback_max_timeout)
        deadline = time.time() + timeout
        playback_started_at: float | None = None
        summary = WebRTCProbeSummary()
        self.logger.info("Started Tuya IPC recording playback for %s %s-%s session %s", session.dev_id, start, end, session.session_id)
        while time.time() < deadline:
            for event in session.drain_helper_events():
                summary.add_helper_event(event)
                if event.get("type") == "result" and int(event.get("bytes") or 0) > 0:
                    return
                if event.get("type") == "result" and int(event.get("bytes") or 0) <= 0:
                    deadline = time.time()
                    break
            if (
                playback_started_at is not None
                and summary.first_rtp <= 0
                and time.time() - playback_started_at >= NO_MEDIA_AFTER_PLAYBACK_START_TIMEOUT
            ):
                break
            try:
                payload = session.inbound.get(timeout=0.2)
            except queue.Empty:
                continue
            if payload.get("protocol") == 302 and mqtt_session_id(payload) == session.session_id and mqtt_message_type(payload) == "candidate":
                summary.add_mqtt_message("candidate")
                session.add_remote_candidates(payload, summary)
                continue
            if payload.get("protocol") != 312 or mqtt_session_id(payload) != session.session_id or mqtt_message_type(payload) != "playbackStart":
                continue
            summary.add_mqtt_message("playbackStart")
            playback_started_at = playback_started_at or time.time()
            msg = mqtt_message_body(payload)
            if msg.get("resCode") not in (None, 0):
                raise TuyaIpcPlaybackError(msg.get("resCode"))
        error_detail = f"{summary.describe()}; {session.drain_helper_errors()}"
        self.logger.warning(
            "Tuya IPC recording playback produced no media for %s %s-%s session %s: %s",
            session.dev_id,
            start,
            end,
            session.session_id,
            error_detail,
        )
        raise RuntimeError(f"Tuya IPC playback did not produce media: {error_detail}")


class _IpcSession:
    def __init__(
        self,
        *,
        dev_id: str,
        config: dict[str, Any],
        mqtt_auth: dict[str, Any],
        query_timeout: int,
        playback_timeout: int,
        h264_output: Path | None,
        helper_timeout: int | None,
        logger: Any,
    ) -> None:
        self.dev_id = dev_id
        self.config = config
        self.msid = mqtt_auth["msid"]
        self.password = mqtt_auth["password"]
        self.moto_id = config.get("motoId")
        self.protocol_version = config.get("protocolVersion") or "2.3"
        self.mqtt_client_id = mqtt_auth.get("client_id") or f"web_{self.msid}"
        self.mqtt_username = mqtt_auth.get("username") or self.mqtt_client_id
        self.mqtt_url = mqtt_auth.get("url") or "wss://m1.tuyaus.com:443/mqtt"
        self.source_topic = ((mqtt_auth.get("source_topic") or {}).get("ipc") if isinstance(mqtt_auth.get("source_topic"), dict) else "") or f"/av/u/{self.msid}"
        sink_topic = ((mqtt_auth.get("sink_topic") or {}).get("ipc") if isinstance(mqtt_auth.get("sink_topic"), dict) else "") or ""
        self.topic = (
            sink_topic.replace("{device_id}", dev_id).replace("moto_id", str(self.moto_id))
            if sink_topic
            else f"/av/moto/{self.moto_id}/u/{dev_id}"
        )
        self.session_id = str(uuid.uuid4())
        self.query_timeout = query_timeout
        self.logger = logger
        self.inbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self.subscribed = threading.Event()
        self.helper_events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.helper_errors: queue.Queue[str] = queue.Queue()
        self.proc = start_pion_helper(
            pion_helper_path(),
            config,
            query_timeout=query_timeout,
            playback_timeout=playback_timeout,
            h264_output=h264_output,
            helper_timeout=helper_timeout,
            process_module=subprocess,
        )
        parsed_url = urlsplit(self.mqtt_url)
        transport = "websockets" if parsed_url.scheme in {"ws", "wss"} else "tcp"
        self.mqtt_client = mqtt.Client(client_id=self.mqtt_client_id, transport=transport, protocol=mqtt.MQTTv311)

    def __enter__(self) -> "_IpcSession":
        try:
            return self._open()
        except Exception:
            self.close()
            raise

    def _open(self) -> "_IpcSession":
        assert self.proc.stdout is not None
        assert self.proc.stdin is not None
        offer = json.loads(self.proc.stdout.readline())
        if "sdp" not in offer:
            raise RuntimeError(f"Tuya IPC helper returned invalid offer: {offer}")
        threading.Thread(target=read_helper_stdout, args=(self.proc, self.helper_events), daemon=True).start()
        threading.Thread(target=read_helper_stderr, args=(self.proc, self.helper_errors), daemon=True).start()
        offer["candidates"] = browser_relay_candidate_list(offer.get("candidates") or [])

        self.mqtt_client.username_pw_set(self.mqtt_username, self.password)
        parsed_url = urlsplit(self.mqtt_url)
        if parsed_url.scheme in {"ws", "wss"}:
            self.mqtt_client.ws_set_options(path=parsed_url.path or "/mqtt")
        if parsed_url.scheme in {"ssl", "tls", "mqtts", "wss"}:
            self.mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_subscribe = self._on_subscribe
        self.mqtt_client.on_message = self._on_message
        host = parsed_url.hostname or "m1.tuyaus.com"
        port = parsed_url.port or (443 if parsed_url.scheme in {"wss"} else 8883 if parsed_url.scheme in {"ssl", "tls", "mqtts"} else 1883)
        self.mqtt_client.connect(host, port, keepalive=60)
        self.mqtt_client.loop_start()
        if not self.subscribed.wait(10):
            raise RuntimeError("Tuya IPC MQTT subscribe did not complete")

        self.publish(
            302,
            "offer",
            {
                "sdp": strip_sdp_candidates(offer["sdp"]),
                "auth": self.config.get("auth"),
                "mode": "webrtc",
                "datachannel_enable": False,
                "token": (self.config.get("p2pConfig") or {}).get("ices") or self.config.get("iceServers"),
                "stream_type": 0,
                "replay": {"is_replay": 1},
            },
        )
        for candidate in offer.get("candidates") or []:
            self.publish(302, "candidate", {"candidate": normalize_outgoing_candidate(candidate), "mode": "webrtc"})

        answer_sdp = self.wait_for_answer()
        try:
            self.proc.stdin.write(json.dumps({"type": "answer", "sdp": answer_sdp}) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as err:
            raise RuntimeError(f"Tuya IPC helper exited after answer: {self.drain_helper_errors()}") from err
        self.wait_until_connected()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: dict[str, Any], rc: int) -> None:
        if rc == 0:
            client.subscribe(self.source_topic, qos=1)

    def _on_subscribe(self, client: mqtt.Client, userdata: Any, mid: int, granted_qos: Any) -> None:
        self.subscribed.set()

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        try:
            payload = json.loads(message.payload.decode())
        except Exception:
            return
        self.inbound.put(payload)

    def publish(self, protocol: int, msg_type: str, msg: dict[str, Any], tid: str = "") -> None:
        payload = p2p_envelope(self.msid, self.dev_id, self.moto_id, self.session_id, self.protocol_version, protocol, msg_type, msg, tid=tid)
        mqtt_publish(self.mqtt_client, self.topic, payload)

    def wait_for_answer(self) -> str:
        deadline = time.time() + self.query_timeout
        while time.time() < deadline:
            self.drain_helper_events()
            try:
                payload = self.inbound.get(timeout=0.1)
            except queue.Empty:
                continue
            if payload.get("protocol") != 302 or mqtt_session_id(payload) != self.session_id:
                continue
            if mqtt_message_type(payload) == "answer":
                answer = mqtt_message_body(payload).get("sdp") or mqtt_message_body(payload).get("value")
                if answer:
                    return answer
            if mqtt_message_type(payload) == "disconnect":
                raise RuntimeError(f"Tuya camera disconnected before answer: {mqtt_message_body(payload)}")
        raise RuntimeError("Tuya camera did not answer recording offer")

    def wait_until_connected(self) -> None:
        deadline = time.time() + self.query_timeout
        summary = WebRTCProbeSummary()
        self.logger.debug("Waiting for Tuya IPC WebRTC connection for %s session %s", self.dev_id, self.session_id)
        while time.time() < deadline:
            for event in self.drain_helper_events():
                summary.add_helper_event(event)
                if event.get("type") == "connectionState" and event.get("state") == "connected":
                    self.logger.info("Tuya IPC WebRTC connected for %s session %s: %s", self.dev_id, self.session_id, summary.describe())
                    return
            try:
                payload = self.inbound.get(timeout=0.2)
            except queue.Empty:
                continue
            if payload.get("protocol") != 302 or mqtt_session_id(payload) != self.session_id:
                continue
            summary.add_mqtt_message(mqtt_message_type(payload))
            if mqtt_message_type(payload) == "candidate":
                self.add_remote_candidates(payload, summary)
        error_detail = f"{summary.describe()}; {self.drain_helper_errors()}"
        self.logger.warning("Tuya IPC WebRTC did not connect for %s session %s: %s", self.dev_id, self.session_id, error_detail)
        raise RuntimeError(f"Tuya IPC WebRTC session did not connect: {error_detail}")

    def add_remote_candidates(self, payload: dict[str, Any], summary: WebRTCProbeSummary) -> None:
        assert self.proc.stdin is not None
        msg = mqtt_message_body(payload)
        candidates = msg.get("candidates") if isinstance(msg.get("candidates"), list) else [msg.get("candidate")]
        for candidate in candidates:
            if not candidate:
                continue
            summary.remote_candidates += 1
            try:
                self.proc.stdin.write(json.dumps({"type": "candidate", "candidate": str(candidate).removeprefix("a=")}) + "\n")
                self.proc.stdin.flush()
                summary.remote_candidates_added += 1
            except BrokenPipeError as err:
                raise RuntimeError(
                    "Tuya IPC WebRTC helper exited while adding candidate: "
                    f"{summary.describe()}; {self.drain_helper_errors()}"
                ) from err

    def drain_helper_events(self) -> list[dict[str, Any]]:
        return drain_helper_events(
            self.helper_events,
            self.mqtt_client,
            self.topic,
            self.msid,
            self.dev_id,
            self.moto_id,
            self.session_id,
            self.protocol_version,
        )

    def drain_helper_errors(self) -> str:
        return drain_helper_errors(self.helper_errors)


def mqtt_publish(client: mqtt.Client, topic: str, payload: dict[str, Any]) -> None:
    client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)


def drain_helper_errors(errors: queue.Queue[str]) -> str:
    messages: list[str] = []
    while True:
        try:
            messages.append(errors.get_nowait())
        except queue.Empty:
            break
    return "; ".join(message for message in messages if message) or "no helper stderr"


def drain_helper_events(
    events: queue.Queue[dict[str, Any]],
    mqtt_client: mqtt.Client,
    topic: str,
    msid: str,
    dev_id: str,
    moto_id: str,
    session_id: str,
    protocol_version: str,
) -> list[dict[str, Any]]:
    other_events: list[dict[str, Any]] = []
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return other_events
        if event.get("type") == "localCandidate":
            candidate = str(event.get("candidate") or "")
            if candidate:
                mqtt_publish(
                    mqtt_client,
                    topic,
                    p2p_envelope(
                        msid,
                        dev_id,
                        moto_id,
                        session_id,
                        protocol_version,
                        302,
                        "candidate",
                        {"candidate": normalize_outgoing_candidate(candidate), "mode": "webrtc"},
                    ),
                )
        elif event.get("type") != "localCandidateEnd":
            other_events.append(event)


def pion_helper_path() -> Path:
    base_path = Path(__file__).parents[1]
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armv7",
        "armv6l": "armv6",
    }.get(machine, machine)

    candidates = [
        base_path / f"pion_offer_{system}_{arch}",
        base_path / "pion_offer",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]
