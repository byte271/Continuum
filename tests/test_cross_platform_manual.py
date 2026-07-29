from __future__ import annotations

import os
import platform
import unittest
from pathlib import Path

from continuum.image import load_image


class CrossPlatformManualTests(unittest.TestCase):
    @unittest.skipUnless(
        platform.system() == "Darwin"
        and platform.machine().lower() in {"arm64", "aarch64"}
        and bool(os.environ.get("CONTINUUM_LINUX_IMAGE")),
        "requires Apple Silicon and CONTINUUM_LINUX_IMAGE from a real Linux x86_64 run",
    )
    def test_real_linux_x86_64_image_resumes_on_apple_silicon(self):
        image = Path(os.environ["CONTINUUM_LINUX_IMAGE"])
        loaded = load_image(image)
        self.assertEqual(loaded.manifest["source"]["os"], "Linux")
        self.assertEqual(loaded.manifest["source"]["architecture"], "x86_64")
        vm = loaded.restore_vm("bundle")
        vm.run()


if __name__ == "__main__":
    unittest.main()

