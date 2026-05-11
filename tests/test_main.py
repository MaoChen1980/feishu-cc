"""Tests for __main__ — CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _call_with_level(calls, level: str) -> bool:
    """Check if any logger.add() call includes the given level."""
    return any(
        ca[1].get("level") == level for ca in calls
    )


def test_main_default_args() -> None:
    """main() with default arguments should use INFO log level and no config."""
    with (
        patch("sys.argv", ["feishu-cc"]),
        patch("feishu_cc.__main__.logger.remove") as mock_remove,
        patch("feishu_cc.__main__.logger.add") as mock_add,
        patch("feishu_cc.__main__.CONFIG_DIR") as mock_config_dir,
        patch("feishu_cc.app.FeishuCCApp") as mock_app_cls,
    ):
        mock_config_dir.__truediv__.return_value = MagicMock()
        mock_app = mock_app_cls.return_value

        from feishu_cc.__main__ import main
        main()

        mock_remove.assert_called_once()
        assert _call_with_level(mock_add.call_args_list, "INFO")
        mock_app_cls.assert_called_once_with(config_path=None)
        mock_app.run.assert_called_once()


def test_main_custom_config() -> None:
    """main() with --config should pass the path to FeishuCCApp."""
    with (
        patch("sys.argv", ["feishu-cc", "--config", "/custom/path.json"]),
        patch("feishu_cc.__main__.logger.remove"),
        patch("feishu_cc.__main__.logger.add"),
        patch("feishu_cc.__main__.CONFIG_DIR") as mock_config_dir,
        patch("feishu_cc.app.FeishuCCApp") as mock_app_cls,
    ):
        mock_config_dir.__truediv__.return_value = MagicMock()

        from feishu_cc.__main__ import main
        main()

        mock_app_cls.assert_called_once_with(config_path="/custom/path.json")


def test_main_debug_log_level() -> None:
    """main() with --log-level DEBUG should set stderr handler to DEBUG."""
    with (
        patch("sys.argv", ["feishu-cc", "--log-level", "DEBUG"]),
        patch("feishu_cc.__main__.logger.remove"),
        patch("feishu_cc.__main__.logger.add") as mock_add,
        patch("feishu_cc.__main__.CONFIG_DIR") as mock_config_dir,
        patch("feishu_cc.app.FeishuCCApp"),
    ):
        mock_config_dir.__truediv__.return_value = MagicMock()

        from feishu_cc.__main__ import main
        main()

        assert _call_with_level(mock_add.call_args_list, "DEBUG")


def test_main_creates_log_dir() -> None:
    """main() should create the logs directory."""
    with (
        patch("sys.argv", ["feishu-cc"]),
        patch("feishu_cc.__main__.logger.remove"),
        patch("feishu_cc.__main__.logger.add"),
        patch("feishu_cc.__main__.CONFIG_DIR") as mock_config_dir,
        patch("feishu_cc.app.FeishuCCApp"),
    ):
        mock_log_dir = MagicMock()
        mock_config_dir.__truediv__.return_value = mock_log_dir

        from feishu_cc.__main__ import main
        main()

        mock_log_dir.mkdir.assert_called_once_with(parents=True, exist_ok=True)
