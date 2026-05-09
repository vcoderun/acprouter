from __future__ import annotations as _annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from html import escape

from acp.schema import (
    AgentPlanUpdate,
    AvailableCommand,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    SessionConfigSelectOption,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
)

__all__ = (
    "PlanChecklist",
    "PlanChecklistTask",
    "TELEGRAM_TEXT_LIMIT",
    "append_text_chunk",
    "build_plan_checklist",
    "render_approval_preview",
    "render_approval_resolution",
    "render_plan_update",
    "render_selection_surface",
    "render_tool_update",
    "should_project_tool_update",
    "truncate_for_telegram",
)

TELEGRAM_TEXT_LIMIT = 4096
_TRUNCATION_MARKER = "\n... [truncated]"


@dataclass(slots=True, frozen=True)
class _RenderedBlock:
    body: str
    title: str | None = None
    language: str | None = None


@dataclass(slots=True, frozen=True)
class PlanChecklistTask:
    key: str
    text: str
    is_done: bool


@dataclass(slots=True, frozen=True)
class PlanChecklist:
    title: str
    tasks: list[PlanChecklistTask]


def append_text_chunk(current_text: str, chunk: str) -> str:
    updated = f"{current_text}{chunk}"
    return updated[-3500:]


def truncate_for_telegram(text: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    if limit <= len(_TRUNCATION_MARKER):
        return text[:limit]
    return f"{text[: limit - len(_TRUNCATION_MARKER)]}{_TRUNCATION_MARKER}"


def render_approval_preview(
    update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
) -> str:
    title = update.title or "Tool update"
    metadata = [("tool", title), ("status", _normalize_status(update.status or "in_progress"))]
    blocks = _render_tool_contents(update.content or [])
    if not blocks:
        raw_input_block = _render_tool_raw_input(update)
        if raw_input_block is not None:
            blocks = [raw_input_block]
    return _render_blocks_card(
        title="Approval required",
        metadata=metadata,
        blocks=blocks or [_RenderedBlock("No structured preview available.")],
        caution=None,
    )


def render_tool_update(
    update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
    *,
    approval_label: str | None = None,
) -> str:
    title = update.title or "Tool update"
    metadata = [("tool", title), ("status", _normalize_status(update.status or "in_progress"))]
    if isinstance(approval_label, str) and approval_label.strip() != "":
        metadata.append(("approval", approval_label.lower()))
    blocks = _render_tool_contents(update.content or [])
    if not blocks:
        raw_input_block = _render_tool_raw_input(update)
        if raw_input_block is not None:
            blocks = [raw_input_block]
    raw_output_block = _render_tool_raw_output(update)
    if raw_output_block is not None:
        blocks.append(raw_output_block)
    return _render_blocks_card(
        title="Tool update",
        metadata=metadata,
        blocks=blocks or [_RenderedBlock("No structured projection was provided.")],
        caution=None,
    )


def render_approval_resolution(
    *,
    tool_title: str,
    selected_option: str,
    state: str = "waiting for execution result",
) -> str:
    return _render_plain_card(
        title="Approval resolved",
        metadata=(
            ("decision", selected_option.lower()),
            ("tool", tool_title),
            ("state", state),
        ),
    )


def render_plan_update(update: AgentPlanUpdate) -> str:
    if not update.entries:
        return _render_plain_card(title="Current plan", metadata=(("status", "empty"),))
    lines: list[str] = []
    for entry in update.entries:
        lines.append(f"- [{entry.status}] {entry.content}")
    return _render_single_block_card(
        title="Current plan",
        metadata=(),
        block_body="\n".join(lines),
    )


def build_plan_checklist(update: AgentPlanUpdate) -> PlanChecklist:
    seen_contents: dict[str, int] = {}
    tasks: list[PlanChecklistTask] = []
    for entry in update.entries:
        occurrence = seen_contents.get(entry.content, 0)
        seen_contents[entry.content] = occurrence + 1
        tasks.append(
            PlanChecklistTask(
                key=f"{entry.content}\x00{occurrence}",
                text=_format_plan_task_text(content=entry.content, status=entry.status),
                is_done=entry.status == "completed",
            )
        )
    return PlanChecklist(title="Current plan", tasks=tasks)


def render_selection_surface(
    *,
    current_mode_id: str | None,
    current_model_id: str | None,
    selection_options: list[SessionConfigOptionBoolean | SessionConfigOptionSelect],
    commands: list[AvailableCommand] | None = None,
) -> str:
    metadata: list[tuple[str, str]] = []
    if current_mode_id is not None:
        metadata.append(("mode", current_mode_id))
    if current_model_id is not None:
        metadata.append(("model", current_model_id))

    lines: list[str] = []
    for option in selection_options:
        if option.id == "mode" and current_mode_id is not None:
            continue
        if option.id == "model" and current_model_id is not None:
            continue
        current_value = _display_current_value(option)
        lines.append(f"{option.id}: {current_value}")
    if commands:
        lines.append("")
        lines.append("Commands:")
        for command in commands[:12]:
            if command.input is not None:
                lines.append(f"/{command.name} {command.input.root.hint}")
            else:
                lines.append(f"/{command.name}")
    body = (
        "\n".join(line for line in lines if line is not None).strip()
        or "No selection state available."
    )
    return _render_single_block_card(
        title="Session selections",
        metadata=metadata,
        block_body=body,
        caution=None,
    )


def should_project_tool_update(update: ToolCallUpdate | ToolCallStart | ToolCallProgress) -> bool:
    lowered_title = (update.title or "").lower()
    if any(
        marker in lowered_title
        for marker in ("read", "write", "file", "terminal", "command", "shell", "exec")
    ):
        return True
    for content in update.content or []:
        kind = getattr(content, "type", None)
        if kind in {"diff", "terminal"}:
            return True
    return False


def _render_tool_contents(contents: Iterable[object]) -> list[_RenderedBlock]:
    snippets: list[_RenderedBlock] = []
    for content in contents:
        kind = getattr(content, "type", None)
        if kind == "diff":
            path = str(getattr(content, "path", "unknown"))
            old_text = getattr(content, "old_text", None) or ""
            new_text = getattr(content, "new_text", "")
            snippets.append(
                _RenderedBlock(
                    _render_diff(path, old_text, new_text),
                    title="Diff:",
                    language="diff",
                )
            )
        elif kind == "terminal":
            terminal_id = getattr(content, "terminal_id", "unknown")
            snippets.append(_RenderedBlock(f"# terminal\n{terminal_id}"))
        elif kind == "content":
            block = getattr(content, "content", None)
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "").strip()
                if text:
                    snippets.append(_RenderedBlock(text, title="Content:"))
    return snippets


