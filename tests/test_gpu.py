"""GPU probe must not spawn nvidia-smi when NVML answers."""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _name in ("feedparser", "httpx", "yaml"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from app.gpu import GpuStatus, _probe_nvidia_smi  # noqa: E402


class NvmlFirst(unittest.TestCase):
    def test_nvml_rows_skip_nvidia_smi(self) -> None:
        st = GpuStatus()
        rows = [{"index": 0, "name": "GPU", "util": 1, "used": 10, "total": 100}]
        with patch("app.gpu._nvml_rows", return_value=rows):
            with patch("app.gpu.subprocess.run") as run:
                _probe_nvidia_smi(st)
        run.assert_not_called()
        self.assertEqual(st.probes.get("nvidia_smi"), "nvml")
        self.assertEqual(len(st.gpus), 1)

    def test_hidden_kwargs_hide_the_console(self) -> None:
        import os
        import subprocess
        from app.gpu import _hidden

        kwargs = _hidden()
        if os.name != "nt":
            self.assertEqual(kwargs, {})
            return
        self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(kwargs["creationflags"], 0x08000000)


if __name__ == "__main__":
    unittest.main()
