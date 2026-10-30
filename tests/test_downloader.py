"""
Unit and integration tests for clipwa downloader timeout and fallback logic.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from clipwa.downloader import MediaDownloader
import yt_dlp


def test_downloader_options():
    dl = MediaDownloader()
    print("[TEST] Checking default options and config...")
    assert dl.ffmpeg_path is not None
    print("✓ MediaDownloader initialized successfully.")


def test_fallback_on_range_timeout():
    """
    Simulate yt-dlp raising a ReadTimeoutError / DownloadError during range request.
    Verify that download_clip_section catches the error and smoothly switches to fallback stream download.
    """
    dl = MediaDownloader()
    output_target = str(dl.temp_dir / "test_clip_timeout_fallback.mp4")

    if os.path.exists(output_target):
        try:
            os.remove(output_target)
        except Exception:
            pass

    real_YoutubeDL = yt_dlp.YoutubeDL
    call_count = {"count": 0}

    def mock_YoutubeDL(params=None, *args, **kwargs):
        call_count["count"] += 1
        current_call = call_count["count"]

        # Verify that timeout and retries options are properly configured
        assert params is not None, "params should not be None"
        assert params.get("socket_timeout") == 30, f"Expected socket_timeout 30, got {params.get('socket_timeout')}"
        assert params.get("retries") == 20, f"Expected retries 20, got {params.get('retries')}"
        assert params.get("fragment_retries") == 20, f"Expected fragment_retries 20, got {params.get('fragment_retries')}"
        assert params.get("skip_unavailable_fragments") is True

        instance = real_YoutubeDL(params, *args, **kwargs)

        def fake_download(urls):
            if current_call == 1:
                # Simulate YouTube CDN Range Request Timeout
                raise yt_dlp.utils.DownloadError("HTTPSConnectionPool(host='rr2---sn.googlevideo.com', port=443): Read timed out.")
            else:
                outtmpl = params.get("outtmpl", "")
                if isinstance(outtmpl, dict):
                    outtmpl_str = outtmpl.get("default", "")
                else:
                    outtmpl_str = str(outtmpl)
                fallback_file = outtmpl_str.replace("%(ext)s", "mp4")
                cmd = [
                    dl.ffmpeg_path,
                    "-y",
                    "-f", "lavfi",
                    "-i", "color=c=blue:s=320x240:d=2",
                    "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "2",
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    fallback_file
                ]
                import subprocess
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                return 0

        instance.download = fake_download
        return instance

    with patch("yt_dlp.YoutubeDL", side_effect=mock_YoutubeDL):
        res = dl.download_clip_section(
            url_or_path="https://www.youtube.com/watch?v=mock_video_id",
            start_time=0.0,
            end_time=1.0,
            output_path=output_target
        )

        assert os.path.exists(res), f"Result file {res} should exist!"
        assert os.path.getsize(res) > 0, "Result file should not be empty!"
        print(f"✓ Fallback test passed! File produced: {res} ({os.path.getsize(res)} bytes)")

        try:
            os.remove(res)
        except Exception:
            pass


if __name__ == "__main__":
    test_downloader_options()
    test_fallback_on_range_timeout()
    print("ALL DOWNLOADER TIMEOUT & FALLBACK UNIT TESTS PASSED!")
