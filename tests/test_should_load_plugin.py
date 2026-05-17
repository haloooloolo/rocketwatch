from rocketwatch.bot import RocketWatch

should_load_plugin = RocketWatch.should_load_plugin


class TestEmptyLists:
    def test_empty_lists_load_everything(self):
        # No include and no exclude → every plugin gets loaded.
        assert should_load_plugin("apr", set(), set()) is True
        assert should_load_plugin("anything_else", set(), set()) is True


class TestExcludeOnly:
    def test_excluded_plugin_skipped(self):
        assert should_load_plugin("apr", set(), {"apr"}) is False

    def test_non_excluded_plugin_loaded(self):
        # With exclude-only configuration, everything not on the list still loads.
        assert should_load_plugin("queue", set(), {"apr"}) is True


class TestIncludeOnly:
    def test_included_plugin_loaded(self):
        assert should_load_plugin("apr", {"apr"}, set()) is True

    def test_non_included_plugin_skipped(self):
        # With an include list configured, anything not listed is denied —
        # this is the "allow-list" mode.
        assert should_load_plugin("queue", {"apr"}, set()) is False


class TestIncludeWins:
    def test_include_overrides_exclude(self):
        # Spec: inclusion always wins when a name appears in both lists.
        # Useful as an escape hatch when an exclude list lives in shared config
        # and a single env wants to force one back on.
        assert should_load_plugin("apr", {"apr"}, {"apr"}) is True


class TestEdgeCases:
    def test_unknown_name_with_empty_config_loads(self):
        # The function makes no claim about whether the plugin actually exists —
        # presence on disk is the caller's concern.
        assert should_load_plugin("nonexistent_plugin", set(), set()) is True

    def test_case_sensitive(self):
        # Plugin names match by exact string; case differences are not normalised.
        assert should_load_plugin("APR", {"apr"}, set()) is False
        assert should_load_plugin("apr", {"APR"}, set()) is False
