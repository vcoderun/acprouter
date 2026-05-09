from __future__ import annotations as _annotations

from acp import plan_entry, start_tool_call, tool_diff_content, update_plan
from acp.schema import (
    AvailableCommand,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    ToolCallUpdate,
)

from acprouter.projection import (
    TELEGRAM_TEXT_LIMIT,
    _card_prefix_lines,
    _compose_with_blocks,
    _display_current_value,
    _extract_read_target_from_parsed_command,
    _extract_read_target_from_raw_input,
    _flatten_select_options,
    _is_read_tool_update,
    _normalize_status,
    _RenderedBlock,
    _truncate_block_body,
    append_text_chunk,
    build_plan_checklist,
    render_approval_preview,
    render_approval_resolution,
    render_plan_update,
    render_selection_surface,
    render_tool_update,
    should_project_tool_update,
    truncate_for_telegram,
)


def test_append_text_chunk_keeps_recent_content():
    text = append_text_chunk("", "hello")
    text = append_text_chunk(text, " world")

    assert text == "hello world"


def test_render_tool_update_includes_diff_preview():
    update = start_tool_call(
        "tool-1",
        "write file",
        status="in_progress",
        content=[tool_diff_content("README.md", "# new", "# old")],
    )

    rendered = render_tool_update(update)

    assert "<b>Tool update</b>" in rendered
    assert "README.md" in rendered
    assert "<b>Diff:</b>" in rendered
    assert '<pre language="diff">' in rendered
    assert "write file" in rendered
    assert "--- a/README.md" in rendered
    assert "+++ b/README.md" in rendered


def test_render_approval_preview_uses_approval_title():
    update = start_tool_call(
        "tool-1",
        "write file",
        status="in_progress",
        content=[tool_diff_content("../README.md", "# new", "# old")],
    )

    rendered = render_approval_preview(update)

    assert "<b>Approval required</b>" in rendered
    assert "write file" in rendered
    assert "<b>Diff:</b>" in rendered


def test_render_tool_update_uses_raw_input_path_preview_for_file_tools():
    update = ToolCallUpdate(
        tool_call_id="tool-2",
        title="mcp_host_write_workspace_file",
        status="in_progress",
        raw_input={
            "path": "/Users/mert/Desktop/playground/main.py",
            "content": "print('hello')\n",
        },
    )

    rendered = render_tool_update(update)

    assert "/Users/mert/Desktop/playground/main.py" in rendered
    assert "print" in rendered
    assert "<b>Content:</b>" in rendered


def test_render_plan_update_lists_entries():
    plan = update_plan(
        [
            plan_entry("first", status="pending"),
            plan_entry("second", status="completed"),
        ]
    )

    rendered = render_plan_update(plan)

    assert "first" in rendered
    assert "completed" in rendered
    assert "<b>Current plan</b>" in rendered
    assert "<pre>" in rendered


def test_build_plan_checklist_maps_plan_status_to_checklist_tasks():
    plan = update_plan(
        [
            plan_entry("first", status="pending"),
            plan_entry("second", status="in_progress"),
            plan_entry("first", status="completed"),
        ]
    )

    checklist = build_plan_checklist(plan)

    assert checklist.title == "Current plan"
    assert [(task.key, task.text, task.is_done) for task in checklist.tasks] == [
        ("first\x000", "first", False),
        ("second\x000", "[in progress] second", False),
        ("first\x001", "first", True),
    ]


def test_render_selection_surface_lists_current_values_and_commands():
    rendered = render_selection_surface(
        current_mode_id="agent",
        current_model_id="openai:gpt-5",
        selection_options=[
            SessionConfigOptionSelect.model_validate(
                {
                    "id": "thinking",
                    "name": "Thinking",
                    "description": "Control reasoning depth.",
                    "type": "select",
                    "currentValue": "medium",
                    "options": [
                        {"name": "Medium", "value": "medium"},
                        {"name": "High", "value": "high"},
                    ],
                }
            )
        ],
        commands=[
            AvailableCommand.model_validate(
                {
                    "name": "model",
                    "description": "Show or set the current model.",
                    "input": {"hint": "provider:model"},
                }
            ),
            AvailableCommand.model_validate(
                {
                    "name": "thinking",
                    "description": "Show or set the reasoning level.",
                    "input": {"hint": "medium|high"},
                }
            ),
        ],
    )

    assert "Session selections" in rendered
    assert "agent" in rendered
    assert "openai:gpt-5" in rendered
    assert "thinking: Medium" in rendered
    assert "/model provider:model" in rendered
    assert "/thinking medium|high" in rendered


