import './vendor/plyr/plyr.min.js';

// Plyr owns presentation; camera seeks remain signed, serialized server requests.
export function attachCameraPlayer(video, initialClip, signPath, options = {}) {
  if (video.plyr) {
    video.plyr.destroy();
  }
  let offset = 0;
  let paused = false;
  let revision = 0;
  let disposed = false;
  let releasing = false;
  let streamStarting = false;
  let streamController = null;
  let timelineSocket = null;
  let timelineSeekTimer = null;
  let pendingTimelineSeek = null;
  let streamObjectUrl = '';
  let audioContext = null;
  let audioGain = null;
  let audioNextTime = 0;
  let audioReady = null;
  let audioSampleRate = 8000;
  let audioChannels = 1;
  let audioTimestampScale = null;
  let firstVideoTimestamp = null;
  let videoAudioStartTime = 0;
  const audioSources = new Set();
  let clipEndTimer = null;
  let waitingTimer = null;
  let playingReportedRevision = -1;
  let player = null;
  let clip = initialClip;
  // A config-entry reload does not replace already imported Python modules.
  // Only use the WebSocket player when the backend actually supplied its
  // timeline endpoint; an older in-memory backend still supplies clip URLs.
  const continuous = options.mode === 'timeline' && initialClip.playback_url.includes('/timeline/');
  const initialOffset = Math.max(0, Number(options.initialOffset) || 0);
  const debug = (phase, detail = null) => options.debug?.({ phase, ...(detail || {}) });
  let streamTransport = clip.cached ? 'file' : (clip.stream_transport || 'native');
  let isControlledStream = streamTransport === 'native' && !clip.cached;
  const selectClip = nextClip => {
    clip = nextClip;
    streamTransport = clip.cached ? 'file' : (clip.stream_transport || 'native');
    isControlledStream = streamTransport === 'native' && !clip.cached;
  };
  const active = () => Boolean(video.getAttribute('src') || video.srcObject);
  const messageFor = error => error?.message || String(error || 'unknown error');
  const toggleRemotePlayback = event => {
    if (disposed || !isControlledStream) return true;
    event?.preventDefault?.();
    event?.stopPropagation?.();
    paused || !active() ? resume(!continuous && offset >= clip.duration ? 0 : offset) : pause();
    return false;
  };
  const status = video.parentElement.querySelector('[role="status"]');
  const setLoading = loading => {
    player?.elements?.container?.classList.toggle('plyr--loading', Boolean(loading));
  };
  const position = () => continuous
    ? offset + (Number(video.currentTime) || 0)
    : Math.min(clip.duration, offset + (Number(video.currentTime) || 0));
  const release = () => {
    releasing = true;
    setLoading(false);
    if (clipEndTimer !== null) clearTimeout(clipEndTimer);
    clipEndTimer = null;
    if (waitingTimer !== null) clearTimeout(waitingTimer);
    waitingTimer = null;
    streamController?.abort();
    streamController = null;
    if (timelineSeekTimer !== null) clearTimeout(timelineSeekTimer);
    timelineSeekTimer = null;
    pendingTimelineSeek = null;
    if (timelineSocket) {
      timelineSocket.close();
      timelineSocket = null;
    }
    for (const source of audioSources) {
      try { source.stop(); } catch (_error) {}
    }
    audioSources.clear();
    audioContext?.close().catch(() => {});
    audioContext = null;
    audioGain = null;
    audioNextTime = 0;
    audioReady = null;
    audioSampleRate = 8000;
    audioChannels = 1;
    audioTimestampScale = null;
    firstVideoTimestamp = null;
    videoAudioStartTime = 0;
    const hadSource = Boolean(video.getAttribute('src') || video.srcObject);
    video.removeAttribute('src');
    if (video.srcObject) video.srcObject = null;
    video.pause();
    if (hadSource) video.load();
    if (streamObjectUrl) URL.revokeObjectURL(streamObjectUrl);
    streamObjectUrl = '';
    releasing = false;
  };
  const queueTimelineSeek = wanted => {
    // Scrubbing can generate a large burst of positions. The camera only
    // needs the final one, so collapse them before they leave the browser.
    pendingTimelineSeek = wanted;
    if (timelineSeekTimer !== null) return;
    timelineSeekTimer = setTimeout(() => {
      timelineSeekTimer = null;
      const position = pendingTimelineSeek;
      pendingTimelineSeek = null;
      if (disposed || paused || !Number.isFinite(position)) return;
      if (timelineSocket?.readyState !== WebSocket.OPEN) return;
      timelineSocket.send(JSON.stringify({ action: 'seek', position }));
    }, 60);
  };
  const pause = () => {
    if (disposed || releasing || !active()) return;
    if (continuous && paused) return;
    offset = position();
    paused = true;
    revision++;
    if (continuous && isControlledStream && timelineSocket?.readyState === WebSocket.OPEN) {
      timelineSocket.send(JSON.stringify({ action: 'pause' }));
      video.pause();
      audioContext?.suspend().catch(() => {});
      status.textContent = 'Paused';
      return;
    }
    release();
    status.textContent = 'Paused';
  };
  const finishPlayback = () => {
    if (disposed || releasing || paused) return;
    offset = clip.duration;
    paused = true;
    revision++;
    release();
    status.textContent = 'Finished';
    debug('finished');
  };
  const resume = async (time, preferredTransport = null) => {
    if (disposed) return;
    if (continuous && isControlledStream && timelineSocket?.readyState === WebSocket.OPEN) {
      const wanted = clip.start + Math.max(0, Math.floor(time));
      video.parentElement.classList.add('camera-player--switching');
      status.textContent = paused ? 'Resuming camera stream' : 'Seeking camera recording';
      setLoading(true);
      if (paused) {
        paused = false;
        await audioContext?.resume();
        timelineSocket.send(JSON.stringify({ action: 'resume' }));
        video.play().catch(() => {});
      } else {
        offset = Math.max(0, Math.floor(time));
        streamStarting = true;
        video.pause();
        queueTimelineSeek(wanted);
      }
      return;
    }
    const request = ++revision;
    release();
    video.parentElement.classList.add('camera-player--switching');
    streamStarting = true;
    if (isControlledStream) prepareAudio();
    offset = continuous
      ? Math.max(0, Math.floor(time))
      : Math.max(0, Math.min(clip.duration - 1, Math.floor(time)));
    paused = false;
    status.textContent = 'Connecting to camera...';
    setLoading(true);
    try {
      debug('sign-start', { offset });
      const separator = clip.playback_url.includes('?') ? '&' : '?';
      const transport = preferredTransport || (isControlledStream ? streamTransport : '');
      const url = await signPath(`${clip.playback_url}${separator}position=${clip.start + offset}${transport ? `&transport=${transport}` : ''}`);
      debug('signed', { offset });
      if (disposed || request !== revision) return;
      if (isControlledStream) {
        if (continuous) await startTimelineStream(url, request);
        else await startNativeStream(url, request);
      } else {
        video.src = url;
        video.load();
        await video.play();
      }
    } catch (error) {
      if (disposed || request !== revision) return;
      streamStarting = false;
      paused = true;
      release();
      status.textContent = error?.name === 'NotAllowedError' ? 'Tap play to start' : `Playback unavailable: ${messageFor(error)}`;
      debug('error', { name: error?.name || 'Error', message: messageFor(error) });
    }
  };
  const startTimelineStream = async (signedUrl, request) => {
    // Timeline playback has no fetch() controller of its own. Give the
    // WebSocket path the same lifecycle controller as direct clip playback so
    // MediaSource setup and disposal share one valid cancellation signal.
    const controller = new AbortController();
    streamController = controller;
    const socketUrl = new URL(signedUrl, window.location.href);
    socketUrl.protocol = socketUrl.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(socketUrl);
    socket.binaryType = 'arraybuffer';
    timelineSocket = socket;
    let bodySettled = false;
    const body = new ReadableStream({
      start(controller) {
        const fail = error => {
          if (bodySettled) return;
          bodySettled = true;
          controller.error(error);
        };
        const close = () => {
          if (bodySettled) return;
          bodySettled = true;
          controller.close();
        };
        socket.addEventListener('message', event => {
          if (bodySettled) return;
          if (typeof event.data === 'string') {
            try {
              const message = JSON.parse(event.data);
              if (message.type === 'error') fail(new Error(message.message || 'Timeline playback failed'));
            } catch (error) {
              fail(error);
            }
            return;
          }
          try {
            controller.enqueue(new Uint8Array(event.data));
          } catch (error) {
            fail(error);
          }
        });
        socket.addEventListener('close', close);
        socket.addEventListener('error', () => fail(new Error('Timeline connection failed')));
      },
      cancel() {
        bodySettled = true;
        if (socket.readyState < WebSocket.CLOSING) socket.close();
      },
    });
    // Register the media listener before awaiting `open`. The server may send
    // its stream header immediately after the upgrade completes; attaching
    // afterward can lose those first bytes and leave the player connecting
    // forever with an otherwise healthy camera session.
    await openWebSocket(socket, controller.signal);
    if (disposed || request !== revision) {
      await body.cancel();
      return;
    }
    await consumeNativeStream(new Response(body, { status: 200 }), request);
  };
  const startNativeStream = async (url, request) => {
    const controller = new AbortController();
    streamController = controller;
    const response = await fetch(url, {
      credentials: 'same-origin',
      signal: controller.signal,
    });
    debug('response', { status: response.status, ok: response.ok });
    if (!response.ok || !response.body) {
      throw new Error(response.statusText || `HTTP ${response.status}`);
    }
    await consumeNativeStream(response, request);
  };
  const consumeNativeStream = async (response, request) => {
    const signal = streamController?.signal || controller.signal;
    const MediaSourceType = window.MediaSource || window.ManagedMediaSource;
    const mediaSource = new MediaSourceType();
    streamObjectUrl = URL.createObjectURL(mediaSource);
    video.src = streamObjectUrl;
    video.load();
    await once(mediaSource, 'sourceopen', signal);
    if (disposed || request !== revision) return;
    const mime = [
      'video/mp4; codecs="avc1.64001f"',
      'video/mp4; codecs="avc1.4d401f"',
      'video/mp4; codecs="avc1.42e01e"',
    ].find(candidate => MediaSourceType.isTypeSupported(candidate));
    if (!mime) throw new Error('H.264 Media Source playback is unavailable');
    const sourceBuffer = mediaSource.addSourceBuffer(mime);
    sourceBuffer.mode = 'sequence';
    await audioReady;

    const reader = response.body.getReader();
    let bytes = new Uint8Array();
    let magicRead = false;
    let videoStarted = false;
    let videoRecords = 0;
    let streamComplete = false;
    let playbackError = null;
    let playbackPromise = null;
    const queuedAudio = [];
    const startVideoPlayback = () => {
      if (videoStarted) return;
      videoStarted = true;
      debug('first-video');
      playbackPromise = video.play()
        .then(() => {
          videoAudioStartTime = audioContext.currentTime + 0.08;
          audioTimestampScale = inferTimestampScale(queuedAudio, audioSampleRate, audioChannels);
          for (const item of queuedAudio.splice(0)) {
            if (!firstVideoTimestamp || !item.timestamp || item.timestamp >= firstVideoTimestamp) {
              scheduleAudio(item.kind, item.payload, item.timestamp);
            }
          }
        })
        .catch(error => { playbackError = error; });
    };
    while (true) {
      const { value, done } = await reader.read();
      if (playbackError) throw playbackError;
      if (done) break;
      bytes = concatBytes(bytes, value);
      while (bytes.length >= 4 && String.fromCharCode(...bytes.subarray(0, 4)) === 'TRS2') {
        bytes = bytes.subarray(4);
        if (magicRead) {
          video.parentElement.classList.add('camera-player--switching');
          video.pause();
          if (sourceBuffer.updating) await once(sourceBuffer, 'updateend', signal);
          if (sourceBuffer.buffered.length) {
            sourceBuffer.remove(
              sourceBuffer.buffered.start(0),
              sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1),
            );
            await once(sourceBuffer, 'updateend', signal);
          }
          video.currentTime = 0;
          videoStarted = false;
          videoRecords = 0;
          queuedAudio.splice(0);
          for (const source of audioSources) {
            try { source.stop(); } catch (_error) {}
          }
          audioSources.clear();
          audioNextTime = 0;
          firstVideoTimestamp = null;
          debug('stream-reset');
        }
        magicRead = true;
        debug('stream-header');
      }
      if (!magicRead) {
        if (bytes.length < 4) continue;
        if (String.fromCharCode(...bytes.subarray(0, 4)) !== 'TRS2') {
          throw new Error('Camera stream header is invalid');
        }
        continue;
      }
      while (bytes.length >= 13) {
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const kind = view.getUint8(0);
        const timestamp = Number(view.getBigUint64(1));
        const size = view.getUint32(9);
        if (size > 16 * 1024 * 1024) throw new Error('Camera stream record is too large');
        if (bytes.length < 13 + size) break;
        const payload = bytes.slice(13, 13 + size);
        bytes = bytes.subarray(13 + size);
        if (kind === 1) {
          if (firstVideoTimestamp === null) firstVideoTimestamp = timestamp;
          await appendBuffer(sourceBuffer, payload, signal);
          videoRecords += 1;
          const bufferedSeconds = sourceBuffer.buffered.length
            ? sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1) - sourceBuffer.buffered.start(0)
            : 0;
          if (!videoStarted && (bufferedSeconds >= 2 || videoRecords >= 12)) {
            startVideoPlayback();
          }
        } else if (kind === 2 || kind === 4) {
          if (!queuedAudio.length && !audioNextTime) debug('first-audio', { kind });
          if (videoStarted && !playbackError && !video.paused) scheduleAudio(kind, payload, timestamp);
          else queuedAudio.push({ kind, payload, timestamp });
        } else if (kind === 5) {
          if (payload.length !== 5) throw new Error('Camera audio format is invalid');
          const format = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
          audioSampleRate = format.getUint32(0);
          audioChannels = format.getUint8(4);
          if (!audioSampleRate || !audioChannels) throw new Error('Camera audio format is invalid');
          debug('audio-format', { sampleRate: audioSampleRate, channels: audioChannels });
        } else if (kind === 3) {
          streamComplete = true;
          if (mediaSource.readyState === 'open' && !sourceBuffer.updating) {
            mediaSource.endOfStream();
          }
          if (!videoStarted && videoRecords > 0) startVideoPlayback();
          if (playbackPromise) await playbackPromise;
          if (playbackError) throw playbackError;
          return;
        } else {
          throw new Error(`Unknown camera stream record ${kind}`);
        }
      }
    }
    if (!streamComplete) throw new Error('Camera stream ended before completion');
  };
  const prepareAudio = () => {
    if (audioContext && audioGain && audioReady) return audioReady;
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) throw new Error('Web Audio is unavailable');
    audioContext = new AudioContextClass();
    audioGain = audioContext.createGain();
    audioGain.connect(audioContext.destination);
    syncAudioVolume();
    audioReady = audioContext.resume();
    return audioReady;
  };
  const syncAudioVolume = () => {
    if (audioGain) audioGain.gain.value = video.muted ? 0 : video.volume;
  };
  const scheduleAudio = (kind, payload, timestamp) => {
    if (kind === 2) schedulePcm16le(payload, timestamp);
    else scheduleMulaw(payload, timestamp);
  };
  const schedulePcm16le = (payload, timestamp) => {
    if (!audioContext || !audioGain || payload.length < 2) return;
    const samples = Math.floor(payload.length / (2 * audioChannels));
    if (!samples) return;
    const buffer = audioContext.createBuffer(audioChannels, samples, audioSampleRate);
    const view = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
    for (let channelIndex = 0; channelIndex < audioChannels; channelIndex++) {
      const channel = buffer.getChannelData(channelIndex);
      for (let index = 0; index < samples; index++) {
        channel[index] = view.getInt16((index * audioChannels + channelIndex) * 2, true) / 32768;
      }
    }
    scheduleAudioBuffer(buffer, timestamp);
  };
  const scheduleMulaw = (payload, timestamp) => {
    if (!audioContext || !audioGain || !payload.length) return;
    const samples = Math.floor(payload.length / audioChannels);
    if (!samples) return;
    const buffer = audioContext.createBuffer(audioChannels, samples, audioSampleRate);
    for (let channelIndex = 0; channelIndex < audioChannels; channelIndex++) {
      const channel = buffer.getChannelData(channelIndex);
      for (let index = 0; index < samples; index++) {
        channel[index] = decodeMulaw(payload[index * audioChannels + channelIndex]);
      }
    }
    scheduleAudioBuffer(buffer, timestamp);
  };
  const scheduleAudioBuffer = (buffer, timestamp) => {
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioGain);
    audioSources.add(source);
    source.addEventListener('ended', () => audioSources.delete(source), { once: true });
    const contiguousTime = Math.max(audioNextTime, audioContext.currentTime + 0.02);
    const timestampTime = audioTimestampScale && firstVideoTimestamp !== null && timestamp
      ? videoAudioStartTime + ((timestamp - firstVideoTimestamp) / audioTimestampScale)
      : contiguousTime;
    const startTime = Math.max(contiguousTime, timestampTime);
    source.start(startTime);
    audioNextTime = startTime + buffer.duration;
  };
  player = new window.Plyr(video, {
    controls: continuous
      ? ['play', 'mute', 'volume', 'fullscreen']
      : ['play', 'progress', 'current-time', 'duration', 'mute', 'volume', 'fullscreen'],
    iconUrl: new URL('./vendor/plyr/plyr.svg', import.meta.url).href,
    loadSprite: false,
    storage: { enabled: false },
    keyboard: { focused: false, global: false },
    fullscreen: { enabled: true, fallback: true, iosNative: false },
    hideControls: false,
    ratio: '16:9',
    duration: clip.duration,
    invertTime: false,
    toggleInvert: false,
    settings: [],
    listeners: clip.cached ? {} : {
      play: () => {
        if (paused || !active()) {
          resume(!continuous && offset >= clip.duration ? 0 : offset);
          return false;
        }
        // video.play() emits this event after the native stream's first
        // fragment is appended. Let that programmatic start proceed instead
        // of interpreting it as another toggle and tearing the stream down.
        return true;
      },
    },
  });
  const controller = new AbortController();
  const on = (target, event, callback) => target.addEventListener(event, callback, { signal: controller.signal });
  if (isControlledStream) {
    Object.defineProperty(player, 'currentTime', { get: position, set: time => resume(time), configurable: true });
    on(video, 'pause', () => {
      if (!streamStarting) pause();
    });
    on(video, 'ended', () => {
      if (!video.ended) return;
      if (!continuous) offset = clip.duration;
      paused = true;
      release();
      status.textContent = continuous ? 'End of available recording' : 'Finished';
    });
    on(video, 'click', toggleRemotePlayback);
  }
  on(video, 'playing', () => {
    streamStarting = false;
    paused = false;
    if (waitingTimer !== null) clearTimeout(waitingTimer);
    waitingTimer = null;
    setLoading(false);
    status.textContent = '';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!disposed) video.parentElement.classList.remove('camera-player--switching');
    }));
    if (isControlledStream && !continuous && clipEndTimer === null) {
      const remainingMs = Math.max(0, (clip.duration - position()) * 1000);
      clipEndTimer = setTimeout(finishPlayback, remainingMs + 100);
    }
    if (playingReportedRevision !== revision) {
      playingReportedRevision = revision;
      debug('playing');
    }
  });
  on(video, 'timeupdate', () => {
    // The old MediaSource can emit one last timeupdate after a timeline seek.
    // Do not let that stale position pull the ruler away from the user's drop.
    if (streamStarting || video.parentElement.classList.contains('camera-player--switching')) return;
    options.onPosition?.(clip.start + position());
    if (isControlledStream && !continuous && !paused && offset + (Number(video.currentTime) || 0) >= clip.duration) {
      finishPlayback();
    }
  });
  on(video, 'volumechange', syncAudioVolume);
  on(video, 'tuya-recordings-playblocked', () => { paused = true; status.textContent = 'Tap play to start'; });
  on(video, 'waiting', () => {
    if (!video.getAttribute('src')) return;
    if (waitingTimer !== null) clearTimeout(waitingTimer);
    waitingTimer = setTimeout(() => {
      waitingTimer = null;
      if (!disposed && !paused && video.getAttribute('src') && video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
        setLoading(true);
        status.textContent = 'Waiting for video';
      }
    }, 350);
  });
  on(video, 'error', () => { if (!video.getAttribute('src')) return; paused = true; release(); status.textContent = `Playback unavailable: ${video.error?.message || 'video error'}`; });
  if (clip.cached) {
    video.play().catch(() => { status.textContent = 'Tap play to start'; });
  } else if (options.autoplay) {
    resume(initialOffset);
  }
  const dispose = () => {
    disposed = true;
    revision++;
    controller.abort();
    release();
    if (video.plyr === player) {
      player.destroy();
    }
  };
  dispose.pause = pause;
  dispose.playClip = (nextClip, time = 0) => {
    if (disposed) return;
    selectClip(nextClip);
    paused = !(continuous && timelineSocket?.readyState === WebSocket.OPEN);
    resume(Math.max(0, Number(time) || 0));
  };
  return dispose;
}

