import pytest

from rocketwatch.utils.megapool import BondCurve
from tests.lib.scripted_rocketpool import ScriptedRocketPool

ETH = 10**18

# base bond array [4, 8], reduced bond 2
_CURVE = BondCurve(base_bonds=(4.0, 8.0), reduced_bond=2.0)


class TestBondCurve:
    def test_follows_base_bond_array_then_reduced_bond(self) -> None:
        reqs = [_CURVE.requirement(n) for n in range(5)]
        assert reqs == pytest.approx([0.0, 4.0, 8.0, 10.0, 12.0])

    async def test_fetch_reads_protocol_settings(
        self, scripted_rp: ScriptedRocketPool
    ) -> None:
        scripted_rp.set_call(
            "rocketDAOProtocolSettingsNode.getBaseBondArray", [4 * ETH, 8 * ETH]
        )
        scripted_rp.set_call("rocketDAOProtocolSettingsNode.getReducedBond", 2 * ETH)

        curve = await BondCurve.fetch()

        assert curve.requirement(4) == pytest.approx(12.0)
