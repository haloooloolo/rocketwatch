import io

from discord import File


def TextFile(content: str, filename: str) -> File:
    return File(io.BytesIO(content.encode()), filename)
