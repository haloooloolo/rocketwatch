import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from typing import Literal, cast

import aiohttp
import numpy as np
from discord import File, Interaction, app_commands
from discord.app_commands import describe
from discord.ext import commands
from eth_typing import ChecksumAddress, HexStr
from matplotlib import figure, ticker
from matplotlib import font_manager as fm
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

from rocketwatch.bot import RocketWatch
from rocketwatch.utils.embeds import Embed
from rocketwatch.utils.liquidity import (
    CEX,
    DEX,
    HTX,
    MEXC,
    OKX,
    BalancerV2,
    BalancerV3,
    Binance,
    BingX,
    Bitget,
    Bithumb,
    BitMart,
    Bitrue,
    Bitvavo,
    Bybit,
    Coinbase,
    CoinTR,
    CryptoDotCom,
    Exchange,
    GateIO,
    Kraken,
    Kucoin,
    Liquidity,
    Market,
    UniswapV3,
    UniswapV4,
)
from rocketwatch.utils.rocketpool import rp
from rocketwatch.utils.shared_w3 import w3
from rocketwatch.utils.time_debug import timerun, timerun_async
from rocketwatch.utils.visibility import is_hidden


@dataclass(frozen=True)
class MarketConfig:
    """Parameters for a single /wall subcommand (RPL vs rETH)."""

    title: str
    # Primary/quote axis labels. Primary is the x-axis & dashed reference line.
    primary_prefix: str  # e.g. "$" or "Ξ "
    secondary_prefix: str  # e.g. "Ξ " or "$"
    # Min step size for x-axis grid.
    step_size: float
    # Default {min,max}_price = multiplier * current price when user doesn't supply.
    default_min_multiplier: float
    default_max_multiplier: float


log = logging.getLogger("rocketwatch.wall")


