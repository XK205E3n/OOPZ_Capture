from __future__ import annotations

import unittest


class BrowserCaptureIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_controls_work_before_join(self) -> None:
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
            snapshot = await probe.snapshot()
            self.assertEqual(await probe.drain_audio(), [])
            await probe.stop_audio_capture()
        finally:
            await backend.close()

        event_types = [item.get("type") for item in snapshot.events]
        self.assertIn("capture_started", event_types)


if __name__ == "__main__":
    unittest.main()
