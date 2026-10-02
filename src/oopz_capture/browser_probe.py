from __future__ import annotations

import asyncio
from typing import Any

from .models import ProbeSnapshot


PROBE_VERSION = "m1-m6-v3"


INSTALL_PROBE_SCRIPT = r"""
(() => {
  if (window.__oopzCaptureProbe?.version === "m1-m6-v3") return true;
  const state = {
    version: "m1-m6-v3", voiceStates: new Map(), events: [], audioChunks: [],
    tracks: new Map(), trackGenerations: new Map(), subscriptions: new Set(),
    clientAttached: false, recording: false, startedAtMs: 0, sequence: 0,
    audioContext: null,
  };
  const now = () => new Date().toISOString();
  const pushEvent = (type, detail = {}) => {
    state.events.push({at: now(), type, ...detail});
    if (state.events.length > 1000) state.events.splice(0, state.events.length - 1000);
  };
  const recordMapping = value => {
    if (!value || typeof value !== "object") return;
    const uid = typeof value.uid === "string" ? value.uid.trim() : "";
    const cid = Number(value.cid);
    if (!uid || !Number.isInteger(cid) || cid < 0) return;
    state.voiceStates.set(uid, {uid, cid, micMuted: Number(value.m) === 1,
      speakerMuted: Number(value.hm) === 1, observedAt: now()});
    pushEvent("voice_state", {uid, cid});
  };
  const decodeBase64Json = text => {
    if (typeof text !== "string" || text.length < 4 || text.length > 16384) return null;
    if (!/^[A-Za-z0-9+/=_-]+$/.test(text)) return null;
    try {
      const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/"));
      return JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, ch => ch.charCodeAt(0))));
    } catch (_) { return null; }
  };
  const inspectValue = (value, depth = 0, seen = new Set()) => {
    if (depth > 7 || value === null || value === undefined) return;
    if (typeof value === "string") {
      try { inspectValue(JSON.parse(value), depth + 1, seen); return; } catch (_) {}
      const decoded = decodeBase64Json(value);
      if (decoded) inspectValue(decoded, depth + 1, seen);
      return;
    }
    if (typeof value !== "object" || seen.has(value)) return;
    seen.add(value); recordMapping(value);
    for (const item of Array.isArray(value) ? value : Object.values(value)) {
      inspectValue(item, depth + 1, seen);
    }
  };
  const inspectMessage = async event => {
    try {
      let data = event.data;
      if (data instanceof Blob) data = await data.text();
      else if (data instanceof ArrayBuffer) data = new TextDecoder().decode(data);
      inspectValue(data);
    } catch (_) {}
  };
  const attachSocket = socket => {
    if (!socket || socket.__oopzCaptureAttached) return socket;
    socket.__oopzCaptureAttached = true;
    socket.addEventListener("message", inspectMessage);
    socket.addEventListener("open", () => pushEvent("agora_signal_open"));
    socket.addEventListener("close", event => pushEvent("agora_signal_close", {code: Number(event.code) || 0}));
    return socket;
  };
  const PreviousWebSocket = window.WebSocket;
  function ProbeWebSocket(url, protocols) {
    return attachSocket(protocols === undefined ? new PreviousWebSocket(url) : new PreviousWebSocket(url, protocols));
  }
  ProbeWebSocket.prototype = PreviousWebSocket.prototype;
  Object.setPrototypeOf(ProbeWebSocket, PreviousWebSocket);
  for (const key of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) ProbeWebSocket[key] = PreviousWebSocket[key];
  window.WebSocket = ProbeWebSocket;

  const bytesToBase64 = bytes => {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  };
  const enqueuePcm = (uid, generation, sampleRate, pcm) => {
    state.audioChunks.push({
      uid, generation, sequence: state.sequence++,
      sessionOffsetMs: Math.max(0, performance.now() - state.startedAtMs),
      sampleRate, frameCount: pcm.length,
      pcm16Base64: bytesToBase64(new Uint8Array(pcm.buffer)),
    });
    // This is an aggregate queue across every remote track. 4096 frames is
    // roughly 10 seconds for four 10 ms tracks and protects short browser or
    // scheduler stalls without allowing unbounded memory growth.
    if (state.audioChunks.length > 4096) {
      const dropped = state.audioChunks.length - 4096;
      state.audioChunks.splice(0, dropped);
      pushEvent("audio_queue_overflow", {dropped});
    }
  };
  const floatsToPcm = planes => {
    const pcm = new Int16Array(planes[0].length);
    for (let i = 0; i < pcm.length; i += 1) {
      let sample = 0;
      for (const plane of planes) sample += plane[i];
      sample = Math.max(-1, Math.min(1, sample / planes.length));
      pcm[i] = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
    }
    return pcm;
  };
  const stopTrack = (uid, reason) => {
    const key = String(uid ?? "");
    const current = state.tracks.get(key);
    if (!current) return;
    current.cancelled = true;
    try { if (current.reader) void current.reader.cancel(); } catch (_) {}
    try { if (current.processor) current.processor.disconnect(); } catch (_) {}
    try { if (current.source) current.source.disconnect(); } catch (_) {}
    state.tracks.delete(key);
    pushEvent("audio_track_stopped", {uid: key, generation: current.generation, reason});
  };
  const pumpTrackProcessor = async (uid, current) => {
    try {
      while (state.recording && !current.cancelled) {
        const {value: data, done} = await current.reader.read();
        if (done) break;
        try {
          const planes = [];
          for (let channel = 0; channel < data.numberOfChannels; channel += 1) {
            const bytes = data.allocationSize({planeIndex: channel, format: "f32-planar"});
            const plane = new Float32Array(bytes / 4);
            data.copyTo(plane, {planeIndex: channel, format: "f32-planar"});
            planes.push(plane);
          }
          enqueuePcm(uid, current.generation, data.sampleRate, floatsToPcm(planes));
        } finally { data.close(); }
      }
    } catch (error) {
      if (!current.cancelled) pushEvent("audio_track_failed", {uid, error: String(error?.message || error)});
    }
  };
  const startLegacyTrack = async (uid, generation, mediaTrack) => {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!state.audioContext) state.audioContext = new AudioContextClass();
    if (state.audioContext.state === "suspended") await state.audioContext.resume();
    const source = state.audioContext.createMediaStreamSource(new MediaStream([mediaTrack]));
    const processor = state.audioContext.createScriptProcessor(4096, 1, 1);
    const current = {source, processor, reader: null, generation, cancelled: false, mode: "script-processor-fallback"};
    processor.onaudioprocess = event => {
      if (!state.recording || current.cancelled) return;
      const input = event.inputBuffer;
      const planes = [];
      for (let channel = 0; channel < input.numberOfChannels; channel += 1) planes.push(input.getChannelData(channel));
      event.outputBuffer.getChannelData(0).fill(0);
      enqueuePcm(uid, generation, input.sampleRate, floatsToPcm(planes));
    };
    source.connect(processor); processor.connect(state.audioContext.destination);
    state.tracks.set(uid, current);
    pushEvent("audio_track_started", {uid, generation, sampleRate: state.audioContext.sampleRate, mode: current.mode});
  };
  const startTrack = async (user, audioTrack) => {
    const uid = String(user?.uid ?? "");
    if (!uid || !audioTrack || !state.recording) return;
    stopTrack(uid, "replaced");
    const generation = (state.trackGenerations.get(uid) || 0) + 1;
    state.trackGenerations.set(uid, generation);
    try {
      const mediaTrack = audioTrack.getMediaStreamTrack();
      if (typeof MediaStreamTrackProcessor === "function") {
        const trackProcessor = new MediaStreamTrackProcessor({track: mediaTrack});
        const current = {reader: trackProcessor.readable.getReader(), processor: null, source: null,
          generation, cancelled: false, mode: "media-stream-track-processor"};
        state.tracks.set(uid, current);
        pushEvent("audio_track_started", {uid, generation, sampleRate: 0, mode: current.mode});
        void pumpTrackProcessor(uid, current);
      } else {
        await startLegacyTrack(uid, generation, mediaTrack);
      }
    } catch (error) {
      pushEvent("audio_track_failed", {uid, error: String(error?.message || error)});
    }
  };
  const subscribeAudio = async user => {
    const uid = String(user?.uid ?? "");
    if (!uid || state.subscriptions.has(uid)) return;
    state.subscriptions.add(uid);
    try {
      await client.subscribe(user, "audio");
      pushEvent("audio_subscribed", {uid});
      await startTrack(user, user.audioTrack);
    } catch (error) {
      pushEvent("audio_subscribe_failed", {uid, error: String(error?.message || error)});
    } finally { state.subscriptions.delete(uid); }
  };
  const attachClient = () => {
    try {
      if (typeof client === "undefined" || !client) return false;
      if (!state.clientAttached) {
        state.clientAttached = true;
        const uidOf = user => String(user?.uid ?? "");
        client.on("user-joined", user => pushEvent("remote_user_joined", {uid: uidOf(user)}));
        client.on("user-left", user => { const uid = uidOf(user); stopTrack(uid, "user-left"); pushEvent("remote_user_left", {uid}); });
        client.on("user-published", (user, mediaType) => {
          const uid = uidOf(user); pushEvent("remote_user_published", {uid, mediaType: String(mediaType)});
          if (mediaType === "audio") void subscribeAudio(user);
        });
        client.on("user-unpublished", (user, mediaType) => {
          const uid = uidOf(user); if (mediaType === "audio") stopTrack(uid, "user-unpublished");
          pushEvent("remote_user_unpublished", {uid, mediaType: String(mediaType)});
        });
        client.on("connection-state-change", (current, previous, reason) => {
          pushEvent("agora_connection_state", {
            current: String(current || "unknown"),
            previous: String(previous || "unknown"),
            reason: String(reason || ""),
          });
        });
        pushEvent("agora_client_attached");
      }
      for (const user of client.remoteUsers || []) if (user.hasAudio) void subscribeAudio(user);
      return true;
    } catch (error) {
      pushEvent("agora_client_attach_failed", {error: String(error?.message || error)}); return false;
    }
  };
  const originalJoin = window.agoraJoin;
  window.agoraJoin = async (...args) => { const result = await originalJoin(...args); attachClient(); return result; };
  window.__oopzCaptureProbe = state;
  window.oopzCaptureStart = async () => {
    state.recording = true; state.startedAtMs = performance.now(); state.sequence = 0; state.audioChunks = [];
    attachClient(); pushEvent("capture_started"); return {ok: true};
  };
  window.oopzCaptureDrain = maxChunks => state.audioChunks.splice(0, Math.max(1, Math.min(256, Number(maxChunks) || 128)));
  window.oopzCaptureStop = async () => {
    state.recording = false;
    for (const uid of Array.from(state.tracks.keys())) stopTrack(uid, "capture-stop");
    pushEvent("capture_stopped"); return {ok: true, queuedChunks: state.audioChunks.length};
  };
  window.oopzCaptureSnapshot = () => {
    attachClient(); let remoteUsers = []; let connectionState = "unknown";
    try {
      if (typeof client !== "undefined" && client) {
        connectionState = String(client.connectionState || "unknown");
        remoteUsers = (client.remoteUsers || []).map(user => ({uid: String(user.uid), hasAudio: Boolean(user.hasAudio), hasAudioTrack: Boolean(user.audioTrack)}));
      }
    } catch (_) {}
    return {version: state.version, connectionState, remoteUsers,
      voiceStates: Array.from(state.voiceStates.values()), events: state.events.slice(),
      capture: {recording: state.recording, queuedChunks: state.audioChunks.length, activeTracks: Array.from(state.tracks.keys())}};
  };
  pushEvent("probe_installed", {version: state.version}); return true;
})()
"""


