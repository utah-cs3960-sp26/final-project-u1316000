from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PROMPT_SENTINEL = "Enter response. Finish with a line containing only END."
EMPTY_STEP_RESPONSE_RETRY_LIMIT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the story worker in human mode and have Codex fill each step."
    )
    parser.add_argument("--codex-command", default="codex.cmd", help="Codex CLI command to execute.")
    parser.add_argument("--codex-model", help="Optional model to pass through to Codex CLI.")
    parser.add_argument(
        "--worker-model",
        default="human-wrapper",
        help="Placeholder model value to satisfy run_story_worker_local while using --author-mode human.",
    )
    return parser


def strip_markdown_fences(raw_text: str) -> str:
    stripped = raw_text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def build_codex_step_prompt(worker_prompt: str) -> str:
    return (
        "You are filling a human-author form for a story worker.\n"
        "The worker is already orchestrating the run, validation, retries, DB writes, and assets.\n"
        "Your job is only to answer the current step.\n"
        "This is NOT JSON. Use the worker's simple newline-based field/input format exactly as shown.\n"
        "Return only the exact fields and corresponding values requested by the worker prompt.\n"
        "Do not add commentary, explanations, markdown fences, or any extra fields.\n"
        "If the worker prompt says the step may be skipped and skipping is the best response, return exactly END.\n"
        "Keep continuity across this run. Prior accepted steps shown in the worker prompt are real.\n\n"
        "Worker prompt:\n\n"
        f"{worker_prompt}"
    )


def extract_thread_id_from_jsonl(stdout_text: str) -> str | None:
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started" and payload.get("thread_id"):
            return str(payload["thread_id"])
    return None


def extract_last_agent_message_from_jsonl(stdout_text: str) -> str | None:
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "item.completed":
            continue
        item = payload.get("item") or {}
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None


def run_codex_step(
    *,
    codex_command: str,
    codex_model: str | None,
    project_root: Path,
    prompt_text: str,
    thread_id: str | None,
) -> tuple[str, str | None]:
    with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".txt", encoding="utf-8") as handle:
        output_last_message_path = Path(handle.name)

    try:
        if thread_id is None:
            command = [codex_command, "exec", "--json", "--output-last-message", str(output_last_message_path), "-"]
            if codex_model:
                command.extend(["--model", codex_model])
            command.extend(["--sandbox", "read-only"])
        else:
            command = [
                codex_command,
                "exec",
                "resume",
                thread_id,
                "--json",
                "--output-last-message",
                str(output_last_message_path),
                "-",
            ]
            if codex_model:
                command.extend(["--model", codex_model])

        completed = subprocess.run(
            command,
            input=build_codex_step_prompt(prompt_text),
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            stdout = completed.stdout.strip()
            detail = stderr or stdout or f"Codex command failed with exit code {completed.returncode}"
            raise RuntimeError(detail)

        next_thread_id = thread_id or extract_thread_id_from_jsonl(completed.stdout)
        response_text = output_last_message_path.read_text(encoding="utf-8").strip()
        if not response_text:
            response_text = extract_last_agent_message_from_jsonl(completed.stdout) or ""
        response_text = strip_markdown_fences(response_text)
        return response_text, next_thread_id
    finally:
        output_last_message_path.unlink(missing_ok=True)


def request_nonempty_codex_step(
    *,
    codex_command: str,
    codex_model: str | None,
    project_root: Path,
    prompt_text: str,
    thread_id: str | None,
    empty_retry_limit: int = EMPTY_STEP_RESPONSE_RETRY_LIMIT,
) -> tuple[str, str | None]:
    current_thread_id = thread_id
    response_text = ""
    for _ in range(max(empty_retry_limit, 1)):
        response_text, current_thread_id = run_codex_step(
            codex_command=codex_command,
            codex_model=codex_model,
            project_root=project_root,
            prompt_text=prompt_text,
            thread_id=current_thread_id,
        )
        if response_text.strip():
            return response_text, current_thread_id
    return "", current_thread_id


def build_worker_command(*, worker_model: str, passthrough_args: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "app.tools.run_story_worker_local",
        "--author-mode",
        "human",
        "--model",
        worker_model,
        *passthrough_args,
    ]


def main() -> None:
    parser = build_parser()
    args, passthrough_args = parser.parse_known_args()
    project_root = Path(__file__).resolve().parents[2]

    worker = subprocess.Popen(
        build_worker_command(worker_model=args.worker_model, passthrough_args=passthrough_args),
        cwd=project_root,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    if worker.stdin is None or worker.stdout is None:
        raise RuntimeError("Failed to open worker subprocess pipes.")

    prompt_buffer: list[str] = []
    codex_thread_id: str | None = None
    last_prompt_block = ""
    last_response_text = ""
    shutdown_reason: str | None = None

    try:
        while True:
            line = worker.stdout.readline()
            if not line:
                break

            print(line, end="")
            prompt_buffer.append(line)

            if PROMPT_SENTINEL not in line:
                continue

            prompt_block = "".join(prompt_buffer)
            prompt_buffer = []
            last_prompt_block = prompt_block
            try:
                response_text, codex_thread_id = request_nonempty_codex_step(
                    codex_command=args.codex_command,
                    codex_model=args.codex_model,
                    project_root=project_root,
                    prompt_text=prompt_block,
                    thread_id=codex_thread_id,
                )
            except Exception as exc:
                print(
                    f"[codex-human-wrapper] Codex step failed; submitting a blank response so the worker can retry: {exc}",
                    file=sys.stderr,
                )
                response_text = ""
            last_response_text = response_text

            try:
                if response_text.strip():
                    worker.stdin.write(response_text + "\n")
                worker.stdin.write("END\n")
                worker.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                shutdown_reason = f"Worker stdin closed unexpectedly while sending a step response: {exc}"
                break
    finally:
        if worker.stdin and not worker.stdin.closed:
            try:
                worker.stdin.close()
            except OSError:
                pass

    return_code = worker.wait()
    if shutdown_reason is not None:
        raise RuntimeError(
            f"{shutdown_reason}\n\n"
            f"Last worker prompt block:\n{last_prompt_block}\n\n"
            f"Last Codex response:\n{last_response_text}"
        )
    if return_code != 0:
        raise RuntimeError(
            f"Human-wrapper worker exited unexpectedly with code {return_code}.\n\n"
            f"Last worker prompt block:\n{last_prompt_block}\n\n"
            f"Last Codex response:\n{last_response_text}"
        )


if __name__ == "__main__":
    main()
