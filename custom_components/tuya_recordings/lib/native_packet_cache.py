"""Remux native callback packets with their camera clock; never encode frames."""

from contextlib import ExitStack
from fractions import Fraction
import io
from pathlib import Path
import struct

import av

PACKET_HEADER = struct.Struct(">BBQQI")
MAX_PACKET_BYTES = 8 * 1024 * 1024
RAW_AUDIO_FORMATS = {"pcm_s16le": "s16le", "g711u": "mulaw", "g711a": "alaw"}


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
        packet.pts = packet.dts = round((stamp - self.origin) * 1_000_000)
        if self.pending is not None:
            interval = packet.pts - self.pending.pts
            if interval <= 0:
                raise ValueError("Native video timestamps are not increasing")
            self.pending.duration = self.interval = interval
            self.target.mux(self.pending)
        self.pending = packet

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


def mux_camera_packets(video_path: Path, audio_path: Path, packet_path: Path,
                       output_path: Path, *, audio_codec: str, sample_rate: int,
                       channels: int, origin: Fraction) -> None:
    """Copy compressed/PCM packets into an ISO/QuickTime-family container."""
    with ExitStack() as stack:
        video = stack.enter_context(av.open(str(video_path), format="h264"))
        if video.streams.video[0].codec_context.has_b_frames:
            raise ValueError("Native recording has B frames but no decode timestamps")
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
                if kind in pending:
                    old = pending.pop(kind)
                    old.duration = packet.pts - old.pts
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
