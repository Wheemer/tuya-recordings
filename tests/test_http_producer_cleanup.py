import asyncio
import threading
from types import SimpleNamespace

import pytest

from custom_components.tuya_recordings import http as recordings_http


@pytest.mark.parametrize(
    "failure", ["prepare", "first_cancel", "body_cancel", "replaced", "deadline"]
)
def test_native_http_joins_producer_before_return(monkeypatch, failure):
    async def exercise():
        stop = threading.Event()
        entered = threading.Event()
        stopping = threading.Event()
        release = threading.Event()
        exited = threading.Event()
        body_wait = asyncio.Event()
        jobs = []

        async def executor(fn):
            job = asyncio.create_task(asyncio.to_thread(fn))
            jobs.append(job)
            return await job

        def stream(*args, **kwargs):
            try:
                if failure in {"prepare", "body_cancel"}:
                    args[3](b"fixture")
                entered.set()
                assert stop.wait(3), "HTTP cleanup did not signal producer"
                stopping.set()
                assert release.wait(3), "test did not release teardown"
            finally:
                exited.set()

        class Response:
            def __init__(self, **kwargs):
                pass

            async def prepare(self, request):
                if failure == "prepare":
                    raise ConnectionResetError("offline prepare failure")

            async def write(self, chunk):
                body_wait.set()

            async def write_eof(self):
                pass

        monkeypatch.setattr(recordings_http.web, "StreamResponse", Response)
        view = recordings_http.TuyaRecordingsPlaybackView(
            SimpleNamespace(async_add_executor_job=executor)
        )
        task = asyncio.create_task(view._stream_native_clip(
            None, SimpleNamespace(stream_clip=stream), "fixture", 100, 110, 100,
            stop_event=stop,
        ))
        deadline = None

        async def wait_thread(event):
            async with asyncio.timeout(2):
                while not event.is_set():
                    await asyncio.sleep(0.001)

        try:
            await wait_thread(entered)
            if failure == "body_cancel":
                await asyncio.wait_for(body_wait.wait(), 2)
                await asyncio.sleep(0)
            if failure == "replaced":
                stop.set()
            elif failure == "deadline":
                deadline = asyncio.create_task(asyncio.wait_for(task, 0.02))
            elif failure != "prepare":
                task.cancel()
            await wait_thread(stopping)
            assert not task.done(), "HTTP owner returned before producer teardown"
            assert not exited.is_set()
            if deadline is not None:
                assert not deadline.done(), "deadline bypassed producer teardown"
            if failure in {"first_cancel", "body_cancel"}:
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "repeated cancellation released ownership"
            release.set()
            error = (
                ConnectionResetError if failure == "prepare"
                else TimeoutError if failure == "deadline"
                else recordings_http.CameraWorkCancelled if failure == "replaced"
                else asyncio.CancelledError
            )
            with pytest.raises(error):
                await asyncio.wait_for(deadline if deadline is not None else task, 2)
            assert stop.is_set()
            assert exited.is_set()
            assert all(job.done() and not job.cancelled() for job in jobs)
            assert len(jobs) == 1
        finally:
            stop.set()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if deadline is not None:
                await asyncio.gather(deadline, return_exceptions=True)
            await asyncio.gather(*jobs, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("result", ["empty", "error", "video"])
def test_native_http_terminal_results(monkeypatch, result):
    async def exercise():
        stop = threading.Event()
        exited = threading.Event()
        written = []

        def stream(*args, **kwargs):
            try:
                if result == "error":
                    raise ValueError("offline producer failure")
                if result == "video":
                    args[3](b"video")
            finally:
                exited.set()

        class Response:
            def __init__(self, **kwargs):
                pass

            async def prepare(self, request):
                pass

            async def write(self, chunk):
                written.append(chunk)

            async def write_eof(self):
                written.append(b"eof")

        monkeypatch.setattr(recordings_http.web, "StreamResponse", Response)
        view = recordings_http.TuyaRecordingsPlaybackView(
            SimpleNamespace(async_add_executor_job=asyncio.to_thread)
        )
        request = view._stream_native_clip(
            None, SimpleNamespace(stream_clip=stream), "fixture", 100, 110, 100,
            stop_event=stop,
        )
        if result == "video":
            await asyncio.wait_for(request, 2)
            assert written == [b"video", b"eof"]
        else:
            error = ValueError if result == "error" else recordings_http.web.HTTPServiceUnavailable
            with pytest.raises(error):
                await asyncio.wait_for(request, 2)
        assert stop.is_set()
        assert exited.is_set()

    asyncio.run(exercise())
