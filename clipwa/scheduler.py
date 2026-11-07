"""
Research-backed peak engagement window scheduler and persistent ledger for YouTube Shorts.
Enforces 3-4 daily peak slots, multi-day rollover, and rolling 24-hour upload safety limits.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config, atomic_write_json


@dataclass
class PeakSlotDef:
    hour: int
    minute: int
    name: str
    tag: str
    time_str: str


# 2-Slot Daily Peak Model (Default: 2/day with 8-hour daytime gap and 16-hour overnight gap)
PEAK_SLOTS_2: List[PeakSlotDef] = [
    PeakSlotDef(12, 30, "Midday Lunch & Global Scroll Spike", "Midday Peak", "12:30 PM"),
    PeakSlotDef(20, 30, "Prime Nighttime Binge & US/UK Overlap", "Night Peak", "8:30 PM"),
]

# 4-Slot Peak Model (Optional: 4/day with 4 to 5 hour breathing windows)
PEAK_SLOTS_4: List[PeakSlotDef] = [
    PeakSlotDef(8, 0, "Morning Motivation Peak", "Morning Peak", "8:00 AM"),
    PeakSlotDef(12, 30, "Midday Lunch Spike", "Midday Peak", "12:30 PM"),
    PeakSlotDef(17, 30, "Early Evening Prime Lead-In", "Evening Peak", "5:30 PM"),
    PeakSlotDef(21, 30, "Nighttime Binge & US/UK Overlap", "Night Peak", "9:30 PM"),
]

# 3-Slot Peak Model (Optional: 3/day with 5.5 to 6 hour breathing windows)
PEAK_SLOTS_3: List[PeakSlotDef] = [
    PeakSlotDef(8, 30, "Morning Motivation Peak", "Morning Peak", "8:30 AM"),
    PeakSlotDef(14, 0, "Afternoon Focus Spike", "Afternoon Peak", "2:00 PM"),
    PeakSlotDef(20, 0, "Evening Prime Window", "Evening Peak", "8:00 PM"),
]

# Rolling 24-hour Studio upload safety cap
STUDIO_UPLOAD_SAFETY_CAP: int = 4


class ScheduleLedger:
    """Manages the persistent schedule ledger in output/schedule_ledger.json."""

    def __init__(self, ledger_path: Optional[Path] = None):
        self.ledger_path = Path(ledger_path) if ledger_path else (Config.OUTPUT_DIR / "schedule_ledger.json")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        """Load ledger data from disk or return default structure."""
        if not self.ledger_path.exists():
            return {
                "version": "1.0",
                "updated_at": datetime.now().isoformat(),
                "scheduled_videos": [],
                "upload_history": [],
            }
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {
                "version": "1.0",
                "updated_at": datetime.now().isoformat(),
                "scheduled_videos": [],
                "upload_history": [],
            }

    def save(self, data: Dict[str, Any]) -> None:
        """Save ledger data atomically to disk with default=str."""
        data["updated_at"] = datetime.now().isoformat()
        try:
            atomic_write_json(self.ledger_path, data)
        except Exception as e:
            print(f"[ScheduleLedger] Warning writing ledger: {e}")

    def get_scheduled_entries(self) -> List[Dict[str, Any]]:
        """Return all scheduled video entries."""
        data = self.load()
        return data.get("scheduled_videos", [])

    def get_all_scheduled_datetimes(self) -> List[datetime]:
        """Return parsed datetime objects of all scheduled videos."""
        entries = self.get_scheduled_entries()
        result: List[datetime] = []
        for entry in entries:
            iso = entry.get("iso_timestamp")
            if iso:
                try:
                    dt = datetime.fromisoformat(iso)
                    result.append(dt)
                    continue
                except Exception:
                    pass
            # Fallback to date + time
            d_str = entry.get("scheduled_date")
            t_str = entry.get("scheduled_time")
            if d_str and t_str:
                try:
                    dt = datetime.strptime(f"{d_str} {t_str}", "%b %d, %Y %I:%M %p")
                    result.append(dt)
                except Exception:
                    pass
        return sorted(result)

    def get_recent_upload_count(self, hours: int = 24, platform: Optional[str] = None) -> int:
        """Count uploads performed in the last N hours to enforce rolling safety cap."""
        data = self.load()
        history = data.get("upload_history", [])
        cutoff = datetime.now() - timedelta(hours=hours)
        count = 0
        for h in history:
            if platform and h.get("platform") != platform:
                continue
            ts_str = h.get("uploaded_at")
            if ts_str:
                try:
                    dt = datetime.fromisoformat(ts_str)
                    if dt >= cutoff:
                        count += 1
                except Exception:
                    pass
        return count

    def record_scheduled_clip(
        self,
        title: str,
        video_id: str,
        video_url: str,
        scheduled_date: str,
        scheduled_time: str,
        iso_timestamp: str,
        slot_name: str,
        channel_id: Optional[str] = None,
        clip_id: Optional[int] = None,
        platform: str = "youtube",
    ) -> None:
        """Record a newly confirmed scheduled video in the persistent ledger."""
        data = self.load()
        scheduled_videos = data.setdefault("scheduled_videos", [])
        upload_history = data.setdefault("upload_history", [])

        entry = {
            "clip_id": clip_id,
            "title": title,
            "video_id": video_id,
            "video_url": video_url,
            "platform": platform,
            "scheduled_date": scheduled_date,
            "scheduled_time": scheduled_time,
            "iso_timestamp": iso_timestamp,
            "slot_name": slot_name,
            "channel_id": channel_id or ("instagram" if platform == "instagram" else "active"),
            "recorded_at": datetime.now().isoformat(),
        }

        # Deduplicate existing entry with matching video_id or matching date+time for the same platform
        scheduled_videos = [
            v for v in scheduled_videos
            if not (
                v.get("video_id") == video_id and v.get("platform", "youtube") == platform
            ) and not (
                v.get("scheduled_date") == scheduled_date
                and v.get("scheduled_time") == scheduled_time
                and v.get("platform", "youtube") == platform
            )
        ]
        scheduled_videos.append(entry)
        data["scheduled_videos"] = scheduled_videos

        # Record upload event for 24h rolling rate limit
        upload_history.append({
            "title": title,
            "video_id": video_id,
            "platform": platform,
            "uploaded_at": datetime.now().isoformat(),
        })
        data["upload_history"] = upload_history

        self.save(data)
        print(f"[ScheduleLedger] [SCHEDULED] Ledger updated ({platform}): '{title}' scheduled for {scheduled_date} @ {scheduled_time} ({slot_name})")

    def merge_studio_scraped_slots(self, scraped_slots: List[Dict[str, Any]]) -> None:
        """Merge live scheduled shorts found in YouTube Studio into local ledger."""
        if not scraped_slots:
            return
        data = self.load()
        existing = data.setdefault("scheduled_videos", [])
        existing_keys = {
            (v.get("scheduled_date"), v.get("scheduled_time"))
            for v in existing
        }

        added = 0
        for s in scraped_slots:
            k = (s.get("scheduled_date"), s.get("scheduled_time"))
            if k not in existing_keys and s.get("scheduled_date") and s.get("scheduled_time"):
                existing.append(s)
                existing_keys.add(k)
                added += 1

        if added > 0:
            self.save(data)
            print(f"[ScheduleLedger] [SYNC] Synced {added} live scheduled Shorts from YouTube Studio calendar.")


class PeakSlotResolver:
    """Calculates optimal peak engagement slots with strict algorithmic spacing and multi-day rollover."""

    def __init__(self, ledger: Optional[ScheduleLedger] = None):
        self.ledger = ledger or ScheduleLedger()

    def get_peak_slot_defs(self, max_per_day: int = 2) -> List[PeakSlotDef]:
        """Return slot definitions based on daily posting density."""
        if max_per_day <= 2:
            return PEAK_SLOTS_2
        elif max_per_day == 3:
            return PEAK_SLOTS_3
        return PEAK_SLOTS_4

    def get_next_available_slots(
        self,
        num_clips: int,
        max_per_day: int = 2,
        extra_occupied_dts: Optional[List[datetime]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Calculates the next available peak slots starting >= 60 minutes from now.
        Guarantees >= 6.5 hours between shorts on the same day and enforces max_per_day per date.
        """
        now = datetime.now()
        earliest_dt = now + timedelta(minutes=60)
        slot_defs = self.get_peak_slot_defs(max_per_day=max_per_day)

        # Minimum breathing space: >= 6.5 hours (390 mins) for 2/day, 5 hours for 3/day, 3.5 hours for 4/day
        min_breathing_mins = 390 if max_per_day <= 2 else (300 if max_per_day == 3 else 210)

        # Gather all currently occupied scheduled datetimes
        all_occupied: List[datetime] = [
            dt for dt in self.ledger.get_all_scheduled_datetimes()
            if dt >= now - timedelta(hours=1)
        ]
        if extra_occupied_dts:
            for edt in extra_occupied_dts:
                if edt not in all_occupied:
                    all_occupied.append(edt)

        # Count occupied slots per calendar date
        date_counts: Dict[str, int] = {}
        for occ in all_occupied:
            d_key = occ.strftime("%Y-%m-%d")
            date_counts[d_key] = date_counts.get(d_key, 0) + 1

        assigned_slots: List[Dict[str, Any]] = []
        day_offset = 0

        while len(assigned_slots) < num_clips and day_offset < 365:
            target_date = (now + timedelta(days=day_offset)).date()
            d_key = target_date.strftime("%Y-%m-%d")

            # Check if this date already reached its max allowed slots
            if date_counts.get(d_key, 0) >= max_per_day:
                day_offset += 1
                continue

            for slot in slot_defs:
                slot_dt = datetime(
                    target_date.year,
                    target_date.month,
                    target_date.day,
                    slot.hour,
                    slot.minute,
                )

                # Rule 1: Never schedule in the past or < 60 min away
                if slot_dt < earliest_dt:
                    continue

                # Rule 2: Minimum breathing room (>= 6.5 hours for 2/day) from any scheduled video
                conflict = False
                for occ in all_occupied:
                    diff_mins = abs((slot_dt - occ).total_seconds()) / 60.0
                    if diff_mins < min_breathing_mins:
                        conflict = True
                        break

                if conflict:
                    continue

                # Slot is open and valid!
                day_label = (
                    "Today"
                    if day_offset == 0
                    else ("Tomorrow" if day_offset == 1 else f"Day +{day_offset}")
                )
                date_str = slot_dt.strftime("%b %d, %Y")
                time_str = slot.time_str

                slot_info = {
                    "datetime": slot_dt.isoformat(),
                    "scheduled_date": date_str,
                    "scheduled_time": time_str,
                    "iso_timestamp": slot_dt.isoformat(),
                    "slot_name": slot.name,
                    "slot_label": f"{slot_dt.strftime('%a, %b %d')} @ {time_str}",
                    "tag": slot.tag,
                    "day_label": day_label,
                    "display_plan": f"{slot_dt.strftime('%a, %b %d')} @ {time_str} ({slot.tag})",
                }

                assigned_slots.append(slot_info)
                all_occupied.append(slot_dt)
                date_counts[d_key] = date_counts.get(d_key, 0) + 1

                if len(assigned_slots) == num_clips:
                    break
                if date_counts.get(d_key, 0) >= max_per_day:
                    break

            day_offset += 1

        return assigned_slots
