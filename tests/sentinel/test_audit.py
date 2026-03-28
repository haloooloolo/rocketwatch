import logging

from audit import log_action


class TestLogAction:
    def test_emits_info_log(self, caplog):
        with caplog.at_level(logging.INFO, logger="sentinel.audit"):
            log_action("delete_message", 1, 2, "spam", "success")
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.INFO

    def test_log_format(self, caplog):
        with caplog.at_level(logging.INFO, logger="sentinel.audit"):
            log_action("ban_member", 111, 222, "bad actor", "success")
        msg = caplog.records[0].message
        assert "ban_member" in msg
        assert "guild=111" in msg
        assert "target=222" in msg
        assert "status=success" in msg
        assert "'bad actor'" in msg
