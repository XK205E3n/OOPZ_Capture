from __future__ import annotations

import asyncio
import unittest


class BrowserPcmIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_remote_media_track_produces_pcm_chunks(self) -> None:
        try:
            from oopz_sdk import OopzConfig
            from oopz_sdk.transport.voice_browser import BrowserVoiceTransport
        except ModuleNotFoundError as error:
            self.skipTest(f"oopz-sdk is not installed: {error}")
        from oopz_capture.browser_probe import AgoraBrowserProbe

        backend = BrowserVoiceTransport(OopzConfig(voice_browser_headless=True))
        try:
            try:
                await backend.start()
            except RuntimeError as error:
                if "Executable doesn't exist" in str(error):
                    self.skipTest("Playwright Chromium is not installed")
                raise
            probe = AgoraBrowserProbe(backend)
            await probe.install()
            await probe.start_audio_capture()
            await probe._evaluate("""
                (async () => {
                  const sourceContext = new AudioContext();
                  const oscillator = sourceContext.createOscillator();
                  const destination = sourceContext.createMediaStreamDestination();
                  oscillator.connect(destination);
                  oscillator.start();
                  const user = {
                    uid: 777,
                    hasAudio: true,
                    audioTrack: {
                      getMediaStreamTrack: () => destination.stream.getAudioTracks()[0]
                    }
                  };
                  client = {
                    connectionState: "CONNECTED",
                    remoteUsers: [user],
                    on: () => {},
                    subscribe: async () => {}
                  };
                  window.__oopzTestAudio = {sourceContext, oscillator};
                  window.oopzCaptureSnapshot();
                  return true;
                })()
            """)
            await asyncio.sleep(0.35)
            chunks = await probe.drain_audio()
            await probe.stop_audio_capture()
        finally:
            await backend.close()

        self.assertGreater(len(chunks), 0)
        self.assertEqual({str(item["uid"]) for item in chunks}, {"777"})
        self.assertTrue(all(item["frameCount"] > 0 for item in chunks))
        self.assertTrue(all(item["pcm16Base64"] for item in chunks))


if __name__ == "__main__":
    unittest.main()