class AgoraBrowserProbe:
    """Identity diagnostics and isolated per-remote-track PCM capture."""

    def __init__(self, backend: Any):
        self._backend = backend

    async def _evaluate(self, expression: str) -> Any:
        await self._backend.start()
        browser_loop = getattr(self._backend, "_thread_loop", None)
        page = getattr(self._backend, "_page", None)
        if browser_loop is None or page is None:
            raise RuntimeError("oopz-sdk BrowserVoiceTransport is not ready or changed incompatibly")
        async def invoke() -> Any:
            return await page.evaluate(expression)
        future = asyncio.run_coroutine_threadsafe(invoke(), browser_loop)
        return await asyncio.wrap_future(future)

    async def install(self) -> None:
        if not await self._evaluate(INSTALL_PROBE_SCRIPT):
            raise RuntimeError("failed to install the Agora browser capture probe")

    async def snapshot(self) -> ProbeSnapshot:
        return ProbeSnapshot.from_browser(await self._backend._run_on_browser("oopzCaptureSnapshot"))

    async def start_audio_capture(self) -> None:
        result = await self._backend._run_on_browser("oopzCaptureStart")
        if not result or not result.get("ok"):
            raise RuntimeError("browser audio capture did not start")

    async def drain_audio(self, max_chunks: int = 128) -> list[dict[str, Any]]:
        return list(await self._backend._run_on_browser("oopzCaptureDrain", max_chunks) or [])

    async def stop_audio_capture(self) -> None:
        await self._backend._run_on_browser("oopzCaptureStop")
