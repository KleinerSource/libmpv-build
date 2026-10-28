import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("mpv_build", Path(__file__).with_name("build.py"))
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildContracts(unittest.TestCase):
    def test_ffmpeg_comes_from_official_upstream(self):
        self.assertEqual(build.LOCK["sources"]["ffmpeg"]["url"], "https://github.com/FFmpeg/FFmpeg.git")
        for spec in build.LOCK["sources"].values():
            self.assertNotIn("KleinerSource", spec["url"])
        self.assertNotIn("iOSNative", Path(build.__file__).read_text(encoding="utf-8"))

    def test_partial_release_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "libmpv-ios.tar.gz").write_bytes(b"ios")
            with self.assertRaisesRegex(ValueError, "全部 Android ABI"):
                build.manifest(root)

    def test_complete_release_records_content_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("libmpv-ios.tar.gz", *(f"full-{abi}.jar" for abi in build.ABIS)):
                (root / name).write_bytes(name.encode())
            build.manifest(root)
            data = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "0.41.0")
            self.assertEqual(len(data["assets"]), 5)
            self.assertEqual(data["assets"]["libmpv-ios.tar.gz"],
                             build.hashlib.sha256(b"libmpv-ios.tar.gz").hexdigest())

    def test_pgs_decoder_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.h").write_text("", encoding="utf-8")
            config = "#define CONFIG_H264_DECODER 1\n#define CONFIG_HEVC_DECODER 1\n"
            path = root / "config_components.h"
            path.write_text(config, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PGS"):
                build.check_ffmpeg_config(root)
            path.write_text(config + "#define CONFIG_HDMV_PGS_SUBTITLE_DECODER 1\n", encoding="utf-8")
            build.check_ffmpeg_config(root)


if __name__ == "__main__":
    unittest.main()
