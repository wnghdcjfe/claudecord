import asyncio
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from src.auth import is_authorized
from src.greetings import direct_reply_for
from src.orchestrator import create_job, run_job
from src.outputs import send_outputs
from src.parser import parse
from src.sessions import clear_session, get_session_state, set_session
from src.status import (
    format_working_status,
    make_working_gif_file,
    run_spinning_loader,
    stop_spinning_loader,
)

MISSING_CONVERSATION_MARKER = "No conversation found with session ID"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _is_missing_conversation_error(meta: dict) -> bool:
    if not (meta.get("type") == "error" or meta.get("is_error")):
        return False
    return MISSING_CONVERSATION_MARKER in str(meta.get("text") or "")


async def _run_job_with_session_recovery(
    job_dir: Path,
    *,
    resume_id: str | None,
    channel_id: int,
    explicit_session: bool,
) -> dict:
    meta = await run_job(job_dir, resume=resume_id)

    if resume_id and not explicit_session and _is_missing_conversation_error(meta):
        clear_session(channel_id)
        meta = await run_job(job_dir, resume=None)
        meta["retried_without_stale_session"] = True
        meta["stale_session_id"] = resume_id

    return meta


def _resolve_resume_and_workdir(cmd, channel_id: int) -> tuple[str | None, str | None, bool]:
    explicit_session = cmd.session_id is not None
    if explicit_session:
        return cmd.session_id, cmd.workdir, True

    session_state = get_session_state(channel_id)
    resume_id = session_state.session_id if session_state else None
    workdir = cmd.workdir or (session_state.workdir if session_state else None)
    return resume_id, workdir, False


@client.event
async def on_ready():
    print(f"[bot] logged in as {client.user}")


@client.event
async def on_message(msg: discord.Message):
    if not is_authorized(msg):
        return
    if not msg.clean_content.strip():
        return

    text = msg.clean_content.strip()

    if text == "/clear":
        clear_session(msg.channel.id)
        await msg.reply("세션을 초기화했습니다. 다음 메시지부터 새 대화로 시작합니다.")
        return

    direct_reply = direct_reply_for(text)
    if direct_reply:
        await msg.reply(direct_reply)
        return

    cmd = parse(text)
    resume_id, workdir, explicit_session = _resolve_resume_and_workdir(cmd, msg.channel.id)
    try:
        job_dir = create_job(cmd.prompt, workdir, cmd.system_hint)
    except ValueError as exc:
        if resume_id and workdir and cmd.workdir is None and not explicit_session:
            clear_session(msg.channel.id)
            resume_id = None
            job_dir = create_job(cmd.prompt, None, cmd.system_hint)
        else:
            await msg.reply(f"실행 불가: {exc}")
            return

    working_gif = make_working_gif_file()
    if working_gif:
        ack = await msg.reply(format_working_status(job_dir.name), file=working_gif)
    else:
        ack = await msg.reply(format_working_status(job_dir.name))
    loader_task = asyncio.create_task(run_spinning_loader(ack, job_dir.name))

    try:
        meta = await _run_job_with_session_recovery(
            job_dir,
            resume_id=resume_id,
            channel_id=msg.channel.id,
            explicit_session=explicit_session,
        )
    except Exception as exc:
        await stop_spinning_loader(loader_task)
        await ack.edit(content=f"작업 실패 · {job_dir.name}")
        await msg.channel.send(f"내부 오류: {exc}")
        return

    await stop_spinning_loader(loader_task)

    if meta.get("session_id"):
        set_session(msg.channel.id, meta["session_id"], workdir=meta.get("workdir"))

    failed = meta.get("type") == "error" or meta.get("is_error")
    status = "작업 실패" if failed else "작업 완료"
    await ack.edit(content=f"{status} · {job_dir.name}")
    if failed and meta.get("text"):
        await msg.channel.send(str(meta["text"])[:1900])
    await send_outputs(msg.channel, job_dir, warn_missing_manifest=not failed)


def main():
    client.run(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    main()
