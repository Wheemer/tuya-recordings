"""Remux native callback packets with their camera clock; never encode frames."""

from contextlib import ExitStack
from dataclasses import dataclass
from fractions import Fraction
import io
import logging
from pathlib import Path
import struct

import av

from .commands import _WORK_CANCEL, CameraWorkCancelled

PACKET_HEADER = struct.Struct(">BBQQI")
MAX_PACKET_BYTES = 8 * 1024 * 1024
RAW_AUDIO_FORMATS = {"pcm_s16le": "s16le", "g711u": "mulaw", "g711a": "alaw"}
_LOGGER = logging.getLogger(__name__)
_SD_CLOCK_GAP = Fraction(1050, 1000)


@dataclass
class _ClockTrack:
    source: Fraction
    presented: Fraction
    interval: Fraction
    offset: Fraction
    epoch: int
    scale: int


class NativePresentationClock:
    """Re-anchor discontinuous SD clocks without changing encoded media.

    CameraSDK's SD no-drop dispatcher re-anchors a delay over 1049 ms using
    its audio clock and frame cadence; its synchronizer also resets backwards
    clocks. Share the new epoch between tracks, allowing queued old-fragment
    packets on the other track to finish first.
    """

    def __init__(self):
        self._tracks = {}
        self._epoch = 0
        self._offset = Fraction(0)
        self._latest_source = None

    def map(self, kind, timestamp, scale, *, duration=None):
        if (type(timestamp) is not int or not 0 <= timestamp < 1 << 64
                or type(scale) is not int or not 0 < scale <= 1_000_000):
            raise ValueError("Native packet has invalid camera timing")
        stamp = Fraction(timestamp, scale)
        previous = self._tracks.get(kind)
        interval = duration or (previous.interval if previous else Fraction(1, 25))
        offset = previous.offset if previous else Fraction(0)
        epoch = previous.epoch if previous else 0
        delta = stamp - previous.source if previous else None
        if previous and delta is not None and 0 < delta < _SD_CLOCK_GAP:
            interval = duration or delta
        elif previous:
            if epoch != self._epoch and self._latest_source is not None and abs(stamp - self._latest_source) < _SD_CLOCK_GAP:
                offset, epoch = self._offset, self._epoch
            else:
                audio = self._tracks.get(2)
                end = audio.presented + audio.interval if audio else None
                # Use the audio sample clock as master, rather than stretching
                # a final video frame and introducing a hole in copied audio.
                if end is None or end <= max(track.presented for track in self._tracks.values()):
                    end = max(track.presented + track.interval for track in self._tracks.values())
                self._epoch += 1
                offset = self._offset = end - stamp
                epoch = self._epoch
                self._latest_source = stamp
                if epoch <= 8:
                    _LOGGER.debug(
                        "Native presentation clock reset: track=%s epoch=%s previous=%s/%s source=%s/%s delta_seconds=%s",
                        kind, epoch, previous.source.numerator, previous.source.denominator,
                        timestamp, scale, float(delta),
                    )
        elif self._epoch and self._latest_source is not None and abs(stamp - self._latest_source) < _SD_CLOCK_GAP:
            offset, epoch = self._offset, self._epoch
        if epoch == self._epoch and previous and previous.epoch == epoch:
            # A late joining track may have moved the shared synchronizer
            # forward since this track's last packet. Adopt that same reset.
            offset = self._offset
        presented = stamp + offset
        if previous and presented <= previous.presented:
            # Old-fragment frames can finish after the other track has already
            # started a new epoch. Re-anchor both tracks at the completed tail;
            # do not emit backwards MP4 time or fail a valid SDK transition.
            presented = previous.presented + previous.interval
            offset = presented - stamp
            self._offset = offset
            epoch = self._epoch
        output_scale = previous.scale if previous else scale
        value = round(presented * output_scale)
        if not 0 <= value < 1 << 64:
            raise ValueError("Native presentation timestamp is outside its wire range")
        self._tracks[kind] = _ClockTrack(stamp, presented, interval, offset, epoch, output_scale)
        if epoch == self._epoch:
            self._latest_source = stamp
        return value, output_scale


class _ChunkWriter(io.RawIOBase):
    def __init__(self, callback):
        self.callback = callback
        self.enabled = True

    def writable(self):
        return True

    def write(self, data):
        if self.enabled:
            self.callback(bytes(data))
        return len(data)


