from utils import solidity
from utils.solidity import (
    BEACON_START_DATE,
    beacon_block_to_date,
    date_to_beacon_block,
    mp_state_to_str,
    slot_to_beacon_day_epoch_slot,
    to_float,
    to_int,
)


class TestToFloat:
    def test_wei_to_ether(self):
        assert to_float(10**18) == 1.0

    def test_zero(self):
        assert to_float(0) == 0.0

    def test_fractional(self):
        assert to_float(5 * 10**17) == 0.5

    def test_custom_decimals(self):
        assert to_float(1_000_000, decimals=6) == 1.0

    def test_string_input(self):
        assert to_float("1000000000000000000") == 1.0

    def test_large_value(self):
        assert to_float(32 * 10**18) == 32.0


class TestToInt:
    def test_wei_to_ether(self):
        assert to_int(10**18) == 1

    def test_truncates(self):
        assert to_int(15 * 10**17) == 1

    def test_zero(self):
        assert to_int(0) == 0

    def test_custom_decimals(self):
        assert to_int(1_500_000, decimals=6) == 1


class TestBeaconBlockDate:
    def test_block_zero(self):
        assert beacon_block_to_date(0) == BEACON_START_DATE

    def test_block_one(self):
        assert beacon_block_to_date(1) == BEACON_START_DATE + 12

    def test_roundtrip(self):
        block = 1_000_000
        date = beacon_block_to_date(block)
        assert date_to_beacon_block(date) == block

    def test_date_to_block_truncates(self):
        date = BEACON_START_DATE + 13  # not a clean 12-second boundary
        assert date_to_beacon_block(date) == 1


class TestSlotToBeaconDayEpochSlot:
    def test_slot_zero(self):
        assert slot_to_beacon_day_epoch_slot(0) == (0, 0, 0)

    def test_slot_32(self):
        # slot 32 = epoch 1, slot 0 within epoch, day 0
        assert slot_to_beacon_day_epoch_slot(32) == (0, 1, 0)

    def test_full_day(self):
        # 225 epochs per day, 32 slots per epoch = 7200 slots per day
        slots_per_day = 225 * 32
        assert slot_to_beacon_day_epoch_slot(slots_per_day) == (1, 0, 0)


class TestMpStateToStr:
    def test_all_known_states(self):
        assert mp_state_to_str(0) == "initialised"
        assert mp_state_to_str(1) == "prelaunch"
        assert mp_state_to_str(2) == "staking"
        assert mp_state_to_str(3) == "withdrawable"
        assert mp_state_to_str(4) == "dissolved"

    def test_unknown_state(self):
        assert mp_state_to_str(99) == "99"


class TestTimeConstants:
    def test_seconds(self):
        assert solidity.seconds == 1

    def test_minutes(self):
        assert solidity.minutes == 60

    def test_hours(self):
        assert solidity.hours == 3600

    def test_days(self):
        assert solidity.days == 86400

    def test_weeks(self):
        assert solidity.weeks == 604800

    def test_years(self):
        assert solidity.years == 365 * 86400
