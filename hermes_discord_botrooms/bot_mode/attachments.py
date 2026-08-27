"""Safe staging for adapter-uploaded room attachments."""

from __future__ import annotations

import base64
import binascii
import os
import re
import uuid
from pathlib import Path

from .models import RoomAttachment

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def stage_data_attachment(
    root: Path,
    *,
    data_url: str,
    name: str,
    kind: str,
    mime_type: str = "",
) -> RoomAttachment:
    match = re.fullmatch(r"data:([^;,]+)?;base64,([A-Za-z0-9+/=\s]+)", data_url or "")
    if not match:
        raise ValueError("room attachment must be a base64 data URL")
    try:
        payload = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("room attachment has invalid base64 data") from exc
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise ValueError("room attachment exceeds the 25 MiB limit")
    safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(name or "attachment").name)
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "attachment"
    directory = Path(root) / "plugin-data" / "hermes-discord-botrooms" / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = directory / f"{uuid.uuid4().hex}-{safe_name}"
    with path.open("xb") as stream:
        stream.write(payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return RoomAttachment(
        path=str(path),
        name=safe_name,
        kind=kind if kind in {"image", "pdf", "file"} else "file",
        mime_type=mime_type or match.group(1) or "application/octet-stream",
    )