class TimestampedVideoMuxer:
    """Copy timestamped H264 access units into live fMP4 fragments."""

    def __init__(self, first_packet, callback):
        self.origin = None
        self.pending = None
        self.pending_pts = None
        self.interval = 40000
        self.source = av.open(io.BytesIO(first_packet), format="h264")
        self.writer = _ChunkWriter(callback)
        self.target = None
        try:
            if self.source.streams.video[0].codec_context.has_b_frames:
                raise ValueError("Native recording has B frames but no decode timestamps")
            self.target = av.open(self.writer, "w", format="mp4", buffer_size=1024,
                options={"movflags": "frag_every_frame+empty_moov+default_base_moof", "flush_packets": "1"})
            self.stream = self.target.add_stream_from_template(self.source.streams.video[0])
        except BaseException as error:
            try:
                self.close(flush=False)
            except Exception as cleanup_error:
                error.add_note(
                    f"Packet mux cleanup also failed: {type(cleanup_error).__name__}"
                )
            raise

    def push(self, payload, timestamp, scale, keyframe):
        stamp = Fraction(timestamp, scale)
        if self.origin is None:
            self.origin = stamp
        packet = av.Packet(payload)
        packet.stream = self.stream
        packet.is_keyframe = bool(keyframe)
        packet.time_base = Fraction(1, 1_000_000)
        pts = round((stamp - self.origin) * 1_000_000)
        packet.pts = packet.dts = pts
        if self.pending is not None:
            interval = pts - self.pending_pts
            if interval <= 0:
                raise ValueError("Native video timestamps are not increasing")
            if interval > 0x7fffffff:
                raise ValueError(f"Native packet duration exceeds the MP4 limit: duration_us={interval}")
            self.pending.duration = self.interval = interval
            self.target.mux(self.pending)
        self.pending = packet
        self.pending_pts = pts

    def close(self, *, flush=True):
        target, self.target = self.target, None
        if not flush:
            self.writer.enabled = False
        try:
            if target is not None:
                try:
                    if flush and self.pending is not None:
                        self.pending.duration = self.interval
                        target.mux(self.pending)
                finally:
                    target.close()
        finally:
            self.source.close()
            self.writer.enabled = False


class TimestampedAacMuxer(TimestampedVideoMuxer):
    """Copy AAC access units to a browser-native audio MediaSource track."""

    def __init__(self, first_adts_packet, callback):
        self.config, _ = _aac_packet_config(first_adts_packet)
        self.origin = None
        self.pending = None
        self.pending_pts = None
        self.source = av.open(io.BytesIO(first_adts_packet), format="aac")
        self.writer = _ChunkWriter(callback)
        self.target = None
        try:
            source = self.source.streams.audio[0]
            profile, frequency, channels = self.config
            if profile != 2 or frequency >= 13 or not channels:
                raise ValueError("Native live AAC requires an AAC-LC decoder configuration")
            source.codec_context.extradata = (
                (profile << 11) | (frequency << 7) | (channels << 3)
            ).to_bytes(2, "big")
            self.interval = round(Fraction(1024, source.codec_context.sample_rate) * 1_000_000)
            self.target = av.open(self.writer, "w", format="mp4", buffer_size=1024,
                options={"movflags": "frag_every_frame+empty_moov+default_base_moof", "flush_packets": "1"})
            self.stream = self.target.add_stream_from_template(source)
            self.stream.codec_context.extradata = source.codec_context.extradata
        except BaseException as error:
            try:
                self.close(flush=False)
            except Exception as cleanup_error:
                error.add_note(f"AAC cleanup also failed: {type(cleanup_error).__name__}")
            raise

    def push(self, payload, timestamp, scale, keyframe=True):
        # MP4 carries raw AAC access units, not ADTS transport headers.
        config, header_size = _aac_packet_config(payload)
        if config != self.config:
            raise ValueError("Native AAC decoder configuration changed")
        super().push(payload[header_size:], timestamp, scale, keyframe)


def _aac_packet_config(payload):
    if len(payload) < 7 or payload[0] != 0xff or payload[1] & 0xf6 != 0xf0:
        raise ValueError("Native AAC transport header is invalid")
    header_size = 7 if payload[1] & 1 else 9
    size = ((payload[3] & 3) << 11) | (payload[4] << 3) | (payload[5] >> 5)
    if size != len(payload) or size <= header_size or payload[6] & 3:
        raise ValueError("Native AAC requires one complete access unit per packet")
    return (((payload[2] >> 6) + 1, (payload[2] >> 2) & 15,
             ((payload[2] & 1) << 2) | (payload[3] >> 6)), header_size)