def test_render_selection_surface_skips_duplicate_mode_and_model_options():
    rendered = render_selection_surface(
        current_mode_id="agent",
        current_model_id="codex:gpt-5.4",
        selection_options=[
            SessionConfigOptionSelect.model_validate(
                {
                    "id": "mode",
                    "name": "Mode",
                    "description": "Current mode.",
                    "type": "select",
                    "currentValue": "agent",
                    "options": [{"name": "Agent", "value": "agent"}],
                }
            ),
            SessionConfigOptionSelect.model_validate(
                {
                    "id": "model",
                    "name": "Model",
                    "description": "Current model.",
                    "type": "select",
                    "currentValue": "codex:gpt-5.4",
                    "options": [{"name": "GPT-5.4", "value": "codex:gpt-5.4"}],
                }
            ),
        ],
        commands=[
            AvailableCommand.model_validate(
                {
                    "name": "models",
                    "description": "List models.",
                }
            )
        ],
    )

    assert "<b>mode:</b> <code>agent</code>" in rendered
    assert "<b>model:</b> <code>codex:gpt-5.4</code>" in rendered
    assert rendered.count("<b>mode:</b> <code>agent</code>") == 1
    assert rendered.count("<b>model:</b> <code>codex:gpt-5.4</code>") == 1
    assert "/models" in rendered


def test_truncate_for_telegram_handles_small_limits():
    assert truncate_for_telegram("hello", limit=0) == ""
    assert truncate_for_telegram("hello", limit=2) == "he"


def test_render_plan_update_handles_empty_plans():
    plan = update_plan([])

    rendered = render_plan_update(plan)

    assert "<b>Current plan</b>" in rendered
    assert "<b>status:</b> <code>empty</code>" in rendered


def test_render_approval_resolution_formats_metadata():
    rendered = render_approval_resolution(
        tool_title="write file",
        selected_option="Allow Once",
        state="completed",
    )

    assert "Approval resolved" in rendered
    assert "allow once" in rendered
    assert "completed" in rendered


def test_render_tool_update_falls_back_when_no_structured_projection_exists():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-3",
            "title": "generic tool",
            "status": "completed",
        }
    )

    rendered = render_tool_update(update)

    assert "No structured projection was provided." in rendered


def test_render_tool_update_uses_raw_input_command_preview():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-4",
            "title": "run command",
            "status": "completed",
            "rawInput": {
                "command": "python",
                "args": ["-m", "http.server"],
                "cwd": "/workspace/app",
            },
        }
    )

    rendered = render_tool_update(update)

    assert '<pre language="bash">' in rendered
    assert "# command" in rendered
    assert "cwd: /workspace/app" in rendered
    assert "python -m http.server" in rendered


def test_render_tool_update_uses_parsed_read_target_from_raw_input() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5a",
            "title": "Read CHANGELOG",
            "status": "in_progress",
            "rawInput": {"parsed_cmd": [{"Read": {"name": "CHANGELOG", "path": "CHANGELOG"}}]},
        }
    )

    rendered = render_tool_update(update)

    assert "Requested file" in rendered
    assert "CHANGELOG" in rendered


def test_render_tool_update_renders_read_content_from_raw_output() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5b",
            "title": "Read CHANGELOG",
            "status": "completed",
            "rawInput": {"parsed_cmd": [{"Read": {"name": "CHANGELOG", "path": "CHANGELOG"}}]},
            "rawOutput": {"aggregated_output": "0.8.3\n- main additions"},
        }
    )

    rendered = render_tool_update(update)

    assert "Content" in rendered
    assert "0.8.3" in rendered


