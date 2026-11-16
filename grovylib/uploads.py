"""Files sent from the phone (attachments)."""
import os
import re
import secrets
import stat
import time

from . import core


MAX_UPLOAD = 100 * 1024 * 1024   # bytes per attachment sent from the phone
ATTACH_TTL = 7 * 86400          # attachments older than this are deleted
ATTACH_NAME = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}-[A-Za-z0-9._-]{1,80}")   # what save_attachment names files
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def private_dir(path):
    """Create PATH (mode 700) under a shared temp dir. Its parent must be ours, a real directory and private:
    anyone can create names in /tmp, so a planted symlink or open directory there is refused, never written into."""
    top = os.path.dirname(path)
    os.makedirs(top, mode=0o700, exist_ok=True)
    st = os.lstat(top)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OSError(f"{top} is not a private directory of this user")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def prune_attachments(top, ttl=ATTACH_TTL):
    """Delete attachments older than ttl, and session folders left empty."""
    cutoff = time.time() - ttl
    for d in os.scandir(top):
        if not d.is_dir(follow_symlinks=False):
            continue
        for f in os.scandir(d.path):
            try:
                if f.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(f.path)
            except OSError:
                pass
        try:
            os.rmdir(d.path)   # only succeeds when empty
        except OSError:
            pass


class Attachments:
    """Files sent from the phone: part of Store."""

    # ------------------------------------------------------------ pages (local "artifacts")
    def save_attachment(self, name, filename, read, size):
        """Write a file sent from the phone where the session's Claude may read it. read(n) yields the body."""
        with self.lock:
            if name not in self.sessions:
                return 404, "no such session"
        if size > MAX_UPLOAD:
            return 413, f"too large (the limit is {MAX_UPLOAD // 2**20} MB)"
        if size <= 0:
            return 400, "empty file"
        try:
            top = private_dir(core.attachments_dir())
            prune_attachments(top)
            d = os.path.join(top, name)
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError as e:
            return 500, f"can't use the attachments folder: {e}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(filename or "")).strip("._-")[:80] or "file"
        path = os.path.join(d, f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}-{safe}")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except OSError as e:
            return 500, f"can't save it: {e}"
        try:
            with os.fdopen(fd, "wb") as f:
                left = size
                while left:
                    chunk = read(min(left, 1 << 16))
                    if not chunk:
                        raise OSError("upload cut short")
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as e:
            try:
                os.unlink(path)
            except OSError:
                pass
            return 400, f"not saved: {e}"
        return 200, {"path": path, "name": safe, "size": size}

    def read_attachment(self, name, filename, images=False):
        """An image the phone sent (thumbnails), or with images=True one Claude kept with `grovy image` (image
        widgets): (content type, bytes) or None."""
        ext = filename.rsplit(".", 1)[-1].lower()
        if not core.valid_name(name) or not ATTACH_NAME.fullmatch(filename) or ext not in IMAGE_TYPES:
            return None
        try:
            top = os.path.join(self.dir, "images") if images else private_dir(core.attachments_dir())
            fd = os.open(os.path.join(top, name, filename), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return None
                return IMAGE_TYPES[ext], f.read(MAX_UPLOAD + 1)
        except OSError:
            return None
