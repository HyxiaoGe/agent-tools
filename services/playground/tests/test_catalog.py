from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.catalog import TOOLS, catalog


def test_every_tool_has_complete_valid_examples_and_explicit_control_defaults():
    tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).date().isoformat()
    for tool in catalog(False)["tools"]:
        assert tool["examples"], f"{tool['id']} 缺少可直接运行的示例"
        names = {field["name"] for field in tool["fields"]}
        assert set(tool["defaults"]) == names
        for field in tool["fields"]:
            assert "default" in field
            if field["type"] == "select":
                options = [option["value"] if isinstance(option, dict) else option for option in field["options"]]
                assert field["default"] in options
            elif field["type"] == "checkbox":
                assert isinstance(field["default"], bool)
            elif field["type"] == "multiselect":
                assert field["default"]
            elif field["name"] == "departure_date":
                assert field["default"] == tomorrow
        for example in tool["examples"]:
            assert set(example["arguments"]) == names
            arguments = {name: value for name, value in example["arguments"].items() if value != ""}
            TOOLS[tool["id"]]["model"].model_validate(arguments)


def test_examples_reset_optional_filters_and_backend_defaults():
    tools = {tool["id"]: tool for tool in catalog(False)["tools"]}
    for example in tools["search_web"]["examples"]:
        assert example["arguments"]["count"] == 5
        assert example["arguments"]["recency_days"] == ""
    assert tools["read_page"]["examples"][0]["arguments"]["full_page"] is False
    assert tools["generate_image"]["examples"][1]["arguments"]["aspect_ratio"] == "1:1"
    assert tools["generate_image"]["examples"][1]["arguments"]["quality"] == "standard"
    assert tools["route_compare"]["examples"][0]["arguments"]["destination_city"] == ""
    assert tools["route_compare"]["examples"][0]["arguments"]["modes"] == ["driving", "transit"]
    assert tools["get_weather"]["examples"][1]["arguments"]["requested_date"] == ""
    assert tools["search_trains"]["examples"][0]["arguments"]["train_category"] == "high_speed"
