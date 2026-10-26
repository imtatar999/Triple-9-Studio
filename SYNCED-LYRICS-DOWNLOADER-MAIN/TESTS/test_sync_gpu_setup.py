import sys
import unittest
from pathlib import Path
from unittest import mock


SOURCE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_DIR))

import studio_sync_setup as setup


class SyncGpuSetupTests(unittest.TestCase):
    def test_nvidia_detection_uses_nvidia_smi(self):
        with mock.patch.object(setup.shutil, "which", return_value="nvidia-smi.exe"), \
                mock.patch.object(setup, "run", return_value="GPU 0: NVIDIA") as runner:
            self.assertTrue(setup.nvidia_gpu_available())
        runner.assert_called_once_with(["nvidia-smi.exe", "--list-gpus"], timeout=15)

    def test_torch_cuda_info_reads_cpu_only_build(self):
        payload = '{"installed": true, "version": "2.14.0+cpu", "cuda_build": null, "cuda_available": false, "device": ""}'
        with mock.patch.object(setup, "run", return_value=payload):
            info = setup.torch_cuda_info()
        self.assertTrue(info["installed"])
        self.assertIsNone(info["cuda_build"])
        self.assertFalse(info["cuda_available"])

    def test_gpu_repair_uses_official_cuda_index_and_verifies_result(self):
        before = {"installed": True, "version": "2.14.0+cpu", "cuda_build": None, "cuda_available": False}
        after = {"installed": True, "version": "2.14.0+cu128", "cuda_build": "12.8",
                 "cuda_available": True, "device": "NVIDIA GeForce RTX 3060"}
        commands = []

        def fake_run(command, timeout=180):
            commands.append((command, timeout))
            return "installed"

        with mock.patch.object(setup, "nvidia_gpu_available", return_value=True), \
                mock.patch.object(setup.studio_runtime, "running_in_runtime", return_value=False), \
                mock.patch.object(setup, "torch_cuda_info", side_effect=[before, after]), \
                mock.patch.object(setup, "run", side_effect=fake_run):
            result = setup.install_pytorch_gpu(lambda _message: None)

        pip_commands = [command for command, _timeout in commands if "pip" in command]
        self.assertEqual(len(pip_commands), 1)
        self.assertIn("--force-reinstall", pip_commands[0])
        self.assertIn(setup.PYTORCH_CUDA_INDEX_URL, pip_commands[0])
        self.assertTrue(result["cuda_available"])


if __name__ == "__main__":
    unittest.main()