def test_render_tool_update_uses_type_style_parsed_read_target() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5d",
            "title": "generic tool",
            "status": "in_progress",
            "rawInput": {"parsed_cmd": [{"type": "read", "path": "README.md"}]},
        }
    )

    rendered = render_tool_update(update)

    assert "Requested file" in rendered
    assert "README.md" in rendered


def test_render_tool_update_prefers_formatted_output_for_reads() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5e",
            "title": "Read README.md",
            "status": "completed",
            "rawInput": {"parsed_cmd": [{"type": "read", "path": "README.md"}]},
            "rawOutput": {"formatted_output": "```\\n# readme\\n```"},
        }
    )

    rendered = render_tool_update(update)

    assert "Content" in rendered
    assert "# readme" in rendered


def test_render_tool_update_labels_string_raw_output_for_reads_as_content() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5f",
            "title": "Read README.md",
            "status": "completed",
            "rawOutput": "plain output",
        }
    )

    rendered = render_tool_update(update)

    assert "Content" in rendered
    assert "plain output" in rendered


def test_render_approval_preview_uses_parsed_read_target_from_raw_input() -> None:
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5c",
            "title": "Read CHANGELOG",
            "status": "in_progress",
            "rawInput": {"parsed_cmd": [{"Read": {"name": "CHANGELOG", "path": "CHANGELOG"}}]},
        }
    )

    rendered = render_approval_preview(update)

    assert "Requested file" in rendered


def test_render_tool_update_renders_raw_output_and_approval_metadata():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-9",
            "title": "run command",
            "status": "completed",
            "rawInput": {"command": "pytest"},
            "rawOutput": {
                "returncode": 1,
                "stdout": "line one\nline two",
                "stderr": "boom",
            },
        }
    )

    rendered = render_tool_update(update, approval_label="Allow Once")

    assert "<b>approval:</b> <code>allow once</code>" in rendered
    assert "exit: 1" in rendered
    assert "# stdout" in rendered
    assert "line one" in rendered
    assert "# stderr" in rendered
    assert "boom" in rendered


def test_render_tool_update_covers_empty_and_fallback_raw_payloads() -> None:
    assert "No structured projection" in render_tool_update(
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-empty-input",
                "title": "generic tool",
                "status": "completed",
                "rawInput": {"unknown": "value"},
            }
        )
    )
    assert "No structured projection" in render_tool_update(
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-empty-output",
                "title": "generic tool",
                "status": "completed",
                "rawOutput": "   ",
            }
        )
    )
    rendered_signal = render_tool_update(
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-signal",
                "title": "run command",
                "status": "completed",
                "rawOutput": {"signal": "SIGTERM"},
            }
        )
    )
    rendered_output = render_tool_update(
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-output",
                "title": "run command",
                "status": "completed",
                "rawOutput": {"output": "fallback text"},
            }
        )
    )
    assert "signal: SIGTERM" in rendered_signal
    assert "# output" in rendered_output
    assert "No structured projection" in render_tool_update(
        ToolCallUpdate.model_validate(
            {
                "toolCallId": "tool-empty-dict",
                "title": "generic tool",
                "status": "completed",
                "rawOutput": {},
            }
        )
    )


def test_read_target_helpers_cover_invalid_shapes() -> None:
    assert _extract_read_target_from_raw_input({"parsed_cmd": ["bad", {"x": "y"}]}) is None
    assert _extract_read_target_from_parsed_command({"type": "write", "path": "README.md"}) is None
    assert _extract_read_target_from_parsed_command({"Write": {"path": "README.md"}}) is None
    assert _extract_read_target_from_parsed_command({"Read": {}}) is None
    assert (
        _is_read_tool_update(
            ToolCallUpdate.model_validate(
                {
                    "toolCallId": "tool-not-read",
                    "title": "generic tool",
                    "status": "completed",
                    "rawInput": "bad",
                }
            )
        )
        is False
    )
    assert (
        _is_read_tool_update(
            ToolCallUpdate.model_validate(
                {
                    "toolCallId": "tool-not-read-dict",
                    "title": "generic tool",
                    "status": "completed",
                    "rawInput": {"parsed_cmd": [{"Write": {"path": "README.md"}}]},
                }
            )
        )
        is False
    )


