from __future__ import annotations

import aiofiles
from pathlib import Path


async def async_write_file(
    output_dir: str,
    test_type: str,
    filename: str,
    content: str,
) -> str:
    dest = Path(output_dir) / test_type / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(dest, "w") as f:
        await f.write(content)
    return str(dest)
