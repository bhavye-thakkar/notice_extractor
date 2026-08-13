"""Put a real image on the Windows clipboard.

Not a path, not a filename, not base64 - the actual pixels, so Ctrl+V in
Paint, Word, WhatsApp or an email pastes the notice.

Why ctypes rather than a library: the only thing needed is CF_DIB, which is
four calls to user32/kernel32.  pywin32 is a 10 MB dependency and Pillow's
own ImageGrab has no setter, so a new install requirement to move ~40 lines
of memory would be a poor trade in an app whose whole setup story is "one
button installs what is missing".

The awkward part is ownership.  After SetClipboardData succeeds the SYSTEM
owns the memory block and freeing it is a double-free; if it fails, WE still
own it and not freeing it is a leak.  Both directions are handled below -
see set_image().

Self-check:  python -m notice_extractor.utils.clipboard
"""

from __future__ import annotations

import io
import sys
from typing import Optional

#: Standard clipboard format for a device-independent bitmap.
CF_DIB = 8
#: BMP files start with a 14-byte BITMAPFILEHEADER that a DIB must not have.
_BMP_FILE_HEADER = 14
GMEM_MOVEABLE = 0x0002


class ClipboardError(RuntimeError):
    """The clipboard could not be written - with a reason worth showing."""


def available() -> bool:
    """Can this machine copy images at all?  (Windows only.)"""
    return sys.platform.startswith("win")


def _dib_bytes(image) -> bytes:
    """The image as a DIB: a BMP with its file header removed.

    Converted to RGB first.  A DIB carrying an alpha channel is read
    inconsistently - some apps paste the notice on a black rectangle - and a
    newspaper crop has nothing to be transparent about."""
    from PIL import Image                       # already a hard dependency

    if image.mode not in ("RGB", "L"):
        if image.mode in ("RGBA", "LA", "P"):
            # Flatten onto white: paper, not a black box.
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])
            image = flat
        else:
            image = image.convert("RGB")
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, "BMP")
    return buffer.getvalue()[_BMP_FILE_HEADER:]


def set_image(image, retries: int = 5) -> None:
    """Copy a PIL image to the clipboard.  Raises ClipboardError on failure.

    `retries` because OpenClipboard fails outright while another process has
    it open - a browser or Office does this for a few milliseconds at a time
    - and the honest response to "someone else is mid-copy" is to wait, not
    to tell the user copying is broken."""
    if not available():
        raise ClipboardError("Copying images to the clipboard is only "
                             "implemented on Windows.")
    import ctypes
    from ctypes import wintypes
    import time

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    data = _dib_bytes(image)

    opened = False
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05 * (attempt + 1))
    if not opened:
        raise ClipboardError("Another program is using the clipboard. "
                             "Try again in a moment.")

    handle = None
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ClipboardError("Out of memory while copying the image.")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("Could not lock the clipboard memory.")
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_DIB, handle):
            raise ClipboardError("Windows refused the image.")
        # Handed over: the system owns the block now, and freeing it here
        # would be a double free.
        handle = None
    finally:
        if handle is not None:        # we still own it - do not leak it
            kernel32.GlobalFree(handle)
        user32.CloseClipboard()


def get_image():
    """Read an image back off the clipboard (used by the self-check).

    Returns a PIL image, or None when the clipboard holds no bitmap."""
    try:
        from PIL import ImageGrab
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    return data if hasattr(data, "size") else None


# --- self-check ---------------------------------------------------------------

def demo() -> None:
    """Round-trip a picture through the real clipboard.

    Deliberately a REAL clipboard write and a REAL read back: the whole
    point of this module is the handover to Windows, and a mock of that
    proves nothing."""
    if not available():
        print("clipboard self-check skipped (not Windows)")
        return
    from PIL import Image, ImageDraw

    source = Image.new("RGB", (240, 90), (255, 255, 255))
    pen = ImageDraw.Draw(source)
    pen.rectangle((8, 8, 231, 81), outline=(20, 72, 110), width=3)
    pen.rectangle((20, 24, 120, 62), fill=(20, 72, 110))
    set_image(source)

    back = get_image()
    assert back is not None, "nothing came back off the clipboard"
    assert back.size == source.size, f"{back.size} != {source.size}"
    # Same picture: compare a few pixels rather than bytes (the round trip
    # goes through BMP, so the encoding differs even when the image does not).
    for xy in ((40, 40), (200, 20), (5, 5)):
        want = source.convert("RGB").getpixel(xy)
        got = back.convert("RGB").getpixel(xy)
        assert max(abs(a - b) for a, b in zip(want, got)) <= 2, \
            f"pixel {xy}: {got} != {want}"

    # An image WITH alpha must flatten onto white, not black.
    transparent = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    set_image(transparent)
    back = get_image()
    assert back is not None
    assert back.convert("RGB").getpixel((30, 20)) == (255, 255, 255), \
        "a transparent image pasted onto black"

    # Several copies in a row must each land (clipboard ownership released).
    for shade in (30, 120, 210):
        set_image(Image.new("RGB", (32, 32), (shade, shade, shade)))
    back = get_image()
    assert back is not None and \
        abs(back.convert("RGB").getpixel((16, 16))[0] - 210) <= 2, \
        "the last of several copies is not what is on the clipboard"
    print("clipboard self-check OK")


if __name__ == "__main__":
    demo()