def test_selection_value_helpers_cover_boolean_grouped_and_unmatched_values() -> None:
    boolean = SessionConfigOptionBoolean.model_validate(
        {"id": "web", "name": "Web", "type": "boolean", "currentValue": True}
    )
    unmatched = SessionConfigOptionSelect.model_validate(
        {
            "id": "effort",
            "name": "Effort",
            "type": "select",
            "currentValue": "xhigh",
            "options": [{"name": "High", "value": "high"}],
        }
    )
    grouped = SessionConfigOptionSelect.model_validate(
        {
            "id": "model",
            "name": "Model",
            "type": "select",
            "currentValue": "gpt",
            "options": [
                {
                    "group": "openai",
                    "name": "OpenAI",
                    "options": [{"name": "GPT", "value": "gpt"}],
                }
            ],
        }
    )
    assert _display_current_value(boolean) == "True"
    grouped.current_value = None
    assert _display_current_value(grouped) == "None"
    assert _display_current_value(unmatched) == "xhigh"
    assert [item.value for item in _flatten_select_options(grouped.options)] == ["gpt"]


def test_block_composition_and_truncation_cover_tight_limits() -> None:
    prefix = "x" * TELEGRAM_TEXT_LIMIT
    assert _compose_with_blocks(prefix, [_RenderedBlock("body")]) == prefix
    assert _truncate_block_body("abcdefghijklmnopqrstuvwxyz", available=20).endswith("[truncated]")


def test_should_project_tool_update_returns_false_for_generic_tools():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-5",
            "title": "generic tool",
            "status": "completed",
        }
    )

    assert should_project_tool_update(update) is False


def test_should_project_tool_update_uses_content_markers_without_title_hints():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-6",
            "title": "generic tool",
            "status": "completed",
            "content": [{"type": "terminal", "terminalId": "term-1"}],
        }
    )

    assert should_project_tool_update(update) is True


def test_render_tool_update_renders_terminal_and_text_content_blocks():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-7",
            "title": "generic tool",
            "status": "failed",
            "content": [
                {"type": "terminal", "terminalId": "term-1"},
                {"type": "content", "content": {"type": "text", "text": " finished "}},
            ],
        }
    )

    rendered = render_tool_update(update)

    assert "term-1" in rendered
    assert "finished" in rendered
    assert "<b>Content:</b>" in rendered
    assert "failed" in rendered


def test_render_tool_update_handles_no_visible_diff_changes():
    update = ToolCallUpdate.model_validate(
        {
            "toolCallId": "tool-8",
            "title": "write file",
            "status": "failed",
            "content": [
                {
                    "type": "diff",
                    "path": "README.md",
                    "oldText": "same",
                    "newText": "same",
                }
            ],
        }
    )

    rendered = render_tool_update(update)

    assert "(no visible changes)" in rendered
    assert "failed" in rendered


def test_private_projection_helpers_cover_suffix_and_truncation_edges():
    assert _truncate_block_body("abcdef", available=1) == "a"
    assert _normalize_status("cancelled") == "cancelled"
    assert _normalize_status("failed") == "failed"
    assert _card_prefix_lines(title="Card", caution="careful") == [
        "<b>CAUTION:</b> careful",
        "<b>Card</b>",
    ]
    assert _compose_with_blocks("prefix", [_RenderedBlock("body")], suffix="tail").endswith("tail")


def test_truncate_for_telegram_uses_marker_when_space_allows() -> None:
    text = "x" * (TELEGRAM_TEXT_LIMIT + 20)

    rendered = truncate_for_telegram(text)

    assert rendered.endswith("... [truncated]")
