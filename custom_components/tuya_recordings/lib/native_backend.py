"""Backend boundary for in-process APK-native Tuya SD-card recording access."""

from __future__ import annotations

import io
import queue
import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, UnidentifiedImageError

from .commands import CAMERA_COMMANDS, CameraWorkCancelled, Cancellation
from .native_mux import (
    NativePlaybackBrowserStreamMuxer,
    NativePlaybackInteractiveBrowserMuxer,
    NativePlaybackMuxer,
)
from .native_playback import (
    NativePlaybackError,
    NativePlaybackSessionRunner,
    NativePlaybackUnavailable,
    native_playback_request,
)
from .native_provider import NativeCameraConfigAuthError
from .native_session import (
    NativeCameraSessionConfig,
    NativePlaybackRequest,
)
from .native_thumbnail import NativePlaybackThumbnailExtractor
from .recordings import normalize_clip


class RecordingBackendError(RuntimeError):
    """The selected recordings backend cannot complete the requested work."""


class RecordingBackendUnavailable(RecordingBackendError):
    """The requested backend is not usable for this device or install."""


class RecordingBackendAuthError(RecordingBackendError):
    """The Smart Life app session must be renewed."""


_MAX_THUMBNAIL_PIXELS = 4096 * 4096


class RecordingBackend(Protocol):
    """Synchronous backend contract used from HA executor jobs."""

    name: str
    apk_native: bool
    clip_playback_available: bool

    def recordings_for_day(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        day: date,
    ) -> list[dict[str, Any]]:
        """Return normalized clips for one camera day."""

    def recordings_for_days(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        days: list[date],
        *,
        cancel_event: Cancellation | threading.Event | None = None,
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        """Return normalized clips for newest-first day batches."""

    def receive_clip(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        """Receive selected recording media from an APK playback stream."""

    def stream_clip(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        chunk_callback: Callable[[bytes], None],
        **kwargs: Any,
    ) -> None:
        """Stream selected recording media as fragmented MP4 chunks."""

    def stream_timeline(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        request: NativePlaybackRequest,
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
        chunk_callback: Callable[[bytes], None],
        **kwargs: Any,
    ) -> None:
        """Stream and control one APK-style timeline session."""

    def receive_thumbnail(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
    ) -> None:
        """Download the selected clip's native SD-card JPEG."""

    def receive_thumbnail_from_stream(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        """Decode one JPEG from the selected clip's playback stream."""

    def direct_thumbnail_available(self, dev_id: str) -> bool:
        """Return whether camera firmware advertises direct JPEG transfer."""


class NativeCameraSession(Protocol):
    """Operations an opened APK-native camera session exposes."""

    def recordings_for_day(self, day: date) -> list[dict[str, Any]]:
        """Return raw or normalized recordings for one day."""

    def recordings_for_days(self, days: list[date]) -> list[tuple[date, list[dict[str, Any]]]]:
        """Return several days over one connected native camera session."""

    def start_playback_stream(
        self,
        request: NativePlaybackRequest,
        event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Start APK playback and receive its video/audio callback stream."""

    def download_thumbnail(self, start: int, end: int) -> bytes:
        """Download the recording JPEG without opening media playback."""

@dataclass(slots=True)
class NativeBackendNotConfigured:
    """Fail-closed backend used when native session prerequisites are missing."""

    reason: str = "APK-native camera session is not configured"
    name = "apk-native-not-configured"
    apk_native = True
    clip_playback_available = False

    def recordings_for_day(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise RecordingBackendUnavailable(self.reason)

    def recordings_for_days(self, *args: Any, **kwargs: Any) -> list[tuple[date, list[dict[str, Any]]]]:
        raise RecordingBackendUnavailable(self.reason)

    def receive_clip(self, *args: Any, **kwargs: Any) -> None:
        raise RecordingBackendUnavailable(self.reason)

    def stream_clip(self, *args: Any, **kwargs: Any) -> None:
        raise RecordingBackendUnavailable(self.reason)

    def stream_timeline(self, *args: Any, **kwargs: Any) -> None:
        raise RecordingBackendUnavailable(self.reason)

    def receive_thumbnail(self, *args: Any, **kwargs: Any) -> None:
        raise RecordingBackendUnavailable(self.reason)

    def receive_thumbnail_from_stream(self, *args: Any, **kwargs: Any) -> None:
        raise RecordingBackendUnavailable(self.reason)

    def direct_thumbnail_available(self, dev_id: str) -> bool:
        del dev_id
        return False


@dataclass(frozen=True, slots=True)
class NativeCameraSessionProvider:
    """Provider shape for APK-native session bootstrap.

    `session_config` must be created from Smart Life app-session camera config
    plus the Tuya device local key. `open_session` returns the in-process relay
    session implementing the native SDK operations below.
    """

    session_config: Callable[[str], NativeCameraSessionConfig]
    open_session: Callable[[NativeCameraSessionConfig, Cancellation | threading.Event | None], NativeCameraSession]
    close: Callable[[], None] | None = None


class ApkNativeRecordingBackend:
    """APK-native catalog, thumbnail, and playback backend."""

    name = "apk-native"
    apk_native = True
    clip_playback_available = True

    def __init__(
        self,
        provider: NativeCameraSessionProvider,
        *,
        muxer_factory: Callable[[Path], NativePlaybackMuxer] = NativePlaybackMuxer,
        stream_muxer_factory: Callable[[Callable[[bytes], None]], NativePlaybackBrowserStreamMuxer] = NativePlaybackBrowserStreamMuxer,
        interactive_muxer_factory: Callable[
            [Callable[[bytes], None]], NativePlaybackInteractiveBrowserMuxer
        ] = NativePlaybackInteractiveBrowserMuxer,
        thumbnail_extractor_factory: Callable[[Path], NativePlaybackThumbnailExtractor] = NativePlaybackThumbnailExtractor,
    ) -> None:
        self._provider = provider
        self._muxer_factory = muxer_factory
        self._stream_muxer_factory = stream_muxer_factory
        self._interactive_muxer_factory = interactive_muxer_factory
        self._thumbnail_extractor_factory = thumbnail_extractor_factory

    def close(self) -> None:
        """Release reusable native camera workers owned by this backend."""
        if self._provider.close is not None:
            self._provider.close()

    def direct_thumbnail_available(self, dev_id: str) -> bool:
        """Match Smart Life's event-timeline gate for recording JPEGs."""
        try:
            return self._provider.session_config(dev_id).event_catalog_supported
        except NativeCameraConfigAuthError as err:
            raise RecordingBackendAuthError(str(err)) from err
        except RecordingBackendError:
            raise
        except Exception as err:
            raise RecordingBackendUnavailable(
                f"APK-native camera capability lookup failed for {dev_id}: "
                f"{type(err).__name__}"
            ) from err

    def recordings_for_day(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        day: date,
    ) -> list[dict[str, Any]]:
        del config, auth
        event_catalog = self.direct_thumbnail_available(dev_id)
        with self._open_session(dev_id, None) as session:
            ordinary = _normalize_recordings(
                self._call_required(session, "recordings_for_day", "getRecordFragmentsByDay")(day),
                catalog_day=day,
            )
            if not event_catalog:
                return ordinary
            events = _normalize_recordings(
                self._call_required(
                    session,
                    "event_recordings_for_day",
                    "getEventRecordFragmentsByDay",
                )(day),
                catalog_day=day,
            )
            return events or ordinary

    def recordings_for_days(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        days: list[date],
        *,
        cancel_event: Cancellation | threading.Event | None = None,
    ) -> list[tuple[date, list[dict[str, Any]]]]:
        del config, auth
        if days != newest_first_days(days):
            raise ValueError("Recording catalog days must be unique and newest first")
        event_catalog = self.direct_thumbnail_available(dev_id)
        results: list[tuple[date, list[dict[str, Any]]]] = []
        with self._open_session(dev_id, cancel_event) as session:
            query_many = getattr(session, "recordings_for_days", None)
            if callable(query_many):
                queried = query_many(days)
                if not isinstance(queried, list) or len(queried) != len(days):
                    raise RecordingBackendError(
                        "APK-native catalog batch returned an invalid response"
                    )
                for expected_day, item in zip(days, queried, strict=True):
                    if (
                        not isinstance(item, tuple)
                        or len(item) != 2
                        or item[0] != expected_day
                    ):
                        raise RecordingBackendError(
                            "APK-native catalog batch returned days out of order"
                        )
                    ordinary = _normalize_recordings(
                        item[1], catalog_day=expected_day
                    )
                    if event_catalog:
                        events = _normalize_recordings(
                            self._call_required(
                                session,
                                "event_recordings_for_day",
                                "getEventRecordFragmentsByDay",
                            )(expected_day),
                            catalog_day=expected_day,
                        )
                        results.append((expected_day, events or ordinary))
                    else:
                        results.append((expected_day, ordinary))
                return results
            query = self._call_required(session, "recordings_for_day", "getRecordFragmentsByDay")
            for day in days:
                if _cancelled(cancel_event):
                    raise RecordingBackendUnavailable("APK-native catalog was cancelled")
                ordinary = _normalize_recordings(query(day), catalog_day=day)
                if event_catalog:
                    events = _normalize_recordings(
                        self._call_required(
                            session,
                            "event_recordings_for_day",
                            "getEventRecordFragmentsByDay",
                        )(day),
                        catalog_day=day,
                    )
                    results.append((day, events or ordinary))
                else:
                    results.append((day, ordinary))
        return results

    def receive_clip(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        del config, auth
        native_playback_request(start, end, **kwargs)
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        with self._open_session(
            dev_id,
            kwargs.get("cancel_event"),
            playback=True,
        ) as session:
            start_stream = self._call_required(session, "start_playback_stream", "startPlayBack")
            try:
                _runner(start_stream, self._muxer_factory, self._stream_muxer_factory).receive_clip(
                    start,
                    end,
                    output_path,
                    **kwargs,
                )
            except NativePlaybackError as err:
                raise _map_native_playback_error(err) from err

    def stream_clip(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        chunk_callback: Callable[[bytes], None],
        **kwargs: Any,
    ) -> None:
        del config, auth
        native_playback_request(start, end, **kwargs)
        with self._open_session(
            dev_id,
            kwargs.get("cancel_event"),
            playback=True,
        ) as session:
            start_stream = self._call_required(session, "start_playback_stream", "startPlayBack")
            try:
                _runner(start_stream, self._muxer_factory, self._stream_muxer_factory).stream_clip(
                    start,
                    end,
                    chunk_callback,
                    **kwargs,
                )
            except NativePlaybackError as err:
                raise _map_native_playback_error(err) from err

    def stream_timeline(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        request: NativePlaybackRequest,
        commands: queue.Queue[tuple[str, NativePlaybackRequest | None]],
        chunk_callback: Callable[[bytes], None],
        **kwargs: Any,
    ) -> None:
        """Run one persistent Smart Life timeline camera session."""
        del config, auth
        request = request.validate()
        with self._open_session(
            dev_id,
            kwargs.get("cancel_event"),
            playback=True,
        ) as session:
            start = self._call_required(
                session,
                "start_interactive_playback_stream",
                "startPlayBack",
            )
            try:
                with self._interactive_muxer_factory(chunk_callback) as muxer:
                    start(request, commands, event_handler=muxer.handle_event)
            except NativePlaybackError as err:
                raise _map_native_playback_error(err) from err

    def receive_thumbnail(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
    ) -> None:
        del config, auth
        if not self.direct_thumbnail_available(dev_id):
            raise RecordingBackendUnavailable(
                "Camera firmware does not advertise direct recording JPEG transfer"
            )
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
        output_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
        with self._open_session(dev_id, None) as session:
            download = self._call_required(
                session,
                "download_thumbnail",
                "downloadPlaybackImage",
            )
            try:
                image = download(start, end)
                _validate_thumbnail_jpeg(image)
                temporary.write_bytes(image)
                temporary.replace(output_path)
            finally:
                temporary.unlink(missing_ok=True)

    def receive_thumbnail_from_stream(
        self,
        dev_id: str,
        config: dict[str, Any],
        auth: dict[str, Any],
        start: int,
        end: int,
        output_path: Path,
        **kwargs: Any,
    ) -> None:
        """Decode one JPEG from a bounded prefix of native playback."""
        del config, auth
        native_playback_request(start, end, **kwargs)
        output_path = Path(output_path)
        with self._open_session(dev_id, kwargs.get("cancel_event")) as session:
            start_stream = self._call_required(
                session, "start_playback_stream", "startPlayBack"
            )
            try:
                _runner(
                    start_stream,
                    self._muxer_factory,
                    self._stream_muxer_factory,
                    self._thumbnail_extractor_factory,
                ).receive_thumbnail(start, end, output_path, **kwargs)
                _validate_thumbnail_jpeg(output_path.read_bytes())
            except NativePlaybackError as err:
                output_path.unlink(missing_ok=True)
                raise _map_native_playback_error(err) from err
            except BaseException:
                output_path.unlink(missing_ok=True)
                raise
    @staticmethod
    def _unsupported(session_config: NativeCameraSessionConfig, operation: str) -> None:
        raise RecordingBackendUnavailable(
            f"APK-native {operation} is not implemented for {session_config.dev_id}; "
            "the Smart Life native camera session is unavailable"
        )

    @contextmanager
    def _open_session(
        self,
        dev_id: str,
        cancel_event: Cancellation | threading.Event | None,
        *,
        playback: bool = False,
    ):
        if _cancelled(cancel_event):
            raise RecordingBackendUnavailable("APK-native camera work was cancelled")
        with CAMERA_COMMANDS.slot(cancel_event, playback=playback) as queued_cancel:
            session_config: NativeCameraSessionConfig | None = None
            try:
                session_config = self._provider.session_config(dev_id).validate()
                session = self._provider.open_session(session_config, queued_cancel)
            except NativeCameraConfigAuthError as err:
                raise RecordingBackendAuthError(str(err)) from err
            except RecordingBackendError:
                raise
            except Exception as err:
                raise RecordingBackendUnavailable(
                    f"APK-native session could not open for {dev_id}: {type(err).__name__}"
                ) from err
            assert session_config is not None
            if session is None:
                self._unsupported(session_config, "native camera session")
            entered = None
            try:
                if hasattr(session, "__enter__") and hasattr(session, "__exit__"):
                    entered = session.__enter__()
                    yield entered
                else:
                    yield session
            finally:
                target = entered if entered is not None else session
                try:
                    if entered is not None:
                        session.__exit__(None, None, None)
                    elif hasattr(target, "close"):
                        target.close()
                    elif hasattr(target, "disconnect"):
                        target.disconnect()
                except Exception:
                    pass

    @staticmethod
    def _call_required(session: Any, name: str, operation: str) -> Callable[..., Any]:
        call = getattr(session, name, None)
        if not callable(call):
            raise RecordingBackendUnavailable(
                f"APK-native {operation} is not implemented by the camera session"
            )
        return call


def _validate_thumbnail_jpeg(image: bytes) -> None:
    if (
        not isinstance(image, bytes)
        or len(image) < 4
        or not image.startswith(b"\xff\xd8")
        or not image.endswith(b"\xff\xd9")
    ):
        raise RecordingBackendError(
            "APK-native thumbnail response is not a valid JPEG"
        )
    try:
        with Image.open(io.BytesIO(image)) as thumbnail:
            if thumbnail.format != "JPEG":
                raise RecordingBackendError(
                    "APK-native thumbnail response is not a JPEG"
                )
            width, height = thumbnail.size
            if width <= 0 or height <= 0 or width * height > _MAX_THUMBNAIL_PIXELS:
                raise RecordingBackendError(
                    "APK-native thumbnail dimensions exceed the safety limit"
                )
            thumbnail.verify()
    except (UnidentifiedImageError, OSError) as err:
        raise RecordingBackendError(
            "APK-native thumbnail response could not be decoded"
        ) from err


def ensure_backend_available(backend: RecordingBackend) -> None:
    if not getattr(backend, "apk_native", False):
        raise RecordingBackendUnavailable(
            "Tuya Recordings is not using an APK-native recordings backend"
        )


def newest_first_days(days: Iterable[date]) -> list[date]:
    day_list = list(days)
    if any(type(day) is not date for day in day_list):
        raise ValueError("Recording catalog days must be date objects")
    return sorted(set(day_list), reverse=True)


def _normalize_recordings(
    items: Any, *, catalog_day: date | None = None
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise RecordingBackendError("APK-native catalog returned a non-list response")
    clips: list[dict[str, Any]] = []
    for item in items:
        if normalized := normalize_clip(item, catalog_day=catalog_day):
            clips.append(normalized)
    return clips


def _cancelled(cancel_event: Cancellation | threading.Event | None) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _runner(
    start_stream: Callable[..., None],
    muxer_factory: Callable[[Path], NativePlaybackMuxer],
    stream_muxer_factory: Callable[[Callable[[bytes], None]], NativePlaybackBrowserStreamMuxer],
    thumbnail_extractor_factory: Callable[[Path], NativePlaybackThumbnailExtractor] = NativePlaybackThumbnailExtractor,
) -> NativePlaybackSessionRunner:
    return NativePlaybackSessionRunner(
        start_stream,
        muxer_factory=muxer_factory,
        stream_muxer_factory=stream_muxer_factory,
        thumbnail_extractor_factory=thumbnail_extractor_factory,
    )


def _map_native_playback_error(err: NativePlaybackError) -> RecordingBackendError:
    if isinstance(err, NativePlaybackUnavailable):
        return RecordingBackendUnavailable(str(err))
    if "cancelled" in str(err).lower() or "canceled" in str(err).lower():
        return CameraWorkCancelled(str(err))
    return RecordingBackendError(str(err))
