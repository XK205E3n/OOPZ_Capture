from __future__ import annotations

import unittest


class BrowserProbeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_installs_in_sdk_browser_transport(self) -> None:
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
            snapshot = await probe.snapshot()
        finally:
            await backend.close()

        self.assertIn("probe_installed", [item.get("type") for item in snapshot.events])
        self.assertEqual(snapshot.remote_users, [])

