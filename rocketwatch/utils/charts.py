import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any

from matplotlib.figure import Figure

# one worker: keeps rendering off the event loop without running matplotlib
# concurrently, which it doesn't guarantee to be safe
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chart")


async def render_png(
    draw: Callable[[Figure], None],
    figsize: tuple[float, float] | None = None,
    **savefig_kwargs: Any,
) -> BytesIO:
    """Draw on a fresh figure and save it as PNG in a worker thread."""

    def render() -> BytesIO:
        fig = Figure(figsize=figsize)
        draw(fig)
        img = BytesIO()
        fig.savefig(img, format="png", **savefig_kwargs)
        img.seek(0)
        return img

    return await asyncio.get_running_loop().run_in_executor(_executor, render)
