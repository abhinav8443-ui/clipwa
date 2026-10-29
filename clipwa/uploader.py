"""
Automated YouTube Shorts uploader.
Dual-Engine:
- Engine A: YouTube Data API v3 (Used if client_secrets.json exists)
- Engine B: Zero-Config Playwright Browser Automation Uploader (Default when no client_secrets.json exists)
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config


class YouTubeShortsUploader:
    """Manages YouTube Shorts authentication and uploads via API or Playwright Studio automation."""

    def __init__(
        self,
        client_secrets_file: Optional[str] = None,
        token_file: Optional[str] = None,
        user_data_dir: Optional[Path] = None,
    ):
        self.client_secrets_file = client_secrets_file or Config.YOUTUBE_CLIENT_SECRETS_FILE
        self.token_file = token_file or Config.YOUTUBE_TOKEN_FILE
        self.user_data_dir = user_data_dir or (Config.BASE_DIR / "youtube_auth_profile")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._youtube_client = None

    # -------------------------------------------------------------------------
    # Engine B: Zero-Config Playwright YouTube Studio Automation
    # -------------------------------------------------------------------------

    def login_studio(self, timeout_sec: int = 300) -> bool:
        """
        Open persistent browser to YouTube Studio so user can log in once.
        Session is permanently saved inside youtube_auth_profile.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        print(f"\n[YouTubeUploader] Launching browser with persistent profile at: {self.user_data_dir}")

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=browser_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport=None,
            )
            page = context.new_page() if not context.pages else context.pages[0]

            print("[YouTubeUploader] Navigating to https://studio.youtube.com ...")
            try:
                page.goto("https://studio.youtube.com", wait_until="load", timeout=60000)
            except Exception:
                pass

            # Handle browser upgrade notice if shown
            try:
                skip_btn = page.locator("text='SKIP TO YOUTUBE STUDIO', text='Skip to YouTube Studio'")
                if skip_btn.count() > 0 and skip_btn.first.is_visible():
                    skip_btn.first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            current_url = page.url
            if "accounts.google.com" in current_url or "signin" in current_url.lower():
                print("\n" + "=" * 75)
                print(" 🔓 Please sign in to your YouTube account in the opened browser window!")
                print("    (One-time setup — your session and cookies will be saved forever!)")
                print("=" * 75 + "\n")

                start_t = time.time()
                logged_in = False
                while time.time() - start_t < timeout_sec:
                    try:
                        cur = page.url
                        if "studio.youtube.com" in cur and "accounts.google.com" not in cur:
                            logged_in = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

                if not logged_in:
                    print("[YouTubeUploader] Login timed out. Please run 'clipwa login' again.")
                    context.close()
                    return False
            else:
                print("✅ Already logged in to YouTube Studio!")

            print("✅ Successfully authenticated to YouTube Studio! Saving session...")
            page.wait_for_timeout(3000)
            context.close()
            return True

    def upload_via_studio(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads a video to YouTube Shorts using Playwright Studio automation without API keys.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        # Ensure #Shorts is present in title (max 100 chars)
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"
        title = title[:100]

        if dry_run:
            if progress_callback:
                progress_callback({"stage": "upload", "message": "Dry-run: YouTube Studio upload skipped."})
            return {
                "status": "dry_run_success",
                "engine": "studio_playwright",
                "video_id": "dry_run_studio_id",
                "video_url": "https://youtube.com/shorts/dry_run_studio",
                "title": title,
                "privacy": privacy_status,
            }

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        if progress_callback:
            progress_callback({"stage": "upload", "message": "Opening YouTube Studio in browser..."})

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=browser_args,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport=None,
            )
            page = context.new_page() if not context.pages else context.pages[0]

            # 1. Check Studio Access
            try:
                page.goto("https://studio.youtube.com", wait_until="load", timeout=60000)
            except Exception:
                pass

            # Handle browser upgrade notice if shown
            try:
                skip_btn = page.locator("text='SKIP TO YOUTUBE STUDIO', text='Skip to YouTube Studio'")
                if skip_btn.count() > 0 and skip_btn.first.is_visible():
                    skip_btn.first.click()
                    page.wait_for_timeout(3000)
            except Exception:
                pass

            cur_url = page.url
            if "accounts.google.com" in cur_url or "signin" in cur_url.lower():
                print("\n" + "=" * 75)
                print(" 🔓 Please sign in to your YouTube account in the opened browser window!")
                print("    (One-time setup — your session and cookies will be saved forever!)")
                print("=" * 75 + "\n")
                start_login = time.time()
                while time.time() - start_login < 300:
                    try:
                        if "studio.youtube.com" in page.url and "accounts.google.com" not in page.url:
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

            # Wait for Studio main layout
            page.wait_for_timeout(3000)

            if progress_callback:
                progress_callback({"stage": "upload", "message": "Opening upload dialog in YouTube Studio..."})

            # 2. Trigger File Upload Picker
            try:
                create_btn = page.locator('#create-icon, button[aria-label="Create"], ytcp-button#create-icon').first
                create_btn.wait_for(state="visible", timeout=20000)
                create_btn.click()
                page.wait_for_timeout(1000)

                upload_item = page.locator("ytcp-text-menu-item:has-text('Upload videos'), #text-item-0").first
                upload_item.wait_for(state="visible", timeout=10000)
                upload_item.click()
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"[YouTubeUploader] Notice triggering Create menu: {e}")

            file_input = page.locator("input[type='file']").first
            file_input.wait_for(state="attached", timeout=20000)
            file_input.set_input_files(str(video_path))

            # 3. Wait for Upload Dialog & Title Input
            if progress_callback:
                progress_callback({"stage": "upload", "message": "Uploading video file to YouTube Studio..."})

            title_box = page.locator("#title-textarea #textbox, #textbox[aria-label*='title' i]").first
            title_box.wait_for(state="visible", timeout=60000)
            page.wait_for_timeout(2000)

            # 4. Fill Title
            try:
                title_box.click()
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                title_box.fill(title)
            except Exception as e:
                print(f"[YouTubeUploader] Warning filling title: {e}")

            # 5. Fill Description
            try:
                desc_box = page.locator("#description-textarea #textbox, #textbox[aria-label*='description' i]").first
                if desc_box.is_visible():
                    desc_box.click()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    full_desc = f"{description}\n\n" + " ".join(f"#{t.strip('#')}" for t in (tags or []))
                    desc_box.fill(full_desc.strip())
            except Exception as e:
                print(f"[YouTubeUploader] Warning filling description: {e}")

            # 6. Select 'Not Made for Kids'
            try:
                not_for_kids = page.locator(
                    "tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK'], "
                    "tp-yt-paper-radio-button:has-text('No, it\'s not made for kids')"
                ).first
                if not_for_kids.is_visible():
                    not_for_kids.click()
            except Exception as e:
                print(f"[YouTubeUploader] Notice setting audience: {e}")

            # 7. Next buttons (3 times: Details -> Video Elements -> Checks -> Visibility)
            for step_idx in range(3):
                page.wait_for_timeout(1500)
                try:
                    next_btn = page.locator("#next-button, ytcp-button#next-button, button:has-text('Next')").first
                    next_btn.wait_for(state="visible", timeout=20000)
                    next_btn.click()
                except Exception as e:
                    print(f"[YouTubeUploader] Notice on Next step {step_idx+1}: {e}")

            page.wait_for_timeout(2000)

            # 8. Visibility Radio Selection
            priv_upper = privacy_status.upper()
            try:
                if priv_upper == "PUBLIC":
                    vis_radio = page.locator("tp-yt-paper-radio-button[name='PUBLIC'], tp-yt-paper-radio-button:has-text('Public')").first
                elif priv_upper == "UNLISTED":
                    vis_radio = page.locator("tp-yt-paper-radio-button[name='UNLISTED'], tp-yt-paper-radio-button:has-text('Unlisted')").first
                else:
                    vis_radio = page.locator("tp-yt-paper-radio-button[name='PRIVATE'], tp-yt-paper-radio-button:has-text('Private')").first

                if vis_radio.is_visible():
                    vis_radio.click()
            except Exception as e:
                print(f"[YouTubeUploader] Notice setting visibility: {e}")

            # Extract video URL before or after save
            video_url = None
            try:
                link_elem = page.locator("a.style-scope.ytcp-video-info, a[href*='youtu.be']").first
                if link_elem.is_visible():
                    video_url = link_elem.get_attribute("href")
            except Exception:
                pass

            # 9. Wait for upload to complete and click Done / Publish
            if progress_callback:
                progress_callback({"stage": "upload", "message": "Finalizing upload and publishing Short..."})

            try:
                done_btn = page.locator("#done-button, ytcp-button#done-button, button:has-text('Save'), button:has-text('Publish')").first
                done_btn.wait_for(state="visible", timeout=30000)

                # Wait until Done button is enabled
                for _ in range(45):
                    if not done_btn.get_attribute("disabled"):
                        break
                    page.wait_for_timeout(2000)

                done_btn.click()
            except Exception as e:
                print(f"[YouTubeUploader] Notice clicking publish: {e}")

            page.wait_for_timeout(5000)

            # Fallback URL extraction from publish modal
            if not video_url:
                try:
                    share_link = page.locator("a[href*='youtu.be'], a[href*='youtube.com/shorts']").first
                    if share_link.is_visible():
                        video_url = share_link.get_attribute("href")
                except Exception:
                    pass

            context.close()

            return {
                "status": "uploaded",
                "engine": "studio_playwright",
                "video_id": video_url.split("/")[-1] if video_url else "unknown",
                "video_url": video_url or "https://youtube.com/shorts",
                "title": title,
                "privacy": privacy_status,
            }

    # -------------------------------------------------------------------------
    # Engine A: YouTube Data API v3 (Used when client_secrets.json exists)
    # -------------------------------------------------------------------------

    def get_authenticated_service(self):
        """Authenticate via OAuth 2.0 and return YouTube resource."""
        if self._youtube_client is not None:
            return self._youtube_client

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(
                    self.token_file, ["https://www.googleapis.com/auth/youtube.upload"]
                )
            except Exception:
                creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, ["https://www.googleapis.com/auth/youtube.upload"]
                )
                creds = flow.run_local_server(port=0)

            with open(self.token_file, "w", encoding="utf-8") as token:
                token.write(creds.to_json())

        self._youtube_client = build("youtube", "v3", credentials=creds)
        return self._youtube_client

    def _upload_via_api(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24",
        privacy_status: str = "public",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Resumable upload using YouTube Data API v3."""
        from googleapiclient.http import MediaFileUpload

        youtube = self.get_authenticated_service()

        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"[:100]

        all_tags = list(tags or [])
        if "shorts" not in [t.lower() for t in all_tags]:
            all_tags.append("shorts")

        body = {
            "snippet": {
                "title": title,
                "description": f"{description}\n\n#Shorts",
                "tags": all_tags,
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": privacy_status.lower(),
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video_path,
            chunksize=5 * 1024 * 1024,
            resumable=True,
            mimetype="video/mp4",
        )

        request = youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress_callback:
                pct = int(status.progress() * 100)
                progress_callback({
                    "stage": "upload_progress",
                    "percent": pct,
                    "message": f"Uploading to YouTube API: {pct}%",
                })

        video_id = response.get("id")
        video_url = f"https://youtube.com/shorts/{video_id}"

        return {
            "status": "uploaded",
            "engine": "youtube_data_api_v3",
            "video_id": video_id,
            "video_url": video_url,
            "title": title,
            "privacy": privacy_status,
        }

    # -------------------------------------------------------------------------
    # Main Universal Upload Dispatcher
    # -------------------------------------------------------------------------

    def upload_short(
        self,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        category_id: str = "24",
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads a video file directly to YouTube Shorts.
        - Uses YouTube Data API v3 if client_secrets.json exists.
        - Automatically uses Playwright YouTube Studio uploader if no API keys are present!
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        if dry_run:
            if progress_callback:
                progress_callback({"stage": "upload", "message": "Dry-run: Upload skipped."})
            return {
                "status": "dry_run_success",
                "engine": "dry_run",
                "video_id": "dry_run_id",
                "video_url": "https://youtube.com/shorts/dry_run_preview",
                "title": title,
                "privacy": privacy_status,
            }

        # Select Engine A (API) if client_secrets.json exists, otherwise Engine B (Playwright Studio)
        if os.path.exists(self.client_secrets_file):
            try:
                return self._upload_via_api(
                    video_path=video_path,
                    title=title,
                    description=description,
                    tags=tags,
                    category_id=category_id,
                    privacy_status=privacy_status,
                    progress_callback=progress_callback,
                )
            except Exception as e:
                print(f"[YouTubeUploader] YouTube API upload failed ({e}), falling back to Playwright Studio uploader...")

        # Engine B: Playwright Studio Uploader
        return self.upload_via_studio(
            video_path=video_path,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
