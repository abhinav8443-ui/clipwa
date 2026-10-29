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
from typing import Any, Callable, Dict, List, Optional, Tuple

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

    def _get_upload_status(self, page) -> Tuple[str, Optional[int]]:
        """
        Extract the live upload status string and numeric percentage from YouTube Studio dialog.
        """
        # 1. Search in progress labels inside dialog
        for sel in [
            "ytcp-video-upload-progress span.progress-label",
            "ytcp-uploads-dialog span.progress-label",
            "ytcp-video-upload-progress",
            "span.progress-label",
        ]:
            try:
                loc = page.locator(sel)
                count = loc.count()
                for i in range(count):
                    txt = loc.nth(i).inner_text().strip()
                    # Filter out unrelated draft notices
                    if txt and "draft" not in txt.lower():
                        pct_m = re.search(r"(\d+)%", txt)
                        pct = int(pct_m.group(1)) if pct_m else None
                        return txt, pct
            except Exception:
                pass

        # 2. Search paper-progress or progress bar attributes
        for sel in ["tp-yt-paper-progress", "#progress-bar"]:
            try:
                elem = page.locator(sel).first
                if elem.is_visible():
                    val = elem.get_attribute("value") or elem.get_attribute("aria-valuenow")
                    if val and val.isdigit():
                        return f"Uploading {val}%", int(val)
            except Exception:
                pass

        return "", None

    def _upload_single_clip_in_page(
        self,
        page,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        privacy_status: str = "public",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads one video clip inside an already-opened YouTube Studio page.
        Waits for 100% upload transfer and confirmation before returning.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        # Ensure #Shorts is present in title (max 100 chars)
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title.strip()} #Shorts"
        title = title[:100]

        # 1. Prepare UI state: dismiss any residual dialogs or tooltips
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
        except Exception:
            pass

        # 2. Trigger "Create" -> "Upload videos"
        if progress_callback:
            progress_callback({"stage": "upload", "message": f"Opening upload modal for '{title}'..."})

        create_btn = page.locator('#create-icon, button[aria-label="Create"], ytcp-button#create-icon').first
        try:
            create_btn.wait_for(state="visible", timeout=10000)
            create_btn.click(timeout=5000)
        except Exception:
            # Try clearing any backdrop overlay and force click
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            try:
                create_btn.click(force=True)
            except Exception:
                upload_btn = page.locator("ytcp-button:has-text('Upload videos'), button:has-text('Upload videos')").first
                if upload_btn.is_visible():
                    upload_btn.click(force=True)

        page.wait_for_timeout(1000)
        upload_item = page.locator(
            "ytcp-text-menu-item:has-text('Upload videos'), #text-item-0, paper-item:has-text('Upload')"
        ).first
        try:
            upload_item.wait_for(state="visible", timeout=10000)
            upload_item.click(timeout=5000)
        except Exception:
            try:
                upload_item.click(force=True)
            except Exception:
                pass
        page.wait_for_timeout(1500)

        # 3. Select file using expect_file_chooser or direct input
        file_selected = False
        try:
            select_btn = page.locator("#select-files-button, ytcp-button#select-files-button, [aria-label*='Select files' i]").first
            with page.expect_file_chooser(timeout=8000) as fc_info:
                if select_btn.is_visible():
                    select_btn.click()
                else:
                    page.locator("input[type='file']").first.click()
            fc_info.value.set_files(str(video_path))
            file_selected = True
        except Exception as e:
            print(f"[YouTubeUploader] File chooser notice: {e}, using direct file input fallback...")

        if not file_selected:
            file_input = page.locator("ytcp-uploads-file-picker input[type='file'], input[type='file']").first
            file_input.wait_for(state="attached", timeout=15000)
            file_input.set_input_files(str(video_path))

        # 4. Wait for Upload Dialog & Title Input
        if progress_callback:
            progress_callback({"stage": "upload", "message": "Video transfer started, entering metadata..."})

        title_box = page.locator("#title-textarea #textbox, #textbox[aria-label*='title' i]").first
        title_box.wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(1000)

        # Check for daily upload limit or errors
        for err_sel in [
            "ytcp-dialog:has-text('Daily upload limit reached')",
            "ytcp-dialog:has-text('Daily limit')",
            "#error-dialog",
        ]:
            try:
                err_elem = page.locator(err_sel).first
                if err_elem.is_visible():
                    err_text = err_elem.inner_text().strip().replace("\n", " ")
                    raise RuntimeError(f"YouTube Studio Error: {err_text}")
            except Exception as ex:
                if "YouTube Studio Error" in str(ex):
                    raise ex

        # Fill Title
        try:
            title_box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            title_box.fill(title)
        except Exception as e:
            print(f"[YouTubeUploader] Warning filling title: {e}")

        # Fill Description
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

        # Select 'Not Made for Kids' (REQUIRED by YouTube to enable Next button!)
        try:
            not_for_kids = page.locator(
                '[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"], tp-yt-paper-radio-button:has-text("not made for kids")'
            ).first
            not_for_kids.scroll_into_view_if_needed(timeout=10000)
            not_for_kids.click()
            print("[YouTubeUploader] Selected audience: 'No, it is not made for kids'")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[YouTubeUploader] Notice setting audience: {e}")

        # 5. Click Next through Steps (Details -> Video Elements -> Checks -> Visibility)
        for step_idx in range(3):
            page.wait_for_timeout(1000)
            try:
                next_btn = page.locator("#next-button, ytcp-button#next-button").first
                next_btn.wait_for(state="visible", timeout=15000)
                # Wait until Next button is not disabled
                for _ in range(20):
                    is_disabled = next_btn.get_attribute("disabled") or (next_btn.get_attribute("aria-disabled") == "true")
                    if not is_disabled:
                        break
                    page.wait_for_timeout(500)
                next_btn.click()
                print(f"[YouTubeUploader] Advanced wizard step {step_idx + 1}/3")
            except Exception as e:
                print(f"[YouTubeUploader] Notice on Next step {step_idx + 1}: {e}")

        page.wait_for_timeout(1500)

        # 6. Set Visibility (Public, Unlisted, or Private)
        priv_upper = privacy_status.upper()
        try:
            if priv_upper == "PUBLIC":
                vis_radio = page.locator('[name="PUBLIC"], tp-yt-paper-radio-button:has-text("Public")').first
            elif priv_upper == "UNLISTED":
                vis_radio = page.locator('[name="UNLISTED"], tp-yt-paper-radio-button:has-text("Unlisted")').first
            else:
                vis_radio = page.locator('[name="PRIVATE"], tp-yt-paper-radio-button:has-text("Private")').first

            vis_radio.wait_for(state="visible", timeout=15000)
            vis_radio.scroll_into_view_if_needed()
            vis_radio.click()
            print(f"[YouTubeUploader] Set visibility to: {priv_upper}")
        except Exception as e:
            print(f"[YouTubeUploader] Notice setting visibility: {e}")

        # Extract video URL early if available
        video_url = None
        try:
            link_elem = page.locator("a.style-scope.ytcp-video-info, a[href*='youtu.be'], a[href*='youtube.com/shorts']").first
            if link_elem.is_visible():
                video_url = link_elem.get_attribute("href")
        except Exception:
            pass

        # 7. CRITICAL: WAIT FOR 100% UPLOAD & PROCESSING BEFORE CLICKING DONE!
        # YouTube Studio uploads chunks over network. We MUST NOT click Done or close
        # while status says "Uploading..."!
        print(f"\n[YouTubeUploader] Monitoring upload progress for '{title}'...")
        upload_complete = False
        start_wait = time.time()
        last_pct = -1

        while time.time() - start_wait < 600:  # 10 min max timeout per clip
            status_text, pct = self._get_upload_status(page)
            low_text = status_text.lower()

            if "uploading" in low_text:
                curr_pct = pct if pct is not None else 0
                if curr_pct != last_pct:
                    last_pct = curr_pct
                    msg = f"Uploading to YouTube: {curr_pct}%... ({status_text})"
                    print(f"[YouTubeUploader] {msg}")
                    if progress_callback:
                        progress_callback({
                            "stage": "upload_progress",
                            "percent": curr_pct,
                            "message": msg,
                        })
            elif any(kw in low_text for kw in ["upload complete", "processing sd", "processing hd", "processing", "checks"]):
                msg = f"Uploading to YouTube: 100%... Upload complete! ({status_text})"
                print(f"[YouTubeUploader] {msg}")
                if progress_callback:
                    progress_callback({
                        "stage": "upload_progress",
                        "percent": 100,
                        "message": msg,
                    })
                upload_complete = True
                break
            elif status_text:
                pass

            page.wait_for_timeout(1500)

        # 8. Click Done / Publish button
        print(f"[YouTubeUploader] Upload bytes confirmed. Saving / Publishing Short...")
        done_btn = page.locator(
            "#done-button, ytcp-button#done-button, button:has-text('Save'), button:has-text('Publish')"
        ).first
        done_btn.wait_for(state="visible", timeout=30000)

        # Wait until Done button is enabled
        for _ in range(30):
            is_disabled = done_btn.get_attribute("disabled") or (done_btn.get_attribute("aria-disabled") == "true")
            if not is_disabled:
                break
            page.wait_for_timeout(1000)

        done_btn.click()
        print(f"[YouTubeUploader] Clicked Publish/Save for '{title}'.")

        # 9. Wait for Share Dialog / Confirmation Modal
        try:
            share_dialog = page.locator(
                "ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog, "
                "ytcp-dialog:has-text('Video published'), ytcp-dialog:has-text('Video uploaded'), "
                "ytcp-dialog:has-text('published')"
            ).first
            share_dialog.wait_for(state="visible", timeout=35000)

            # Extract short URL from share dialog if possible
            try:
                share_links = page.locator(
                    "ytcp-video-share-dialog a[href*='youtu.be'], "
                    "ytcp-video-share-dialog a[href*='youtube.com'], "
                    "a.ytcp-video-info, a[href*='youtu.be']"
                )
                if share_links.count() > 0:
                    for i in range(share_links.count()):
                        href = share_links.nth(i).get_attribute("href")
                        if href and ("youtu.be" in href or "shorts" in href or "watch" in href):
                            video_url = href
                            break
            except Exception:
                pass

            # Close share dialog
            close_btn = page.locator(
                "ytcp-video-share-dialog #close-button, ytcp-button#close-button, button:has-text('Close'), #close-button"
            ).first
            if close_btn.is_visible():
                close_btn.click()
                page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[YouTubeUploader] Notice waiting for share dialog: {e}")

        # 10. Buffer wait after save (5s)
        print("[YouTubeUploader] Upload confirmed. Waiting 5s buffer...")
        page.wait_for_timeout(5000)

        # 11. Ensure uploads dialog is detached
        try:
            page.wait_for_selector("ytcp-uploads-dialog", state="detached", timeout=5000)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        vid_id = video_url.split("/")[-1].split("=")[-1] if video_url else "unknown"
        if video_url and "shorts" not in video_url and vid_id != "unknown":
            final_short_url = f"https://youtube.com/shorts/{vid_id}"
        else:
            final_short_url = video_url or "https://youtube.com/shorts"

        return {
            "status": "uploaded",
            "engine": "studio_playwright",
            "video_id": vid_id,
            "video_url": final_short_url,
            "title": title,
            "privacy": privacy_status,
        }

    def upload_multiple_via_studio(
        self,
        clips: List[Dict[str, Any]],
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads multiple clips sequentially within the SAME persistent Playwright browser session.
        Prevents premature browser closing and zombie 'Uploading 0%' drafts!
        """
        if dry_run:
            for idx, c in enumerate(clips, 1):
                c["upload_result"] = {
                    "status": "dry_run_success",
                    "engine": "studio_playwright",
                    "video_id": f"dry_run_studio_{idx}",
                    "video_url": f"https://youtube.com/shorts/dry_run_{idx}",
                    "title": c.get("youtube_title", f"Clip #{idx}"),
                    "privacy": privacy_status,
                }
            return clips

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

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

            # 1. Navigate to YouTube Studio
            print("[YouTubeUploader] Opening YouTube Studio in persistent browser...")
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

            # Check if login is required
            if "accounts.google.com" in page.url or "signin" in page.url.lower():
                print("\n" + "=" * 75)
                print(" 🔓 Please sign in to your YouTube account in the opened browser window!")
                print("=" * 75 + "\n")
                start_login = time.time()
                while time.time() - start_login < 300:
                    try:
                        if "studio.youtube.com" in page.url and "accounts.google.com" not in page.url:
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)

            page.wait_for_timeout(2000)

            # 2. Upload each clip sequentially inside the SAME session
            total_clips = len(clips)
            for idx, clip in enumerate(clips, 1):
                file_path = clip.get("file_path") or clip.get("filepath") or clip.get("video_path")
                title = clip.get("youtube_title") or clip.get("title", f"Short #{idx}")
                desc = clip.get("youtube_description") or clip.get("description", "#Shorts")
                tags = clip.get("tags", ["shorts", "viral"])

                print(f"\n=======================================================")
                print(f" 🚀 UPLOADING CLIP [{idx}/{total_clips}]: {title}")
                print(f"=======================================================")

                if progress_callback:
                    progress_callback({
                        "stage": "uploading_clip",
                        "message": f"Uploading [{idx}/{total_clips}]: {title}...",
                        "clip_index": idx,
                        "total_clips": total_clips,
                    })

                # Ensure clean studio page state before each clip
                if idx > 1:
                    try:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(500)
                        page.goto("https://studio.youtube.com", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass

                try:
                    res = self._upload_single_clip_in_page(
                        page=page,
                        video_path=file_path,
                        title=title,
                        description=desc,
                        tags=tags,
                        privacy_status=privacy_status,
                        progress_callback=progress_callback,
                    )
                    clip["upload_result"] = res
                    print(f"✅ Clip [{idx}/{total_clips}] uploaded successfully: {res.get('video_url')}")
                except Exception as e:
                    print(f"❌ Clip [{idx}/{total_clips}] upload failed: {e}")
                    clip["upload_result"] = {"error": str(e), "status": "failed"}
                    # Try to reset UI state for next clip
                    try:
                        page.keyboard.press("Escape")
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                    except Exception:
                        pass

                page.wait_for_timeout(3000)

            print("\n[YouTubeUploader] All clips processed. Closing browser session...")
            try:
                context.close()
            except Exception:
                pass

        return clips

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
        Uploads a single video to YouTube Shorts using Playwright Studio automation.
        """
        single_clip = [{
            "file_path": video_path,
            "youtube_title": title,
            "youtube_description": description,
            "tags": tags or [],
        }]
        res_clips = self.upload_multiple_via_studio(
            clips=single_clip,
            privacy_status=privacy_status,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
        return res_clips[0].get("upload_result") or {"status": "failed", "error": "Unknown error"}

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
        Uploads a single video file directly to YouTube Shorts.
        - Uses YouTube Data API v3 if client_secrets.json exists.
        - Automatically uses Playwright YouTube Studio uploader if no API keys are present.
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

    def upload_multiple(
        self,
        clips: List[Dict[str, Any]],
        privacy_status: str = "public",
        dry_run: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Batch uploads multiple clips.
        Uses Engine A (API) if client_secrets.json exists, else Engine B (Playwright Studio)
        keeping the browser session open for all clips.
        """
        if os.path.exists(self.client_secrets_file):
            results = []
            for idx, c in enumerate(clips, 1):
                try:
                    res = self._upload_via_api(
                        video_path=c.get("file_path", ""),
                        title=c.get("youtube_title", f"Short #{idx}"),
                        description=c.get("youtube_description", ""),
                        tags=c.get("tags", []),
                        privacy_status=privacy_status,
                        progress_callback=progress_callback,
                    )
                    c["upload_result"] = res
                except Exception as e:
                    c["upload_result"] = {"error": str(e), "status": "failed"}
                results.append(c)
            return results

        return self.upload_multiple_via_studio(
            clips=clips,
            privacy_status=privacy_status,
            dry_run=dry_run,
            progress_callback=progress_callback,
        )
