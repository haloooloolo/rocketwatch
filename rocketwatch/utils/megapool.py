from dataclasses import dataclass
from typing import Self

from rocketwatch.utils import solidity
from rocketwatch.utils.rocketpool import rp


@dataclass(frozen=True, slots=True)
class BondCurve:
    """RPIP-42 bond curve: base bonds for the first validators, then a flat
    reduced bond for each one after."""

    base_bonds: tuple[float, ...]
    reduced_bond: float

    @classmethod
    async def fetch(cls) -> Self:
        base_bonds = await rp.call("rocketDAOProtocolSettingsNode.getBaseBondArray")
        reduced_bond = await rp.call("rocketDAOProtocolSettingsNode.getReducedBond")
        return cls(
            tuple(solidity.to_float(bond) for bond in base_bonds),
            solidity.to_float(reduced_bond),
        )

    def requirement(self, num_validators: int) -> float:
        """Total bond for `num_validators`, mirroring RocketNodeDeposit.getBondRequirement."""
        if num_validators == 0:
            return 0.0
        if num_validators <= len(self.base_bonds):
            return self.base_bonds[num_validators - 1]
        extra = num_validators - len(self.base_bonds)
        return self.base_bonds[-1] + extra * self.reduced_bond
