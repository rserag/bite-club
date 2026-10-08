"""Bounded in-memory Telegram photo retrieval; private images never enter logs."""

import asyncio
import io
import struct
from collections.abc import Buffer

from aiogram import Bot
from aiogram.types import Message

from nutrition_bot.domain.ai import MAX_AI_IMAGE_BYTES, AiUnavailable


class LimitedPhotoBuffer(io.BytesIO):
    def write(self, data: Buffer, /) -> int:
        if self.tell() + memoryview(data).nbytes > MAX_AI_IMAGE_BYTES:
            raise AiUnavailable("The photo is too large. Send a smaller meal photo.")
        return super().write(data)


def image_media_type(data: bytes) -> str:
    """Validate actual bytes and dimensions before passing a single image upstream."""
    if not 1 <= len(data) <= MAX_AI_IMAGE_BYTES:
        raise AiUnavailable("The photo size is unsupported.")
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        kind = "image/png"
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        kind = "image/jpeg"
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            marker = data[offset + 1]
            if marker == 0xFF:
                offset += 1
                continue
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            length = int.from_bytes(data[offset + 2 : offset + 4], "big")
            if length < 2 or offset + 2 + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
                break
            offset += length + 2
    else:
        raise AiUnavailable("Send one JPEG or PNG meal photo.")
    if not (1 <= width <= 1600 and 1 <= height <= 1600 and width * height <= 2_560_000):
        raise AiUnavailable("The photo dimensions are unsupported. Send a smaller meal photo.")
    return kind


async def download_meal_photo(bot: Bot, message: Message) -> bytes:
    if not message.photo or message.media_group_id:
        raise AiUnavailable("Send one meal photo at a time, outside an album.")
    eligible = [
        photo
        for photo in message.photo
        if photo.file_size is not None
        and 0 < photo.file_size <= MAX_AI_IMAGE_BYTES
        and 0 < photo.width <= 1600
        and 0 < photo.height <= 1600
    ]
    if not eligible:
        raise AiUnavailable("Send a meal photo smaller than 2 MB and 1600 pixels per side.")
    selected = max(eligible, key=lambda photo: photo.width * photo.height)
    try:
        async with asyncio.timeout(15):
            file = await bot.get_file(selected.file_id, request_timeout=10)
            if not file.file_path or file.file_size is None or file.file_size > MAX_AI_IMAGE_BYTES:
                raise AiUnavailable("The photo file is unavailable or too large.")
            with LimitedPhotoBuffer() as buffer:
                await bot.download_file(
                    file.file_path, destination=buffer, timeout=10, chunk_size=65536
                )
                data = buffer.getvalue()
            image_media_type(data)
            return data
    except AiUnavailable:
        raise
    except Exception:
        raise AiUnavailable("The photo could not be downloaded. Enter the meal manually.") from None