function inferTimestampScale(items, sampleRate, channels) {
  const candidates = [1, 1000, 90000, 1000000];
  let best = null;
  for (let index = 1; index < items.length; index++) {
    const previous = items[index - 1];
    const current = items[index];
    const delta = current.timestamp - previous.timestamp;
    const bytesPerSample = previous.kind === 2 ? 2 : 1;
    const duration = previous.payload.length / (bytesPerSample * channels * sampleRate);
    if (delta <= 0 || duration <= 0) continue;
    for (const candidate of candidates) {
      const error = Math.abs(Math.log((delta / candidate) / duration));
      if (!best || error < best.error) best = { scale: candidate, error };
    }
  }
  return best && best.error < 1 ? best.scale : null;
}

function once(target, event, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Playback cancelled', 'AbortError'));
    target.addEventListener(event, resolve, signal ? { once: true, signal } : { once: true });
    signal?.addEventListener('abort', abort, { once: true });
  });
}

function openWebSocket(socket, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Playback cancelled', 'AbortError'));
    const failed = () => reject(new Error('Timeline connection failed'));
    socket.addEventListener('open', resolve, { once: true, signal });
    socket.addEventListener('error', failed, { once: true, signal });
    socket.addEventListener('close', failed, { once: true, signal });
    signal.addEventListener('abort', abort, { once: true });
  });
}

function appendBuffer(sourceBuffer, payload, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException('Playback cancelled', 'AbortError'));
    const error = () => reject(new Error('Browser rejected the H.264 stream'));
    sourceBuffer.addEventListener('updateend', resolve, { once: true, signal });
    sourceBuffer.addEventListener('error', error, { once: true, signal });
    signal.addEventListener('abort', abort, { once: true });
    sourceBuffer.appendBuffer(payload);
  });
}

function concatBytes(left, right) {
  if (!left.length) return right.slice();
  const joined = new Uint8Array(left.length + right.length);
  joined.set(left);
  joined.set(right, left.length);
  return joined;
}

function decodeMulaw(value) {
  const sample = (~value) & 0xff;
  const sign = sample & 0x80;
  const exponent = (sample >> 4) & 0x07;
  const mantissa = sample & 0x0f;
  const magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84;
  return Math.max(-1, Math.min(1, (sign ? -magnitude : magnitude) / 32768));
}
