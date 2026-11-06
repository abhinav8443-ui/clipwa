"""
Stealth human interaction helpers for Playwright browser automation.
Simulates natural human mouse movement, bezier-like trajectories, typing variance,
and realistic thinking delays to preserve YouTube Studio & Instagram trust scores.
"""

import random
import time
from typing import Optional


def human_sleep(min_sec: float = 2.0, max_sec: float = 4.5) -> None:
    """
    Sleeps for a randomized duration between min_sec and max_sec
    to simulate human reading, decision-making, and observation delays.
    """
    time.sleep(random.uniform(min_sec, max_sec))


def human_click(
    page,
    locator,
    min_sleep_after: float = 1.0,
    max_sleep_after: float = 2.5,
    timeout: int = 10000,
) -> None:
    """
    Executes a human-like click:
    1. Scrolls smoothly into view if needed.
    2. Hovers over the element with a slight random offset using realistic mouse steps.
    3. Pauses 0.3s to 0.8s simulating cursor aiming and human reaction time.
    4. Clicks the element and follows up with natural post-click sleep.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass

    time.sleep(random.uniform(0.2, 0.5))

    clicked = False
    try:
        box = locator.bounding_box()
        if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
            offset_x = random.randint(-5, 5)
            offset_y = random.randint(-3, 3)
            target_x = max(box["x"] + 2, min(box["x"] + box["width"] - 2, box["x"] + box["width"] / 2.0 + offset_x))
            target_y = max(box["y"] + 2, min(box["y"] + box["height"] - 2, box["y"] + box["height"] / 2.0 + offset_y))

            steps = random.randint(12, 25)
            page.mouse.move(target_x, target_y, steps=steps)
            time.sleep(random.uniform(0.3, 0.8))  # Human aiming pause
            page.mouse.click(target_x, target_y)
            clicked = True
    except Exception:
        pass

    if not clicked:
        try:
            locator.click(timeout=timeout, force=True)
        except Exception:
            try:
                page.evaluate("el => el.click()", locator)
            except Exception:
                pass

    if min_sleep_after > 0:
        human_sleep(min_sleep_after, max_sleep_after)


def human_type(
    page,
    locator,
    text: str,
    clear_first: bool = True,
    press_enter: bool = False,
    pause_after: float = 1.5,
) -> None:
    """
    Types text with human-like rhythm:
    - Never injects text instantaneously.
    - Clicks input, pauses 0.4s to 0.7s, clears existing content.
    - Types character-by-character with 35ms-95ms delay.
    - Micro-pauses (0.3s-0.5s) after punctuation and newlines.
    """
    human_click(page, locator, min_sleep_after=0.4, max_sleep_after=0.7)

    if clear_first:
        try:
            page.keyboard.press("Control+A")
            time.sleep(random.uniform(0.1, 0.2))
            page.keyboard.press("Backspace")
            time.sleep(random.uniform(0.2, 0.4))
        except Exception:
            pass

    for ch in text:
        delay_ms = random.randint(35, 95)
        try:
            locator.press_sequentially(ch, delay=delay_ms)
        except Exception:
            try:
                page.keyboard.type(ch, delay=delay_ms)
            except Exception:
                pass

        if ch in ".!?,:;\n":
            time.sleep(random.uniform(0.3, 0.5))
        elif random.random() < 0.07:
            time.sleep(random.uniform(0.1, 0.25))

    if press_enter:
        time.sleep(random.uniform(0.3, 0.6))
        page.keyboard.press("Enter")

    if pause_after > 0:
        time.sleep(random.uniform(pause_after * 0.8, pause_after * 1.2))


def human_move_mouse_jitter(page, count: int = 2) -> None:
    """Subtly moves mouse around viewport to signal live human presence."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        curr_w = viewport.get("width", 1280)
        curr_h = viewport.get("height", 800)
        for _ in range(count):
            rx = random.randint(int(curr_w * 0.2), int(curr_w * 0.8))
            ry = random.randint(int(curr_h * 0.2), int(curr_h * 0.8))
            page.mouse.move(rx, ry, steps=random.randint(8, 18))
            time.sleep(random.uniform(0.2, 0.5))
    except Exception:
        pass
