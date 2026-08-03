"""TDD — chuẩn hoá lệnh chẩn đoán ở BIÊN, không tin hình dạng output của LLM.

Ba lỗi thật, session `omni:diag:session:ra-da66cac8746b` (2026-08-02):

1. `ps` nhận ``args=["aux --sort=-%cpu"]`` — cả chuỗi nhồi vào MỘT phần tử ⇒
   ``error: unsupported option (BSD syntax)``, rc=1, không đo được gì.
2. `top` gọi không cờ ⇒ chế độ tương tác, không tty ⇒ rc=1, stdout rỗng.
3. `exit_code` vắng mặt trong kết quả lưu (đọc ra `None`) trong khi thẻ Telegram
   hiển thị `rc=1` — hai tên cho cùng một thứ, hai đầu không khớp.

Chuẩn hoá phải KHÔNG được nới lỏng guard: nó chạy TRƯỚC `validate_command`, nên
mọi token sau khi tách vẫn bị quét metachar/WRITE_VERBS như cũ.
"""
from __future__ import annotations

from pkg.diagnostics.command_normalize import normalize_command


class TestPackedArgs:
    def test_ps_packed_bsd_syntax_is_split(self) -> None:
        """CA GỐC: `ps` với cả chuỗi trong args[0]."""
        cmd, args = normalize_command("ps", ["aux --sort=-%cpu"])
        assert cmd == "ps"
        assert args == ["aux", "--sort=-%cpu"]

    def test_journalctl_packed_flags_are_split(self) -> None:
        cmd, args = normalize_command("journalctl", ["-u aoip-agent.service -n 50"])
        assert args == ["-u", "aoip-agent.service", "-n", "50"]

    def test_command_field_carrying_args_is_split(self) -> None:
        cmd, args = normalize_command("systemctl is-failed", [])
        assert (cmd, args) == ("systemctl", ["is-failed"])

    def test_value_with_spaces_and_no_flag_token_is_left_alone(self) -> None:
        """`--since "1 hour ago"`: giá trị có khoảng trắng nhưng KHÔNG có token cờ
        ⇒ tách là phá lệnh đúng."""
        _, args = normalize_command("journalctl", ["--since", "1 hour ago"])
        assert args == ["--since", "1 hour ago"]

    def test_awk_script_is_never_split(self) -> None:
        _, args = normalize_command("awk", ["{print $1} -x"])
        assert args == ["{print $1} -x"]

    def test_sql_statement_value_is_never_split(self) -> None:
        _, args = normalize_command("mysql", ["-e", "SHOW STATUS -x"])
        assert args == ["-e", "SHOW STATUS -x"]

    def test_already_correct_args_untouched(self) -> None:
        _, args = normalize_command("df", ["-h", "/var"])
        assert args == ["-h", "/var"]

    def test_metachar_survives_normalization_for_the_validator(self) -> None:
        """Tách không được nuốt metachar — validator phải vẫn thấy nó."""
        _, args = normalize_command("ps", ["aux -o pid;rm"])
        assert any(";" in a for a in args)


class TestTopBatchMode:
    def test_bare_top_gets_batch_flags(self) -> None:
        """CA GỐC: `top` không cờ ⇒ fail ngoài tty."""
        cmd, args = normalize_command("top", [])
        assert cmd == "top"
        assert "-b" in args
        assert "-n" in args and args[args.index("-n") + 1] == "1"

    def test_existing_batch_flags_preserved(self) -> None:
        _, args = normalize_command("top", ["-b", "-n", "3"])
        assert args == ["-b", "-n", "3"]

    def test_iteration_count_kept_when_only_b_given(self) -> None:
        _, args = normalize_command("top", ["-b"])
        assert args[:1] == ["-b"]
        assert "-n" in args

    def test_packed_top_args_normalized_then_batched(self) -> None:
        _, args = normalize_command("top", ["-o %CPU"])
        assert "-b" in args and "-n" in args and "-o" in args

    def test_other_commands_get_no_extra_flags(self) -> None:
        _, args = normalize_command("free", [])
        assert args == []


class TestNormalizedCommandStillValidates:
    def test_split_ps_passes_the_shared_validator(self) -> None:
        from pkg.diagnostics.validator import validate_command

        cmd, args = normalize_command("ps", ["aux --sort=-%cpu"])
        ok, reason = validate_command(cmd, args)
        assert ok, reason

    def test_split_top_passes_the_shared_validator(self) -> None:
        from pkg.diagnostics.validator import validate_command

        cmd, args = normalize_command("top", [])
        ok, reason = validate_command(cmd, args)
        assert ok, reason

    def test_env_dump_still_blocked_after_split(self) -> None:
        from pkg.diagnostics.validator import validate_command

        cmd, args = normalize_command("ps", ["auxe"])
        ok, _ = validate_command(cmd, args)
        assert not ok

    def test_write_verb_still_blocked_after_split(self) -> None:
        from pkg.diagnostics.validator import validate_command

        cmd, args = normalize_command("systemctl", ["restart nginx"])
        ok, _ = validate_command(cmd, args)
        assert not ok


class TestExitCodeAlias:
    async def test_executor_result_carries_both_rc_and_exit_code(self) -> None:
        """`rc` và `exit_code` phải luôn khớp — không được để một đầu đọc ra None."""
        from remote_agent.command_executor import execute_command

        res = await execute_command("cmd-test-1", "uptime", [])
        assert res["rc"] == res["exit_code"]

    async def test_blocked_result_also_carries_exit_code(self) -> None:
        from remote_agent.command_executor import execute_command

        res = await execute_command("cmd-test-2", "rm", ["-rf", "/"])
        assert res["blocked"] is True
        assert res["exit_code"] == res["rc"]

    async def test_executor_normalizes_packed_ps_args(self) -> None:
        """Hàng rào cuối ở phía agent: bundle cũ vẫn được cứu bởi lớp này."""
        from remote_agent.command_executor import execute_command

        res = await execute_command("cmd-test-3", "ps", ["aux --sort=-%cpu"])
        assert res.get("blocked") is not True
        assert "BSD syntax" not in (res.get("stderr") or "")