class Wall(commands.GroupCog, name="wall"):
    def __init__(self, bot: RocketWatch):
        super().__init__()
        self.bot = bot
        self.cex_rpl: set[CEX] = {
            Binance("RPL", ["USDT", "USDC"]),
            Coinbase("RPL", ["USDC"]),
            GateIO("RPL", ["USDT"]),
            OKX("RPL", ["USDT"]),
            Bitget("RPL", ["USDT"]),
            MEXC("RPL", ["USDT"]),
            Bybit("RPL", ["USDT"]),
            CryptoDotCom("RPL", ["USD"]),
            Kraken("RPL", ["USD", "EUR"]),
            Kucoin("RPL", ["USDT"]),
            Bithumb("RPL", ["KRW"]),
            BingX("RPL", ["USDT"]),
            Bitvavo("RPL", ["EUR"]),
            HTX("RPL", ["USDT"]),
            BitMart("RPL", ["USDT"]),
            Bitrue("RPL", ["USDT"]),
            CoinTR("RPL", ["USDT"]),
        }
        self.dex_rpl: set[DEX] | None = None
        self.dex_reth: set[DEX] | None = None

    async def _get_dex_rpl(self) -> set[DEX]:
        if self.dex_rpl is None:
            self.dex_rpl = {
                BalancerV2(
                    [
                        await BalancerV2.WeightedPool.create(
                            HexStr(
                                "0x9f9d900462492d4c21e9523ca95a7cd86142f298000200000000000000000462"
                            )
                        )
                    ]
                ),
                await UniswapV3.create(
                    [
                        cast(
                            ChecksumAddress,
                            "0xe42318eA3b998e8355a3Da364EB9D48eC725Eb45",
                        ),
                        cast(
                            ChecksumAddress,
                            "0xcf15aD9bE9d33384B74b94D63D06B4A9Bd82f640",
                        ),
                    ]
                ),
                await UniswapV4.create(
                    [
                        # 1% ETH/RPL
                        (
                            HexStr(
                                "0xf54ebae2cdfe65593f7b9dbf655f498796c7744107a69d78456627faf98dc36f"
                            ),
                            200,
                            cast(
                                ChecksumAddress,
                                "0x0000000000000000000000000000000000000000",
                            ),
                            cast(
                                ChecksumAddress,
                                "0xD33526068D116cE69F19A9ee46F0bd304F21A51f",
                            ),
                        ),
                        # 0.45% USDC/RPL
                        (
                            HexStr(
                                "0xd36acc983941d38f6edb0ff6f6ee730e59cba0f2f720fea3ce240ae9b90fc4d0"
                            ),
                            90,
                            cast(
                                ChecksumAddress,
                                "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                            ),
                            cast(
                                ChecksumAddress,
                                "0xD33526068D116cE69F19A9ee46F0bd304F21A51f",
                            ),
                        ),
                    ]
                ),
            }
        return self.dex_rpl

    async def _get_dex_reth(self) -> set[DEX]:
        if self.dex_reth is None:
            # Both V3 rETH/WETH pools: primary=rETH=token_0 (rETH sorts before WETH)
            reth_uni_v3 = await UniswapV3.create(
                [
                    cast(
                        ChecksumAddress,
                        "0x553e9C493678d8606d6a5ba284643dB2110Df823",
                    ),
                    cast(
                        ChecksumAddress,
                        "0xa4e0faA58465A2D369aa21B3e42d43374c6F9613",
                    ),
                ],
                primary_is_token_0=True,
            )
            # Balancer V3 waEthWETH/rETH ComposableStable — primary=rETH=token_1
            # (waEthWETH sorts before rETH), so primary_is_token_0=False.
            reth_bal_v3 = BalancerV3(
                [
                    await BalancerV3.StablePool.create(
                        w3.to_checksum_address(
                            "0x1ea5870f7c037930ce1d5d8d9317c670e89e13e3"
                        ),
                        primary_is_token_0=False,
                    )
                ]
            )
            self.dex_reth = {reth_uni_v3, reth_bal_v3}
        return self.dex_reth

    @staticmethod
    def _get_market_depth_and_liquidity[K](
        markets: dict[K, Liquidity],
        x: np.ndarray,
        rpl_usd: float,
    ) -> tuple[np.ndarray, float]:
        depth = np.zeros_like(x)
        liquidity = 0.0

        for liq in markets.values():
            conv = liq.price / rpl_usd
            depth += np.array(list(map(liq.depth_at, x * conv))) / conv
            liquidity += (
                liq.depth_at(float(x[0] * conv)) + liq.depth_at(float(x[-1] * conv))
            ) / conv

        return depth, liquidity

    @timerun_async
    async def _get_cex_data(
        self, cex_set: set[CEX], x: np.ndarray, ref_price: float
    ) -> OrderedDict[CEX, np.ndarray]:
        depth: dict[CEX, np.ndarray] = {}
        liquidity: dict[CEX, float] = {}
        async with aiohttp.ClientSession() as session:
            requests = [cex.get_liquidity(session) for cex in cex_set]
            for result in zip(
                cex_set,
                await asyncio.gather(*requests, return_exceptions=True),
                strict=False,
            ):
                cex, maybe_markets = result
                if not isinstance(maybe_markets, BaseException):
                    markets: dict[Market, Liquidity] = maybe_markets
                    depth[cex], liquidity[cex] = self._get_market_depth_and_liquidity(
                        markets, x, ref_price
                    )
                elif isinstance(maybe_markets, Exception):
                    await self.bot.report_error(maybe_markets)

        return OrderedDict(
            sorted(depth.items(), key=lambda e: liquidity[e[0]], reverse=True)
        )

    @timerun
    async def _get_dex_data(
        self, dex_set: set[DEX], x: np.ndarray, ref_price: float
    ) -> OrderedDict[DEX, np.ndarray]:
        depth: dict[DEX, np.ndarray] = {}
        liquidity: dict[DEX, float] = {}
        for dex in dex_set:
            if pools := await dex.get_liquidity():
                depth[dex], liquidity[dex] = self._get_market_depth_and_liquidity(
                    pools, x, ref_price
                )

        return OrderedDict(
            sorted(depth.items(), key=lambda e: liquidity[e[0]], reverse=True)
        )

    @staticmethod
    def _label_exchange_data[E: Exchange](
        data: OrderedDict[E, np.ndarray], max_unique: int, color_other: str
    ) -> list[tuple[np.ndarray, str, str]]:
        ret = []
        for exchange, depth in list(data.items())[:max_unique]:
            ret.append((depth, str(exchange), exchange.color))

        if len(data) > max_unique:
            y = np.sum([depth for depth in list(data.values())[max_unique:]], axis=0)
            ret.append((y, "Other", color_other))

        return ret

    @staticmethod
    def _get_formatter(
        base_fmt: str,
        *,
        scale: float = 1.0,
        offset: float = 0.0,
        prefix: str = "",
        suffix: str = "",
    ) -> ticker.FuncFormatter:
        """Tick formatter with automatic K/M/B suffixing and four-figure
        full-number output (1000-9999) for readability."""

        def formatter(_x: float, _pos: float) -> str:
            value = _x * scale + offset
            levels = [
                (1_000_000_000, 1_000_000_000, "B"),
                (1_000_000, 1_000_000, "M"),
                (10_000, 1_000, "K"),
            ]
            modifier = ""
            for threshold, divisor, s in levels:
                if value >= threshold:
                    modifier = s
                    value /= divisor
                    break

            if value >= 1000:
                value_str = f"{value:,.0f}"
            else:
                value_str = f"{value:{base_fmt}}".rstrip(".")
            return prefix + value_str + modifier + suffix

        return ticker.FuncFormatter(formatter)

    @staticmethod
    def _plot_data(
        x: np.ndarray,
        primary_price: float,
        cex_data: OrderedDict[CEX, np.ndarray],
        dex_data: OrderedDict[DEX, np.ndarray],
        config: MarketConfig,
        bottom_formatter: ticker.Formatter,
        top_formatter: ticker.Formatter,
        y_right_formatter: ticker.Formatter,
    ) -> figure.Figure:
        fig, ax = plt.subplots(figsize=(10, 5))

        ax.minorticks_on()
        ax.grid(True, which="major", linestyle="--", linewidth=0.5, alpha=0.5)
        ax.grid(True, which="minor", linestyle=":", linewidth=0.3, alpha=0.5)

        ax.set_xlabel("price")
        ax.xaxis.labelpad = 8
        ax.set_ylabel("depth")
        ax.yaxis.labelpad = 10

        y = []
        colors = []

        max_unique = 7 - min(len(dex_data), 4) if dex_data else 9
        cex_data_aggr = Wall._label_exchange_data(cex_data, max_unique, "#555555")
        max_unique = 7 - min(len(cex_data), 4) if cex_data else 9
        dex_data_aggr = Wall._label_exchange_data(dex_data, max_unique, "#777777")

        y_offset = 0.0
        max_label_length: int = np.max(
            [len(t[1]) for t in (cex_data_aggr + dex_data_aggr)]
        )

        def add_data(
            _data: list[tuple[np.ndarray, str, str]], _name: str | None
        ) -> None:
            labels, handles = [], []
            for y_values, label, color in _data:
                y.append(y_values)
                labels.append(f"{label:\u00a0<{max_label_length}}")
                colors.append(color)
                handles.append(Rectangle((0, 0), 1, 1, color=color))

            nonlocal y_offset
            legend = ax.legend(
                handles,
                labels,
                title=_name,
                loc="upper left",
                bbox_to_anchor=(0, 1 - y_offset),
                prop=fm.FontProperties(family="monospace", size=10),
            )
            ax.add_artist(legend)
            y_offset += 0.025 + 0.055 * (len(_data) + int(_name is not None))

        if dex_data and cex_data:
            add_data(dex_data_aggr, "DEX")
            add_data(cex_data_aggr, "CEX")
        elif dex_data:
            add_data(dex_data_aggr, None)
        else:
            add_data(cex_data_aggr, None)

        ax.stackplot(
            x, np.array(y[::-1]), colors=colors[::-1], edgecolor="black", linewidth=0.3
        )
        ax.axvline(primary_price, color="black", linestyle="--", linewidth=1)

        range_size = x[-1] - x[0]
        ax.set_xlim((x[0], x[-1]))

        # Matplotlib's default locator may suggest ticks outside xlim for
        # visual margin — clamp to the actual data range so labels don't spill
        # past the plot edges.
        x_ticks = [t for t in ax.get_xticks() if x[0] <= t <= x[-1]]
        ax.set_xticks(
            [t for t in x_ticks if abs(t - primary_price) >= range_size / 20]
            + [primary_price]
        )
        ax.xaxis.set_major_formatter(bottom_formatter)

        ax_top = ax.twiny()
        ax_top.minorticks_on()
        ax_top.set_xlim(ax.get_xlim())
        ax_top.set_xticks(
            [t for t in x_ticks if abs(t - primary_price) >= range_size / 10]
            + [primary_price]
        )
        ax_top.xaxis.set_major_formatter(top_formatter)

        ax.yaxis.set_major_formatter(
            Wall._get_formatter("#.3g", prefix=config.primary_prefix)
        )

        ax_right = ax.twinx()
        ax_right.minorticks_on()
        ax_right.set_yticks(ax.get_yticks())
        ax_right.set_ylim(ax.get_ylim())
        ax_right.yaxis.set_major_formatter(y_right_formatter)

        return fig

    async def _run(
        self,
        interaction: Interaction,
        min_price: float | None,
        max_price: float | None,
        sources: Literal["All", "CEX", "DEX"],
        *,
        config: MarketConfig,
        primary_price: float,
        secondary_price: float,
        bottom_formatter: ticker.Formatter,
        top_formatter: ticker.Formatter,
        y_right_formatter: ticker.Formatter,
        cex_set: set[CEX],
        dex_set: set[DEX],
    ) -> None:
        embed = Embed(title=config.title)

        async def on_fail() -> None:
            embed.set_image(
                url="https://media1.giphy.com/media/hEc4k5pN17GZq/giphy.gif"
            )
            await interaction.followup.send(embed=embed)

        if min_price is None:
            min_price = config.default_min_multiplier * primary_price
        elif min_price < 0:
            min_price = primary_price + min_price

        if max_price is None:
            max_price = config.default_max_multiplier * primary_price
        elif max_price < 0:
            max_price = primary_price - max_price

        step = config.step_size
        min_price = max(0.0, min(min_price, primary_price - 5 * step))
        max_price = min(100 * primary_price, max(max_price, primary_price + 5 * step))
        x = np.arange(min_price, max_price + step, step)

        source_desc: list[str] = []
        cex_data: OrderedDict[CEX, np.ndarray] = OrderedDict()
        dex_data: OrderedDict[DEX, np.ndarray] = OrderedDict()

        try:
            if sources != "CEX" and dex_set:
                dex_data = await self._get_dex_data(dex_set, x, primary_price)
                source_desc.append(f"{len(dex_data)} DEX")
            if sources != "DEX" and cex_set:
                cex_data = await self._get_cex_data(cex_set, x, primary_price)
                source_desc.append(f"{len(cex_data)} CEX")
        except Exception as e:
            await self.bot.report_error(e, interaction)
            return await on_fail()

        if (not cex_data) and (not dex_data):
            log.error("No liquidity data found")
            return await on_fail()

        liquidity_primary = sum((y[0] + y[-1]) for y in (dex_data | cex_data).values())
        liquidity_secondary = liquidity_primary * (secondary_price / primary_price)

        buffer = BytesIO()
        fig = self._plot_data(
            x,
            primary_price,
            cex_data,
            dex_data,
            config,
            bottom_formatter=bottom_formatter,
            top_formatter=top_formatter,
            y_right_formatter=y_right_formatter,
        )
        fig.savefig(buffer, format="png")
        plt.close(fig)
        buffer.seek(0)

        embed.add_field(
            name="Current Price",
            value=(
                f"{config.primary_prefix}{primary_price:#,.6g} | "
                f"{config.secondary_prefix}{secondary_price:#,.6g}"
            ).replace("Ξ ", "Ξ"),
        )
        embed.add_field(
            name="Observed Liquidity",
            value=(
                f"{config.primary_prefix}{liquidity_primary:,.0f} | "
                f"{config.secondary_prefix}{liquidity_secondary:,.0f}"
            ).replace("Ξ ", "Ξ"),
        )
        embed.add_field(name="Sources", value=", ".join(source_desc))

        file_name = "wall.png"
        embed.set_image(url=f"attachment://{file_name}")
        await interaction.followup.send(embed=embed, files=[File(buffer, file_name)])
        return None

    @app_commands.command(name="rpl")
    @describe(
        min_price="lower end of price range in USD",
        max_price="upper end of price range in USD",
        sources="choose places to pull liquidity data from",
    )
    async def rpl(
        self,
        interaction: Interaction,
        min_price: float | None = None,
        max_price: float | None = None,
        sources: Literal["All", "CEX", "DEX"] = "All",
    ) -> None:
        """Show the current RPL market depth across exchanges"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embed_on_fail = Embed(title="RPL Market Depth")
        try:
            async with aiohttp.ClientSession() as session:
                rpl_usd = next(
                    iter(
                        (await Binance("RPL", ["USDT"]).get_liquidity(session)).values()
                    )
                ).price
                eth_usd = await rp.get_eth_usdc_price()
                rpl_eth = rpl_usd / eth_usd
        except Exception as e:
            await self.bot.report_error(e, interaction)
            embed_on_fail.set_image(
                url="https://media1.giphy.com/media/hEc4k5pN17GZq/giphy.gif"
            )
            await interaction.followup.send(embed=embed_on_fail)
            return

        config = MarketConfig(
            title="RPL Market Depth",
            primary_prefix="$",
            secondary_prefix="Ξ ",
            step_size=0.001,
            default_min_multiplier=0.0,
            default_max_multiplier=5.0,
        )
        secondary_scale = rpl_eth / rpl_usd
        await self._run(
            interaction,
            min_price,
            max_price,
            sources,
            config=config,
            primary_price=rpl_usd,
            secondary_price=rpl_eth,
            bottom_formatter=self._get_formatter(".2f", prefix="$"),
            top_formatter=self._get_formatter(
                ".5f", prefix="Ξ ", scale=secondary_scale
            ),
            y_right_formatter=self._get_formatter(
                "#.3g", prefix="Ξ ", scale=secondary_scale
            ),
            cex_set=self.cex_rpl,
            dex_set=await self._get_dex_rpl(),
        )

    @app_commands.command(name="reth")
    @describe(
        min_price="lower end of price range in ETH",
        max_price="upper end of price range in ETH",
    )
    async def reth(
        self,
        interaction: Interaction,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> None:
        """Show the current rETH market depth across DEXes"""
        await interaction.response.defer(ephemeral=is_hidden(interaction))
        embed_on_fail = Embed(title="rETH Market Depth")
        try:
            dex_set = await self._get_dex_reth()
            # Use the deepest rETH/WETH pool as the market-price oracle.
            # Proxy "depth" as cumulative depth at ±50% of each pool's own
            # spot price — bounded and roughly proportional to real TVL.
            market_price: float | None = None
            deepest_tvl = -1.0
            for dex in dex_set:
                pools = await dex.get_liquidity()
                for liq in pools.values():
                    tvl = liq.depth_at(liq.price * 0.5) + liq.depth_at(liq.price * 2.0)
                    if tvl > deepest_tvl:
                        deepest_tvl = tvl
                        market_price = liq.price
            if market_price is None or market_price <= 0:
                raise RuntimeError("no rETH pools returned liquidity")

            eth_usd = await rp.get_eth_usdc_price()
            reth_usd = market_price * eth_usd
        except Exception as e:
            await self.bot.report_error(e, interaction)
            embed_on_fail.set_image(
                url="https://media1.giphy.com/media/hEc4k5pN17GZq/giphy.gif"
            )
            await interaction.followup.send(embed=embed_on_fail)
            return

        config = MarketConfig(
            title="rETH Market Depth",
            primary_prefix="Ξ ",
            secondary_prefix="$",
            step_size=0.0001,
            default_min_multiplier=0.98,
            default_max_multiplier=1.02,
        )
        secondary_scale = reth_usd / market_price  # usd per eth
        await self._run(
            interaction,
            min_price,
            max_price,
            sources="DEX",
            config=config,
            primary_price=market_price,
            secondary_price=reth_usd,
            # Bottom = % relative to current price:
            # scaled value = x * (100/primary) - 100 = (x/primary - 1) * 100.
            bottom_formatter=self._get_formatter(
                "+.3g", scale=100 / market_price, offset=-100, suffix="%"
            ),
            top_formatter=self._get_formatter(".4f", prefix="Ξ "),
            y_right_formatter=self._get_formatter(
                "#.3g", prefix="$", scale=secondary_scale
            ),
            cex_set=set(),
            dex_set=dex_set,
        )


async def setup(bot: RocketWatch) -> None:
    await bot.add_cog(Wall(bot))