def _reordered_video_timing(video, packet_path: Path, origin: Fraction):
    """Decode picture order only; retain the original encoded access units."""
    if not video.streams.video[0].codec_context.has_b_frames:
        return None
    times = []
    cancellation = _WORK_CANCEL.get()
    length = packet_path.stat().st_size
    with packet_path.open("rb") as source:
        while header := source.read(PACKET_HEADER.size):
            if cancellation is not None and cancellation.is_set():
                raise CameraWorkCancelled("Native picture-order analysis cancelled")
            if len(header) != PACKET_HEADER.size:
                raise ValueError("Native packet index is truncated")
            kind, _flags, stamp, scale, size = PACKET_HEADER.unpack(header)
            if not 0 < size <= MAX_PACKET_BYTES or source.tell() + size > length:
                raise ValueError("Native packet data is truncated")
            if kind == 1:
                if not 0 < scale <= 1_000_000 or len(times) >= 1_000_000:
                    raise ValueError("Native video reorder metadata exceeds bounds")
                times.append(round((Fraction(stamp, scale) - origin) * 1_000_000))
            source.seek(size, 1)
    decoder = av.CodecContext.create("h264", "r")
    decoder.thread_count = 1
    order = []
    count = 0
    # Deferred to avoid the muxer's import of this packet-cache module.
    from .native_mux import _h264_metadata_only

    headers = b""
    for packet in video.demux(video.streams.video[0]):
        if cancellation is not None and cancellation.is_set():
            raise CameraWorkCancelled("Native picture-order analysis cancelled")
        if not packet.size:
            continue
        data = bytes(packet)
        if _h264_metadata_only(data):
            if len(headers) + len(data) > MAX_PACKET_BYTES:
                raise ValueError("Native video decoder headers exceed packet limit")
            headers += data
            continue
        if headers:
            packet = av.Packet(headers + data)
            headers = b""
        packet.pts = count
        packet.dts = None
        packet.time_base = Fraction(1, 1000)
        order.extend(frame.pts for frame in decoder.decode(packet))
        count += 1
        if count > len(times):
            raise ValueError("Native video access units do not match the packet index")
    order.extend(frame.pts for frame in decoder.decode(None))
    if count != len(times) or sorted(order) != list(range(count)):
        raise ValueError("Native video picture order does not match the packet index")
    ranks = {packet_index: rank for rank, packet_index in enumerate(order)}
    delay = max((times[i] - times[ranks[i]] for i in range(count)), default=0)
    return [(times[ranks[i]], times[i] - delay) for i in range(count)]


def mux_camera_packets(video_path: Path, audio_path: Path, packet_path: Path,
                       output_path: Path, *, audio_codec: str, sample_rate: int,
                       channels: int, origin: Fraction) -> None:
    """Copy compressed/PCM packets into an ISO/QuickTime-family container."""
    with ExitStack() as stack:
        video = stack.enter_context(av.open(str(video_path), format="h264"))
        video_timing = _reordered_video_timing(video, packet_path, origin)
        video_index = 0
        audio = None
        if audio_codec:
            audio_format = RAW_AUDIO_FORMATS.get(audio_codec, "aac")
            options = ({"sample_rate": str(sample_rate), "ch_layout": "mono" if channels == 1 else "stereo"}
                       if audio_codec in RAW_AUDIO_FORMATS else {})
            audio = stack.enter_context(av.open(str(audio_path), format=audio_format, options=options))
        # QuickTime sample entries retain G.711/PCM bytes without conversion.
        container = "mov" if audio_codec in RAW_AUDIO_FORMATS else "mp4"
        target = stack.enter_context(av.open(str(output_path), "w", format=container,
                                             options={"movflags": "+faststart"}))
        streams = {1: target.add_stream_from_template(video.streams.video[0])}
        if audio is not None:
            streams[2] = target.add_stream_from_template(audio.streams.audio[0])
        pending = {}
        intervals = {}
        previous = {}
        with packet_path.open("rb") as source:
            while header := source.read(PACKET_HEADER.size):
                if len(header) != PACKET_HEADER.size:
                    raise ValueError("Native packet index is truncated")
                kind, flags, timestamp, scale, size = PACKET_HEADER.unpack(header)
                if kind not in streams or not 0 < scale <= 1_000_000 or not 0 < size <= MAX_PACKET_BYTES:
                    raise ValueError("Native packet index contains invalid metadata")
                payload = source.read(size)
                if len(payload) != size:
                    raise ValueError("Native packet data is truncated")
                time = Fraction(timestamp, scale) - origin
                if kind in previous and time <= previous[kind]:
                    raise ValueError("Native camera packet timestamps are not increasing")
                previous[kind] = time
                packet = av.Packet(payload)
                packet.is_keyframe = kind == 1 and bool(flags & 1)
                packet.stream = streams[kind]
                packet.time_base = Fraction(1, 1_000_000)
                packet.pts = packet.dts = round(time * 1_000_000)
                if kind == 1 and video_timing is not None:
                    packet.pts, packet.dts = video_timing[video_index]
                    video_index += 1
                if kind in pending:
                    old = pending.pop(kind)
                    old.duration = packet.dts - old.dts
                    intervals[kind] = old.duration
                    target.mux(old)
                pending[kind] = packet
        for kind, packet in pending.items():
            if kind == 2 and audio_codec in RAW_AUDIO_FORMATS:
                width = 2 if audio_codec == "pcm_s16le" else 1
                packet.duration = round(Fraction(packet.size, width * channels * sample_rate) * 1_000_000)
            elif kind == 2:
                packet.duration = round(Fraction(1024, sample_rate) * 1_000_000)
            else:
                packet.duration = intervals.get(kind, 40_000)
            target.mux(packet)
