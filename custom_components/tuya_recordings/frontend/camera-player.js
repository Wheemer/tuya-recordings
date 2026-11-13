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
  let audioTimestampScaleHint = null;
  let firstVideoTimestamp = null;
  let videoTimestampScaleHint = null;
  let cameraClockMappings = [];
  let aacElement = null;
  let aacObjectUrl = '';
  let aacNode = null;
  let firstAacTimestamp = null;
  let videoAudioStartTime = 0;
  const audioSources = new Set();
  let audioStalled = false;
  let activeSegmentGeneration = -1;
  let expectedTimelinePosition = null;
  let discardSegment = false;
  let waitingTimer = null;
  let lastPlaybackProgress = 0;
  let playingReportedRevision = -1;
  let progressReportedRevision = -1;
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
  const position = () => {
    const time = Number(video.currentTime) || 0;
    if (continuous) {
      const anchor = cameraClockMappings.findLast(item => item.mediaTime <= time);
      return offset + (anchor ? anchor.cameraTime + time - anchor.mediaTime : time);
    }
    return Math.min(clip.duration, offset + time);
  };
  const clearScheduledAudio = () => {
    for (const source of audioSources) {
      try { source.stop(); } catch (_error) {}
    }
    audioSources.clear();
    audioNextTime = 0;
    firstVideoTimestamp = null;
    videoAudioStartTime = 0;
    audioTimestampScale = null;
  };
  const clearAac = () => {
    if (aacElement) {
      aacElement.pause();
      aacElement.removeAttribute('src');
      aacElement.load();
      aacElement = null;
    }
    aacNode?.disconnect();
    aacNode = null;
    if (aacObjectUrl) URL.revokeObjectURL(aacObjectUrl);
    aacObjectUrl = '';
    firstAacTimestamp = null;
  };
  const syncAac = () => {
    if (!aacElement || firstAacTimestamp === null || firstVideoTimestamp === null) return;
    const scale = audioTimestampScaleHint;
    if (!scale) return;
    const wanted = video.currentTime + firstVideoTimestamp / (videoTimestampScaleHint || scale) - firstAacTimestamp / scale;
    if (streamStarting || video.paused || video.readyState < 3 || wanted < 0 || audioStalled) {
      aacElement.pause();
      return;
    }
    if (aacElement.readyState < 1) return;
    if (Math.abs(aacElement.currentTime - wanted) > 0.15) aacElement.currentTime = wanted;
    aacElement.playbackRate = video.playbackRate;
    if (aacElement.paused) aacElement.play().catch(error => {
      debug('aac-play-error', { message: messageFor(error) });
      status.textContent = 'Audio playback failed: ' + messageFor(error);
    });
  };
  const release = () => {
    releasing = true;
    cameraClockMappings = [];
    setLoading(false);
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
    clearScheduledAudio();
    clearAac();
    audioContext?.close().catch(() => {});
    audioContext = null;
    audioGain = null;
    audioReady = null;
    audioStalled = false;
    audioSampleRate = 8000;
    audioChannels = 1;
    audioTimestampScaleHint = null;
    videoTimestampScaleHint = null;
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
      expectedTimelinePosition = position;
      debug('seek-requested', { position });
      timelineSocket.send(JSON.stringify({ action: 'seek', position }));
    }, 60);
  };
  const pause = () => {
    if (disposed || releasing || !active()) return;
    if (continuous && paused) return;
    paused = true;
    if (continuous && isControlledStream && timelineSocket?.readyState === WebSocket.OPEN) {
      // Retain both the reader revision and its media-time origin. Only a
      // replacement stream owns a new revision and a rebased offset.
      timelineSocket.send(JSON.stringify({ action: 'pause' }));
      video.pause();
      audioContext?.suspend().catch(() => {});
      status.textContent = 'Paused';
      return;
    }
    offset = position();
    revision++;
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
  const resume = async (time, preferredTransport = null, forceTimelineSeek = false) => {
    if (disposed) return;
    if (continuous && isControlledStream && timelineSocket?.readyState === WebSocket.OPEN) {
      const wanted = clip.start + Math.max(0, Math.floor(time));
      video.parentElement.classList.add('camera-player--switching');
      status.textContent = paused ? 'Resuming camera stream' : 'Seeking camera recording';
      setLoading(true);
      aacElement?.pause();
      if (paused) {
        // A timeline card selection may choose a different recording while
        // the camera session is paused. Seek first, then resume, so the
        // camera never emits old-position frames into the fresh UI state.
        if (forceTimelineSeek) {
          offset = Math.max(0, Math.floor(time));
          expectedTimelinePosition = wanted;
          streamStarting = true;
          debug('seek-requested', { position: wanted });
          timelineSocket.send(JSON.stringify({ action: 'seek', position: wanted }));
        }
        paused = false;
        await audioContext?.resume();
        timelineSocket.send(JSON.stringify({ action: 'resume' }));
        video.play().catch(() => {});
      } else {
        offset = Math.max(0, Math.floor(time));
        streamStarting = true;
        // Keep the media element playing while its hidden buffer is replaced.
        // Pausing here makes a successful timeline seek stop at its new point.
        queueTimelineSeek(wanted);
      }
      return;
    }
    const request = ++revision;
    release();
    // A direct clip opens a distinct HTTP stream, so its protocol generation
    // restarts at zero. Timeline seeks stay within one WebSocket and retain
    // their generation for stale-frame rejection.
    activeSegmentGeneration = -1;
    expectedTimelinePosition = null;
    discardSegment = false;
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
          if (socket.readyState < WebSocket.CLOSING) socket.close();
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
              if (message.type === 'seeking') {
                const position = Number(message.position);
                if (Number.isFinite(position)) {
                  expectedTimelinePosition = position;
                  debug('seek-accepted', { position });
                }
                return;
              }
              if (message.type === 'error') fail(new Error(message.message || 'Timeline playback failed'));
            } catch (error) {
              fail(error);
            }
            return;
          }
          try {
            if (event.data.byteLength > 8 * 1024 * 1024 || controller.desiredSize < event.data.byteLength) {
              fail(new Error('Playback receiver exceeded its media buffer limit'));
              return;
            }
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
    }, { highWaterMark: 8 * 1024 * 1024, size: chunk => chunk.byteLength });
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
    const signal = streamController?.signal;
    if (!signal) throw new Error('Camera stream was not initialized');
    const MediaSourceType = window.MediaSource || window.ManagedMediaSource;
    const mime = [
      'video/mp4; codecs="avc1.64001f"',
      'video/mp4; codecs="avc1.4d401f"',
      'video/mp4; codecs="avc1.42e01e"',
    ].find(candidate => MediaSourceType.isTypeSupported(candidate));
    if (!mime) throw new Error('H.264 Media Source playback is unavailable');
    await audioReady;

    let mediaSource = null;
    let sourceBuffer = null;
    let aacSourceBuffer = null;
    let aacMediaSource = null;
    let mediaEpoch = 0;
    let frameConfirmationEpoch = -1;
    let frameConfirmationTimer = null;
    let frameConfirmationTimerEpoch = -1;
    signal.addEventListener('abort', () => {
      if (frameConfirmationTimer !== null) {
        clearTimeout(frameConfirmationTimer);
        frameConfirmationTimer = null;
      }
      frameConfirmationTimerEpoch = -1;
      frameConfirmationEpoch = -1;
    }, { once: true });
    const resetAudioTimeline = () => {
      clearScheduledAudio();
    };
    const createMediaPipeline = async () => {
      clearAac();
      aacSourceBuffer = null;
      aacMediaSource = null;
      if (frameConfirmationTimer !== null) clearTimeout(frameConfirmationTimer);
      frameConfirmationTimer = null;
      frameConfirmationTimerEpoch = -1;
      const epoch = ++mediaEpoch;
      const nextMediaSource = new MediaSourceType();
      const nextUrl = URL.createObjectURL(nextMediaSource);
      const previousUrl = streamObjectUrl;
      // A SourceBuffer reset leaves Chromium's decode clock alive. Replacing
      // the MediaSource gives a timeline seek a clean video epoch while the
      // camera WebSocket remains the one active transport.
      streamObjectUrl = nextUrl;
      video.src = nextUrl;
      video.load();
      await once(nextMediaSource, 'sourceopen', signal);
      if (disposed || request !== revision) {
        URL.revokeObjectURL(nextUrl);
        if (streamObjectUrl === nextUrl) streamObjectUrl = '';
        return false;
      }
      const nextSourceBuffer = nextMediaSource.addSourceBuffer(mime);
      nextSourceBuffer.mode = 'sequence';
      mediaSource = nextMediaSource;
      sourceBuffer = nextSourceBuffer;
      if (previousUrl) URL.revokeObjectURL(previousUrl);
      return epoch;
    };
    if (!await createMediaPipeline()) return;

    const reader = response.body.getReader();
    let bytes = new Uint8Array();
    let magicRead = false;
    let videoStarted = false;
    let videoPlaybackConfirmed = false;
    let videoRecords = 0;
    let streamComplete = false;
    let segmentPosition = 0;
    let playbackError = null;
    let playbackPromise = null;
    let activeMediaEpoch = 0;
    const queuedAudio = [];
    const confirmVideoPlayback = (epoch = activeMediaEpoch) => {
      if (
        epoch !== mediaEpoch
        || !videoStarted
        || videoPlaybackConfirmed
        || video.paused
      ) return;
      videoPlaybackConfirmed = true;
      videoAudioStartTime = audioContext.currentTime + 0.08;
      audioTimestampScale = audioTimestampScaleHint || inferTimestampScale(queuedAudio, audioSampleRate, audioChannels);
      for (const item of queuedAudio.splice(0)) {
        if (!firstVideoTimestamp || !item.timestamp || item.timestamp / audioTimestampScale >= firstVideoTimestamp / (videoTimestampScaleHint || audioTimestampScale)) {
          scheduleAudio(item.kind, item.payload, item.timestamp);
        }
      }
      debug('video-confirmed', { position: Math.round(video.currentTime * 100) / 100 });
    };
    const requestVideoConfirmation = () => {
      const epoch = activeMediaEpoch;
      if (
        epoch !== mediaEpoch
        || videoPlaybackConfirmed
        || frameConfirmationEpoch === epoch
      ) return;
      frameConfirmationEpoch = epoch;
      const finish = () => {
        if (frameConfirmationEpoch === epoch) frameConfirmationEpoch = -1;
        if (frameConfirmationTimer !== null && frameConfirmationTimerEpoch === epoch) {
          clearTimeout(frameConfirmationTimer);
          frameConfirmationTimer = null;
          frameConfirmationTimerEpoch = -1;
        }
        confirmVideoPlayback(epoch);
      };
      if (typeof video.requestVideoFrameCallback === 'function') {
        video.requestVideoFrameCallback(finish);
        // Keep older Chromium/WebView builds from withholding audio forever
        // if they expose the API but never invoke its callback for MSE.
        frameConfirmationTimer = setTimeout(finish, 750);
        frameConfirmationTimerEpoch = epoch;
      } else {
        finish();
      }
    };
    // The browser can resolve video.play() before it has rendered a frame
    // after replacing a MediaSource. Audio must wait for a real frame/time
    // update instead of treating that promise as playback confirmation.
    video.addEventListener('playing', requestVideoConfirmation, { signal });
    video.addEventListener('timeupdate', requestVideoConfirmation, { signal });
    const startVideoPlayback = () => {
      if (videoStarted) return;
      videoStarted = true;
      debug('first-video');
      playbackPromise = video.play()
        .then(() => {
          debug('play-request-accepted');
          requestVideoConfirmation();
        })
        .catch(error => {
          debug('play-failed', { name: error?.name || 'Error', message: messageFor(error) });
          playbackError = error;
        });
    };
    while (true) {
      while ((audioContext && audioNextTime - audioContext.currentTime > 5)
        || (sourceBuffer.buffered.length && sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1) - video.currentTime > 30)) {
        if (signal.aborted) throw new DOMException('Playback cancelled', 'AbortError');
        await new Promise(resolve => setTimeout(resolve, 25));
      }
      const { value, done } = await reader.read();
      if (playbackError) throw playbackError;
      if (done) break;
      bytes = concatBytes(bytes, value);
      parseRecords: while (true) {
      if (bytes.length >= 4 && String.fromCharCode(...bytes.subarray(0, 4)) === 'TRS3' && bytes.length < 16) break;
      while (bytes.length >= 4 && String.fromCharCode(...bytes.subarray(0, 4)) === 'TRS3') {
        if (bytes.length < 16) break;
        const segmentView = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const generation = segmentView.getUint32(4);
        const position = Number(segmentView.getBigUint64(8));
        bytes = bytes.subarray(16);
        const staleGeneration = generation <= activeSegmentGeneration;
        const wrongPosition = continuous
          && expectedTimelinePosition !== null
          && position !== expectedTimelinePosition;
        discardSegment = staleGeneration || wrongPosition;
        if (discardSegment) {
          debug('segment-discarded', {
            generation,
            position,
            activeGeneration: activeSegmentGeneration,
            expectedPosition: expectedTimelinePosition,
          });
          continue;
        }
        activeSegmentGeneration = generation;
        cameraClockMappings = [];
        segmentPosition = position;
        expectedTimelinePosition = null;
        if (magicRead) {
          video.parentElement.classList.add('camera-player--switching');
          videoStarted = false;
          videoPlaybackConfirmed = false;
          videoRecords = 0;
          queuedAudio.splice(0);
          audioTimestampScaleHint = null;
          videoTimestampScaleHint = null;
          resetAudioTimeline();
          activeMediaEpoch = await createMediaPipeline();
          if (!activeMediaEpoch) return;
          debug('stream-reset');
        }
        if (continuous) offset = Math.max(0, segmentPosition - clip.start);
        if (!activeMediaEpoch) activeMediaEpoch = mediaEpoch;
        magicRead = true;
        debug('stream-header', { generation, position: segmentPosition });
      }
      if (!magicRead) {
        if (bytes.length < 4) break;
        if (String.fromCharCode(...bytes.subarray(0, 4)) !== 'TRS3') {
          throw new Error('Camera stream header is invalid');
        }
        break;
      }
      while (bytes.length >= 13) {
        if (String.fromCharCode(...bytes.subarray(0, 4)) === 'TRS3') continue parseRecords;
        const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        const kind = view.getUint8(0);
        const timestamp = Number(view.getBigUint64(1));
        const size = view.getUint32(9);
        if (size > 16 * 1024 * 1024) throw new Error('Camera stream record is too large');
        if (bytes.length < 13 + size) break;
        const payload = bytes.slice(13, 13 + size);
        bytes = bytes.subarray(13 + size);
        if (discardSegment) continue;
        if (kind === 1) {
          if (firstVideoTimestamp === null) firstVideoTimestamp = timestamp;
          if (video.currentTime > 60 && sourceBuffer.buffered.length && sourceBuffer.buffered.start(0) < video.currentTime - 60) {
            const removed = once(sourceBuffer, 'updateend', signal);
            sourceBuffer.remove(0, video.currentTime - 60);
            await removed;
          }
          await appendBuffer(sourceBuffer, payload, signal);
          videoRecords += 1;
          const bufferedSeconds = sourceBuffer.buffered.length
            ? sourceBuffer.buffered.end(sourceBuffer.buffered.length - 1) - sourceBuffer.buffered.start(0)
            : 0;
          if (!videoStarted && (bufferedSeconds >= 2 || videoRecords >= 12)) {
            startVideoPlayback();
          }
        } else if (kind === 2 || kind === 4 || kind === 6) {
          if (!queuedAudio.length && !audioNextTime) debug('first-audio', { kind });
          if (videoPlaybackConfirmed && !playbackError && !video.paused) scheduleAudio(kind, payload, timestamp);
          else {
            if (queuedAudio.length >= 256) throw new Error('Camera audio arrived without playable video');
            queuedAudio.push({ kind, payload, timestamp });
          }
        } else if (kind === 5) {
          if (payload.length !== 5 && payload.length !== 9) throw new Error('Camera audio format is invalid');
          const format = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
          audioSampleRate = format.getUint32(0);
          audioChannels = format.getUint8(4);
          audioTimestampScaleHint = payload.length === 9 ? format.getUint32(5) : null;
          if (audioTimestampScaleHint) audioTimestampScale = audioTimestampScaleHint;
          if (!audioSampleRate || !audioChannels) throw new Error('Camera audio format is invalid');
          debug('audio-format', { sampleRate: audioSampleRate, channels: audioChannels });
        } else if (kind === 7) {
          if (!audioTimestampScaleHint) throw new Error('AAC camera clock is unavailable');
          if (!aacSourceBuffer) {
            const audioMime = 'audio/mp4; codecs="mp4a.40.2"';
            if (!MediaSourceType.isTypeSupported(audioMime)) throw new Error('AAC Media Source playback is unavailable');
            firstAacTimestamp = timestamp;
            aacMediaSource = new MediaSourceType();
            aacObjectUrl = URL.createObjectURL(aacMediaSource);
            aacElement = new Audio();
            aacElement.disableRemotePlayback = true;
            aacElement.preload = 'auto';
            aacElement.src = aacObjectUrl;
            aacNode = audioContext.createMediaElementSource(aacElement);
            aacNode.connect(audioGain);
            const audioElement = aacElement;
            aacElement.addEventListener('canplay', () => {
              if (aacElement === audioElement && !disposed) syncAac();
            });
            aacElement.addEventListener('error', () => {
              if (aacElement !== audioElement || disposed) return;
              status.textContent = 'Audio playback failed';
              debug('aac-media-error', { code: aacElement?.error?.code });
            });
            aacElement.load();
            await once(aacMediaSource, 'sourceopen', signal);
            aacSourceBuffer = aacMediaSource.addSourceBuffer(audioMime);
            aacSourceBuffer.mode = 'segments';
          }
          if (aacElement.currentTime > 60 && aacSourceBuffer.buffered.length &&
              aacSourceBuffer.buffered.start(0) < aacElement.currentTime - 60) {
            const removed = once(aacSourceBuffer, 'updateend', signal);
            aacSourceBuffer.remove(0, aacElement.currentTime - 60);
            await removed;
          }
          try {
            await appendBuffer(aacSourceBuffer, payload, signal);
          } catch (error) {
            throw new Error('Browser rejected the AAC stream', { cause: error });
          }
          if (aacSourceBuffer.buffered.length &&
              aacSourceBuffer.buffered.end(aacSourceBuffer.buffered.length - 1) - aacElement.currentTime > 15) {
            throw new Error('Camera AAC exceeded its buffered audio limit');
          }
          syncAac();
        } else if (kind === 8) {
          if (payload.length !== 4) throw new Error('Camera video format is invalid');
          videoTimestampScaleHint = new DataView(payload.buffer, payload.byteOffset, payload.byteLength).getUint32(0);
          if (!videoTimestampScaleHint || videoTimestampScaleHint > 1000000) throw new Error('Camera video clock is invalid');
        } else if (kind === 9) {
          if (payload.length !== 16) throw new Error('Camera recording clock is invalid');
          const clock = new DataView(payload.buffer, payload.byteOffset, payload.byteLength);
          const mediaTime = clock.getFloat64(0), cameraTime = clock.getFloat64(8);
          const previous = cameraClockMappings.at(-1);
          if (!Number.isFinite(mediaTime) || !Number.isFinite(cameraTime) || mediaTime < 0
              || cameraTime < 0 || (previous && mediaTime <= previous.mediaTime)) {
            throw new Error('Camera recording clock is invalid');
          }
          cameraClockMappings.push({ mediaTime, cameraTime });
          // Retain the current anchor and bounded future fragment mappings.
          while (cameraClockMappings.length > 1 && cameraClockMappings[1].mediaTime <= video.currentTime) {
            cameraClockMappings.shift();
          }
          if (cameraClockMappings.length > 256) throw new Error('Camera recording clock exceeded its buffer limit');
        } else if (kind === 3) {
          streamComplete = true;
          debug('stream-complete', {
            videoStarted,
            videoConfirmed: videoPlaybackConfirmed,
            currentTime: Math.round(video.currentTime * 100) / 100,
            readyState: video.readyState,
          });
          if (mediaSource.readyState === 'open' && !sourceBuffer.updating) {
            mediaSource.endOfStream();
          }
          if (aacMediaSource?.readyState === 'open' && !aacSourceBuffer.updating) {
            aacMediaSource.endOfStream();
          }
          if (!videoStarted && videoRecords > 0) startVideoPlayback();
          if (playbackPromise) await playbackPromise;
          if (playbackError) throw playbackError;
          return;
        } else {
          throw new Error(`Unknown camera stream record ${kind}`);
        }
      }
      break;
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
    const sampleBytes = kind === 2 ? 2 : 1;
    if (payload.length > sampleBytes * audioChannels * audioSampleRate * 10) {
      throw new Error('Camera audio packet exceeds the scheduling limit');
    }
    if (kind === 2) schedulePcm16le(payload, timestamp);
    else scheduleCompanded(payload, timestamp, kind === 6 ? decodeAlaw : decodeMulaw);
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
  const scheduleCompanded = (payload, timestamp, decode) => {
    if (!audioContext || !audioGain || !payload.length) return;
    const samples = Math.floor(payload.length / audioChannels);
    if (!samples) return;
    const buffer = audioContext.createBuffer(audioChannels, samples, audioSampleRate);
    for (let channelIndex = 0; channelIndex < audioChannels; channelIndex++) {
      const channel = buffer.getChannelData(channelIndex);
      for (let index = 0; index < samples; index++) {
        channel[index] = decode(payload[index * audioChannels + channelIndex]);
      }
    }
    scheduleAudioBuffer(buffer, timestamp);
  };
  const scheduleAudioBuffer = (buffer, timestamp) => {
    if (audioStalled) return;
    const contiguousTime = Math.max(audioNextTime, audioContext.currentTime + 0.02);
    const timestampTime = audioTimestampScale && firstVideoTimestamp !== null && timestamp
      ? videoAudioStartTime + timestamp / audioTimestampScale - firstVideoTimestamp / (videoTimestampScaleHint || audioTimestampScale)
      : contiguousTime;
    const startTime = Math.max(contiguousTime, timestampTime);
    if (audioSources.size >= 512 || startTime + buffer.duration - audioContext.currentTime > 15) {
      throw new Error('Camera audio exceeded its scheduled buffer limit');
    }
    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioGain);
    audioSources.add(source);
    source.addEventListener('ended', () => audioSources.delete(source), { once: true });
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
  on(video, 'timeupdate', syncAac);
  on(video, 'playing', syncAac);
  on(video, 'pause', syncAac);
  on(video, 'waiting', () => aacElement?.pause());
  if (isControlledStream) {
    Object.defineProperty(player, 'currentTime', { get: position, set: time => resume(time), configurable: true });
    on(video, 'pause', () => {
      if (!streamStarting) pause();
    });
    on(video, 'ended', () => {
      // video.load() during an in-place timeline seek can emit a late ended
      // event from the detached MediaSource. It must not pause the new stream.
      if (streamStarting || !video.ended) return;
      if (!continuous) offset = clip.duration;
      paused = true;
      release();
      status.textContent = continuous ? 'End of available recording' : 'Finished';
    });
    on(video, 'click', toggleRemotePlayback);
  }
  const clearWaitingStatus = () => {
    if (waitingTimer !== null) clearTimeout(waitingTimer);
    waitingTimer = null;
    if (status.textContent === 'Waiting for video') {
      setLoading(false);
      status.textContent = '';
    }
  };
  on(video, 'playing', () => {
    if (audioStalled) {
      audioStalled = false;
      audioContext?.resume().catch(() => {});
    }
    streamStarting = false;
    paused = false;
    lastPlaybackProgress = performance.now();
    clearWaitingStatus();
    setLoading(false);
    status.textContent = '';
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!disposed) video.parentElement.classList.remove('camera-player--switching');
    }));
    if (playingReportedRevision !== revision) {
      playingReportedRevision = revision;
      debug('playing');
    }
  });
  on(video, 'timeupdate', () => {
    lastPlaybackProgress = performance.now();
    clearWaitingStatus();
    if (isControlledStream && progressReportedRevision !== revision) {
      progressReportedRevision = revision;
      debug('progress', {
        position: Math.round(position() * 100) / 100,
        readyState: video.readyState,
      });
    }
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
    // MediaSource emits brief waiting events at normal fragment boundaries.
    // Only surface a real stall: no video-time progress for 1.5 seconds.
    if (
      !video.getAttribute('src')
      || streamStarting
      || video.parentElement.classList.contains('camera-player--switching')
      || waitingTimer !== null
    ) return;
    const progressAtWait = lastPlaybackProgress || performance.now();
    waitingTimer = setTimeout(() => {
      waitingTimer = null;
      if (
        !disposed
        && !paused
        && !streamStarting
        && video.getAttribute('src')
        && video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA
        && lastPlaybackProgress <= progressAtWait
      ) {
        // Audio is carried separately. Once video has genuinely stalled,
        // discard scheduled audio rather than letting sound run ahead of a
        // frozen frame.
        clearScheduledAudio();
        audioStalled = true;
        audioContext?.suspend().catch(() => {});
        debug('audio-stalled');
        setLoading(true);
        status.textContent = 'Waiting for video';
      }
    }, 1500);
  });
  on(video, 'error', () => {
    if (continuous && isControlledStream && !video.error) {
      // A detached MediaSource can still dispatch its terminal error through
      // the shared video element after a newer segment is playing. Native
      // transport and SourceBuffer errors reject at their origin above; this
      // anonymous event cannot safely identify the failed segment.
      debug('stale-media-error-ignored');
      return;
    }
    // Loading the fresh MediaSource during a timeline seek can emit a stale
    // error from the detached buffer. The current stream is still valid.
    if (!video.getAttribute('src') || (streamStarting && !video.error)) return;
    paused = true;
    release();
    status.textContent = 'Playback unavailable: ' + (video.error?.message || 'video error');
  });
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
    // Keep the actual session state. A paused timeline selection needs its
    // own seek followed by resume; forcing paused false loses that seek.
    void resume(Math.max(0, Number(time) || 0), null, continuous);
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
  return waitForMediaEvent(target, event, signal);
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
  return waitForMediaEvent(sourceBuffer, 'updateend', signal, () => sourceBuffer.appendBuffer(payload));
}

function waitForMediaEvent(target, event, signal, start = null) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      target.removeEventListener(event, done);
      target.removeEventListener('error', failed);
      signal?.removeEventListener('abort', abort);
    };
    const done = value => { cleanup(); resolve(value); };
    const failed = () => { cleanup(); reject(new Error('Browser rejected the media stream')); };
    const abort = () => { cleanup(); reject(new DOMException('Playback cancelled', 'AbortError')); };
    if (signal?.aborted) { abort(); return; }
    target.addEventListener(event, done);
    target.addEventListener('error', failed);
    signal?.addEventListener('abort', abort);
    try { start?.(); } catch (error) { cleanup(); reject(error); }
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

function decodeAlaw(value) {
  const sample = value ^ 0x55;
  const exponent = (sample >> 4) & 7;
  let magnitude = ((sample & 15) << 4) + (exponent ? 0x108 : 8);
  if (exponent > 1) magnitude <<= exponent - 1;
  return (sample & 0x80 ? magnitude : -magnitude) / 32768;
}