def _render_diff(path: str, old_text: str, new_text: str) -> str:
    diff_lines = list(
        unified_diff(
            old_text.strip().splitlines(),
            new_text.strip().splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    if not diff_lines:
        diff_lines = ["(no visible changes)"]
    return "\n".join(diff_lines[:40])


def _render_tool_raw_input(
    update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
) -> _RenderedBlock | None:
    raw_input = getattr(update, "raw_input", None)
    if not isinstance(raw_input, dict):
        return None
    parsed_read_target = _extract_read_target_from_raw_input(raw_input)
    if parsed_read_target is not None:
        return _RenderedBlock(parsed_read_target, title="Requested file")
    path = raw_input.get("path")
    content = raw_input.get("content")
    if isinstance(path, str) and path.strip() != "":
        lines = [f"# {path.strip()}"]
        if isinstance(content, str) and content.strip() != "":
            preview_lines = content.strip().splitlines()[:20]
            lines.extend(("", *preview_lines))
        return _RenderedBlock("\n".join(lines), title="Content:")
    command = raw_input.get("command")
    if isinstance(command, str) and command.strip() != "":
        lines = ["# command"]
        cwd = raw_input.get("cwd")
        if isinstance(cwd, str) and cwd.strip() != "":
            lines.extend((f"cwd: {cwd.strip()}", ""))
        args = raw_input.get("args")
        if isinstance(args, Sequence) and not isinstance(args, str):
            rendered_args = " ".join(str(arg).strip() for arg in args if str(arg).strip() != "")
            if rendered_args != "":
                command = f"{command.strip()} {rendered_args}".strip()
        lines.append(command.strip())
        return _RenderedBlock("\n".join(lines), language="bash")
    return None


def _render_tool_raw_output(
    update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
) -> _RenderedBlock | None:
    raw_output = getattr(update, "raw_output", None)
    if isinstance(raw_output, str):
        stripped = raw_output.strip()
        if stripped != "":
            title = "Content" if _is_read_tool_update(update) else None
            return _RenderedBlock(stripped, title=title)
        return None
    if not isinstance(raw_output, dict):
        return None

    formatted_output = raw_output.get("formatted_output")
    if isinstance(formatted_output, str) and formatted_output.strip() != "":
        title = "Content" if _is_read_tool_update(update) else "Result"
        return _RenderedBlock(formatted_output.strip(), title=title)
    aggregated_output = raw_output.get("aggregated_output")
    if isinstance(aggregated_output, str) and aggregated_output.strip() != "":
        title = "Content" if _is_read_tool_update(update) else "Result"
        return _RenderedBlock(aggregated_output.strip(), title=title)

    lines: list[str] = []
    signal = raw_output.get("signal")
    if isinstance(signal, str) and signal.strip() != "":
        lines.append(f"signal: {signal.strip()}")
    exit_code = raw_output.get("exit_code", raw_output.get("returncode"))
    if isinstance(exit_code, int):
        lines.append(f"exit: {exit_code}")
    stdout = raw_output.get("stdout")
    if isinstance(stdout, str) and stdout.strip() != "":
        lines.extend(("", "# stdout", *stdout.strip().splitlines()[:20]))
    stderr = raw_output.get("stderr")
    if isinstance(stderr, str) and stderr.strip() != "":
        lines.extend(("", "# stderr", *stderr.strip().splitlines()[:20]))
    output = raw_output.get("output")
    if not lines and isinstance(output, str) and output.strip() != "":
        lines.extend(("# output", *output.strip().splitlines()[:20]))
    rendered = "\n".join(lines).strip()
    if rendered == "":
        return None
    return _RenderedBlock(rendered)


def _extract_read_target_from_raw_input(raw_input: dict[str, object]) -> str | None:
    parsed_commands = raw_input.get("parsed_cmd")
    if not isinstance(parsed_commands, Sequence) or isinstance(parsed_commands, str):
        return None
    for item in parsed_commands:
        if not isinstance(item, dict):
            continue
        normalized_item = {str(key): value for key, value in item.items()}
        read_target = _extract_read_target_from_parsed_command(normalized_item)
        if read_target is not None:
            return read_target
    return None


def _extract_read_target_from_parsed_command(item: dict[str, object]) -> str | None:
    if "type" in item and str(item.get("type")).lower() == "read":
        path = item.get("path") or item.get("name")
        if isinstance(path, str) and path.strip() != "":
            return path.strip()
    if len(item) != 1:
        return None
    variant, payload = next(iter(item.items()))
    if variant.lower() != "read" or not isinstance(payload, dict):
        return None
    normalized_payload = {str(key): value for key, value in payload.items()}
    path = normalized_payload.get("path") or normalized_payload.get("name")
    if isinstance(path, str) and path.strip() != "":
        return path.strip()
    return None


def _is_read_tool_update(update: ToolCallUpdate | ToolCallStart | ToolCallProgress) -> bool:
    title = (update.title or "").strip().lower()
    if title.startswith("read "):
        return True
    raw_input = getattr(update, "raw_input", None)
    if not isinstance(raw_input, dict):
        return False
    return _extract_read_target_from_raw_input(raw_input) is not None


def _format_plan_task_text(*, content: str, status: str) -> str:
    normalized_status = status.replace("_", " ")
    if status in {"pending", "completed"}:
        return content
    return f"[{normalized_status}] {content}"


def _display_current_value(option: SessionConfigOptionBoolean | SessionConfigOptionSelect) -> str:
    current_value = getattr(option, "current_value", None)
    if not isinstance(option, SessionConfigOptionSelect):
        return str(current_value)
    if not isinstance(current_value, str):
        return str(current_value)
    for item in _flatten_select_options(option.options):
        if item.value == current_value:
            return item.name
    return current_value


def _flatten_select_options(
    options: Sequence[SessionConfigSelectOption | SessionConfigSelectGroup],
) -> list[SessionConfigSelectOption]:
    flattened: list[SessionConfigSelectOption] = []
    for item in options:
        if isinstance(item, SessionConfigSelectOption):
            flattened.append(item)
            continue
        flattened.extend(item.options)
    return flattened


def _render_plain_card(
    *,
    title: str,
    metadata: Iterable[tuple[str, str]],
) -> str:
    lines = _card_prefix_lines(title=title, caution=None)
    for label, value in metadata:
        lines.append(_metadata_line(label, value))
    return truncate_for_telegram("\n".join(lines))


def _render_single_block_card(
    *,
    title: str,
    metadata: Iterable[tuple[str, str]],
    block_body: str,
    caution: str | None = None,
) -> str:
    return _render_blocks_card(
        title=title,
        metadata=metadata,
        blocks=[_RenderedBlock(block_body)],
        caution=caution,
    )


def _render_blocks_card(
    *,
    title: str,
    metadata: Iterable[tuple[str, str]],
    blocks: list[_RenderedBlock],
    caution: str | None = None,
) -> str:
    prefix_lines = _card_prefix_lines(title=title, caution=caution)
    for label, value in metadata:
        prefix_lines.append(_metadata_line(label, value))
    prefix = "\n".join(prefix_lines)
    return _compose_with_blocks(prefix, blocks)


def _compose_with_blocks(
    prefix: str,
    blocks: list[_RenderedBlock],
    *,
    suffix: str | None = None,
) -> str:
    rendered = prefix
    for block in blocks:
        separator = "\n\n"
        title = f"<b>{escape(block.title)}</b>\n" if block.title is not None else ""
        open_tag = _open_pre_tag(block.language)
        close_tag = "</pre>"
        overhead = len(separator) + len(title) + len(open_tag) + len(close_tag)
        available = TELEGRAM_TEXT_LIMIT - len(rendered) - overhead
        if available <= 0:
            break
        body = _truncate_block_body(block.body, available=available)
        wrapped = f"{separator}{title}{open_tag}{body}{close_tag}"
        rendered = f"{rendered}{wrapped}"
    if suffix is not None:
        rendered = truncate_for_telegram(f"{rendered}\n\n{escape(suffix.strip())}")
    return truncate_for_telegram(rendered)


def _open_pre_tag(language: str | None) -> str:
    if language is None or language.strip() == "":
        return "<pre>"
    return f'<pre language="{escape(language.strip())}">'


def _truncate_block_body(body: str, *, available: int) -> str:
    normalized = body.strip()
    escaped_body = escape(normalized)
    if len(escaped_body) <= available:
        return escaped_body
    marker = escape(_TRUNCATION_MARKER)
    if available <= len(marker):
        return escaped_body[:available]
    return f"{escaped_body[: available - len(marker)]}{marker}"


def _metadata_line(label: str, value: str) -> str:
    return f"<b>{escape(label)}:</b> <code>{escape(value.strip())}</code>"


def _card_prefix_lines(*, title: str, caution: str | None) -> list[str]:
    lines: list[str] = []
    if caution is not None:
        lines.append(f"<b>CAUTION:</b> {escape(caution.strip())}")
    lines.append(f"<b>{escape(title)}</b>")
    return lines


def _normalize_status(status: str) -> str:
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    if status == "failed":
        return "failed"
    return "in progress"
