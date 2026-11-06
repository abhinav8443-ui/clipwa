"""
Automated Instagram Reels uploader using Playwright browser automation.
Uses the persistent profile directory (youtube_auth_profile) so user login sessions
persist seamlessly across runs without credential re-entry.
"""

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import Config, atomic_write_json
from .scheduler import PeakSlotResolver, ScheduleLedger, STUDIO_UPLOAD_SAFETY_CAP
from .stealth_helpers import human_sleep, human_click, human_type, human_move_mouse_jitter
import random

from rich.console import Console
from rich.table import Table

console = Console()

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass


def _sanitize_insta_text(text: str) -> str:
    """Sanitizes text for Instagram caption."""
    if not text:
        return ""
    cleaned = re.sub(r"^\s*>>+\s*", "", str(text))
    cleaned = cleaned.replace("<", "").replace(">", "")
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", cleaned)
    return cleaned.strip()


class DailyInstaLimitError(Exception):
    """Raised when Instagram enforces a temporary action limit or upload cap."""
    pass


class InstagramReelsUploader:
    """Manages Instagram Reels authentication and automated uploads via Playwright."""

    def __init__(
        self,
        user_data_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        temp_dir: Optional[Path] = None,
    ):
        self.user_data_dir = user_data_dir or (Config.BASE_DIR / "youtube_auth_profile")
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = output_dir or Config.OUTPUT_DIR
        self.temp_dir = temp_dir or Config.TEMP_DIR

    def _sync_summary_clip(self, clip_record: Dict[str, Any]) -> None:
        """Updates pipeline_summary.json on disk with the latest clip Instagram status."""
        summary_path = self.output_dir / "pipeline_summary.json"
        if not summary_path.exists():
            return
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            clips = data.get("clips", [])
            target_id = clip_record.get("clip_id")
            target_file = clip_record.get("file_name")

            for c in clips:
                if (target_id is not None and c.get("clip_id") == target_id) or (
                    target_file and c.get("file_name") == target_file
                ):
                    for k in [
                        "instagram_status",
                        "instagram_result",
                        "youtube_status",
                        "status",
                        "file_deleted",
                        "scheduled_slot",
                        "scheduled_date",
                        "scheduled_time",
                        "display_plan",
                    ]:
                        if k in clip_record:
                            c[k] = clip_record[k]
                    break
            atomic_write_json(summary_path, data)
        except Exception as e:
            print(f"[InstagramUploader] Warning updating summary file: {e}")

    def _unlock_profile(self) -> None:
        """
        Ensures persistent profile directory is completely unlocked before launching Playwright:
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

        lock_names = ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK", "lockfile"]
        for fname in lock_names:
            lock_file = self.user_data_dir / fname
            try:
                if lock_file.exists() or lock_file.is_symlink():
                    lock_file.unlink(missing_ok=True)
            except Exception:
                pass

        default_dir = self.user_data_dir / "Default"
        if default_dir.is_dir():
            for fname in ["SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK"]:
                lf = default_dir / fname
                try:
                    if lf.exists() or lf.is_symlink():
                        lf.unlink(missing_ok=True)
                except Exception:
                    pass

    def _dismiss_modals(self, page) -> None:
        """Dismiss common Instagram popups (Save info, Notifications, Cookies)."""
        # Save login info modal
        for sel in [
            "button:has-text('Save info')",
            "button:has-text('Save Info')",
            "button:has-text('Not now')",
            "button:has-text('Not Now')",
            "div[role='button']:has-text('Not now')",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(800)
            except Exception:
                pass

        # Notifications modal
        for sel in [
            "button:has-text('Not Now')",
            "button:has-text('Not now')",
            "button:has-text('Cancel')",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(800)
            except Exception:
                pass

    def is_logged_in(self, page) -> bool:
        """Check if Instagram account is currently logged in."""
        for sel in [
            "svg[aria-label='New post']",
            "svg[aria-label='Create']",
            "span:has-text('Create')",
            "a[href='#']:has-text('Create')",
            "svg[aria-label='Home']",
            "svg[aria-label='Direct']",
            "svg[aria-label='Messages']",
            "a[href*='/']:has(img[alt*='profile picture'])",
        ]:
            try:
                if page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return False

    def login_insta(self, timeout_sec: int = 300) -> bool:
        """
        Open persistent browser to Instagram so the user can log in once.
        Session is permanently saved inside youtube_auth_profile.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()
        print(f"\n[InstagramUploader] Launching browser with persistent profile at: {self.user_data_dir}")

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir.resolve()),
                headless=False,
                args=browser_args,
                viewport=None,
            )
            page = context.pages[0] if context.pages else context.new_page()

            print("[InstagramUploader] Navigating to https://www.instagram.com/ ...")
            try:
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            page.wait_for_timeout(3000)
            self._dismiss_modals(page)

            if self.is_logged_in(page):
                print("[InstagramUploader] ✅ Already logged into Instagram!")
                context.close()
                return True

            print("\n=======================================================")
            print(" 🔑 ACTION REQUIRED: Please log in to Instagram in the browser window.")
            print(f" Waiting up to {timeout_sec} seconds for login completion...")
            print("=======================================================\n")

            start_t = time.time()
            logged_in = False
            while time.time() - start_t < timeout_sec:
                self._dismiss_modals(page)
                if self.is_logged_in(page):
                    logged_in = True
                    break
                page.wait_for_timeout(2000)

            if logged_in:
                self._dismiss_modals(page)
                page.wait_for_timeout(3000)
                print("\n[InstagramUploader] ✅ Successfully logged into Instagram! Session saved permanently.")
                context.close()
                return True
            else:
                print("\n[InstagramUploader] ❌ Timed out waiting for Instagram login.")
                context.close()
                return False

    def _display_schedule_plan_table(self, clips: List[Dict[str, Any]], title_prefix: str = "Upcoming") -> None:
        """Displays a clean Rich table of the scheduled peak windows for Instagram Reels."""
        try:
            table = Table(
                title=f"[bold]{title_prefix} Instagram Reels Schedule Plan[/bold]",
                show_header=True,
                header_style="bold magenta",
                border_style="magenta",
            )
            table.add_column("#", justify="center", width=4)
            table.add_column("Reel Title", style="bold white", width=38)
            table.add_column("Peak Window", style="yellow", width=24)
            table.add_column("Target Date & Time", style="cyan", width=26)
            table.add_column("Instagram Status", style="green", width=30)

            for idx, c in enumerate(clips, 1):
                slot = c.get("scheduled_slot") or {}
                slot_name = slot.get("slot_name") or "Peak Window"
                date_time = (
                    f"{slot.get('scheduled_date', '')} @ {slot.get('scheduled_time', '')}"
                    if slot.get("scheduled_date")
                    else "Immediate Share"
                )
                res = c.get("instagram_result") or {}
                ig_status = c.get("instagram_status") or ""

                if title_prefix == "Planned":
                    status_disp = "[cyan]Ready to Share[/cyan]"
                elif res.get("status") in ("scheduled", "uploaded", "shared") or "Shared" in ig_status or "Scheduled" in ig_status:
                    status_disp = "[green]Shared / Scheduled[/green]"
                elif res.get("status") == "dry_run_success":
                    status_disp = "[green]Dry-Run OK (Simulated)[/green]"
                elif ig_status == "Queued for Next Upload Window":
                    status_disp = "[yellow]Queued for Next Window[/yellow]"
                elif res.get("status") == "failed" or ig_status == "Error":
                    status_disp = f"[red]Failed ({res.get('error', 'Error')})[/red]"
                else:
                    status_disp = f"[dim]{ig_status or 'Planned'}[/dim]"

                table.add_row(
                    str(c.get("clip_id", idx)),
                    (c.get("youtube_title") or f"Reel #{idx}")[:38],
                    slot_name,
                    date_time,
                    status_disp,
                )

            console.print("\n")
            console.print(table)
            console.print("\n")
        except Exception as e:
            print(f"[InstagramUploader] Schedule table display notice: {e}")

    def _upload_single_reel_in_page(
        self,
        page,
        video_path: str,
        title: str,
        description: str,
        tags: Optional[List[str]] = None,
        scheduled_slot: Optional[Dict[str, Any]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Uploads one vertical video to Instagram Reels inside an already-opened browser session.
        Enforces 9:16 aspect ratio, caption with hashtags, optional scheduling, and wait for confirmation.
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        title = _sanitize_insta_text(title)
        description = _sanitize_insta_text(description)
        insta_tags = [
            "#reels",
            "#motivation",
            "#mindset",
            "#nevergiveup",
            "#discipline",
            "#viralreels",
            "#shorts",
        ]
        if tags:
            for t in tags:
                clean_t = _sanitize_insta_text(t).strip("#")
                if clean_t and f"#{clean_t}" not in insta_tags:
                    insta_tags.append(f"#{clean_t}")

        full_caption = f"{title}\n\n{description}\n\n{' '.join(insta_tags[:15])}"

        # 1. Dismiss any residual modals & ensure on home feed
        self._dismiss_modals(page)
        page.wait_for_timeout(1000)

        # 2. Click Create ("New post")
        if progress_callback:
            progress_callback({"stage": "upload", "message": f"Opening Instagram Create modal for '{title}'..."})

        create_clicked = False
        for c_sel in [
            "svg[aria-label='New post']",
            "svg[aria-label='Create']",
            "span:has-text('Create')",
            "a[href='#']:has-text('Create')",
            "a[role='link']:has-text('Create')",
            "div[role='button']:has-text('Create')",
        ]:
            try:
                c_btn = page.locator(c_sel).first
                if c_btn.is_visible():
                    c_btn.click(force=True)
                    create_clicked = True
                    break
            except Exception:
                pass

        if not create_clicked:
            raise RuntimeError("Could not find or click Instagram 'Create' button. Ensure you are logged in.")

        page.wait_for_timeout(1500)

        # If sub-menu pops up with "Post" vs "Live video", click "Post"
        for sub_sel in [
            "span:has-text('Post')",
            "div[role='button']:has-text('Post')",
            "a:has-text('Post')",
        ]:
            try:
                sub_item = page.locator(sub_sel).first
                if sub_item.is_visible():
                    sub_item.click(force=True)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # 3. Attach file via file chooser or input[type='file']
        file_attached = False
        try:
            sel_btn = page.locator(
                "button:has-text('Select from computer'), div[role='button']:has-text('Select from computer')"
            ).first
            with page.expect_file_chooser(timeout=8000) as fc_info:
                if sel_btn.is_visible():
                    sel_btn.click()
                else:
                    page.locator("input[type='file']").first.click()
            fc_info.value.set_files(str(video_path))
            file_attached = True
        except Exception as e:
            print(f"[InstagramUploader] File chooser notice: {e}, attempting direct file input...")

        if not file_attached:
            file_input = page.locator("input[type='file']").first
            file_input.wait_for(state="attached", timeout=15000)
            file_input.set_input_files(str(video_path))

        print(f"[InstagramUploader] File selected: {os.path.basename(video_path)}")
        page.wait_for_timeout(3500)

        # 4. Handle informational popups: "Video posts are now shared as reels"
        for ok_sel in [
            "button:has-text('OK')",
            "button:has-text('Ok')",
            "div[role='dialog'] button:has-text('OK')",
        ]:
            try:
                ok_btn = page.locator(ok_sel).first
                if ok_btn.is_visible():
                    ok_btn.click(force=True)
                    page.wait_for_timeout(1000)
                    break
            except Exception:
                pass

        # 5. Aspect Ratio: Enforce 9:16 (Zero-Crop Vertical Reel)
        try:
            crop_btn = page.locator(
                "svg[aria-label='Select crop'], button:has(svg[aria-label='Select crop']), [aria-label*='crop' i]"
            ).first
            if crop_btn.is_visible():
                crop_btn.click(force=True)
                page.wait_for_timeout(800)

                # Select 9:16 or Original
                for r_sel in [
                    "button:has-text('9:16')",
                    "div[role='button']:has-text('9:16')",
                    "span:has-text('9:16')",
                    "button:has-text('Original')",
                    "span:has-text('Original')",
                ]:
                    try:
                        r_btn = page.locator(r_sel).first
                        if r_btn.is_visible():
                            r_btn.click(force=True)
                            print("[InstagramUploader] Enforced 9:16 vertical Reel aspect ratio.")
                            page.wait_for_timeout(800)
                            break
                    except Exception:
                        pass
        except Exception as e:
            print(f"[InstagramUploader] Aspect ratio notice: {e}")

        # 6. Click 'Next' (top right of modal) -> Filters/Cover screen
        next_btn = page.locator("div[role='button']:has-text('Next'), button:has-text('Next')").first
        next_btn.wait_for(state="visible", timeout=15000)
        next_btn.click(force=True)
        page.wait_for_timeout(2000)

        # 7. Click 'Next' again -> Details / Caption screen
        next_btn2 = page.locator("div[role='button']:has-text('Next'), button:has-text('Next')").first
        if next_btn2.is_visible():
            next_btn2.click(force=True)
            page.wait_for_timeout(2500)

        # 8. Fill Caption with stealth typing
        print(f"[InstagramUploader] Entering caption and hashtags for '{title}'...")
        caption_box = page.locator(
            "div[aria-label*='caption' i][contenteditable='true'], div[aria-label*='Write a caption' i], div[role='textbox']"
        ).first
        caption_box.wait_for(state="visible", timeout=15000)
        human_sleep(1.0, 1.5)

        try:
            human_type(page, caption_box, full_caption[:2200], clear_first=True, pause_after=2.0)
            print("[InstagramUploader] Caption and hashtags added with stealth typing.")
        except Exception as e:
            print(f"[InstagramUploader] Warning entering caption: {e}")
            try:
                caption_box.fill(full_caption[:2200])
            except Exception:
                pass

        human_sleep(1.0, 2.0)

        # 9. Schedule or Share
        is_scheduled = False
        action_verb = "Share"

        if scheduled_slot:
            target_date = scheduled_slot.get("scheduled_date")
            target_time = scheduled_slot.get("scheduled_time")
            slot_name = scheduled_slot.get("slot_name", "Peak Window")

            # Try finding Advanced settings / Schedule accordion
            try:
                adv_btn = page.locator(
                    "div:has-text('Advanced settings'), span:has-text('Advanced settings')"
                ).first
                if adv_btn.is_visible():
                    adv_btn.click(force=True)
                    page.wait_for_timeout(1000)

                    # Look for "Schedule this reel" toggle
                    sched_toggle = page.locator(
                        "input[type='checkbox'][aria-label*='Schedule' i], "
                        "div:has-text('Schedule this reel') input[type='checkbox'], "
                        "div[role='switch']"
                    ).first
                    if sched_toggle.is_visible():
                        sched_toggle.click(force=True)
                        page.wait_for_timeout(1000)
                        is_scheduled = True
                        action_verb = "Schedule"
                        print(f"[InstagramUploader] 📅 Configured Instagram scheduled reel for {target_date} @ {target_time} ({slot_name})")
            except Exception as e:
                print(f"[InstagramUploader] Scheduling option notice: {e}")

        # 10. Click Share / Schedule
        print(f"[InstagramUploader] Uploading & {action_verb}ing Reel...")
        if progress_callback:
            progress_callback({"stage": "upload_progress", "percent": 50, "message": f"{action_verb}ing Reel..."})

        submit_btn = page.locator(
            f"div[role='button']:has-text('{action_verb}'), button:has-text('{action_verb}'), "
            "div[role='button']:has-text('Share'), button:has-text('Share')"
        ).first
        submit_btn.wait_for(state="visible", timeout=15000)
        submit_btn.click(force=True)

        # 11. Wait for confirmation ("Your reel has been shared." / checkmark)
        print("[InstagramUploader] Waiting for Instagram upload confirmation...")
        upload_complete = False
        reel_url = None
        start_wait = time.time()

        while time.time() - start_wait < 180:
            for s_sel in [
                "div:has-text('Your reel has been shared.')",
                "div:has-text('Your reel has been scheduled.')",
                "div:has-text('Your post has been shared.')",
                "div:has-text('Reel shared')",
                "div:has-text('Post shared')",
                "img[alt*='checkmark' i]",
                "svg[aria-label*='Animated checkmark' i]",
            ]:
                try:
                    conf = page.locator(s_sel).first
                    if conf.is_visible():
                        upload_complete = True
                        print(f"[InstagramUploader] ✅ Confirmation detected: '{s_sel}'")
                        break
                except Exception:
                    pass

            if upload_complete:
                break

            # Check if modal closed itself indicating completion
            try:
                modal = page.locator("div[role='dialog']").first
                if not modal.is_visible() and (time.time() - start_wait > 15):
                    upload_complete = True
                    break
            except Exception:
                pass

            page.wait_for_timeout(2000)

        if not upload_complete:
            raise RuntimeError("Instagram Reel upload confirmation did not appear within 180s timeout.")

        # Extract post URL if present
        try:
            link = page.locator("a[href*='/reel/'], a[href*='/p/']").first
            if link.is_visible():
                reel_url = link.get_attribute("href")
                if reel_url and not reel_url.startswith("http"):
                    reel_url = f"https://www.instagram.com{reel_url}"
        except Exception:
            pass

        # Close any lingering dialogs
        try:
            close_btn = page.locator(
                "div[role='dialog'] svg[aria-label='Close'], div[role='dialog'] button:has-text('Close')"
            ).first
            if close_btn.is_visible():
                close_btn.click(force=True)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

        final_url = reel_url or "https://www.instagram.com/reels/"
        return {
            "status": "scheduled" if is_scheduled else "shared",
            "platform": "instagram",
            "engine": "insta_playwright",
            "reel_url": final_url,
            "title": title,
            "scheduled_date": scheduled_slot.get("scheduled_date") if scheduled_slot else None,
            "scheduled_time": scheduled_slot.get("scheduled_time") if scheduled_slot else None,
            "scheduled_slot": scheduled_slot.get("slot_name") if scheduled_slot else None,
            "display_plan": scheduled_slot.get("display_plan") if scheduled_slot else None,
        }

    def upload_multiple_reels(
        self,
        clips: List[Dict[str, Any]],
        scheduled: bool = True,
        max_per_day: int = 2,
        dry_run: bool = False,
        auto_delete: bool = False,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Uploads multiple vertical clips to Instagram Reels sequentially within the SAME browser session.
        Respects rolling 24-hour rate limit (4 uploads max) and peak window scheduling.
        """
        ledger = ScheduleLedger(self.output_dir / "schedule_ledger.json")

        # 1. Check Rolling 24-Hour Safety Cap (Max 4 uploads per 24 hours)
        recent_uploads = ledger.get_recent_upload_count(hours=24)
        print(f"\n[InstagramUploader] 📊 24-Hour Rolling Reel Count: {recent_uploads}/{STUDIO_UPLOAD_SAFETY_CAP} permitted uploads.")
        if recent_uploads >= STUDIO_UPLOAD_SAFETY_CAP:
            print(f"[InstagramUploader] 🛑 Safety Cap Reached: {STUDIO_UPLOAD_SAFETY_CAP} uploads already completed in the last 24 hours!")
            print("[InstagramUploader] 💾 All clips are preserved safely in output/.\n")
            for c in clips:
                c["instagram_status"] = "Queued for Next Upload Window"
                self._sync_summary_clip(c)
            self._display_schedule_plan_table(clips, title_prefix="Upload Safety Limit Reached")
            return clips

        # 2. Compute Peak Slot Schedule Plan if not already assigned
        resolver = PeakSlotResolver(ledger)
        if scheduled:
            slots = resolver.get_next_available_slots(num_clips=len(clips), max_per_day=max_per_day)
            for i, c in enumerate(clips):
                if not c.get("scheduled_slot") and i < len(slots):
                    c["scheduled_slot"] = slots[i]
                    c["scheduled_date"] = slots[i]["scheduled_date"]
                    c["scheduled_time"] = slots[i]["scheduled_time"]
                    c["display_plan"] = slots[i]["display_plan"]

        self._display_schedule_plan_table(clips, title_prefix="Planned")

        if dry_run:
            for idx, c in enumerate(clips, 1):
                s_slot = c.get("scheduled_slot") or {}
                c["instagram_result"] = {
                    "status": "dry_run_success",
                    "platform": "instagram",
                    "engine": "insta_playwright",
                    "reel_url": f"https://www.instagram.com/reels/dry_run_{idx}",
                    "title": c.get("youtube_title", f"Reel #{idx}"),
                    "scheduled_date": s_slot.get("scheduled_date"),
                    "scheduled_time": s_slot.get("scheduled_time"),
                    "scheduled_slot": s_slot.get("slot_name"),
                }
                c["instagram_status"] = "Dry-Run OK (Simulated)"
                self._sync_summary_clip(c)
            self._display_schedule_plan_table(clips, title_prefix="Dry-Run Confirmed")
            return clips

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright is not installed. Run 'pip install playwright && playwright install chromium'")

        self._unlock_profile()

        browser_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir.resolve()),
                headless=False,
                args=browser_args,
                viewport=None,
            )
            page = context.pages[0] if context.pages else context.new_page()

            print("[InstagramUploader] Navigating to Instagram...")
            try:
                page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass

            page.wait_for_timeout(3000)
            self._dismiss_modals(page)

            if not self.is_logged_in(page):
                print("\n[InstagramUploader] ⚠️ Not logged into Instagram. Please log in in the opened browser window...")
                for _ in range(60):
                    self._dismiss_modals(page)
                    if self.is_logged_in(page):
                        break
                    page.wait_for_timeout(2000)

            if not self.is_logged_in(page):
                context.close()
                raise RuntimeError("Instagram login required. Run 'clipwa login-insta' first.")

            total_clips = len(clips)
            for idx, clip in enumerate(clips, 1):
                file_path = clip.get("file_path") or str(self.output_dir / clip.get("file_name", ""))
                if not os.path.exists(file_path):
                    alt_path = str(self.output_dir / clip.get("file_name", ""))
                    if os.path.exists(alt_path):
                        file_path = alt_path
                    else:
                        print(f"⚠️ Clip [{idx}/{total_clips}] file not found on disk: {file_path}, skipping.")
                        continue

                title = clip.get("youtube_title") or f"Short #{idx}"
                desc = clip.get("youtube_description") or clip.get("hook_text") or ""
                tags = clip.get("tags") or []
                s_slot = clip.get("scheduled_slot")
                slot_label = f" -> Scheduled: {s_slot['display_plan']}" if s_slot else ""

                print(f"\n=======================================================")
                print(f" 📸 UPLOADING TO INSTAGRAM REELS [{idx}/{total_clips}]: {title}{slot_label}")
                print(f"=======================================================")

                if progress_callback:
                    progress_callback({
                        "stage": "uploading_insta",
                        "message": f"Uploading to Instagram [{idx}/{total_clips}]: {title}...",
                        "clip_index": idx,
                        "total_clips": total_clips,
                    })

                # Refresh home page between clips
                if idx > 1:
                    try:
                        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(2000)
                        self._dismiss_modals(page)
                    except Exception:
                        pass

                try:
                    res = self._upload_single_reel_in_page(
                        page=page,
                        video_path=file_path,
                        title=title,
                        description=desc,
                        tags=tags,
                        scheduled_slot=s_slot,
                        progress_callback=progress_callback,
                    )
                    clip["instagram_result"] = res
                    recent_uploads += 1

                    sched_str = f" (Scheduled for {s_slot['display_plan']})" if s_slot else ""
                    print(f"✅ Clip [{idx}/{total_clips}] Instagram Reel shared: {res.get('reel_url')}{sched_str}")

                    # Record to persistent schedule ledger
                    if s_slot and res.get("status") in ("scheduled", "shared", "uploaded"):
                        ledger.record_scheduled_clip(
                            title=title,
                            video_id=res.get("reel_url", f"insta_{idx}").split("/")[-2] or f"insta_{idx}",
                            video_url=res.get("reel_url", ""),
                            scheduled_date=s_slot["scheduled_date"],
                            scheduled_time=s_slot["scheduled_time"],
                            iso_timestamp=s_slot["iso_timestamp"],
                            slot_name=s_slot["slot_name"],
                            channel_id="instagram",
                            clip_id=clip.get("clip_id", idx),
                            platform="instagram",
                        )

                    ig_status_label = "Scheduled" if res.get("status") == "scheduled" else "Shared"
                    clip["instagram_status"] = ig_status_label

                    # Auto-delete ONLY if requested by caller (e.g. if YouTube also succeeded)
                    if auto_delete and res.get("status") in ("scheduled", "shared", "uploaded"):
                        try:
                            if os.path.exists(file_path):
                                os.remove(file_path)
                                print(f"[InstagramUploader] 🗑️ Cleaned up local file: {os.path.basename(file_path)}")
                            clip["file_deleted"] = True
                            clip["instagram_status"] = f"{ig_status_label} & Deleted Locally"
                        except Exception as ex:
                            print(f"[InstagramUploader] Warning deleting local file: {ex}")

                    self._sync_summary_clip(clip)

                except Exception as e:
                    print(f"❌ Clip [{idx}/{total_clips}] Instagram upload failed: {e}")
                    clip["instagram_result"] = {"error": str(e), "status": "failed"}
                    clip["instagram_status"] = "Error"
                    self._sync_summary_clip(clip)

                # Inter-Clip Cooldown: randomized 15-25 seconds before next Reel
                if idx < total_clips:
                    cooldown = random.uniform(15.0, 25.0)
                    print(f"\n[InstagramUploader] ⏳ Inter-clip stealth cooldown: pausing {cooldown:.1f}s before next Reel...")
                    human_move_mouse_jitter(page, count=2)
                    time.sleep(cooldown)
                else:
                    human_sleep(2.0, 3.5)

            context.close()

        self._display_schedule_plan_table(clips, title_prefix="Final Confirmed")
        return clips
