import threading

from matplotlib.figure import Figure
from PIL import Image

from rocketwatch.utils.charts import render_png


class TestRenderPng:
    async def test_renders_png_of_requested_size_off_the_loop_thread(self) -> None:
        threads: list[threading.Thread] = []

        def draw(fig: Figure) -> None:
            threads.append(threading.current_thread())
            fig.subplots().plot([0, 1], [0, 1])

        img = await render_png(draw, figsize=(4, 2), dpi=50)

        with Image.open(img) as png:
            assert png.format == "PNG"
            assert png.size == (200, 100)
        assert threads[0] is not threading.main_thread()
