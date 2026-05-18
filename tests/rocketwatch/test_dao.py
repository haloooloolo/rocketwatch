from collections.abc import Mapping
from typing import Any

import pytest

from rocketwatch.utils.dao import (
    DAO,
    DefaultDAO,
    OracleDAO,
    ProtocolDAO,
    SecurityCouncil,
    _share_repr,
    build_claimer_description,
    decode_setting_multi,
)


class TestSanitize:
    def test_passes_through_short_messages_unchanged(self):
        msg = "Concise proposal"
        assert DAO.sanitize(msg) == msg

    def test_at_max_length_unchanged(self):
        msg = "a" * 150
        assert DAO.sanitize(msg) == msg

    def test_over_max_length_truncates_with_ellipsis(self):
        msg = "a" * 200
        out = DAO.sanitize(msg)
        # 150 chars total, last char is the ellipsis, preceded by 149 'a's.
        assert len(out) == 150
        assert out.endswith("…")
        assert out[:-1] == "a" * 149


class TestShareRepr:
    def test_zero_yields_no_asterisks(self):
        assert _share_repr(0) == ""

    def test_full_yields_max_width_asterisks(self):
        # 100% with default max_width=35.
        assert _share_repr(100) == "*" * 35

    def test_proportional_count(self):
        # 50% of 35 = 17.5 → rounds to 18 (banker's rounding goes to even).
        out = _share_repr(50)
        assert len(out) in (17, 18)
        # And the only allowed character is '*'.
        assert set(out) == {"*"}

    def test_custom_max_width(self):
        assert _share_repr(100, max_width=10) == "*" * 10


class TestBuildClaimerDescription:
    def test_renders_three_share_lines(self):
        # Args store shares as 10**16-denominated integers (50% → 50 * 10**16).
        out = build_claimer_description(
            {
                "nodePercent": 50 * 10**16,
                "protocolPercent": 30 * 10**16,
                "trustedNodePercent": 20 * 10**16,
            }
        )
        assert "Node Operator Share" in out
        assert "Protocol DAO Share" in out
        assert "Oracle DAO Share" in out
        # Percentages formatted as 1-decimal floats.
        assert "50.0%" in out
        assert "30.0%" in out
        assert "20.0%" in out


class TestDecodeSettingMulti:
    def test_decodes_uint_bool_and_address(self, monkeypatch: pytest.MonkeyPatch):
        # The function dispatches on a parallel `types` array:
        #   0=uint256, 1=bool, 2=address. Stub `w3.to_int` and
        #   `w3.to_checksum_address` to verify the dispatch.
        from rocketwatch.utils import dao as dao_module

        monkeypatch.setattr(dao_module.w3, "to_int", lambda b: int.from_bytes(b))
        monkeypatch.setattr(
            dao_module.w3, "to_checksum_address", lambda b: "0xchecksum"
        )

        args: Mapping[str, Any] = {
            "settingContractNames": ["A", "B", "C"],
            "settingPaths": ["a.x", "b.y", "c.z"],
            "types": [0, 1, 2],
        }
        values = [b"\x00\x05", b"\x01", b"\x00" * 20]
        out = decode_setting_multi(args, values)
        # uint => decoded as 5 from b'\x00\x05'.
        assert "`a.x` set to `5`" in out
        # bool => any non-empty bytes is truthy.
        assert "`b.y` set to `True`" in out
        # address => routed through to_checksum_address stub.
        assert "`c.z` set to `0xchecksum`" in out

    def test_unknown_type_falls_through_to_placeholder(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from rocketwatch.utils import dao as dao_module

        monkeypatch.setattr(dao_module.w3, "to_int", lambda b: 0)
        args: Mapping[str, Any] = {
            "settingContractNames": ["A"],
            "settingPaths": ["a.x"],
            "types": [99],
        }
        out = decode_setting_multi(args, [b""])
        assert "`a.x` set to `???`" in out


class TestDefaultDaoInit:
    def test_odao_display_name(self):
        dao = DefaultDAO("rocketDAONodeTrustedProposals")
        assert dao.display_name == "oDAO"
        assert dao.contract_name == "rocketDAONodeTrustedProposals"

    def test_security_council_display_name(self):
        dao = DefaultDAO("rocketDAOSecurityProposals")
        assert dao.display_name == "Security Council"

    def test_unknown_dao_raises(self):
        with pytest.raises(ValueError, match="Unknown DAO"):
            DefaultDAO("rocketRandomThing")  # type: ignore[arg-type]


class TestSubclassDefaults:
    def test_oracle_dao_inherits_odao_setup(self):
        dao = OracleDAO()
        assert dao.contract_name == "rocketDAONodeTrustedProposals"
        assert dao.display_name == "oDAO"

    def test_security_council_inherits_setup(self):
        dao = SecurityCouncil()
        assert dao.contract_name == "rocketDAOSecurityProposals"
        assert dao.display_name == "Security Council"


class TestProtocolDaoProposalVotesTotal:
    def test_excludes_veto_includes_abstain(self):
        # The `votes_total` property sums for+against+abstain but NOT veto —
        # that's how quorum math works for the pDAO.
        p = ProtocolDAO.Proposal(
            id=1,
            proposer="0x0",  # type: ignore[arg-type]
            message="m",
            payload=b"",
            created=0,
            start=0,
            end_phase_1=0,
            end_phase_2=0,
            expires=0,
            votes_for=100.0,
            votes_against=50.0,
            votes_veto=999.0,
            votes_abstain=10.0,
            quorum=200.0,
            veto_quorum=1000.0,
        )
        assert p.votes_total == 160.0
