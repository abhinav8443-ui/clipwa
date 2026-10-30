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

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


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

    def _unlock_profile(self) -> None:
        """
        Ensures youtube_auth_profile is completely unlocked before launching Playwright:
        1. Terminates any zombie chrome/chromium processes holding the user_data_dir.
        2. Deletes stale SingletonLock, SingletonCookie, SingletonSocket, and LOCK files.
        """
        profile_path_str = str(self.user_data_dir.resolve())
        if sys.platform == "win32":
            try:
                ps_script = f"""
                Get-CimInstance Win32_Process | Where-Object {{
                    ($_.Name -match 'chrome|chromium') -and ($_.CommandLine -like '*{profile_path_str}*')
                }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
                """
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
            except Exception:
                pass

        time.sleep(0.8)

        # Remove Singleton and lock files from user_data_dir root
        lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
        for fname in lock_names:
            lock_file = self.user_data_dir / fname
            try:
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        # Also remove from Default directory
        default_dir = self.user_data_dir / "Default"
        if default_dir.is_dir():
            for fname in ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK"]:
                lf = default_dir / fname
                try:
                    if lf.exists() or lf.is_symlink():
                        lf.unlink(missing_ok=True)
                except Exception:
                    pass

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

        self._unlock_profile()
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

    def _extract_channel_info(self, page) -> Tuple[Optional[str], Optional[str]]:
        """Extract active channel ID and channel name from page context."""
        cid = None
        cname = None

        try:
            cur_url = page.url
            id_m = re.search(r"channel/(UC[a-zA-Z0-9_-]+)", cur_url)
            if id_m:
                cid = id_m.group(1)
        except Exception:
            pass

        if not cid:
            try:
                eval_cid = page.evaluate("""
                    () => {
                        try {
                            if (window.ytcfg && window.ytcfg.get) {
                                return window.ytcfg.get('CHANNEL_ID') || (window.ytcfg.data_ && window.ytcfg.data_.CHANNEL_ID);
                            }
                        } catch(e) {}
                        const meta = document.querySelector('meta[itemprop="channelId"], meta[property="og:url"]');
                        if (meta && meta.content) {
                            const m = meta.content.match(/channel\\/(UC[a-zA-Z0-9_-]+)/);
                            if (m) return m[1];
                        }
                        return null;
                    }
                """)
                if eval_cid:
                    cid = eval_cid
            except Exception:
                pass

        for name_sel in ["#channel-name", "#entity-name", ".channel-name", "ytcp-entity-info #entity-name", "#owner-name", "ytd-channel-name"]:
            try:
                loc = page.locator(name_sel).first
                if loc.is_visible():
                    txt = loc.inner_text().strip()
                    if txt:
                        cname = txt
                        break
            except Exception:
                pass

        return cid, cname

    def switch_channel(self, timeout_sec: int = 600) -> Dict[str, Any]:
        """
        Interactive helper: launches browser directly to YouTube Channel Switcher,
        allowing the user to switch to their other YouTube channel.
        Once the user switches or lands in YouTube Studio, automatically detects
        and saves the newly selected channel.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()
        old_channel_id = "UCzIwY0Z1oiofBgoXEt1uUxg"

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

            print("\n" + "=" * 75)
            print(" 🔄 Browser opened to YouTube Channel Switcher!")
            print("    👉 In the opened browser window:")
            print("       1. Select your desired YouTube channel (or avatar top-right -> Switch/Add account)")
            print("       2. Click the green '✅ Use This Channel & Continue' button at the top")
            print(f"    Previous channel: copprant ({old_channel_id})")
            print("=" * 75 + "\n")

            try:
                page.goto("https://www.youtube.com/channel_switcher", wait_until="load", timeout=60000)
            except Exception:
                pass

            def inject_banner():
                try:
                    page.evaluate("""
                        () => {
                            if (document.getElementById('clipwa-switch-banner')) return;
                            const b = document.createElement('div');
                            b.id = 'clipwa-switch-banner';
                            b.innerHTML = `
                                <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 24px;background:#0d47a1;color:#ffffff;font-family:Segoe UI,Roboto,sans-serif;box-shadow:0 4px 12px rgba(0,0,0,0.5);position:fixed;top:0;left:0;right:0;z-index:2147483647;">
                                    <div>
                                        <span style="font-size:16px;font-weight:700;">🎬 CLIPWA:</span>
                                        <span style="font-size:14px;margin-left:12px;color:#e3f2fd;">Select your channel below, or click avatar (top-right) &rarr; 'Switch account' / 'Add account'.</span>
                                    </div>
                                    <button id="clipwa-confirm-btn" style="background:#00e676;color:#000;border:none;border-radius:6px;padding:8px 20px;font-size:14px;font-weight:bold;cursor:pointer;box-shadow:0 2px 5px rgba(0,0,0,0.3);">
                                        ✅ Use This Channel & Continue &rarr;
                                    </button>
                                </div>
                            `;
                            document.body.prepend(b);
                            window.__clipwa_confirmed = false;
                            const btn = document.getElementById('clipwa-confirm-btn');
                            if (btn) {
                                btn.addEventListener('click', () => {
                                    window.__clipwa_confirmed = true;
                                    btn.innerText = '⏳ Saving Channel...';
                                    btn.style.background = '#ffd600';
                                });
                            }
                        }
                    """)
                except Exception:
                    pass

            inject_banner()

            start_t = time.time()
            switched = False
            new_channel_id = None
            new_channel_name = None
            last_checked_url = None

            while time.time() - start_t < timeout_sec:
                inject_banner()
                cur_url = page.url

                # Check if user clicked the green "Use This Channel & Continue" button
                is_confirmed = False
                try:
                    is_confirmed = page.evaluate("() => Boolean(window.__clipwa_confirmed)")
                except Exception:
                    pass

                if is_confirmed:
                    print("\n[YouTubeUploader] 'Use This Channel & Continue' clicked! Inspecting active channel...")
                    page.wait_for_timeout(2000)
                    if "studio.youtube.com" not in cur_url:
                        try:
                            page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                            page.wait_for_timeout(4000)
                        except Exception:
                            pass
                    cid, cname = self._extract_channel_info(page)
                    new_channel_id = cid or old_channel_id
                    new_channel_name = cname or "Active Channel"
                    switched = True
                    break

                # Case A: User navigated to studio.youtube.com
                if "studio.youtube.com" in cur_url:
                    cid, cname = self._extract_channel_info(page)
                    if cid and cid != old_channel_id:
                        new_channel_id = cid
                        new_channel_name = cname
                        switched = True
                        break

                # Case B: User clicked a channel on channel_switcher and redirected to youtube.com homepage or @handle
                elif cur_url != last_checked_url and (
                    "youtube.com/@" in cur_url
                    or ("youtube.com" in cur_url and "channel_switcher" not in cur_url and "account" not in cur_url and "signin" not in cur_url)
                ):
                    last_checked_url = cur_url
                    print(f"[YouTubeUploader] Detected channel navigation to {cur_url}. Verifying in Studio...")
                    page.wait_for_timeout(3000)
                    try:
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=30000)
                        page.wait_for_timeout(4000)
                        cid, cname = self._extract_channel_info(page)
                        if cid and cid != old_channel_id:
                            new_channel_id = cid
                            new_channel_name = cname
                            switched = True
                            break
                    except Exception as e:
                        print(f"[YouTubeUploader] Studio verification note: {e}")

                page.wait_for_timeout(2000)

            # Final check in Studio if not yet switched
            if not switched:
                print("[YouTubeUploader] Navigating to Studio to check active channel...")
                try:
                    page.goto("https://studio.youtube.com", wait_until="load", timeout=45000)
                    page.wait_for_timeout(5000)
                    cid, cname = self._extract_channel_info(page)
                    if cid:
                        new_channel_id = cid
                    if cname:
                        new_channel_name = cname
                    if new_channel_id and new_channel_id != old_channel_id:
                        switched = True
                except Exception as e:
                    print(f"[YouTubeUploader] Studio check note: {e}")

            active_id = new_channel_id or old_channel_id
            active_name = new_channel_name or ("copprant" if active_id == old_channel_id else "Selected Channel")

            print(f"\n=======================================================")
            print(f" ✅ Active YouTube Channel: {active_name}")
            print(f"    Channel ID:   {active_id}")
            print(f"=======================================================\n")

            page.wait_for_timeout(3000)
            context.close()
            return {
                "status": "success",
                "channel_id": active_id,
                "channel_name": active_name,
                "is_different": (active_id != old_channel_id),
            }

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

        # Wait for loading scrim to disappear if present
        try:
            page.locator(".dialog-scrim").wait_for(state="hidden", timeout=10000)
        except Exception:
            pass

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
            title_box.click(force=True)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            title_box.fill(title)
        except Exception as e:
            print(f"[YouTubeUploader] Warning filling title: {e}")

        # Fill Description
        try:
            desc_box = page.locator("#description-textarea #textbox, #textbox[aria-label*='description' i]").first
            if desc_box.is_visible():
                desc_box.click(force=True)
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
            not_for_kids.click(force=True)
            print("[YouTubeUploader] Selected audience: 'No, it is not made for kids'")
            page.wait_for_timeout(1000)
        except Exception as e:
            print(f"[YouTubeUploader] Notice setting audience: {e}")

        # 5. Advance wizard step-by-step until the Visibility step is active
        print("[YouTubeUploader] Advancing wizard through steps to Visibility...")
        for step_attempt in range(6):
            # Check if Visibility tab is already active
            vis_active = page.evaluate("""
                () => {
                    const visTab = document.querySelector('#step-badge-3[aria-selected="true"], [test-id="VISIBILITY"][aria-selected="true"]');
                    const pubRadio = document.querySelector('tp-yt-paper-radio-button[name="PUBLIC"]');
                    const doneBtn = document.querySelector('#done-button:not([hidden])');
                    return Boolean(visTab || (pubRadio && window.getComputedStyle(pubRadio).display !== 'none') || (doneBtn && window.getComputedStyle(doneBtn).display !== 'none'));
                }
            """)
            if vis_active:
                print(f"[YouTubeUploader] Visibility step reached.")
                break

            # Handle scrim or 'Verify it's you' if present
            try:
                verify_box = page.locator("ytcp-dialog:has-text('Verify it\'s you'), :has-text('To continue, we need to confirm it\'s really you')").first
                if verify_box.is_visible():
                    print("\n[YouTubeUploader] ⚠️ Google verification prompt detected on screen! Please complete it in the browser...")
                    for _ in range(30):
                        if not verify_box.is_visible():
                            break
                        page.wait_for_timeout(2000)
            except Exception:
                pass

            try:
                page.locator(".dialog-scrim").wait_for(state="hidden", timeout=2000)
            except Exception:
                pass

            # Try clicking Visibility tab header directly if enabled
            clicked_tab = False
            for tab_sel in ["#step-badge-3", "ytcp-step-badge:has-text('Visibility')", "div[test-id='VISIBILITY']"]:
                try:
                    tab_loc = page.locator(tab_sel).first
                    if tab_loc.is_visible():
                        tab_loc.click()
                        clicked_tab = True
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # Click next button if visible and not hidden
            if not clicked_tab:
                next_btn = page.locator("#next-button:not([hidden]), ytcp-button#next-button:not([hidden])").first
                if next_btn.is_visible():
                    for _ in range(25):
                        is_disabled = next_btn.get_attribute("disabled") or (next_btn.get_attribute("aria-disabled") == "true")
                        if not is_disabled:
                            break
                        page.wait_for_timeout(500)
                    try:
                        next_btn.click(force=True)
                    except Exception:
                        page.evaluate("el => el.click()", next_btn)
                    print(f"[YouTubeUploader] Advanced wizard step {step_attempt + 1}")
                    page.wait_for_timeout(2000)
                else:
                    page.wait_for_timeout(1000)

        page.wait_for_timeout(1500)

        # 6. Set Visibility (Public, Unlisted, or Private)
        priv_upper = privacy_status.upper()
        try:
            if priv_upper == "PUBLIC":
                vis_sel = '[name="PUBLIC"], tp-yt-paper-radio-button:has-text("Public")'
            elif priv_upper == "UNLISTED":
                vis_sel = '[name="UNLISTED"], tp-yt-paper-radio-button:has-text("Unlisted")'
            else:
                vis_sel = '[name="PRIVATE"], tp-yt-paper-radio-button:has-text("Private")'

            vis_radio = page.locator(vis_sel).first
            try:
                vis_radio.wait_for(state="visible", timeout=10000)
                vis_radio.scroll_into_view_if_needed()
                vis_radio.click(force=True)
            except Exception:
                pass

            # Enforce visibility via JavaScript in Polymer context
            page.evaluate(f"""
                () => {{
                    const targetName = '{priv_upper}';
                    const r = document.querySelector(`tp-yt-paper-radio-button[name="${{targetName}}"]`) ||
                              document.querySelector(`[name="${{targetName}}"]`) ||
                              Array.from(document.querySelectorAll('tp-yt-paper-radio-button')).find(el => el.innerText && el.innerText.toUpperCase().includes(targetName));
                    if (r) {{
                        r.click();
                        r.setAttribute('checked', 'true');
                        r.setAttribute('aria-checked', 'true');
                    }}
                }}
            """)
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

        # STRICT 100% UPLOAD VERIFICATION: Never click Done or close if not finished!
        if not upload_complete:
            raise RuntimeError(f"YouTube upload did not reach 100% completion within timeout. Last status: '{status_text}'")

        # 8. Click Done / Publish button
        print(f"[YouTubeUploader] 100% upload bytes confirmed. Saving / Publishing Short...")

        # Ensure we are fully on the final publish step
        for _ in range(4):
            nxt = page.locator("#next-button:not([hidden]):visible, ytcp-button#next-button:not([hidden]):visible").first
            if nxt.is_visible():
                try:
                    nxt.click(force=True)
                except Exception:
                    page.evaluate("el => el.click()", nxt)
                page.wait_for_timeout(1500)
            else:
                break

        # Re-verify visibility selection right before publish
        page.evaluate(f"""
            () => {{
                const targetName = '{priv_upper}';
                const r = document.querySelector(`tp-yt-paper-radio-button[name="${{targetName}}"]`) ||
                          document.querySelector(`[name="${{targetName}}"]`);
                if (r) {{
                    r.click();
                    r.setAttribute('checked', 'true');
                }}
            }}
        """)
        page.wait_for_timeout(1000)

        # Locate Done / Publish button (specifically not hidden)
        done_btn = page.locator(
            "#done-button:not([hidden]):visible, ytcp-button#done-button:not([hidden]):visible, "
            "button:has-text('Publish'):visible, button:has-text('Save'):visible"
        ).first

        # Wait until Done button is enabled
        for _ in range(35):
            if done_btn.is_visible():
                is_disabled = done_btn.get_attribute("disabled") or (done_btn.get_attribute("aria-disabled") == "true")
                if not is_disabled:
                    break
            page.wait_for_timeout(1000)

        # Click publish via Playwright and JavaScript fallback
        published_clicked = False
        try:
            if done_btn.is_visible():
                done_btn.click(timeout=10000)
                published_clicked = True
        except Exception:
            pass

        if not published_clicked:
            published_clicked = page.evaluate("""
                () => {
                    const btn = document.querySelector('#done-button') || 
                                document.querySelector('ytcp-button#done-button') ||
                                document.querySelector('button[aria-label="Publish"]') ||
                                document.querySelector('button[aria-label="Save"]');
                    if (btn) {
                        btn.removeAttribute('disabled');
                        btn.removeAttribute('aria-disabled');
                        btn.removeAttribute('hidden');
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)

        print(f"[YouTubeUploader] Clicked Publish/Save for '{title}'.")

        # 9. Wait 5 extra seconds after publishing to capture the live YouTube URL
        print("[YouTubeUploader] Published! Waiting 5s buffer to capture live YouTube URL...")
        page.wait_for_timeout(5000)

        try:
            share_dialog = page.locator(
                "ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog, "
                "ytcp-dialog:has-text('Video published'), ytcp-dialog:has-text('Video uploaded'), "
                "ytcp-dialog:has-text('published')"
            ).first
            share_dialog.wait_for(state="visible", timeout=25000)

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

        # Fallback to locate URL on page if share dialog didn't provide it
        if not video_url:
            for link_sel in [
                "a.style-scope.ytcp-video-info",
                "a[href*='youtu.be']",
                "a[href*='youtube.com/shorts']",
                "a[href*='watch?v=']",
                "ytcp-video-list-cell-video a",
            ]:
                try:
                    loc = page.locator(link_sel)
                    for i in range(loc.count()):
                        href = loc.nth(i).get_attribute("href")
                        if href and ("youtu.be" in href or "shorts" in href or "watch" in href):
                            video_url = href
                            break
                    if video_url:
                        break
                except Exception:
                    pass

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

        # Automatically clean up any stale browser locks or zombie processes
        self._unlock_profile()

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
            cid, cname = self._extract_channel_info(page)
            print(f"\n=======================================================")
            print(f" 📺 Uploading to Active Channel: {cname or 'Active Channel'} ({cid or 'active'})")
            print(f"=======================================================\n")

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
                        page.wait_for_timeout(1000)
                        page.goto("https://studio.youtube.com", wait_until="load", timeout=45000)
                        page.wait_for_selector("#create-icon, button[aria-label='Create']", state="visible", timeout=30000)
                        page.wait_for_timeout(3000)
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

                    # Automatic local file cleanup upon confirmed successful upload
                    if res.get("status") == "uploaded" and res.get("video_url"):
                        if file_path and os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                print(f"[YouTubeUploader] 🗑️ Cleaned up local file: {os.path.basename(file_path)}")
                                clip["status"] = "Uploaded & Local File Cleaned"
                                clip["local_file_deleted"] = True
                                res["local_file_cleaned"] = True
                            except Exception as ex:
                                print(f"[YouTubeUploader] Warning deleting local file {file_path}: {ex}")
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
